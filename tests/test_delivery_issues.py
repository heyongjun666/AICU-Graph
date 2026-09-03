"""tests/test_delivery_issues.py — P1：DeliveryIssue 三分类 + Repair Loop 消费。

覆盖（V3-2.1 计划 test_delivery_issues.py 用例 + 补充）：
- Gate 错误 → 三分类映射（OUTSIDE_ALLOWED_ROOT/FRAMEWORK_DIR→WRONG_LOCATION；
  MISSING_ARTIFACT/EMPTY_ARTIFACT→ARTIFACT_NOT_CREATED；
  TYPE_MISMATCH/VERDICT_MISSING→CONTRACT_FAILED；空/未知→CONTRACT_FAILED）
- REPAIR_STRATEGIES 固定集成员
- issue_type → repair 策略映射（三分类 + Assembly 四类 + 未知兜底 retry_worker）
- DeliveryIssue 携带新 issue_type 的 to_dict 回环
- Repair Loop 消费：_repair_blocking_delivery 的 fix task 携带
  `strategy=<策略>` 关键字（Assembly 类型 + Gate 三分类各验证一例）
"""

import os

from executables.base import ExecutableContext
from executables.manager import Manager
from runtime.delivery.issues import (
    REPAIR_STRATEGIES,
    DeliveryIssue,
    GATE_ERROR_TO_ISSUE_TYPE,
    issue_type_from_gate_error,
    repair_strategy_for,
)
from runtime.models import Node, NodeStatus


# ============================================================================
# Gate 错误 → 三分类映射（纯函数）
# ============================================================================

def test_issue_type_gate_error_mapping():
    cases = {
        "OUTSIDE_ALLOWED_ROOT: /tmp/evil": "WRONG_LOCATION",
        "FRAMEWORK_DIR: .codegraph": "WRONG_LOCATION",
        "MISSING_ARTIFACT: no such path": "ARTIFACT_NOT_CREATED",
        "EMPTY_ARTIFACT: empty dir": "ARTIFACT_NOT_CREATED",
        "TYPE_MISMATCH: expected .py got .md": "CONTRACT_FAILED",
        "VERDICT_MISSING: no verdict block": "CONTRACT_FAILED",
    }
    for error, expected in cases.items():
        assert issue_type_from_gate_error(error) == expected, error
    assert len(cases) == len(GATE_ERROR_TO_ISSUE_TYPE), \
        "映射表每一项都应被覆盖，避免漏测"


def test_issue_type_gate_error_edge_cases():
    # 空 / None / 未知前缀 → CONTRACT_FAILED（fail-closed）
    assert issue_type_from_gate_error("") == "CONTRACT_FAILED"
    assert issue_type_from_gate_error(None) == "CONTRACT_FAILED"
    assert issue_type_from_gate_error("UNKNOWN_PREFIX: x") == "CONTRACT_FAILED"


def test_repair_strategies_frozenset():
    assert REPAIR_STRATEGIES == {
        "ARTIFACT_NOT_CREATED",
        "WRONG_LOCATION",
        "CONTRACT_FAILED",
    }


# ============================================================================
# issue_type → repair 策略映射
# ============================================================================

def test_repair_strategy_gate_three_types():
    # Gate 三分类：没产物→retry / 写错位置→revise / 契约不满足→qa
    assert repair_strategy_for("ARTIFACT_NOT_CREATED") == "retry_worker"
    assert repair_strategy_for("WRONG_LOCATION") == "revise_instruction"
    assert repair_strategy_for("CONTRACT_FAILED") == "qa_repair"


def test_repair_strategy_assembly_four_types():
    # Assembly 四类沿用：缺产物/空/依赖→retry；碰撞→revise
    assert repair_strategy_for("missing_artifact") == "retry_worker"
    assert repair_strategy_for("empty_artifact") == "retry_worker"
    assert repair_strategy_for("dependency_mismatch") == "retry_worker"
    assert repair_strategy_for("output_collision") == "revise_instruction"


def test_repair_strategy_unknown_defaults():
    assert repair_strategy_for("") == "retry_worker"
    assert repair_strategy_for("bogus_type") == "retry_worker"


# ============================================================================
# DeliveryIssue 携带新 issue_type 的 to_dict 回环
# ============================================================================

def test_delivery_issue_roundtrip_new_types():
    for issue_type in ("ARTIFACT_NOT_CREATED", "WRONG_LOCATION", "CONTRACT_FAILED"):
        issue = DeliveryIssue(
            severity="blocking",
            issue_type=issue_type,
            node_id="n1",
            step_ref="app_v1",
            expected="/shared/app_ws",
            actual="/shared",
            reason="declared but missing",
        )
        d = issue.to_dict()
        assert d["issue_type"] == issue_type
        assert d["severity"] == "blocking"
        assert d["step_ref"] == "app_v1"


# ============================================================================
# Repair Loop 消费：_repair_blocking_delivery 的 fix task 携带 strategy
# ============================================================================

class _Gateway:
    """最小 RuntimeGateway：submit_fix_plan 记录 + 子节点提供 step_ref→role。"""

    def __init__(self, workspace: str, children: list):
        self._workspace = workspace
        self._children = children
        self.fix_plans: list = []

    def get_node_workspace(self, node_id: str) -> str:
        return os.path.join(self._workspace, "nodes", node_id)

    def get_children(self, node_id: str) -> list:
        return self._children

    def get_dependencies(self, node_id: str) -> list:
        return []

    def get_node(self, node_id: str):
        return None

    def read_artifact(self, node_id: str, path: str) -> str:
        return ""

    def update_node_status(self, node_id: str, status: NodeStatus, **updates) -> None:
        pass

    def submit_fix_plan(self, node_id: str, failed: list, iteration: int) -> list:
        self.fix_plans.append((node_id, failed, iteration))
        return []


def _child(step_ref: str, role: str = "code") -> Node:
    return Node(
        id=f"node_{step_ref}", exec_type="worker", role=role, task=f"task {step_ref}",
        config={}, status=NodeStatus.COMPLETED, step_ref=step_ref,
    )


def _manager_node() -> Node:
    return Node(
        id="mgr1", exec_type="manager", role="engineering_coord",
        task="Build app", config={}, status=NodeStatus.READY,
        manager_phase="review", iteration_count=0, max_iterations=3,
    )


def _issue(issue_type: str, step_ref: str = "app_v1") -> DeliveryIssue:
    return DeliveryIssue(
        severity="blocking",
        issue_type=issue_type,
        node_id=f"node_{step_ref}",
        step_ref=step_ref,
        expected="/shared/app_ws",
        actual="/shared",
        reason="deterministic delivery issue",
    )


def test_repair_loop_consumes_strategy_assembly_type(tmp_path):
    """Assembly missing_artifact → fix task 带 strategy=retry_worker。"""
    node = _manager_node()
    gw = _Gateway(str(tmp_path), [_child("app_v1")])
    ctx = ExecutableContext(node=node, runtime=gw, config={"mode": "stub"})
    mgr = Manager(context=ctx, aicu_instance=None)

    from runtime.models import LogEntry, LogLevel
    status = mgr._repair_blocking_delivery(
        ctx, [_issue("missing_artifact")], [], "/tmp/review.md", 0, 3,
    )
    assert status == NodeStatus.WAITING
    assert gw.fix_plans and gw.fix_plans[0][1][0]["fix"]["task"] == (
        "[Assembly] missing_artifact (strategy=retry_worker): "
        "deterministic delivery issue"
    )


def test_repair_loop_consumes_strategy_gate_type(tmp_path):
    """Gate 三分类 WRONG_LOCATION → fix task 带 strategy=revise_instruction。"""
    node = _manager_node()
    gw = _Gateway(str(tmp_path), [_child("app_v1")])
    ctx = ExecutableContext(node=node, runtime=gw, config={"mode": "stub"})
    mgr = Manager(context=ctx, aicu_instance=None)

    status = mgr._repair_blocking_delivery(
        ctx, [_issue("WRONG_LOCATION")], [], "/tmp/review.md", 0, 3,
    )
    assert status == NodeStatus.WAITING
    task = gw.fix_plans[0][1][0]["fix"]["task"]
    assert task.startswith("[Assembly] WRONG_LOCATION")
    assert "strategy=revise_instruction" in task
    assert "deterministic delivery issue" in task
