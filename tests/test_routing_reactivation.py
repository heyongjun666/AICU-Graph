"""Unit tests for Scheduler._handle_routing — 路由重激活机制.

覆盖「验收/测试节点发现缺陷 → 路由回上游生产节点 → 原地重激活修复」链路
的核心调度逻辑（不启动后台线程，直接同步调用 _handle_routing）：

1. 激活路由目标节点：终态 COMPLETED → WAITING，注入 pending_context
2. 方案三：Router 目标可为任意 Node（含 Manager），无可达性边界
3. 防死循环：activation_count >= max_activations 时跳过
4. mark_terminal 标记节点为已完成
5. 上下文合并：多次激活的 pending_context 合并（不覆盖）
6. step_ref 解析：支持 "dev" 与 "dev_v1" 两种形式
7. 父 Manager 回退：激活后清空通知记录 + rewind_parent_chain_for_retry
8. pending_routing 处理后清除（幂等）
"""

from typing import Any, Optional

import pytest

from runtime.graph import Graph
from runtime.models import Node, NodeStatus, Edge, EdgeType, RoutingDecision
from runtime.scheduler import Scheduler
from runtime.planner import Planner
from executables.registry import ExecutableRegistry
from executables.base import ExecutableContext, SchedulerDelegate


# ============================================================================
# Mock objects
# ============================================================================

class MockDelegate:
    """Mock SchedulerDelegate 记录回调。"""

    def __init__(self):
        self.completed_managers: list[str] = []
        self.status_changes: list[tuple[str, NodeStatus]] = []

    def on_manager_children_completed(self, manager_node_id: str) -> None:
        self.completed_managers.append(manager_node_id)

    def on_node_status_changed(self, node_id: str, status: NodeStatus) -> None:
        self.status_changes.append((node_id, status))

    def get_active_execution_count(self) -> int:
        return 0


class MockRuntimeGateway:
    """RuntimeGateway 协议的最小实现（_handle_routing 不依赖它）。"""

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


# ============================================================================
# Helpers
# ============================================================================

def _make_node(node_id: str, step_ref: str = None, parent_id: Optional[str] = None,
               status: NodeStatus = NodeStatus.COMPLETED,
               activation_count: int = 0, max_activations: int = 3,
               exec_type: str = "worker", manager_phase: Optional[str] = None) -> Node:
    return Node(
        id=node_id,
        exec_type=exec_type,
        role="dev",
        task=f"Task {node_id}",
        config={},
        status=status,
        parent_id=parent_id,
        step_ref=step_ref or node_id,
        activation_count=activation_count,
        max_activations=max_activations,
        manager_phase=manager_phase,
    )


def _add_parent_edge(graph: Graph, parent: str, child: str, edge_id: str = None) -> None:
    eid = edge_id or f"p_{parent}_{child}"
    graph.add_edge(Edge(id=eid, type=EdgeType.PARENT, from_node=parent, to_node=child))


class _RoutingHarness:
    """构造 Manager + 两个 sibling（backend/test）+ 未启动的 Scheduler。"""

    def __init__(self, planner: Optional[Planner] = None,
                 backend_status: NodeStatus = NodeStatus.COMPLETED,
                 test_status: NodeStatus = NodeStatus.COMPLETED):
        self.graph = Graph()
        self.mgr = _make_node("M1", step_ref="mgr", exec_type="manager", status=NodeStatus.WAITING)
        self.graph.add_node(self.mgr)
        self.backend = _make_node("B1", step_ref="backend", parent_id="M1", status=backend_status)
        self.test = _make_node("T1", step_ref="test", parent_id="M1", status=test_status)
        self.graph.add_node(self.backend)
        self.graph.add_node(self.test)
        _add_parent_edge(self.graph, "M1", "B1")
        _add_parent_edge(self.graph, "M1", "T1")

        self.delegate = MockDelegate()
        self.scheduler = Scheduler(
            graph=self.graph,
            registry=ExecutableRegistry(),
            delegate=self.delegate,
            runtime_gateway=MockRuntimeGateway(),
            max_concurrency=4,
            planner=planner,
        )

    def _set_source_routing(self, source_id: str, routing: RoutingDecision) -> None:
        self.graph.update_node(source_id, pending_routing=routing)


# ============================================================================
# Tests
# ============================================================================

class TestHandleRoutingActivation:
    """核心：激活路由目标节点（终态 → WAITING + 上下文注入）。"""

    def setup_method(self):
        self.h = _RoutingHarness()

    def test_activates_related_node_in_place(self):
        """test 完成 → 激活 backend：COMPLETED→WAITING + pending_context 注入。"""
        self.h._set_source_routing("T1", RoutingDecision(
            activate=["backend"],
            contexts={"backend": {"issues": [{"severity": "high", "description": "multiply(2,3) 返回 5，应为 6"}]}},
            reason="联调测试发现 multiply 计算错误",
        ))

        self.h.scheduler._handle_routing("T1")

        backend = self.h.graph.get_node("B1")
        assert backend.status == NodeStatus.WAITING, "backend 应从 COMPLETED 回退到 WAITING"
        assert backend.activation_count == 1
        assert len(backend.routing_history) == 1
        assert backend.routing_history[0]["from"] == "T1"
        assert backend.routing_history[0]["step_id"] == "backend"
        assert backend.routing_history[0]["reason"].startswith("联调测试")
        # 上下文注入（含框架追加的激活元数据）
        ctx = backend.pending_context
        assert ctx["issues"][0]["severity"] == "high"
        assert ctx["activated_by"] == "T1"
        assert ctx["activation_reason"].startswith("联调测试")
        assert "activated_at" in ctx
        # 终态残留字段被清除，等待重新调度
        assert backend.artifact_path is None
        assert backend.error is None
        assert backend.started_at is None
        assert backend.finished_at is None

    def test_pending_routing_cleared_on_source(self):
        """处理后源节点 pending_routing 被清除（幂等）。"""
        self.h._set_source_routing("T1", RoutingDecision(
            activate=["backend"], contexts={"backend": {"k": "v"}}, reason="r"
        ))
        self.h.scheduler._handle_routing("T1")
        assert self.h.graph.get_node("T1").pending_routing is None

    def test_noop_without_pending_routing(self):
        """无 pending_routing → 不产生任何变更。"""
        self.h.graph.get_node("B1").update_node = None  # 不应被调用
        self.h.scheduler._handle_routing("T1")  # 不应抛异常
        assert self.h.graph.get_node("B1").status == NodeStatus.COMPLETED
        assert self.h.graph.get_node("B1").activation_count == 0

    def test_noop_when_activate_empty(self):
        """routing.activate 为空 → 直接返回，无变更。"""
        self.h._set_source_routing("T1", RoutingDecision(activate=[], reason="noop"))
        self.h.scheduler._handle_routing("T1")
        assert self.h.graph.get_node("B1").status == NodeStatus.COMPLETED
        assert self.h.graph.get_node("B1").activation_count == 0


class TestHandleRoutingBoundaries:
    """能力边界与防死循环。"""

    def setup_method(self):
        self.h = _RoutingHarness()

    def test_no_reachability_boundary(self, caplog):
        """方案三：旧通信可达性能力边界已删除。

        目标是否被激活只取决于「step_id 能否在组织图兄弟域内解析」——
        同一父作用域内的 sibling（含 Manager）都按激活处理，不存在
        旧通信白名单拦截。本例中 "backend"（兄弟）激活，"mgr"
        （父节点，不在兄弟域）因 step 解析不到被跳过。
        """
        self.h._set_source_routing("T1", RoutingDecision(
            activate=["backend", "mgr"],
            contexts={"mgr": {"event_content": "fix it"}},
            reason="activate sibling",
        ))
        with caplog.at_level("WARNING", logger="runtime.scheduler"):
            self.h.scheduler._handle_routing("T1")

        backend = self.h.graph.get_node("B1")
        assert backend.status == NodeStatus.WAITING, "sibling 目标被激活"
        assert backend.activation_count == 1
        assert any("not found in sibling nodes" in r.message for r in caplog.records)

    def test_unknown_step_id_skipped(self):
        """routing.activate 中不存在的 step_id 被跳过，不崩溃。"""
        self.h._set_source_routing("T1", RoutingDecision(
            activate=["nonexistent"], reason="should skip",
        ))
        self.h.scheduler._handle_routing("T1")
        assert self.h.graph.get_node("B1").status == NodeStatus.COMPLETED

    def test_max_activations_cap(self):
        """C2 已移除：activation_count 达旧 max_activations 仍可激活，不再拦截。"""
        self.h.graph.update_node("B1", activation_count=3, max_activations=3)
        self.h._set_source_routing("T1", RoutingDecision(
            activate=["backend"], contexts={"backend": {"k": "v"}}, reason="over cap",
        ))
        self.h.scheduler._handle_routing("T1")
        backend = self.h.graph.get_node("B1")
        assert backend.activation_count == 4, "无激活上限，计数继续累加"
        assert backend.status == NodeStatus.WAITING, "目标照常回退到 WAITING"

    def test_activation_count_accumulates(self):
        """连续两次激活 → activation_count 累加为 2，pending_context 合并。"""
        r1 = RoutingDecision(activate=["backend"], contexts={"backend": {"issues": ["bug1"]}}, reason="r1")
        r2 = RoutingDecision(activate=["backend"], contexts={"backend": {"issues": ["bug2"]}}, reason="r2")
        self.h._set_source_routing("T1", r1)
        self.h.scheduler._handle_routing("T1")
        # backend 回退后重新完成，再次被激活
        self.h.graph.update_node("B1", status=NodeStatus.COMPLETED)
        self.h._set_source_routing("T1", r2)
        self.h.scheduler._handle_routing("T1")

        backend = self.h.graph.get_node("B1")
        assert backend.activation_count == 2
        assert backend.pending_context["issues"] == ["bug2"], \
            "同一 key 后写覆盖；不同 key 应合并"


class TestHandleRoutingMarkTerminal:
    """mark_terminal：标记其他节点为已完成。"""

    def setup_method(self):
        self.h = _RoutingHarness()

    def test_mark_terminal_completes_target(self):
        """mark_terminal 中的 step 若未完成 → 置为 COMPLETED。"""
        # backend 以 FAILED 初始（将被激活回 WAITING），test 以 READY 初始（将被 mark_terminal 完成）
        self.h = _RoutingHarness(backend_status=NodeStatus.FAILED,
                                 test_status=NodeStatus.READY)
        self.h._set_source_routing("T1", RoutingDecision(
            activate=["backend"],
            mark_terminal=["test"],
            reason="backend 激活，test 标记终止",
        ))
        self.h.scheduler._handle_routing("T1")
        test = self.h.graph.get_node("T1")
        assert test.status == NodeStatus.COMPLETED, "mark_terminal 把 test 置为 COMPLETED"

    def test_mark_terminal_skips_already_completed(self):
        """mark_terminal 目标已 COMPLETED → 不重复变更。"""
        self.h._set_source_routing("T1", RoutingDecision(
            activate=["backend"], mark_terminal=["test"], reason="r",
        ))
        self.h.scheduler._handle_routing("T1")
        # test 原本就是 COMPLETED，不应抛异常、状态不变
        assert self.h.graph.get_node("T1").status == NodeStatus.COMPLETED


class TestStepRefResolution:
    """step_id → Node UUID 解析（含 _vN 后缀回退）。"""

    def test_resolves_plain_step_ref(self):
        """routing 用 step_id "backend" 直接解析。"""
        h = _RoutingHarness()
        h._set_source_routing("T1", RoutingDecision(activate=["backend"], reason="r"))
        h.scheduler._handle_routing("T1")
        assert h.graph.get_node("B1").status == NodeStatus.WAITING

    def test_resolves_v_suffix_step_ref(self):
        """节点 step_ref 为 "backend_v1"，routing 用 "backend" 仍能解析。"""
        h = _RoutingHarness()
        h.graph.update_node("B1", step_ref="backend_v1")
        h._set_source_routing("T1", RoutingDecision(activate=["backend"], reason="r"))
        h.scheduler._handle_routing("T1")
        assert h.graph.get_node("B1").status == NodeStatus.WAITING


class TestParentChainRewind:
    """激活后父 Manager 回退：清空通知 + 拉回 WAITING/waiting。"""

    def test_rewinds_manager_back_to_waiting(self):
        """父 Manager 已被推送到 review 阶段 → 激活后回退 WAITING + 通知清空。"""
        graph = Graph()
        mgr = _make_node("M1", step_ref="mgr", exec_type="manager",
                         status=NodeStatus.READY, manager_phase="review")
        graph.add_node(mgr)
        backend = _make_node("B1", step_ref="backend", parent_id="M1")
        test = _make_node("T1", step_ref="test", parent_id="M1")
        graph.add_node(backend)
        graph.add_node(test)
        _add_parent_edge(graph, "M1", "B1")
        _add_parent_edge(graph, "M1", "T1")

        planner = Planner(graph)
        delegate = MockDelegate()
        scheduler = Scheduler(
            graph=graph, registry=ExecutableRegistry(), delegate=delegate,
            runtime_gateway=MockRuntimeGateway(), max_concurrency=4, planner=planner,
        )
        # 模拟 Manager 已收到过 children completed 回调
        scheduler._notified_managers.add("M1")

        graph.update_node("T1", pending_routing=RoutingDecision(
            activate=["backend"], contexts={"backend": {"k": "v"}}, reason="r",
        ))
        scheduler._handle_routing("T1")

        assert "M1" not in scheduler._notified_managers, "通知记录被清除"
        rewound_mgr = graph.get_node("M1")
        assert rewound_mgr.status == NodeStatus.WAITING, "Manager 拉回 WAITING"
        assert rewound_mgr.manager_phase == "waiting", "Manager phase 回退到 waiting"
        assert graph.get_node("B1").status == NodeStatus.WAITING, "backend 被激活"


class TestNoDecisionObservability:
    """无路由决策时静默返回：不再记旧机制相关 INFO 日志（2.2 已删该分支）。"""

    def test_no_log_when_no_pending_routing(self, caplog):
        """节点完成但无 routing → 不记「without routing decision」日志。"""
        h = _RoutingHarness()
        with caplog.at_level("INFO", logger="runtime.scheduler"):
            h.scheduler._handle_routing("T1")
        assert not any(
            "without routing decision" in r.message for r in caplog.records
        )

    def test_no_log_when_routing_present(self, caplog):
        """有 routing 决策时不记「无决策」日志。"""
        h = _RoutingHarness()
        h._set_source_routing("T1", RoutingDecision(
            activate=["backend"], contexts={"backend": {"k": "v"}}, reason="r",
        ))
        with caplog.at_level("INFO", logger="runtime.scheduler"):
            h.scheduler._handle_routing("T1")
        assert not any(
            "without routing decision" in r.message for r in caplog.records
        )


class TestRoutingAccessCapability:
    """方案三 A3 调整：Manager 目标拦截与 required_role 校验已删除。

    Router 对目标节点的选择完全基于 Event + Producer + 当前 Organization Graph，
    Scheduler 不解析事件语义、不校验目标角色——只保留生命周期与预算安全边界。
    """

    def test_manager_sibling_target_allowed(self):
        """路由目标为 Manager（跨层）→ 允许激活（A3 拦截已删除）。"""
        graph = Graph()
        root = _make_node("R", step_ref="root", exec_type="manager",
                          status=NodeStatus.WAITING)
        submgr = _make_node("M1", step_ref="submgr", parent_id="R", exec_type="manager",
                            status=NodeStatus.COMPLETED)
        backend = _make_node("B1", step_ref="backend", parent_id="R", status=NodeStatus.COMPLETED)
        graph.add_node(root)
        graph.add_node(submgr)
        graph.add_node(backend)
        _add_parent_edge(graph, "R", "M1")
        _add_parent_edge(graph, "R", "B1")
        delegate = MockDelegate()
        scheduler = Scheduler(
            graph=graph, registry=ExecutableRegistry(), delegate=delegate,
            runtime_gateway=MockRuntimeGateway(), max_concurrency=4, planner=None,
        )
        graph.update_node("B1", pending_routing=RoutingDecision(
            activate=["submgr"], contexts={"submgr": {"event_content": "fix it"}}, reason="r",
        ))
        scheduler._handle_routing("B1")
        mgr = graph.get_node("M1")
        assert mgr.status == NodeStatus.WAITING, "Manager 目标允许激活"
        assert mgr.activation_count == 1

    def test_required_role_in_context_not_enforced(self):
        """required_role 不再被 Scheduler 校验（角色判断由 Router LLM 负责）。"""
        h = _RoutingHarness()
        h._set_source_routing("T1", RoutingDecision(
            activate=["backend"],
            contexts={"backend": {"required_role": "test", "event_content": "fix it"}},
            reason="role unchecked",
        ))
        h.scheduler._handle_routing("T1")
        backend = h.graph.get_node("B1")
        assert backend.status == NodeStatus.WAITING, "required_role 不拦截激活"
        assert backend.activation_count == 1
        # required_role 作为普通上下文字段透传，不参与任何 gating
        assert backend.pending_context["required_role"] == "test"

    def test_activation_with_empty_feedback_warns_but_activates(self, caplog):
        """激活但空反馈上下文 → 记 WARNING（行为不变，仍激活）。"""
        h = _RoutingHarness()
        with caplog.at_level("WARNING", logger="runtime.scheduler"):
            h._set_source_routing("T1", RoutingDecision(
                activate=["backend"],
                contexts={"backend": {}},
                reason="activate with no context",
            ))
            h.scheduler._handle_routing("T1")
        backend = h.graph.get_node("B1")
        assert backend.status == NodeStatus.WAITING, "行为不变，仍激活"
        assert backend.activation_count == 1
        assert any("empty feedback context" in r.message for r in caplog.records)
