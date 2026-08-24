"""tests/test_convergence_budget.py — 收敛与升级台账测试。

C2 单节点激活预算（max_activations）已移除：节点可无限重激活，不再拦截、不再记
activation_budget_exceeded 升级。

升级台账：
  - Scheduler.get_escalations() 返回结构化升级记录
  - closed_loop 报告并入 escalations + converged 判定
"""

from datetime import datetime
from typing import Any, Optional

from runtime.closed_loop import build_closed_loop_report
from runtime.graph import Graph
from runtime.models import (
    Edge,
    EdgeType,
    Node,
    NodeStatus,
    RoutingDecision,
)
from runtime.scheduler import Scheduler
from executables.registry import ExecutableRegistry


# ============================================================================
# Scheduler harness
# ============================================================================

class _MockDelegate:
    def on_manager_children_completed(self, manager_node_id: str) -> None:
        pass

    def on_node_status_changed(self, node_id: str, status: NodeStatus) -> None:
        pass

    def get_active_execution_count(self) -> int:
        return 0


class _MockGateway:
    def submit_plan(self, node_id: str, plan: Any) -> None:
        pass

    def update_node_status(self, node_id: str, status: NodeStatus, **updates: Any) -> None:
        pass

    def get_node(self, node_id: str) -> Optional[Node]:
        return None

    def get_children(self, node_id: str) -> list:
        return []

    def read_artifact(self, node_id: str, path: str) -> str:
        return ""

    def register_external_input_wait(self, node_id: str, input_schema: Optional[dict] = None) -> str:
        return "token"

    def deliver_external_input(self, wait_token: str, input_data: Any) -> None:
        pass


def _make_scheduler():
    graph = Graph()
    root = Node(
        id="R", exec_type="manager", role="engineering_coord",
        task="root", config={}, status=NodeStatus.WAITING, step_ref="root",
    )
    dev = Node(
        id="D", exec_type="worker", role="dev",
        task="dev", config={}, status=NodeStatus.COMPLETED,
        parent_id="R", step_ref="dev",
    )
    test = Node(
        id="T", exec_type="worker", role="test",
        task="test", config={}, status=NodeStatus.COMPLETED,
        parent_id="R", step_ref="test",
    )
    for n in (root, dev, test):
        graph.add_node(n)
    graph.add_edge(Edge(id="e1", type=EdgeType.PARENT, from_node="R", to_node="D"))
    graph.add_edge(Edge(id="e2", type=EdgeType.PARENT, from_node="R", to_node="T"))
    scheduler = Scheduler(
        graph=graph, registry=ExecutableRegistry(),
        delegate=_MockDelegate(), runtime_gateway=_MockGateway(),
        max_concurrency=4, planner=None,
    )
    return graph, scheduler


# ============================================================================
# C2 单节点 activation budget（已移除：节点可无限重激活）
# ============================================================================

class TestActivationBudgetRemoved:
    def test_activation_beyond_previous_limit_still_activates(self):
        graph, scheduler = _make_scheduler()
        test = graph.get_node("T")
        test.activation_count = 3  # 已超过旧 max_activations=3，仍应可激活
        graph.update_node("D", pending_routing=RoutingDecision(
            activate=["test"],
            contexts={"test": {
                "collaboration_type": "repair", "feedback": "fix again",
            }},
            reason="still broken",
        ))
        scheduler._handle_routing("D")
        assert graph.get_node("T").status == NodeStatus.WAITING, "无激活上限，节点应被激活"
        assert graph.get_node("T").activation_count == 4, "激活计数应递增"
        assert scheduler.get_escalations() == [], "不再产生 activation_budget_exceeded 升级"

    def test_activation_no_longer_escalates(self):
        """C2 已移除：任何次数的激活都不被拦截、不记升级。"""
        graph, scheduler = _make_scheduler()
        test = graph.get_node("T")
        test.activation_count = 5
        graph.update_node("D", pending_routing=RoutingDecision(
            activate=["test"],
            contexts={"test": {"event_content": "rerun"}},
            reason="rerun",
        ))
        scheduler._handle_routing("D")
        assert scheduler.get_escalations() == [], "激活不再被预算拦截，无升级记录"
        assert graph.get_node("T").status == NodeStatus.WAITING


# ============================================================================
# C2 closed_loop 并入 escalations + converged 判定
# ============================================================================

class TestClosedLoopEscalations:
    def test_report_includes_escalations_and_converged(self):
        mgr = Node(
            id="mgr1", exec_type="manager", role="architecture",
            task="build", config={}, status=NodeStatus.COMPLETED,
            step_ref="plan_mgr",
        )
        children = [Node(
            id="c1", exec_type="worker", role="dev",
            task="t", config={}, status=NodeStatus.COMPLETED, step_ref="c1",
        )]
        es = [{
            "node_id": "D", "kind": "activation_budget_exceeded",
            "reason": "activation budget exhausted", "timestamp": "2026-01-01T00:00:00",
            "budget_exceeded": False,
        }]
        report = build_closed_loop_report(mgr, children, escalations=es)
        assert report["version"] == 2
        assert report["escalations"] == es
        assert report["verdict"]["overall_passed"] is True
        assert report["verdict"]["converged"] is False
        assert report["verdict"]["escalation_count"] == 1

    def test_report_converged_when_no_escalations(self):
        mgr = Node(
            id="mgr1", exec_type="manager", role="architecture",
            task="build", config={}, status=NodeStatus.COMPLETED, step_ref="plan_mgr",
        )
        children = [Node(
            id="c1", exec_type="worker", role="dev",
            task="t", config={}, status=NodeStatus.COMPLETED, step_ref="c1",
        )]
        report = build_closed_loop_report(mgr, children)
        assert report["verdict"]["overall_passed"] is True
        assert report["verdict"]["converged"] is True
        assert report["verdict"]["escalation_count"] == 0
        assert report["escalations"] == []
