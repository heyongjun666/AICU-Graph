"""tests/test_closed_loop.py — A2 闭环台账 + closed_loop.json 业务产物测试。

覆盖:
1. build_closed_loop_report：纯函数构造完整闭环报告（manager/ledger/children/verdict）
2. write_closed_loop_report：原子写盘 + 可 JSON 反序列化
3. Manager _review 全通过 → 累积 collaboration_ledger + 落盘 closed_loop.json
4. Manager _review 多轮：先失败修复、后通过 → ledger 跨轮累积，闭环保含多轮
"""

import json
import os
from typing import Any, Optional

import pytest

from executables.base import ExecutableContext, RuntimeGateway
from executables.manager import Manager
from runtime.closed_loop import (
    build_closed_loop_report,
    build_deliverable_manifest,
    write_closed_loop_report,
    write_deliverable_manifest,
)
from runtime.models import ErrorObject, ExecutionPlan, Node, NodeStatus


class _Gateway(RuntimeGateway):
    """最小 RuntimeGateway 实现：记录状态更新 + 提供 get_node_workspace。"""

    def __init__(self, workspace: str):
        self._workspace = workspace
        self._nodes: dict[str, Node] = {}
        self._children: dict[str, list[Node]] = {}
        self.status_updates: list[tuple[str, NodeStatus, dict]] = []

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
        self.status_updates.append((node_id, status, updates))
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


def _make_manager(iteration: int = 0, max_iterations: int = 3) -> Node:
    return Node(
        id="mgr1", exec_type="manager", role="engineering_coord",
        task="Build app", config={}, status=NodeStatus.READY,
        manager_phase="review", iteration_count=iteration,
        max_iterations=max_iterations,
    )


def _make_child(step_ref: str, role: str = "dev") -> Node:
    return Node(
        id=f"node_{step_ref}", exec_type="worker", role=role,
        task=f"task {step_ref}", config={}, status=NodeStatus.COMPLETED,
        step_ref=step_ref,
    )


# ============================================================================
# 1. 纯函数
# ============================================================================

def test_build_closed_loop_report_structure():
    mgr = _make_manager(iteration=2)
    mgr.collaboration_ledger = [{
        "iteration": 0, "overall_passed": False,
        "passed": ["c1"], "failed": ["c2"],
        "fixes": [{"step_ref": "c2", "role": "dev", "task": "fix multiply"}],
    }]
    children = [_make_child("c1"), _make_child("c2")]
    report = build_closed_loop_report(mgr, children)
    assert report["type"] == "closed_loop"
    assert report["manager"]["iterations"] == 2
    assert report["verdict"]["overall_passed"] is True
    assert report["verdict"]["children_total"] == 2
    assert len(report["collaboration_ledger"]) == 1
    assert report["collaboration_ledger"][0]["fixes"][0]["task"] == "fix multiply"
    assert report["children"][0]["step_ref"] == "c1"


def test_write_closed_loop_report_atomic(tmp_path):
    mgr = _make_manager()
    report = build_closed_loop_report(mgr, [_make_child("c1")])
    path = write_closed_loop_report(report, str(tmp_path))
    assert os.path.exists(path)
    assert path.endswith("output/closed_loop.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["type"] == "closed_loop"
    assert data["verdict"]["overall_passed"] is True


# ============================================================================
# 2. Manager _review 全通过 → ledger + closed_loop.json
# ============================================================================

def test_review_all_pass_writes_closed_loop(tmp_path):
    node = _make_manager()
    gw = _Gateway(str(tmp_path))
    gw.add_node(node)
    gw.add_children(node.id, [_make_child("c1"), _make_child("c2")])
    ctx = ExecutableContext(node=node, runtime=gw,
                            config={"workspace_dir": str(tmp_path), "mode": "stub"})

    class _PassStub:
        def ask(self, prompt: str) -> dict:
            return {
                "overall_passed": True,
                "assessments": [
                    {"step_ref": "c1", "passed": True, "reason": "ok"},
                    {"step_ref": "c2", "passed": True, "reason": "ok"},
                ],
            }

    mgr = Manager(context=ctx, stub=_PassStub())
    result = mgr.run(ctx)
    assert result == NodeStatus.COMPLETED
    assert node.status == NodeStatus.COMPLETED
    # ledger 累积一轮且全通过
    assert len(node.collaboration_ledger) == 1
    assert node.collaboration_ledger[0]["overall_passed"] is True
    assert node.collaboration_ledger[0]["passed"] == ["c1", "c2"]
    assert node.collaboration_ledger[0]["failed"] == []
    # closed_loop.json 落盘
    path = os.path.join(str(tmp_path), "nodes", node.id, "output", "closed_loop.json")
    assert os.path.exists(path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["verdict"]["overall_passed"] is True
    assert data["verdict"]["children_total"] == 2


# ============================================================================
# 3. 多轮：先失败修复 → 后通过，ledger 跨轮累积
# ============================================================================

def test_review_fail_then_pass_accumulates_ledger(tmp_path):
    node = _make_manager()
    gw = _Gateway(str(tmp_path))
    gw.add_node(node)
    gw.add_children(node.id, [_make_child("c1"), _make_child("c2")])
    ctx = ExecutableContext(node=node, runtime=gw,
                            config={"workspace_dir": str(tmp_path), "mode": "stub"})

    class _SeqStub:
        def __init__(self):
            self.calls = 0

        def ask(self, prompt: str) -> dict:
            self.calls += 1
            if self.calls == 1:
                return {
                    "overall_passed": False,
                    "assessments": [
                        {"step_ref": "c1", "passed": True, "reason": "ok"},
                        {"step_ref": "c2", "passed": False, "reason": "multiply bug",
                         "fix": {"role": "dev", "task": "fix multiply",
                                 "file": "utils/tax.py", "line": "10"}},
                    ],
                }
            return {
                "overall_passed": True,
                "assessments": [
                    {"step_ref": "c1", "passed": True, "reason": "ok"},
                    {"step_ref": "c2", "passed": True, "reason": "fixed"},
                ],
            }

    stub = _SeqStub()
    mgr = Manager(context=ctx, stub=stub)

    # 第 1 轮：c2 失败 → WAITING，ledger 记录缺陷与修复指派
    result = mgr.run(ctx)
    assert result == NodeStatus.WAITING
    assert node.status == NodeStatus.WAITING
    assert len(node.collaboration_ledger) == 1
    round1 = node.collaboration_ledger[0]
    assert round1["overall_passed"] is False
    assert round1["failed"] == ["c2"]
    assert round1["fixes"][0]["task"] == "fix multiply"
    assert round1["fixes"][0]["file"] == "utils/tax.py"
    assert round1["fixes"][0]["line"] == "10"
    # 未全部通过 → 不落盘 closed_loop.json
    assert not os.path.exists(os.path.join(
        str(tmp_path), "nodes", node.id, "output", "closed_loop.json"))

    # 模拟修复完成：把 c2 重新标记 COMPLETED，进入第 2 轮 review
    node.iteration_count = 1
    node.manager_phase = "review"
    c2 = gw.get_node("node_c2")
    c2.status = NodeStatus.COMPLETED
    result = mgr.run(ctx)
    assert result == NodeStatus.COMPLETED
    assert node.status == NodeStatus.COMPLETED
    # ledger 跨轮累积：2 轮
    assert len(node.collaboration_ledger) == 2
    assert node.collaboration_ledger[1]["overall_passed"] is True
    # closed_loop.json 落盘，含完整两轮闭环
    path = os.path.join(str(tmp_path), "nodes", node.id, "output", "closed_loop.json")
    assert os.path.exists(path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["verdict"]["overall_passed"] is True
    assert len(data["collaboration_ledger"]) == 2
    assert data["collaboration_ledger"][0]["fixes"][0]["file"] == "utils/tax.py"


# ============================================================================
# 4. C3 deliverable_manifest：PASSED 时落盘交付物清单
# ============================================================================

def test_build_deliverable_manifest_structure():
    mgr = _make_manager()
    c1 = _make_child("c1")
    c2 = _make_child("c2")
    c1.artifact_path = "output/c1.txt"
    c2.artifact_path = None  # 未产出 → incomplete
    manifest = build_deliverable_manifest(mgr, [c1, c2])
    assert manifest["type"] == "deliverable_manifest"
    assert manifest["summary"]["total"] == 2
    assert manifest["summary"]["complete"] == 1
    assert manifest["summary"]["incomplete"] == 1
    assert manifest["summary"]["delivery_complete"] is False
    assert manifest["deliverables"][0]["step_ref"] == "c1"
    assert manifest["deliverables"][0]["complete"] is True
    assert manifest["deliverables"][1]["complete"] is False


def test_write_deliverable_manifest_atomic(tmp_path):
    mgr = _make_manager()
    c1 = _make_child("c1")
    c1.artifact_path = "output/c1.txt"
    manifest = build_deliverable_manifest(mgr, [c1])
    path = write_deliverable_manifest(manifest, str(tmp_path))
    assert os.path.exists(path)
    assert path.endswith("output/deliverable_manifest.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["type"] == "deliverable_manifest"
    assert data["summary"]["delivery_complete"] is True


def test_review_all_pass_writes_manifest(tmp_path):
    """PASSED 时 closed_loop.json + deliverable_manifest.json 同时落盘。"""
    node = _make_manager()
    gw = _Gateway(str(tmp_path))
    gw.add_node(node)
    c1 = _make_child("c1")
    c1.artifact_path = "output/c1.txt"
    gw.add_children(node.id, [c1])
    ctx = ExecutableContext(node=node, runtime=gw,
                            config={"workspace_dir": str(tmp_path), "mode": "stub"})

    class _PassStub:
        def ask(self, prompt: str) -> dict:
            return {
                "overall_passed": True,
                "assessments": [
                    {"step_ref": "c1", "passed": True, "reason": "ok"},
                ],
            }

    mgr = Manager(context=ctx, stub=_PassStub())
    result = mgr.run(ctx)
    assert result == NodeStatus.COMPLETED
    out_dir = os.path.join(str(tmp_path), "nodes", node.id, "output")
    manifest_path = os.path.join(out_dir, "deliverable_manifest.json")
    assert os.path.exists(manifest_path)
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["summary"]["delivery_complete"] is True
    assert data["deliverables"][0]["artifact_path"] == "output/c1.txt"
