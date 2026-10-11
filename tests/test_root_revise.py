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
10. session_server /revise：200 + {"status":"revising"}；非法 node → 409；/restart 已删除（404/405）
11. worker resume 失败兜底：revise_resume 首 ask 抛错 → pop cache + 新会话重试一次成功
12. 「本轮是否需要行动」由 Manager(LLM) 判定：空计划 + reason = 不派活（终态子节点进
    review / 运行中子节点继续等待）；Runtime 不再比对计划文字替它推断 no-op
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

        rt.revise_manager_node(
            mgr_id, "Add pagination", config={"model": "revise-model", "num_ccs": 3}
        )

        node = rt.graph.get_node(mgr_id)
        assert node.status == NodeStatus.WAITING
        # R21/D5：revise_manager_node 经 reactivate(emit_notification=True) 同步发布
        # node_status_changed(Waiting)（原断言锁定静默旧行为，按契约修订）
        assert any(
            e.payload.get("node_id") == mgr_id
            and e.payload.get("status") == "Waiting"
            for e in status_events
        ), f"revise_manager_node 应发布 node_status_changed(Waiting); events={[e.payload for e in status_events]}"
        assert node.manager_phase == "expand"
        assert node.pending_revise is True
        assert node.task == "Add pagination"
        assert node.config["model"] == "revise-model"
        # 遗留 num_ccs 被静默剥除（revise 不 ValueError）
        assert "num_ccs" not in node.config
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
# revise_worker_node：已完成 Worker 的增量续跑入口
# ============================================================================

def _completed_worker_under_root(
    tmp_path, worker_id="W1", role="dev", status=NodeStatus.COMPLETED,
    relpath=None, worker_config: Optional[dict] = None,
):
    """建「已终态根 Manager + 已终态 Worker 子节点」的最小图。

    worker 的 config 预置 _workspace_root/_workspace_relpath，默认工作区
    目录存在（resume 有地基，与 TestReactivateReusedWorker._harness 一致）。
    """
    rt = _make_runtime(tmp_path)
    mgr_id = rt.submit_root_manager(task="Build", role="default")
    rt.graph.update_node(mgr_id, status=NodeStatus.COMPLETED)
    rel = relpath or "nodes/root/children/W1"
    if relpath is None:
        os.makedirs(os.path.join(str(tmp_path), rel), exist_ok=True)
    cfg = {"_workspace_root": str(tmp_path), "_workspace_relpath": rel}
    cfg.update(worker_config or {})
    child = _worker_node(worker_id, mgr_id, role=role, status=status, config=cfg)
    rt.graph.add_node(child)
    _add_parent_edge(rt.graph, mgr_id, child.id)
    return rt, mgr_id, child


class TestReviseWorkerNode:

    def test_revise_legal_completed_to_waiting(self, tmp_path):
        rt, mgr_id, child = _completed_worker_under_root(tmp_path)
        status_events = []
        rt.event_bus.subscribe("node_status_changed", status_events.append)

        rt.revise_worker_node(
            child.id, "Add pagination", config={"model": "revise-model", "num_ccs": 3}
        )

        node = rt.graph.get_node(child.id)
        assert node.status == NodeStatus.WAITING
        # 与 manager revise 一致：经 reactivate(emit_notification=True) 发布 Waiting
        assert any(
            e.payload.get("node_id") == child.id
            and e.payload.get("status") == "Waiting"
            for e in status_events
        ), f"revise_worker_node 应发布 node_status_changed(Waiting); events={[e.payload for e in status_events]}"
        assert node.pending_revise is True
        assert node.task == "Add pagination"
        assert node.config["model"] == "revise-model"
        # 遗留 num_ccs 被静默剥除（revise 不 ValueError）
        assert "num_ccs" not in node.config
        assert node.activation_count == 1  # 复用即一次新的激活
        assert node.iteration_count == 0
        assert node.manager_phase is None
        assert node.pending_routing is None
        assert node.error is None
        pc = node.pending_context or {}
        assert pc["collaboration_type"] == "revise"
        assert pc["incremental_task"] == "Add pagination"
        assert pc["activated_by"] == mgr_id
        assert pc["prior_artifacts"] == []

    def test_revise_accepts_failed_stopped_paused(self, tmp_path):
        for status in (NodeStatus.FAILED, NodeStatus.STOPPED, NodeStatus.PAUSED):
            rt, _, child = _completed_worker_under_root(tmp_path, status=status)
            rt.revise_worker_node(child.id, "again")
            node = rt.graph.get_node(child.id)
            assert node.status == NodeStatus.WAITING
            assert node.pending_revise is True

    def test_revise_illegal_non_worker(self, tmp_path):
        rt = _make_runtime(tmp_path)
        mgr_id = rt.submit_root_manager(task="Build", role="default")
        rt.graph.update_node(mgr_id, status=NodeStatus.COMPLETED)
        with pytest.raises(ValueError, match="expected 'worker'"):
            rt.revise_worker_node(mgr_id, "x")

    def test_revise_illegal_statuses(self, tmp_path):
        for status in (NodeStatus.RUNNING, NodeStatus.READY, NodeStatus.WAITING):
            rt, _, child = _completed_worker_under_root(tmp_path, status=status)
            with pytest.raises(ValueError, match="expected COMPLETED"):
                rt.revise_worker_node(child.id, "x")

    def test_revise_task_too_long(self, tmp_path):
        rt, _, child = _completed_worker_under_root(tmp_path)
        with pytest.raises(ValueError, match="maximum length"):
            rt.revise_worker_node(child.id, "x" * 5001)

    def test_revise_config_non_whitelisted(self, tmp_path):
        rt, _, child = _completed_worker_under_root(tmp_path)
        with pytest.raises(ValueError, match="non-whitelisted"):
            rt.revise_worker_node(child.id, "x", config={"bad_key": 1})

    def test_revise_starts_fresh_session_with_checkpoint(self, tmp_path):
        """Revise 一律新会话：弹 _aicu_cache，并把原任务作为检查点写进 pending_context。"""
        rt, _, child = _completed_worker_under_root(tmp_path)
        original_task = child.task
        sentinel = object()
        with Worker._aicu_cache_lock:
            Worker._aicu_cache[child.id] = sentinel
        try:
            rt.revise_worker_node(child.id, "incremental")
            with Worker._aicu_cache_lock:
                assert Worker._aicu_cache.get(child.id) is None
            pc = rt.graph.get_node(child.id).pending_context
            assert pc["session_mode"] == "fresh"
            assert pc["original_task"] == original_task
            assert pc["incremental_task"] == "incremental"
        finally:
            with Worker._aicu_cache_lock:
                Worker._aicu_cache.pop(child.id, None)

    def test_retry_revise_keeps_resumed_session(self, tmp_path):
        """自动重试（reason=retry）仍续接原会话：失败后紧接着修复需要刚发生的上下文。"""
        rt, _, child = _completed_worker_under_root(tmp_path)
        sentinel = object()
        with Worker._aicu_cache_lock:
            Worker._aicu_cache[child.id] = sentinel
        try:
            rt.revise_worker_node(child.id, "fix it", reason="retry")
            with Worker._aicu_cache_lock:
                assert Worker._aicu_cache.get(child.id) is sentinel
            pc = rt.graph.get_node(child.id).pending_context
            assert "session_mode" not in pc
        finally:
            with Worker._aicu_cache_lock:
                Worker._aicu_cache.pop(child.id, None)

    def test_revise_fresh_session_on_aicu_config_change(self, tmp_path):
        """用户覆盖 model 等 AICU-config → 弹缓存新建会话（不能 resume 旧认知）。"""
        rt, _, child = _completed_worker_under_root(
            tmp_path, worker_config={"model": "gpt-5.6-luna"}
        )
        sentinel = object()
        with Worker._aicu_cache_lock:
            Worker._aicu_cache[child.id] = sentinel
        try:
            rt.revise_worker_node(child.id, "incremental", config={"model": "Qwen3"})
            with Worker._aicu_cache_lock:
                assert Worker._aicu_cache.get(child.id) is None
        finally:
            with Worker._aicu_cache_lock:
                Worker._aicu_cache.pop(child.id, None)

    def test_revise_fresh_session_on_workspace_missing(self, tmp_path):
        """原工作区目录已不存在 → 弹缓存新建会话（resume 无地基）。"""
        rt, _, child = _completed_worker_under_root(
            tmp_path, relpath="nodes/root/children/ghost_ws"
        )
        sentinel = object()
        with Worker._aicu_cache_lock:
            Worker._aicu_cache[child.id] = sentinel
        try:
            rt.revise_worker_node(child.id, "incremental")
            with Worker._aicu_cache_lock:
                assert Worker._aicu_cache.get(child.id) is None
        finally:
            with Worker._aicu_cache_lock:
                Worker._aicu_cache.pop(child.id, None)

    def test_revise_rewinds_parent_chain(self, tmp_path):
        """父 Manager 回到 WAITING + 清通知：revise 后的 Worker 再次完成可重新触发评审。"""
        rt, mgr_id, child = _completed_worker_under_root(tmp_path)
        rt.scheduler._notified_managers.add(mgr_id)
        assert mgr_id in rt.scheduler._notified_managers

        rt.revise_worker_node(child.id, "incremental")

        parent = rt.graph.get_node(mgr_id)
        assert parent.status == NodeStatus.WAITING
        assert parent.manager_phase == "waiting"
        assert parent.pending_revise is False
        assert mgr_id not in rt.scheduler._notified_managers


class TestReviseNodeDispatcher:

    def test_revise_node_dispatches_worker(self, tmp_path):
        rt, _, child = _completed_worker_under_root(tmp_path)

        rt.revise_node(child.id, "worker incremental")

        node = rt.graph.get_node(child.id)
        assert node.status == NodeStatus.WAITING
        assert node.pending_revise is True
        assert node.manager_phase is None  # worker 语义
        assert node.task == "worker incremental"

    def test_revise_node_dispatches_manager(self, tmp_path):
        rt = _make_runtime(tmp_path)
        mgr_id = rt.submit_root_manager(task="Build", role="default")
        rt.graph.update_node(mgr_id, status=NodeStatus.COMPLETED)

        rt.revise_node(mgr_id, "manager incremental")

        node = rt.graph.get_node(mgr_id)
        assert node.status == NodeStatus.WAITING
        assert node.pending_revise is True
        assert node.manager_phase == "expand"  # manager 语义
        assert node.task == "manager incremental"


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

    def test_unreferenced_step_is_created_as_new_node(self, tmp_path):
        """缺 reuse_step_ref 的 step 按契约由 Manager 自己负责（提示词要求"有候选覆盖必须复用"）。

        代码不再比对 role/task 文本判定"重复"：那是语义判断，只能由节点作出 ——
        误判会把 Manager 真正想新建的能力挡在 graph 变更之前，并让节点以
        manager_execution_error 失败。
        """
        rt, mgr_id = self._harness(tmp_path)
        plan = _plan([_step("new_step", role="dev", task="Task A1")])

        created = rt.inject_revision_plan(mgr_id, plan)

        assert len(created) == 1
        assert created[0].step_ref == "new_step"


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
        # 与模块级 _completed_worker_under_root 完全同构（默认工作区存在 → resume 有地基），
        # 直接复用避免重复建图。
        return _completed_worker_under_root(tmp_path, relpath=relpath, role=role)

    def test_session_fresh_by_default_when_reused(self, tmp_path):
        rt, mgr_id, child = self._harness(tmp_path)
        sentinel = object()
        with Worker._aicu_cache_lock:
            Worker._aicu_cache[child.id] = sentinel
        try:
            prepared = rt._prepare_node_config(child.id, {**(child.config or {}), **{}}, mgr_id)
            rt._reactivate_reused_worker(child, _step("s1", role="dev"), prepared)
            with Worker._aicu_cache_lock:
                assert Worker._aicu_cache.get(child.id) is None  # 复用 = 新会话
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
            # 这条路径整体替换 pending_context，原任务在节点上再无留痕，
            # 任务链是唯一的记录。默认 reason 是 reuse（Manager 增量规划复用子节点）。
            assert [e["reason"] for e in node.task_history] == ["reuse"]
            assert node.task_history[0]["task"] == "Task W1"
            assert node.task == "t"
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


class TestNoActionVerdict:
    """「本轮增量/事件是否需要本子树行动」是 Manager(LLM) 的判断。

    唯一表达方式是空计划 + reason（决策 0 ①）；Runtime 不再比对计划与现有树的
    文字差异来替它推断 no-op —— 那种代理指标会把「模型没规划出新东西」误判成
    「任务已完成」，静默吞掉真实增量。
    """

    def test_empty_plan_skips_dispatch_when_children_terminal(self):
        """空计划 + 子节点已全部终态 → 不派活、不提交，直接进入 review。"""
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=True)
        rt.add_node(node)
        rt.add_children("M1", [
            _worker_node("A1", "M1", role="dev", status=NodeStatus.COMPLETED),
        ])
        ctx = ExecutableContext(node=node, runtime=rt)

        result = Manager(
            context=ctx,
            aicu_instance=_NoActionStub("该事件属于其他分支的职责"),
        ).run(ctx)

        assert result == NodeStatus.READY
        assert rt.submitted_revisions == []
        assert rt.submitted_plans == []
        assert node.manager_phase == "review"
        assert node.pending_revise is False
        assert node.pending_context is None
        # 理由落进节点日志，用户能看见「为什么这次没派活」
        assert any("该事件属于其他分支的职责" in e.message for e in node.log)

    def test_empty_plan_stays_waiting_for_active_children(self):
        """空计划 + 仍有运行中的子节点 → 继续等待，不重激活、不提交计划。"""
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=True)
        rt.add_node(node)
        rt.add_children("M1", [
            _worker_node("A1", "M1", role="dev", status=NodeStatus.RUNNING),
        ])
        ctx = ExecutableContext(node=node, runtime=rt)

        result = Manager(
            context=ctx, aicu_instance=_NoActionStub("无需行动"),
        ).run(ctx)

        assert result == NodeStatus.WAITING
        assert rt.submitted_revisions == []
        assert rt.submitted_plans == []
        assert node.manager_phase == "waiting"
        assert node.pending_revise is False

    def test_empty_plan_on_repair_activation_fails_loudly(self):
        """修复激活（父级评审已判定缺陷）收到空计划 → 明确失败，不静默转入 review。

        这不是「要不要行动」的判断（那次判断已经由父级 review 作出），而是计划本身
        不成立；静默滑进 review 会退化成「反复评审不通过 → 迭代耗尽」的假象。
        """
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=False)
        node.pending_context = {
            "collaboration_type": "repair",
            "defects": [{"step_ref": "A1", "reason": "产物缺失"}],
        }
        rt.add_node(node)
        rt.add_children("M1", [
            _worker_node("A1", "M1", role="dev", status=NodeStatus.COMPLETED),
        ])
        ctx = ExecutableContext(node=node, runtime=rt)

        result = Manager(
            context=ctx, aicu_instance=_NoActionStub("无需行动"),
        ).run(ctx)

        assert result == NodeStatus.FAILED
        assert rt.submitted_revisions == []
        assert rt.submitted_plans == []

    def test_childless_revision_empty_plan_still_goes_to_runtime(self):
        """无子节点时「本子树无需行动」不成立：空计划仍交回 Runtime 做结构校验
        （validate_plan 报 "ExecutionPlan has no steps"），不让节点静默停在 WAITING。"""
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=True)
        rt.add_node(node)
        ctx = ExecutableContext(node=node, runtime=rt)

        result = Manager(
            context=ctx, aicu_instance=_NoActionStub("无需行动"),
        ).run(ctx)

        assert result == NodeStatus.WAITING
        assert rt.submitted_revisions == ["M1"]
        assert rt.submitted_plans == []

    def test_identical_reuse_plan_is_still_submitted(self):
        """复用计划与既有子节点逐字相同也不再被代码拦截 —— 提交与否由 Manager 决定。"""
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=True)
        rt.add_node(node)
        child = _worker_node(
            "A1", "M1", role="dev", status=NodeStatus.COMPLETED,
            step_ref="dev",
        )
        rt.add_children("M1", [child])
        plan = _plan([
            _step("dev", role="dev", task=child.task, reuse_step_ref="dev"),
        ])
        ctx = ExecutableContext(node=node, runtime=rt)

        result = Manager(context=ctx, aicu_instance=_PlanStub(plan)).run(ctx)

        assert result == NodeStatus.WAITING
        assert rt.submitted_revisions == ["M1"]
        assert rt.submitted_plans == []


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


class _NoActionStub(_IncrementalExpandStub):
    """Manager 判定「本轮无需本子树行动」：空计划 + reason（决策 0 ①）。

    ask / ask_incremental 都要覆盖：_IncrementalExpandStub.ask_incremental 里的
    super() 按 type(self).__mro__ 解析，会直接落到 _ExpandStub 而跳过本类。
    """

    def __init__(self, reason: str = "与本子树职责无关"):
        super().__init__()
        self.reason = reason

    def _no_action(self) -> dict:
        plan = ExecutionPlan(steps=[], no_action_reason=self.reason)
        return {"execution_plan_json": plan.to_json()}

    def ask(self, prompt: str) -> dict:
        self.normal_prompts.append(prompt)
        return self._no_action()

    def ask_incremental(self, prompt: str) -> dict:
        self.incremental_prompts.append(prompt)
        return self._no_action()


class _PlanStub:
    """原样返回给定计划（用于验证提交路径，不关心计划内容）。"""

    def __init__(self, plan: ExecutionPlan):
        self.plan = plan

    def ask(self, prompt: str) -> dict:
        return {"execution_plan_json": self.plan.to_json()}

    def ask_incremental(self, prompt: str) -> dict:
        return self.ask(prompt)


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

    def test_revise_uses_fresh_full_expand_request(self):
        """Revise 是新会话：发完整首轮 prompt（含原任务 + 增量 + 候选），走 ask 而非 ask_incremental。"""
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
        assert aicu.incremental_prompts == []
        assert len(aicu.normal_prompts) == 1
        prompt = aicu.normal_prompts[0]
        assert "CURRENT_INCREMENTAL_TASK" in prompt
        assert "HISTORICAL_TASK" in prompt  # 检查点：新会话必须显式交代原任务
        assert "Reuse Candidates" in prompt
        assert "step_ref: `dev`" in prompt
        assert "reuse_step_ref" in prompt
        assert "执行上下文快照" in prompt
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
        assert "Task A1" in block  # 无摘要时退回截断后的 task
        assert "reuse_step_ref" in block
        assert "候选角色能够完成" in block
        assert "可由候选覆盖" in block

    def test_block_does_not_read_child_artifacts(self, tmp_path):
        art_dir = tmp_path / "child_ws"
        art_dir.mkdir()
        (art_dir / "secret_output.md").write_text("OUTPUT_BODY")
        rt = _DuckRuntime(with_revision=True)
        node = _manager_node(pending_revise=True)
        rt.add_node(node)
        c1 = _worker_node("A1", "M1", role="dev", status=NodeStatus.COMPLETED,
                          step_ref="dev")
        c1.task_summary = "SUMMARY_A1"
        c1.task = "FULL_TASK_A1"
        c1.artifact_path = str(art_dir)
        rt.add_children("M1", [c1])

        def _forbidden(node_id, path):
            raise AssertionError("candidates block must not read child artifacts")

        rt.read_artifact = _forbidden
        ctx = ExecutableContext(node=node, runtime=rt)
        mgr = Manager(context=ctx, aicu_instance=_ExpandStub())

        block = mgr._build_revise_candidates_block(ctx)

        assert "SUMMARY_A1" in block
        assert "FULL_TASK_A1" not in block  # 有摘要时只给摘要
        assert "artifact" not in block
        assert "secret_output.md" not in block
        assert "OUTPUT_BODY" not in block


# ============================================================================
# Test 10: session_server /revise endpoint
# ============================================================================

class TestReviseEndpoint:

    def test_revise_ok_and_restart_404(self, session_manager, session_client):
        resp = session_client.post("/v1/sessions", json={"task": "Build feature"})
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        # 空白图：手动创建 Manager 根节点
        root = session_client.post(
            f"/v1/sessions/{sid}/nodes",
            json={"task": "t", "exec_type": "manager"},
        )
        assert root.status_code == 201, root.text
        root_id = root.json()["id"]

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
            json={"task": "Add pagination", "config": {"model": "revise-model", "num_ccs": 3}},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "revising"
        assert start_calls == []
        node = rt.graph.get_node(root_id)
        assert node.status == NodeStatus.WAITING
        assert node.manager_phase == "expand"
        assert node.pending_revise is True
        assert node.task == "Add pagination"
        assert node.config["model"] == "revise-model"
        assert "num_ccs" not in node.config

        # 第二次 revise：当前 WAITING（非终态）→ 409
        again = session_client.post(
            f"/v1/sessions/{sid}/nodes/{root_id}/revise",
            json={"task": "More"},
        )
        assert again.status_code == 409

        # /restart 端点已删除：任何未匹配路径都不再可用。
        # 有前端 dist 时 SPA 静态挂载（html=True）对非 GET/HEAD 统一回 405，
        # 无 dist 时回 404——两种都证明该端点已不存在。
        gone = session_client.post(
            f"/v1/sessions/{sid}/nodes/{root_id}/restart", json={}
        )
        assert gone.status_code in (404, 405)

    def test_revise_endpoint_worker_ok(self, session_manager, session_client):
        """已完成 Worker 子节点经 /revise 正常进入增量续跑（不再 409 expected 'manager'）。"""
        import time

        resp = session_client.post("/v1/sessions", json={"task": "Build feature"})
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        root = session_client.post(
            f"/v1/sessions/{sid}/nodes",
            json={"task": "t", "exec_type": "manager"},
        )
        assert root.status_code == 201, root.text
        root_id = root.json()["id"]

        handle = session_manager.get_session(sid)
        rt = handle.runtime

        def _wait_completed(node_id: str) -> None:
            deadline = 5.0
            t0 = time.time()
            while time.time() - t0 < deadline:
                if rt.graph.get_node(node_id).status == NodeStatus.COMPLETED:
                    return
                time.sleep(0.02)
            raise AssertionError(f"node {node_id} did not complete")

        _wait_completed(root_id)

        # 在根 Manager 下建 Worker 子节点并等待其自动完成
        worker = session_client.post(
            f"/v1/sessions/{sid}/nodes",
            json={"task": "w", "exec_type": "worker", "parent_id": root_id},
        )
        assert worker.status_code == 201, worker.text
        worker_id = worker.json()["id"]
        _wait_completed(worker_id)

        # 停掉后台 tick，确保后续状态断言确定
        rt.scheduler.stop()
        start_calls: list[None] = []

        def unexpected_start() -> None:
            start_calls.append(None)

        rt.scheduler.start = unexpected_start  # type: ignore[method-assign]

        ok = session_client.post(
            f"/v1/sessions/{sid}/nodes/{worker_id}/revise",
            json={"task": "Add pagination"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "revising"
        node = rt.graph.get_node(worker_id)
        assert node.status == NodeStatus.WAITING
        assert node.pending_revise is True
        assert node.manager_phase is None
        assert node.task == "Add pagination"

        # 第二次 revise：当前 WAITING（非终态）→ 409
        again = session_client.post(
            f"/v1/sessions/{sid}/nodes/{worker_id}/revise",
            json={"task": "More"},
        )
        assert again.status_code == 409


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


class TestFreshReviseCheckpoint:
    """Worker 新会话 Revise 的检查点：原任务 + 上一版产物位置，不含历史对话。"""

    def test_checkpoint_renders_original_task_and_artifacts(self):
        node = Node(id="W1", exec_type="worker", role="dev", task="NEW_INCREMENT",
                    config={}, status=NodeStatus.WAITING)
        node.pending_context = {
            "collaboration_type": "revise",
            "session_mode": "fresh",
            "original_task": "ORIGINAL_TASK_TEXT",
            "incremental_task": "NEW_INCREMENT",
            "prior_artifacts": ["/ws/W1/cc_output.md"],
        }
        text = Worker._build_routing_context(node)
        assert "新会话检查点" in text
        assert "ORIGINAL_TASK_TEXT" in text
        assert "/ws/W1/cc_output.md" in text
        # 增量任务由「## 你的任务」承载，检查点里不重复
        assert "NEW_INCREMENT" not in text

    def test_non_fresh_revise_keeps_generic_routing_render(self):
        node = Node(id="W1", exec_type="worker", role="dev", task="t",
                    config={}, status=NodeStatus.WAITING)
        node.pending_context = {
            "collaboration_type": "revise",
            "activated_by": "parent-id-123",
            "incremental_task": "fix it",
        }
        text = Worker._build_routing_context(node)
        assert "路由激活上下文" in text
        assert "新会话检查点" not in text
