"""tests/test_rootcause_fixes.py — Manager↔Manager 协作根治测试。

覆盖两个确定性的引擎根因（real 模式 E2E 卡死/协作未路由）的修复：

Fix 1 — review 内容成为 ask() 一等字段（content/content_parsed），_review 只解析
一次内容喂给 _parse_review_assessments / _write_review_output：
  * _resolve_review_content 优先级：content_parsed → content → final_output_path → 原样
  * real 模式元数据 dict（带 content）→ content 被正确解析（回归根因 1 的解析优先级）；
    V3.1 P2 起协作不再经 review JSON collaboration 字段路由（_parse_collaboration 已移除），
    契约中即使残留 collaboration 字段也不产生 pending_routing
  * _write_review_output 契约感知（overall_passed / assessments 反映真实判定）

Fix 2 — "重新激活 → 父 Manager 可重新触发评审"不变量统一：
  * submit_fix_plan 尾部补父链回退（清除父通知 + rewind），回归根因 2（root 卡死 WAITING）
  * rewind_parent_chain_for_retry 对已 WAITING 的 Manager 也清通知
"""

import json
import os
from typing import Any, Optional

import pytest

from runtime.graph import Graph
from runtime.models import (
    Edge,
    EdgeType,
    ExecutionPlan,
    Node,
    NodeStatus,
    RoutingDecision,
)
from runtime.planner import Planner
from runtime.persistence import Persistence
from runtime.runtime import Runtime
from executables.base import ExecutableContext, RuntimeGateway
from executables.manager import Manager


# ============================================================================
# Fix 1: _resolve_review_content 优先级
# ============================================================================

class TestResolveReviewContent:
    def test_content_parsed_takes_priority(self):
        """content_parsed（dict）优先于 content（str）与 final_output_path。"""
        contract = {"overall_passed": True, "assessments": []}
        resolved = Manager._resolve_review_content({
            "content": "unused — content_parsed wins",
            "content_parsed": contract,
            "final_output_path": "/tmp/nope",
        })
        assert resolved is contract

    def test_content_json_string_returns_dict(self):
        """content 为纯 JSON 字符串 → 解析为 dict。"""
        resolved = Manager._resolve_review_content({
            "content": json.dumps({
                "overall_passed": True,
                "assessments": [{"step_ref": "c1", "passed": True}],
            }),
            "final_output_path": "/tmp/nope",
        })
        assert isinstance(resolved, dict)
        assert resolved["overall_passed"] is True

    def test_content_markdown_with_embedded_json_returns_dict(self):
        """content 为叙述性 markdown + 末尾 JSON → 提取为 dict。"""
        resolved = Manager._resolve_review_content({
            "content": (
                "## Review\nc1 通过。\n"
                '{"overall_passed": true, "assessments": '
                '[{"step_ref": "c1", "passed": true}]}'
            ),
        })
        assert isinstance(resolved, dict)
        assert resolved["assessments"][0]["step_ref"] == "c1"

    def test_content_plain_text_returns_str(self):
        """content 为不可解析文本 → 原样返回 str（交由降级路径处理）。"""
        resolved = Manager._resolve_review_content({
            "content": "// artifact echo\nconst x = 1;\n",
            "final_output_path": "/tmp/nope",
        })
        assert isinstance(resolved, str)
        assert "artifact echo" in resolved

    def test_final_output_path_file_read(self, tmp_path):
        """无 content 字段 → 退回读 final_output_path 指向的产出文件。"""
        fpath = tmp_path / "plan_result"
        fpath.write_text(json.dumps({
            "overall_passed": True,
            "assessments": [{"step_ref": "c1", "passed": True}],
        }), encoding="utf-8")
        resolved = Manager._resolve_review_content({"final_output_path": str(fpath)})
        assert isinstance(resolved, dict)
        assert resolved["overall_passed"] is True
        assert resolved["assessments"][0]["step_ref"] == "c1"

    def test_stub_direct_dict_unchanged(self):
        """stub/单测直传契约 dict（无 content/path）→ 原样返回。"""
        contract = {"overall_passed": True,
                    "assessments": [{"step_ref": "c1", "passed": True}]}
        resolved = Manager._resolve_review_content(contract)
        assert resolved is contract

    def test_str_passthrough(self):
        """非 dict 输入（str）→ 原样返回。"""
        assert Manager._resolve_review_content("raw text") == "raw text"


# ============================================================================
# Fix 1: real 模式元数据 dict（带 content）→ content 解析正确，协作不再路由
# ============================================================================

class _Gateway(RuntimeGateway):
    """Manager 测试用最小 RuntimeGateway（与 test_manager_collaboration 同构）。"""

    def __init__(self, workspace: str):
        self._workspace = workspace
        self._nodes: dict[str, Node] = {}
        self._children: dict[str, list[Node]] = {}

    def add_node(self, node: Node) -> None:
        self._nodes[node.id] = node

    def add_children(self, parent_id: str, children: list[Node]) -> None:
        self._children.setdefault(parent_id, []).extend(children)
        for c in children:
            self._nodes[c.id] = c

    def get_node_workspace(self, node_id: str) -> str:
        return os.path.join(self._workspace, "nodes", node_id)

    def get_node(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(node_id)

    def get_children(self, node_id: str) -> list[Node]:
        return self._children.get(node_id, [])

    def get_dependencies(self, node_id: str) -> list[Node]:
        return []

    def read_artifact(self, node_id: str, path: str) -> str:
        return ""

    def update_node_status(self, node_id: str, status: NodeStatus, **updates: Any) -> None:
        node = self._nodes.get(node_id)
        if node is not None:
            for k, v in updates.items():
                if hasattr(node, k):
                    setattr(node, k, v)
            node.status = status

    def submit_plan(self, node_id: str, plan: ExecutionPlan) -> None:
        pass

    def submit_fix_plan(self, node_id: str, failed: list, iteration: int) -> list[Node]:
        return []

    def register_external_input_wait(self, node_id: str, input_schema: Optional[dict] = None) -> str:
        return "token"

    def deliver_external_input(self, wait_token: str, input_data: Any) -> None:
        pass


class _Stub:
    """可配置 ask() 返回的桩。"""

    def __init__(self, payload: dict):
        self._payload = payload
        self.calls = 0

    def ask(self, prompt: str) -> dict:
        self.calls += 1
        return self._payload


def _make_manager(node_id: str = "mgr1", step_ref: str = "plan_mgr",
                  max_iterations: int = 3, **kw) -> Node:
    kw.setdefault("status", NodeStatus.READY)
    kw.setdefault("manager_phase", "review")
    return Node(
        id=node_id, exec_type="manager", role="architecture",
        task="Build app", config={}, step_ref=step_ref,
        max_iterations=max_iterations, **kw,
    )


def _make_child(step_ref: str, role: str = "dev") -> Node:
    return Node(
        id=f"node_{step_ref}", exec_type="worker", role=role,
        task=f"task {step_ref}", config={}, status=NodeStatus.COMPLETED,
        step_ref=step_ref,
    )


def _review_contract() -> dict:
    return {
        "overall_passed": True,
        "assessments": [{"step_ref": "c1", "passed": True, "reason": "ok"}],
        # V3.1 P2：协作不再经 review JSON 顶层 collaboration 字段路由
        # （_parse_collaboration 已移除），字段残留不再产生 pending_routing。
        "collaboration": {
            "type": "request", "target": "req_mgr",
            "required_role": "requirements",
            "task": "review plan", "expected_action": "approve plan",
            "reason": "plan needs requirement review",
        },
    }


class TestRealMetadataReviewContent:
    def _run_review(self, tmp_path, payload: dict) -> tuple[Node, NodeStatus]:
        node = _make_manager(step_ref="plan_mgr")
        gw = _Gateway(str(tmp_path))
        gw.add_node(node)
        gw.add_children(node.id, [_make_child("c1")])
        ctx = ExecutableContext(node=node, runtime=gw,
                                config={"workspace_dir": str(tmp_path), "mode": "stub"})
        mgr = Manager(context=ctx, aicu_instance=_Stub(payload))
        result = mgr.run(ctx)
        return node, result

    def test_metadata_with_content_field_review_passes(self, tmp_path):
        """real 模式元数据 dict + content（JSON 契约）→ 契约被解析、评审通过。

        回归根因 1 的解析优先级：content 里的契约必须被正确读取（不再漏读挂起）。
        V3.1 P2 起即使契约残留 collaboration 字段，也不产生 pending_routing。
        """
        payload = {
            "final_output_path": str(tmp_path / "plan_result"),
            "scores": {"c1": 8.0},
            "audit_passed": True,
            "consensus_reached": True,
            "content": json.dumps(_review_contract(), ensure_ascii=False),
        }
        node, result = self._run_review(tmp_path, payload)

        assert result == NodeStatus.COMPLETED
        assert node.pending_routing is None, \
            "V3.1 P2: review JSON collaboration 字段不再路由 → pending_routing 恒 None"
        assert len(node.collaboration_ledger) == 1
        assert node.collaboration_ledger[0]["overall_passed"] is True

    def test_metadata_with_content_parsed_review_passes(self, tmp_path):
        """元数据 dict + content_parsed（dict）→ 同样命中内容解析。"""
        payload = {
            "final_output_path": str(tmp_path / "plan_result"),
            "content": "unused — content_parsed wins",
            "content_parsed": _review_contract(),
        }
        node, result = self._run_review(tmp_path, payload)
        assert result == NodeStatus.COMPLETED
        assert node.pending_routing is None
        assert len(node.collaboration_ledger) == 1

    def test_metadata_file_only_review_passes(self, tmp_path):
        """元数据 dict + 仅 final_output_path（文件内容为契约）→ 退回读文件命中契约。"""
        fpath = tmp_path / "plan_result"
        fpath.write_text(json.dumps(_review_contract(), ensure_ascii=False),
                         encoding="utf-8")
        payload = {
            "final_output_path": str(fpath),
            "scores": {"c1": 8.0},
            "audit_passed": True,
            "consensus_reached": True,
        }
        node, result = self._run_review(tmp_path, payload)
        assert result == NodeStatus.COMPLETED
        assert node.pending_routing is None
        assert len(node.collaboration_ledger) == 1


# ============================================================================
# Fix 1: _write_review_output 契约感知
# ============================================================================

class TestWriteReviewOutputContract:
    def test_review_file_reflects_contract(self, tmp_path):
        """契约 dict → review_vN.md 反映 overall_passed + assessments（非空模板）。"""
        node = _make_manager()
        gw = _Gateway(str(tmp_path))
        gw.add_node(node)
        ctx = ExecutableContext(node=node, runtime=gw,
                                config={"workspace_dir": str(tmp_path), "mode": "stub"})
        mgr = Manager(context=ctx, aicu_instance=_Stub({}))
        contract = {
            "overall_passed": False,
            "assessments": [
                {"step_ref": "c1", "passed": True, "reason": "ok"},
                {"step_ref": "c2", "passed": False, "reason": "bad",
                 "fix": {"role": "dev", "task": "rework"}},
            ],
            "collaboration": {"type": "request", "target": "req_mgr",
                              "task": "x", "expected_action": "y", "reason": "z"},
        }
        path = mgr._write_review_output(ctx, contract, {"c1": "..."}, 1)
        text = open(path, encoding="utf-8").read()
        assert "**Audit Passed**: False" in text
        assert "1/2 children passed" in text
        assert "- c1" in text
        assert "- c2" in text

    def test_metadata_dict_falls_back_to_legacy(self, tmp_path):
        """元数据 dict（summary/audit_passed）→ 走原回退分支，语义不变。"""
        node = _make_manager()
        gw = _Gateway(str(tmp_path))
        gw.add_node(node)
        ctx = ExecutableContext(node=node, runtime=gw,
                                config={"workspace_dir": str(tmp_path), "mode": "stub"})
        mgr = Manager(context=ctx, aicu_instance=_Stub({}))
        path = mgr._write_review_output(
            ctx, {"audit_passed": True, "summary": "All good"}, {"c1": "..."}, 0,
        )
        text = open(path, encoding="utf-8").read()
        assert "**Audit Passed**: True" in text
        assert "**Summary**: All good" in text


class TestWriteReviewOutputWorkspaceFallback:
    """无 runtime.get_node_workspace 时的 workspace 兜底分支。

    Gateway 置 `get_node_workspace = None` 让 `callable(...)` 为假，走到 else 分支
    —— 这是 config.workspace_dir 唯一被读取的路径，原有测试都走 getter 分支没覆盖到。
    """

    def _ctx(self, tmp_path, node, workspace_dir):
        gw = _Gateway(str(tmp_path))
        gw.add_node(node)
        gw.get_node_workspace = None  # type: ignore[method-assign]
        return ExecutableContext(node=node, runtime=gw,
                                 config={"workspace_dir": workspace_dir, "mode": "stub"})

    def test_workspace_dir_none_falls_back_instead_of_typeerror(self, tmp_path, monkeypatch):
        """回归钉：`workspace_dir` 存在但为 None 时，dict.get 的默认值不生效。

        旧写法 `ctx.config.get("workspace_dir", os.getcwd())` 会返回 None，
        紧接着 `os.path.join(None, filename)` 直接 TypeError，整个 review 落盘失败。
        """
        monkeypatch.chdir(tmp_path)  # 兜底路径基于 cwd，隔离到 tmp_path
        node = _make_manager()
        ctx = self._ctx(tmp_path, node, None)
        mgr = Manager(context=ctx, aicu_instance=_Stub({}))

        path = mgr._write_review_output(
            ctx, {"audit_passed": True, "summary": "ok"}, {"c1": "..."}, 0,
        )
        assert path == os.path.join(str(tmp_path), "workspaces", node.id, "review_v0.md")
        assert os.path.exists(path)

    def test_workspace_dir_value_still_wins(self, tmp_path):
        """显式配置的 workspace_dir 仍然生效（兜底不得把它顶掉）。"""
        node = _make_manager()
        custom = tmp_path / "custom"
        custom.mkdir()
        ctx = self._ctx(tmp_path, node, str(custom))
        mgr = Manager(context=ctx, aicu_instance=_Stub({}))

        path = mgr._write_review_output(
            ctx, {"audit_passed": True, "summary": "ok"}, {"c1": "..."}, 0,
        )
        assert path == os.path.join(str(custom), "review_v0.md")
        assert os.path.exists(path)


# ============================================================================
# Fix 2a: submit_fix_plan 补父链回退（回归根因 2 — root 卡死 WAITING）
# ============================================================================

def _make_runtime(tmp_path) -> Runtime:
    return Runtime(
        persistence=Persistence(file_path=str(tmp_path / "state.json")),
        workspace_root=str(tmp_path),
    )


class TestSubmitFixPlanRewindsParent:
    def test_submit_fix_plan_clears_parent_notification(self, tmp_path):
        """修复子节点后清除父 Manager 通知 → 父 Manager 可再次触发评审。

        回归根因 2：修复前 submit_fix_plan 不清父通知，父 Manager 处于
        WAITING + _notified_managers 集合内，_check_manager_children_completed
        提前返回，修复完成后永远无法重新触发评审（root 卡死 WAITING）。
        """
        runtime = _make_runtime(tmp_path)
        parent = Node(
            id="root", exec_type="manager", role="engineering_coord",
            task="root task", config={}, status=NodeStatus.WAITING,
            manager_phase="waiting", step_ref="root",
        )
        child = Node(
            id="calc", exec_type="worker", role="dev", task="calc", config={},
            status=NodeStatus.FAILED, parent_id="root", step_ref="calc",
            task_summary="calc summary",
        )
        runtime.graph.add_node(parent)
        runtime.graph.add_node(child)
        runtime.graph.add_edge(
            Edge(id="e1", type=EdgeType.PARENT, from_node="root", to_node="calc")
        )

        # 模拟评审后状态：父 Manager 已通知 + WAITING
        runtime.scheduler._notified_managers.add("root")

        affected = runtime.submit_fix_plan(
            "root",
            [{"step_ref": "calc", "reason": "calc bug",
              "fix": {"role": "dev", "task": "fix the calc"}}],
            1,
        )

        assert len(affected) == 1
        assert "root" not in runtime.scheduler._notified_managers, \
            "submit_fix_plan 必须清除父 Manager 通知，否则父节点卡死 WAITING"
        # 失败子节点被原地续接（READY 等待重新拉起），父链已回退可重新评审
        assert runtime.get_node("calc").status == NodeStatus.READY

    def test_submit_fix_plan_root_no_parent_is_noop(self, tmp_path):
        """根节点（parent_id=None）无父链可回退，不抛异常。"""
        runtime = _make_runtime(tmp_path)
        parent = Node(
            id="root", exec_type="manager", role="engineering_coord",
            task="root task", config={}, status=NodeStatus.WAITING,
            manager_phase="waiting", step_ref="root",
        )
        child = Node(
            id="calc", exec_type="worker", role="dev", task="calc", config={},
            status=NodeStatus.FAILED, parent_id="root", step_ref="calc",
            task_summary="calc summary",
        )
        runtime.graph.add_node(parent)
        runtime.graph.add_node(child)
        runtime.graph.add_edge(
            Edge(id="e1", type=EdgeType.PARENT, from_node="root", to_node="calc")
        )
        affected = runtime.submit_fix_plan(
            "root",
            [{"step_ref": "calc", "reason": "bug",
              "fix": {"role": "dev", "task": "fix"}}],
            1,
        )
        assert len(affected) == 1

    def test_submit_fix_plan_manager_keeps_exec_type_despite_worker_role_fix(self, tmp_path):
        """有子节点的 manager 修复时 fix.role 给成 worker 角色 → 收敛回原角色原地续接（不降级）。

        回归：review 模型给 manager 子节点（engineering_coord）配了 worker 角色（code）时，
        旧实现按角色类型推导 exec_type → 新建 worker 替代节点 + STOP 原 manager，让
        backend_impl 的子树（layer）悬空，破坏「worker 不展开」不变量。
        现改为：exec_type 是结构不变量，跨类型 fix.role 收敛回原角色，manager 原地续接
        （身份/子树/exec_type 原样保留，仅重置为 READY 并注入修复上下文）。
        """
        runtime = _make_runtime(tmp_path)
        # 注入角色集，使 _classify_role_type 能把 code 判为 worker 角色
        runtime._manager_role_ids = {"engineering_coord"}
        runtime._worker_role_ids = {"code", "dev"}

        parent = Node(
            id="root", exec_type="manager", role="engineering_coord",
            task="root task", config={}, status=NodeStatus.WAITING,
            manager_phase="waiting", step_ref="root",
        )
        mgr_child = Node(
            id="backend_impl", exec_type="manager", role="engineering_coord",
            task="backend", config={}, status=NodeStatus.FAILED, parent_id="root",
            step_ref="backend_impl", task_summary="backend summary",
        )
        grandchild = Node(
            id="layer", exec_type="worker", role="dev", task="layer", config={},
            status=NodeStatus.COMPLETED, parent_id="backend_impl", step_ref="layer",
        )
        for n in (parent, mgr_child, grandchild):
            runtime.graph.add_node(n)
        runtime.graph.add_edge(
            Edge(id="e1", type=EdgeType.PARENT, from_node="root", to_node="backend_impl")
        )
        runtime.graph.add_edge(
            Edge(id="e2", type=EdgeType.PARENT, from_node="backend_impl", to_node="layer")
        )

        affected = runtime.submit_fix_plan(
            "root",
            [{"step_ref": "backend_impl", "reason": "code defect",
              "fix": {"role": "code", "task": "fix the code"}}],
            1,
        )

        # 原地续接（非新建替代节点）：返回原节点本身，身份不变
        assert len(affected) == 1
        assert affected[0].id == "backend_impl"
        # exec_type / role 未被降级：仍是 manager/engineering_coord
        assert affected[0].exec_type == "manager"
        assert affected[0].role == "engineering_coord"
        # 修复任务注入 + 重新激活为 READY（而非 STOPPED）
        assert affected[0].task == "fix the code"
        assert affected[0].status == NodeStatus.READY
        # 子树原样保留
        assert runtime.graph.children("backend_impl")


# ============================================================================
# Fix 2b: rewind_parent_chain_for_retry 对 WAITING Manager 也清通知
# ============================================================================

class TestRewindClearsWaitingNotification:
    def test_rewind_clears_notification_for_waiting_chain(self):
        """已 WAITING 的 Manager 链也必须清通知（旧逻辑仅清非 WAITING）。"""
        graph = Graph()
        root = Node(
            id="R", exec_type="manager", role="engineering_coord", task="root",
            config={}, status=NodeStatus.WAITING, manager_phase="waiting",
            step_ref="root",
        )
        plan = Node(
            id="P", exec_type="manager", role="architecture", task="plan",
            config={}, status=NodeStatus.WAITING, manager_phase="waiting",
            step_ref="plan_mgr", parent_id="R",
        )
        graph.add_node(root)
        graph.add_node(plan)
        graph.add_edge(Edge(id="e1", type=EdgeType.PARENT, from_node="R", to_node="P"))
        planner = Planner(graph)

        cleared: list[str] = []
        planner.rewind_parent_chain_for_retry("P", cleared.append)

        # 修复前：两者均 WAITING → 通知不清 → cleared 为空；修复后：P 与 R 都清
        assert cleared == ["P", "R"]

    def test_rewind_resets_review_manager_and_clears_notification(self):
        """review 态父 Manager → 拉回 WAITING + 清通知（状态回退语义不变）。"""
        graph = Graph()
        root = Node(
            id="R", exec_type="manager", role="engineering_coord", task="root",
            config={}, status=NodeStatus.WAITING, manager_phase="waiting",
            step_ref="root",
        )
        plan = Node(
            id="P", exec_type="manager", role="architecture", task="plan",
            config={}, status=NodeStatus.READY, manager_phase="review",
            step_ref="plan_mgr", parent_id="R",
        )
        graph.add_node(root)
        graph.add_node(plan)
        graph.add_edge(Edge(id="e1", type=EdgeType.PARENT, from_node="R", to_node="P"))
        planner = Planner(graph)

        cleared: list[str] = []
        planner.rewind_parent_chain_for_retry("P", cleared.append)

        assert plan.status == NodeStatus.WAITING
        assert plan.manager_phase == "waiting"
        assert cleared == ["P", "R"]
