"""Tests for 根节点增量续跑（Revise）机制 — 复用既有子节点继续演进。

覆盖（详见 plan `valiant-snuggling-piglet.md`）：
1.  revise_manager_node 合法：COMPLETED Manager → WAITING + expand + pending_revise
2.  revise_manager_node 非法：非 manager / RUNNING / READY / WAITING → ValueError
3.  revise_manager_node 清除 _notified_managers（子节点再次完成可重新触发回调）
4.  _prevalidate_reuse：未知 reuse_step_ref / 重复复用 / 指向新 step / role 不一致 → ValueError
5.  planner.translate_revision：复用保留原 id、新 step 新 UUID、PARENT 正确、
    旧 DEPENDENCY 边删除、新边按 depends_on 重建（seen 去重）
6.  Session 复用默认：role/AICU-config/workspace 不变 → 保留 _aicu_cache（resume）；
    role 变更 / AICU config 变更 / workspace 缺失 → 清缓存新建会话
7.  复用 worker 重激活：WAITING + pending_context(collaboration_type=revise) + counts 归零；
    复用子 Manager → 自身 expand + pending_revise
8.  Manager._expand duck-typing：无 submit_revision_plan 的 runtime 回退 submit_plan
9.  _build_revise_candidates_block 含子节点 step_ref/role/task
10. session_server /revise：200 + {"status":"revising"}；非法 node → 409；/restart → 404
11. worker resume 失败兜底：revise_resume 首 ask 抛错 → pop cache + 新会话重试一次成功
"""

import os
import threading
from datetime import datetime
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from runtime.graph import Graph
from runtime.models import (
    Edge,
    EdgeType,
    ErrorObject,
    ExecutionPlan,
    LogLevel,
    Node,
    NodeStatus,
    RoutingDecision,
    Step,
)
from runtime.persistence import Persistence
from runtime.planner import Planner
from runtime.runtime import Runtime
from runtime.scheduler import Scheduler
from executables.base import Executable, ExecutableContext
from executables.manager import Manager
from executables.registry import ExecutableRegistry
from executables.worker import Worker
import executables.worker as worker_module


# ============================================================================
# Helpers
# ============================================================================

def _make_runtime(tmp_path) -> Runtime:
    rt = Runtime(
        workspace_root=str(tmp_path),
        persistence=Persistence(file_path=str(tmp_path / "state.json")),
    )
    # 关闭后台 tick 线程，使断言确定性（revise 相关方法内部调用 scheduler.start()）
    rt.scheduler.start = lambda: None  # type: ignore[method-assign]
    return rt


def _add_parent_edge(graph: Graph, parent: str, child: str) -> None:
    graph.add_edge(Edge(
        id=f"p_{parent}_{child}",
        type=EdgeType.PARENT,
        from_node=parent,
        to_node=child,
    ))


def _add_dep_edge(graph: Graph, src: str, dst: str, eid: str) -> None:
    graph.add_edge(Edge(
        id=eid,
        type=EdgeType.DEPENDENCY,
        from_node=src,
        to_node=dst,
    ))


def _worker_node(node_id: str, parent_id: Optional[str], role: str = "dev",
                 status: NodeStatus = NodeStatus.COMPLETED,
                 config: Optional[dict] = None, artifact_path: Optional[str] = None,
                 step_ref: Optional[str] = None) -> Node:
    return Node(
        id=node_id,
        parent_id=parent_id,
        exec_type="worker",
        role=role,
        task=f"Task {node_id}",
        config=config or {},
        status=status,
        step_ref=step_ref or node_id,
        artifact_path=artifact_path,
    )


def _plan(steps: list[Step]) -> ExecutionPlan:
    return ExecutionPlan(steps=steps)


def _step(step_id: str, role: str = "dev", exec_type: str = "worker",
          task: str = "t", task_summary: str = "s",
          reuse_step_ref: Optional[str] = None,
          depends_on: Optional[list] = None, config: Optional[dict] = None) -> Step:
    return Step(
        step_id=step_id,
        exec_type=exec_type,
        role=role,
        task=task,
        task_summary=task_summary,
        config=config or {},
        depends_on=depends_on or [],
        reuse_step_ref=reuse_step_ref,
    )


# ============================================================================
# Tests 1-3: revise_manager_node
# ============================================================================

class TestReviseManagerNode:

    def _completed_root(self, rt: Runtime) -> str:
        mgr_id = rt.submit_root_manager(task="Build feature", role="default")
        rt.graph.update_node(mgr_id, status=NodeStatus.COMPLETED)
        return mgr_id

    def test_revise_legal_completed_to_waiting(self, tmp_path):
        rt = _make_runtime(tmp_path)
        mgr_id = self._completed_root(rt)
        status_events = []
        rt.event_bus.subscribe(
            "node_status_changed", status_events.append
        )

        rt.revise_manager_node(mgr_id, "Add pagination", config={"num_ccs": 3})

        node = rt.graph.get_node(mgr_id)
        assert node.status == NodeStatus.WAITING
        assert status_events == []
        assert node.manager_phase == "expand"
        assert node.pending_revise is True
        assert node.task == "Add pagination"
        assert node.config["num_ccs"] == 3
        assert node.activation_count == 0
        assert node.iteration_count == 0
        assert node.routing_history == []
        assert node.pending_routing is None
        assert node.error is None
        pc = node.pending_context or {}
        assert pc["collaboration_type"] == "revise"
        assert pc["original_task"] == "Build feature"
        assert pc["incremental_task"] == "Add pagination"

    def test_revise_accepts_failed_stopped_paused(self, tmp_path):
        for status in (NodeStatus.FAILED, NodeStatus.STOPPED, NodeStatus.PAUSED):
            rt = _make_runtime(tmp_path)
            mgr_id = rt.submit_root_manager(task="T", role="default")
            rt.graph.update_node(mgr_id, status=status)
            rt.revise_manager_node(mgr_id, "again")
            node = rt.graph.get_node(mgr_id)
            assert node.status == NodeStatus.WAITING
            assert node.pending_revise is True

    def test_revise_illegal_non_manager(self, tmp_path):
        rt = _make_runtime(tmp_path)
        mgr_id = rt.submit_root_manager(task="Build", role="default")
        rt.graph.update_node(mgr_id, status=NodeStatus.COMPLETED)
        child = _worker_node("W1", mgr_id, status=NodeStatus.COMPLETED)
        rt.graph.add_node(child)
        _add_parent_edge(rt.graph, mgr_id, child.id)

        with pytest.raises(ValueError, match="expected 'manager'"):
            rt.revise_manager_node(child.id, "x")

    def test_revise_illegal_statuses(self, tmp_path):
        for status in (NodeStatus.RUNNING, NodeStatus.READY, NodeStatus.WAITING):
            rt = _make_runtime(tmp_path)
            mgr_id = rt.submit_root_manager(task="Build", role="default")
            rt.graph.update_node(mgr_id, status=status)
            with pytest.raises(ValueError, match="expected COMPLETED"):
                rt.revise_manager_node(mgr_id, "x")

    def test_revise_task_too_long(self, tmp_path):
        rt = _make_runtime(tmp_path)
        mgr_id = self._completed_root(rt)
        with pytest.raises(ValueError, match="maximum length"):
            rt.revise_manager_node(mgr_id, "x" * 5001)

    def test_revise_config_non_whitelisted(self, tmp_path):
        rt = _make_runtime(tmp_path)
        mgr_id = self._completed_root(rt)
        with pytest.raises(ValueError, match="non-whitelisted"):
            rt.revise_manager_node(mgr_id, "x", config={"bad_key": 1})

    def test_revise_clears_notified_managers(self, tmp_path):
        rt = _make_runtime(tmp_path)
        mgr_id = self._completed_root(rt)
        rt.scheduler._notified_managers.add(mgr_id)
        assert mgr_id in rt.scheduler._notified_managers

        rt.revise_manager_node(mgr_id, "incremental")

        assert mgr_id not in rt.scheduler._notified_managers

    def test_revise_expand_survives_runtime_restore(self, tmp_path):
        """恢复时不能把 Revise 的 expand 误判为旧 waiting 阶段。"""
        state_file = tmp_path / "state.json"
        rt1 = Runtime(
            workspace_root=str(tmp_path),
            persistence=Persistence(file_path=str(state_file)),
        )
        mgr_id = rt1.submit_root_manager("Build feature")
        child = _worker_node("child", mgr_id, status=NodeStatus.COMPLETED)
        rt1.graph.add_node(child)
        _add_parent_edge(rt1.graph, mgr_id, child.id)
        rt1.graph.update_node(
            mgr_id,
            status=NodeStatus.WAITING,
            manager_phase="expand",
            pending_revise=True,
        )
        rt1.persistence.save(rt1.graph)

        rt2 = Runtime(
            workspace_root=str(tmp_path),
            persistence=Persistence(file_path=str(state_file)),
        )
        rt2.scheduler.start = lambda: None  # type: ignore[method-assign]
        rt2.start()

        restored = rt2.graph.get_node(mgr_id)
        assert restored is not None
        assert restored.status == NodeStatus.WAITING
        assert restored.manager_phase == "expand"
        assert restored.pending_revise is True


# ============================================================================
# Test 4: _prevalidate_reuse
# ============================================================================

class TestPrevalidateReuse:

    def _harness(self, tmp_path):
        rt = _make_runtime(tmp_path)
        mgr_id = rt.submit_root_manager(task="Build", role="default")
        rt.graph.update_node(mgr_id, status=NodeStatus.COMPLETED)
        a = _worker_node("A1", mgr_id, role="dev", status=NodeStatus.COMPLETED,
                         step_ref="dev")
        b = _worker_node("B1", mgr_id, role="test", status=NodeStatus.COMPLETED,
                         step_ref="test")
        for c in (a, b):
            rt.graph.add_node(c)
            _add_parent_edge(rt.graph, mgr_id, c.id)
        return rt, mgr_id

    def test_unknown_reuse_ref(self, tmp_path):
        rt, mgr_id = self._harness(tmp_path)
        with pytest.raises(ValueError, match="does not match any existing child"):
            rt._prevalidate_reuse(_plan([_step("s1", reuse_step_ref="ghost")]), mgr_id)

    def test_duplicate_reuse(self, tmp_path):
        rt, mgr_id = self._harness(tmp_path)
        plan = _plan([
            _step("s1", reuse_step_ref="dev"),
            _step("s2", reuse_step_ref="dev"),
        ])
        with pytest.raises(ValueError, match="reused by multiple steps"):
            rt._prevalidate_reuse(plan, mgr_id)

    def test_reuse_points_to_new_step(self, tmp_path):
        rt, mgr_id = self._harness(tmp_path)
        plan = _plan([_step("s1", reuse_step_ref="s2"), _step("s2")])
        with pytest.raises(ValueError, match="not a new step in this plan"):
            rt._prevalidate_reuse(plan, mgr_id)

    def test_role_mismatch(self, tmp_path):
        rt, mgr_id = self._harness(tmp_path)
        # dev 子节点 role=dev，step role 改为 test → 能力变化应新建
        plan = _plan([_step("s1", role="test", reuse_step_ref="dev")])
        with pytest.raises(ValueError, match="role change is a new capability"):
            rt._prevalidate_reuse(plan, mgr_id)

    def test_resolve_by_node_id_prefix(self, tmp_path):
        rt, mgr_id = self._harness(tmp_path)
        reuse = rt._prevalidate_reuse(_plan([_step("s1", reuse_step_ref="A1")]), mgr_id)
        assert reuse == {"s1": "A1"}

    def test_duplicate_unreferenced_revision_step_rejected(self, tmp_path):
        rt, mgr_id = self._harness(tmp_path)
        before = {node.id for node in rt.graph.all_nodes()}
        plan = _plan([_step("new_step", role="dev", task="Task A1")])

        with pytest.raises(ValueError, match="without reuse_step_ref"):
            rt.inject_revision_plan(mgr_id, plan)

        assert {node.id for node in rt.graph.all_nodes()} == before


# ============================================================================
# Test 5: planner.translate_revision
# ============================================================================

class TestTranslateRevision:

    def test_selective_reuse_keeps_ids_and_rebuilds_deps(self):
        g = Graph()
        mgr = Node(id="M1", exec_type="manager", role="architecture", task="org",
                   config={}, status=NodeStatus.COMPLETED, step_ref="mgr")
        g.add_node(mgr)
        a = _worker_node("A1", "M1", step_ref="arch", status=NodeStatus.COMPLETED)
        b = _worker_node("B1", "M1", step_ref="impl", status=NodeStatus.COMPLETED)
        c = _worker_node("C1", "M1", step_ref="qa", status=NodeStatus.COMPLETED)
        for n in (a, b, c):
            g.add_node(n)
            _add_parent_edge(g, "M1", n.id)
        # 旧依赖：B 依赖 A；C 依赖 B
        _add_dep_edge(g, "A1", "B1", "dep_ab")
        _add_dep_edge(g, "B1", "C1", "dep_bc")

        plan = _plan([
            _step("arch", role="dev", reuse_step_ref="arch"),
            _step("impl", role="dev", reuse_step_ref="impl", depends_on=["arch"]),
            _step("new_role", role="qa", task="new capability", depends_on=["impl"]),
        ])
        reuse_map = {"arch": "A1", "impl": "B1"}
        planner = Planner(g)
        new_nodes, reused_nodes = planner.translate_revision(plan, "M1", reuse_map)

        # 复用保留原 id
        assert {n.id for n in reused_nodes} == {"A1", "B1"}
        # 新 step → 新 UUID
        assert len(new_nodes) == 1
        new_node = new_nodes[0]
        assert new_node.parent_id == "M1"
        assert new_node.step_ref == "new_role"
        assert new_node.status == NodeStatus.WAITING
        # PARENT 边：M1→A1, M1→B1 已存在；M1→new 已添加
        assert new_node.parent_id == "M1"
        assert any(
            e.type == EdgeType.PARENT and e.from_node == "M1" and e.to_node == new_node.id
            for e in g.all_edges()
        )
        # DEPENDENCY：旧 C1→B1 边保持（C1 未被激活）；A1→B1 重建；new→B1 建立
        deps_of_b = {d.id for d in g.dependencies("B1")}
        assert deps_of_b == {"A1"}
        deps_of_new = {d.id for d in g.dependencies(new_node.id)}
        assert deps_of_new == {"B1"}
        # C1 仍是 B1 的下游（父依赖关系反向查询）
        assert "C1" in {e.to_node for e in g.all_edges() if e.type == EdgeType.DEPENDENCY and e.from_node == "B1"}

    def test_translate_revision_empty_reuse_map(self):
        g = Graph()
        mgr = Node(id="M1", exec_type="manager", role="architecture", task="org",
                   config={}, status=NodeStatus.COMPLETED, step_ref="mgr")
        g.add_node(mgr)
        plan = _plan([_step("s1", role="dev")])
        new_nodes, reused_nodes = Planner(g).translate_revision(plan, "M1", {})
        assert len(new_nodes) == 1
        assert reused_nodes == []


# ============================================================================
# Tests 6-7: _reactivate_reused_worker
# ============================================================================

class TestReactivateReusedWorker:

    def _harness(self, tmp_path, relpath=None, role="dev"):
        rt = _make_runtime(tmp_path)
        mgr_id = rt.submit_root_manager(task="Build", role="default")
        rt.graph.update_node(mgr_id, status=NodeStatus.COMPLETED)
        rel = relpath or "nodes/root/children/W1"
        ws = os.path.join(str(tmp_path), rel)
        if relpath is None:
            os.makedirs(ws, exist_ok=True)  # 默认工作区存在 → resume 有地基
        child = _worker_node("W1", mgr_id, role=role, status=NodeStatus.COMPLETED,
                             config={"_workspace_root": str(tmp_path), "_workspace_relpath": rel})
        rt.graph.add_node(child)
        _add_parent_edge(rt.graph, mgr_id, child.id)
        return rt, mgr_id, child

    def test_session_reuse_default_when_unchanged(self, tmp_path):
        rt, mgr_id, child = self._harness(tmp_path)
        sentinel = object()
        with Worker._aicu_cache_lock:
            Worker._aicu_cache[child.id] = sentinel
        try:
            prepared = rt._prepare_node_config(child.id, {**(child.config or {}), **{}}, mgr_id)
            rt._reactivate_reused_worker(child, _step("s1", role="dev"), prepared)
            with Worker._aicu_cache_lock:
                assert Worker._aicu_cache.get(child.id) is sentinel  # resume 保留
            node = rt.graph.get_node(child.id)
            assert node.status == NodeStatus.WAITING
            assert node.pending_revise is True
            # 复用即一次新的激活：首建 0 → 本次复用 1
            assert node.activation_count == 1
            assert node.iteration_count == 0
            assert node.manager_phase is None
            assert node.pending_routing is None
            pc = node.pending_context or {}
            assert pc["collaboration_type"] == "revise"
            assert pc["incremental_task"] == "t"
            assert pc["activated_by"] == mgr_id
        finally:
            with Worker._aicu_cache_lock:
                Worker._aicu_cache.pop(child.id, None)

    def test_session_pop_on_role_change(self, tmp_path):
        rt, mgr_id, child = self._harness(tmp_path)
        sentinel = object()
        with Worker._aicu_cache_lock:
            Worker._aicu_cache[child.id] = sentinel
        try:
            prepared = rt._prepare_node_config(child.id, {**(child.config or {}), **{}}, mgr_id)
            rt._reactivate_reused_worker(child, _step("s1", role="test"), prepared)
            with Worker._aicu_cache_lock:
                assert Worker._aicu_cache.get(child.id) is None  # role 变 → 新会话
        finally:
            with Worker._aicu_cache_lock:
                Worker._aicu_cache.pop(child.id, None)

    def test_session_pop_on_aicu_config_change(self, tmp_path):
        rt, mgr_id, child = self._harness(tmp_path)
        rt.graph.update_node(child.id, config={**child.config, "model": "gpt-5.6-luna"})
        sentinel = object()
        with Worker._aicu_cache_lock:
            Worker._aicu_cache[child.id] = sentinel
        try:
            prepared = rt._prepare_node_config(
                child.id, {**(child.config or {}), "model": "Qwen3"}, mgr_id
            )
            rt._reactivate_reused_worker(child, _step("s1", role="dev"), prepared)
            with Worker._aicu_cache_lock:
                assert Worker._aicu_cache.get(child.id) is None  # AICU config 变 → 新会话
        finally:
            with Worker._aicu_cache_lock:
                Worker._aicu_cache.pop(child.id, None)

    def test_session_pop_on_workspace_missing(self, tmp_path):
        # relpath 指向不存在的目录 → workspace 失效 → 新会话
        rt, mgr_id, child = self._harness(
            tmp_path, relpath="nodes/root/children/ghost_ws"
        )
        sentinel = object()
        with Worker._aicu_cache_lock:
            Worker._aicu_cache[child.id] = sentinel
        try:
            prepared = rt._prepare_node_config(
                child.id, {**(child.config or {}), **{}}, mgr_id
            )
            rt._reactivate_reused_worker(child, _step("s1", role="dev"), prepared)
            with Worker._aicu_cache_lock:
                assert Worker._aicu_cache.get(child.id) is None
        finally:
            with Worker._aicu_cache_lock:
                Worker._aicu_cache.pop(child.id, None)

    def test_reused_submanager_reexpands(self, tmp_path):
        rt = _make_runtime(tmp_path)
        root = rt.submit_root_manager(task="Build", role="default")
        rt.graph.update_node(root, status=NodeStatus.COMPLETED)
        sub = Node(id="S1", parent_id=root, exec_type="manager", role="architecture",
                   task="sub", config={}, status=NodeStatus.COMPLETED, step_ref="sub_mgr")
        rt.graph.add_node(sub)
        _add_parent_edge(rt.graph, root, sub.id)

        prepared = rt._prepare_node_config(sub.id, {}, root)
        rt._reactivate_reused_worker(sub, _step("s1", role="architecture", exec_type="manager"), prepared)

        node = rt.graph.get_node(sub.id)
        assert node.status == NodeStatus.WAITING
        assert node.manager_phase == "expand"
        assert node.pending_revise is True


# ============================================================================
# Test 8: Manager._expand duck-typing
# ============================================================================


class TestRevisionPlanNoop:
    """增量计划未改变组织结构和可执行工作时应被识别为 no-op。"""

    def _runtime_with_children(self, tmp_path):
        rt = _make_runtime(tmp_path)
        parent_id = rt.submit_root_manager("root")
        initial = _plan([
            _step("first", role="dev", task="Do A", task_summary="A"),
            _step(
                "second", role="qa", task="Do B", task_summary="B",
                depends_on=["first"],
            ),
        ])
        children = rt.inject_plan(parent_id, initial)
        return rt, parent_id, children

    def _unchanged_plan(self, children):
        return _plan([
            _step(
                "revised-first", role="dev", task="Do A", task_summary="A",
                reuse_step_ref=children[0].step_ref,
            ),
            _step(
                "revised-second", role="qa", task="Do B", task_summary="B",
                depends_on=["revised-first"],
                reuse_step_ref=children[1].step_ref,
            ),
        ])

    def test_identical_reused_plan_is_noop(self, tmp_path):
        rt, parent_id, children = self._runtime_with_children(tmp_path)
        assert rt.is_revision_plan_noop(
            parent_id, self._unchanged_plan(children)
        ) is True

    @pytest.mark.parametrize(
        "change",
        ["new_step", "task", "partial_reuse", "dependency"],
    )
    def test_structural_or_executable_change_is_not_noop(self, tmp_path, change):
        rt, parent_id, children = self._runtime_with_children(tmp_path)
        plan = self._unchanged_plan(children)
        if change == "new_step":
            plan.steps.append(_step("new", role="writer", task="Do C"))
        elif change == "task":
            plan.steps[0].task = "Do something else"
        elif change == "partial_reuse":
            plan.steps[1].reuse_step_ref = None
        else:
            plan.steps[1].depends_on = []
        assert rt.is_revision_plan_noop(parent_id, plan) is False


class _ExpandStub:
    def __init__(self, role="dev", task="t"):
        self._role = role
        self._task = task

    def ask(self, prompt: str) -> dict:
        return {
            "execution_plan_json": _plan([_step("s1", role=self._role, task=self._task)]).to_json(),
        }


class _IncrementalExpandStub(_ExpandStub):
    def __init__(self, role="dev", task="t"):
        super().__init__(role=role, task=task)
        self.incremental_prompts: list[str] = []
        self.normal_prompts: list[str] = []

    def ask(self, prompt: str) -> dict:
        self.normal_prompts.append(prompt)
        return super().ask(prompt)

    def ask_incremental(self, prompt: str) -> dict:
        self.incremental_prompts.append(prompt)
        return super().ask(prompt)


class _DuckRuntime:
    """记录 submit_plan / submit_revision_plan 调用的最小 RuntimeGateway 模拟。"""

    def __init__(self, with_revision: bool):
        self.with_revision = with_revision
        self.submitted_plans: list[str] = []
        self.submitted_revisions: list[str] = []
        self._nodes: dict[str, Node] = {}
        self._children: dict[str, list[Node]] = {}
        self.status_updates: list[tuple] = []

    def add_node(self, node: Node) -> None:
        self._nodes[node.id] = node

    def add_children(self, parent_id: str, children: list[Node]) -> None:
        self._children.setdefault(parent_id, []).extend(children)

    def submit_plan(self, node_id: str, plan: Any, iteration_count: int = 0) -> list:
        self.submitted_plans.append(node_id)
        return []

    def __getattr__(self, name: str) -> Any:
        # 仅当 with_revision=True 时暴露 submit_revision_plan，否则让
        # getattr(rt, "submit_revision_plan", None) 返回 None 以真实走 submit_plan 回退。
        if name == "submit_revision_plan" and self.with_revision:
            return self._submit_revision
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute '{name}'"
        )

    def _submit_revision(self, node_id: str, plan: Any) -> list:
        self.submitted_revisions.append(node_id)
        return []

    def update_node_status(self, node_id: str, status: NodeStatus, **updates: Any) -> None:
        self.status_updates.append((node_id, status, updates))
        if node_id in self._nodes:
            node = self._nodes[node_id]
            for k, v in updates.items():
                setattr(node, k, v)
            node.status = status

    def get_node(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(node_id)

    def get_children(self, node_id: str) -> list[Node]:
        return self._children.get(node_id, [])

    def get_dependencies(self, node_id: str) -> list:
        return []

    def read_artifact(self, node_id: str, path: str) -> str:
        return ""

    def register_external_input_wait(self, node_id: str, input_schema: Optional[dict] = None) -> str:
        return "token"

    def deliver_external_input(self, wait_token: str, input_data: Any) -> None:
        pass


def _manager_node(pending_revise: bool = False, status: NodeStatus = NodeStatus.READY) -> Node:
    return Node(
        id="M1", exec_type="manager", role="coder", task="Do something",
        config={}, status=status, manager_phase="expand", pending_revise=pending_revise,
    )


class TestExpandDuckTyping:

    def test_revise_uses_incremental_aicu_request_only(self):
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=True)
        node.task = "CURRENT_TASK_FIELD"
        node.pending_context = {
            "collaboration_type": "revise",
            "original_task": "HISTORICAL_TASK",
            "incremental_task": "CURRENT_INCREMENTAL_TASK",
        }
        rt.add_node(node)
        child = _worker_node("A1", "M1", role="dev", step_ref="dev")
        rt.add_children("M1", [child])
        aicu = _IncrementalExpandStub()
        mgr = Manager(context=ExecutableContext(node=node, runtime=rt), aicu_instance=aicu)

        result = mgr.run(ExecutableContext(node=node, runtime=rt))

        assert result == NodeStatus.WAITING
        assert len(aicu.incremental_prompts) == 1
        assert aicu.normal_prompts == []
        prompt = aicu.incremental_prompts[0]
        assert "CURRENT_INCREMENTAL_TASK" in prompt
        assert "HISTORICAL_TASK" not in prompt
        assert "Reuse Candidates" in prompt
        assert "step_ref: `dev`" in prompt
        assert "reuse_step_ref" in prompt
        assert "执行上下文快照" not in prompt
        assert "CodeGraph" not in prompt
        assert rt.submitted_revisions == ["M1"]

    def test_pending_revise_with_revision_runtime(self):
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=True)
        rt.add_node(node)
        ctx = ExecutableContext(node=node, runtime=rt)
        mgr = Manager(context=ctx, aicu_instance=_ExpandStub())

        result = mgr.run(ctx)

        assert result == NodeStatus.WAITING
        assert rt.submitted_revisions == ["M1"]
        assert rt.submitted_plans == []

    def test_pending_revise_falls_back_to_submit_plan(self):
        rt = _DuckRuntime(with_revision=False)
        node = _manager_node(pending_revise=True)
        rt.add_node(node)
        ctx = ExecutableContext(node=node, runtime=rt)
        mgr = Manager(context=ctx, aicu_instance=_ExpandStub())

        result = mgr.run(ctx)

        assert result == NodeStatus.WAITING
        assert rt.submitted_revisions == []
        assert rt.submitted_plans == ["M1"]

    def test_normal_expand_uses_submit_plan_even_with_revision_runtime(self):
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=False)
        rt.add_node(node)
        ctx = ExecutableContext(node=node, runtime=rt)
        mgr = Manager(context=ctx, aicu_instance=_ExpandStub())

        mgr.run(ctx)

        assert rt.submitted_plans == ["M1"]
        assert rt.submitted_revisions == []

    def test_has_children_uses_revision_even_without_pending_revise(self):
        """有子节点的 Manager 重激活（修复/路由）一律走 Revise——即使 pending_revise=False。

        删除保护原则：绝不删除原有子节点整体重建；has_children 即安全网，
        保证修复/路由重激活的 Manager 走选择性复用通道。
        """
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=False)
        rt.add_node(node)
        rt.add_children("M1", [
            _worker_node("A1", "M1", role="dev", status=NodeStatus.COMPLETED, step_ref="dev"),
        ])
        ctx = ExecutableContext(node=node, runtime=rt)
        mgr = Manager(context=ctx, aicu_instance=_ExpandStub())

        mgr.run(ctx)

        assert rt.submitted_revisions == ["M1"]
        assert rt.submitted_plans == []

    def test_expand_skips_noop_revision_when_children_are_terminal(self):
        """no-op Revise 不提交计划，已有终态子节点直接进入 review。"""
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=True)
        rt.add_node(node)
        child = _worker_node(
            "A1", "M1", role="dev", status=NodeStatus.COMPLETED,
        )
        rt.add_children("M1", [child])
        calls: list[str] = []
        rt.is_revision_plan_noop = lambda node_id, plan: calls.append(node_id) or True

        result = Manager(
            context=ExecutableContext(node=node, runtime=rt),
            aicu_instance=_ExpandStub(),
        ).run(ExecutableContext(node=node, runtime=rt))

        assert result == NodeStatus.READY
        assert calls == ["M1"]
        assert rt.submitted_revisions == []
        assert rt.submitted_plans == []
        assert node.manager_phase == "review"
        assert node.pending_revise is False
        assert node.pending_context is None

    def test_expand_keeps_waiting_for_active_children_on_noop_revision(self):
        """no-op Revise 遇到运行中的子节点时继续等待，不重新激活。"""
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=True)
        rt.add_node(node)
        child = _worker_node("A1", "M1", role="dev", status=NodeStatus.RUNNING)
        rt.add_children("M1", [child])
        rt.is_revision_plan_noop = lambda node_id, plan: True

        result = Manager(
            context=ExecutableContext(node=node, runtime=rt),
            aicu_instance=_ExpandStub(),
        ).run(ExecutableContext(node=node, runtime=rt))

        assert result == NodeStatus.WAITING
        assert rt.submitted_revisions == []
        assert rt.submitted_plans == []
        assert node.manager_phase == "waiting"
        assert node.pending_revise is False

    def test_repair_context_block_renders_defects(self):
        """2c：pending_context 为 repair 类型（本 Manager 被父级 review 修复）→ 渲染缺陷明细。"""
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=False)
        node.pending_context = {
            "collaboration_type": "repair",
            "activation_reason": "Manager review 第 1 轮发现产出缺陷",
            "defects": [{
                "step_ref": "be",
                "reason": "code has a bug",
                "fix_role": "code",
                "fix_task": "fix compute_tax",
                "file": "utils/tax.py",
                "line": 10,
            }],
        }
        rt.add_node(node)
        ctx = ExecutableContext(node=node, runtime=rt)
        mgr = Manager(context=ctx, aicu_instance=_ExpandStub())

        block = mgr._build_repair_context_block(ctx)

        assert "## 🔧 修复要求 (Repair)" in block
        assert "utils/tax.py:10" in block
        assert "fix compute_tax" in block
        assert "绝不删除原有节点整体重新规划" in block

    def test_repair_context_block_empty_for_non_repair(self):
        """2c 空守卫：非 repair 上下文（首跑/事件/revise）字节级不变（空串）。"""
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=True)  # revise 类型 pending_context 未设置
        node.pending_context = {"collaboration_type": "revise", "incremental_task": "t"}
        rt.add_node(node)
        ctx = ExecutableContext(node=node, runtime=rt)
        mgr = Manager(context=ctx, aicu_instance=_ExpandStub())

        assert mgr._build_repair_context_block(ctx) == ""


# ============================================================================
# Test 9: _build_revise_candidates_block
# ============================================================================

class TestReviseCandidatesBlock:

    def test_block_lists_children(self):
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=True)
        rt.add_node(node)
        c1 = _worker_node("A1", "M1", role="dev", status=NodeStatus.COMPLETED, step_ref="dev")
        rt.add_children("M1", [c1])
        ctx = ExecutableContext(node=node, runtime=rt)
        mgr = Manager(context=ctx, aicu_instance=_ExpandStub())

        block = mgr._build_revise_candidates_block(ctx)

        assert "## 可复用候选节点 (Reuse Candidates)" in block
        assert "step_ref: `dev`" in block
        assert "role: dev" in block
        assert "Task A1" in block
        assert "reuse_step_ref" in block
        assert "候选角色能够完成" in block
        assert "可由候选覆盖" in block


# ============================================================================
# Test 10: session_server /revise endpoint
# ============================================================================

class TestReviseEndpoint:

    def test_revise_ok_and_restart_404(self, session_manager, session_client):
        resp = session_client.post("/v1/sessions", json={"task": "Build feature"})
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        root_id = resp.json()["root_node_id"]

        handle = session_manager.get_session(sid)
        rt = handle.runtime
        # 等待 root COMPLETED（会话 executable 为 _TestExecutable 自动完成）
        deadline = 5.0
        import time
        t0 = time.time()
        while time.time() - t0 < deadline:
            if rt.graph.get_node(root_id).status == NodeStatus.COMPLETED:
                break
            time.sleep(0.02)
        assert rt.graph.get_node(root_id).status == NodeStatus.COMPLETED

        # 停掉后台 tick，确保后续状态断言确定；Revise 不得重新启动 Scheduler。
        rt.scheduler.stop()
        start_calls: list[None] = []

        def unexpected_start() -> None:
            start_calls.append(None)

        rt.scheduler.start = unexpected_start  # type: ignore[method-assign]

        ok = session_client.post(
            f"/v1/sessions/{sid}/nodes/{root_id}/revise",
            json={"task": "Add pagination", "config": {"num_ccs": 3}},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "revising"
        assert start_calls == []
        node = rt.graph.get_node(root_id)
        assert node.status == NodeStatus.WAITING
        assert node.manager_phase == "expand"
        assert node.pending_revise is True
        assert node.task == "Add pagination"

        # 第二次 revise：当前 WAITING（非终态）→ 409
        again = session_client.post(
            f"/v1/sessions/{sid}/nodes/{root_id}/revise",
            json={"task": "More"},
        )
        assert again.status_code == 409

        # /restart 端点已删除 → 404
        gone = session_client.post(
            f"/v1/sessions/{sid}/nodes/{root_id}/restart", json={}
        )
        assert gone.status_code == 404


# ============================================================================
# Test 11: Worker resume-failure fallback
# ============================================================================

class _FlakyAsk:
    """首次 ask 抛错（模拟 --resume 失效），用于验证兜底重试触发。"""

    def __init__(self, workspace: str):
        self.calls = 0
        self.workspace = workspace

    def ask(self, prompt: str) -> dict:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("CC resume failed")
        return {"final_output_path": os.path.join(self.workspace, "ws", "result.md")}


class _SuccessAsk:
    """新建会话兜底桩：首次 ask 即成功（无 --resume）。"""

    def __init__(self, workspace: str):
        self.calls = 0
        self.workspace = workspace

    def ask(self, prompt: str) -> dict:
        self.calls += 1
        return {"final_output_path": os.path.join(self.workspace, "ws", "result.md")}


class _WorkerTestRuntime:
    """Worker.run 所需最小 runtime 模拟。"""

    def __init__(self, tmp_path: str):
        self.workspace = tmp_path
        self.updates: list[tuple] = []

    def update_node_status(self, node_id: str, status: NodeStatus, **updates: Any) -> None:
        self.updates.append((node_id, status, updates))

    def get_node_workspace(self, node_id: str) -> str:
        ws = os.path.join(self.workspace, "ws")
        os.makedirs(ws, exist_ok=True)
        return ws

    def flush_node_events(self, node_id: str) -> None:
        return

    def append_node_log(self, *args: Any, **kwargs: Any) -> None:
        return

    def update_cc_activity(self, *args: Any, **kwargs: Any) -> None:
        return


class TestWorkerResumeFallback:

    def test_revise_resume_failure_rebuilds_and_retries(self, tmp_path, monkeypatch):
        rt = _WorkerTestRuntime(str(tmp_path))
        node = Node(
            id="W1", exec_type="worker", role="dev", task="incremental task",
            config={"_workspace_root": str(tmp_path), "_workspace_relpath": "ws"},
            status=NodeStatus.WAITING,
            pending_context={
                "collaboration_type": "revise",
                "incremental_task": "incremental task",
            },
        )
        ctx = ExecutableContext(node=node, runtime=rt)

        flaky = _FlakyAsk(str(tmp_path))
        worker = Worker(context=ctx, aicu_instance=flaky)

        sentinel = object()
        with Worker._aicu_cache_lock:
            Worker._aicu_cache[node.id] = sentinel  # 触发 is_continuation
        try:
            # 兜底：新会话重建 → 直接返回成功 stub（无 --resume）
            monkeypatch.setattr(
                worker_module, "_create_real_aicu", lambda _ctx: _SuccessAsk(str(tmp_path))
            )

            result = worker.run(ctx)

            assert result == NodeStatus.COMPLETED
            assert flaky.calls == 1  # 首次抛错
            with Worker._aicu_cache_lock:
                assert Worker._aicu_cache.get(node.id) is None  # 旧会话已 pop
            assert any(
                u[1] == NodeStatus.COMPLETED for u in rt.updates
            )
        finally:
            with Worker._aicu_cache_lock:
                Worker._aicu_cache.pop(node.id, None)

    def test_non_revise_continuation_propagates_error(self, tmp_path, monkeypatch):
        # 非 revise 的续接（如普通 routing/repair）首 ask 抛错 → 原样传播（不做兜底）
        rt = _WorkerTestRuntime(str(tmp_path))
        node = Node(
            id="W2", exec_type="worker", role="dev", task="t",
            config={}, status=NodeStatus.WAITING,
            pending_context={"collaboration_type": "request"},
        )
        ctx = ExecutableContext(node=node, runtime=rt)
        flaky = _FlakyAsk(str(tmp_path))
        worker = Worker(context=ctx, aicu_instance=flaky)
        with Worker._aicu_cache_lock:
            Worker._aicu_cache[node.id] = object()
        try:
            result = worker.run(ctx)
            assert result == NodeStatus.FAILED  # 异常原样传播 → 失败状态
        finally:
            with Worker._aicu_cache_lock:
                Worker._aicu_cache.pop(node.id, None)
