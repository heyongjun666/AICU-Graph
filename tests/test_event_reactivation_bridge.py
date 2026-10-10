"""tests/test_event_reactivation_bridge.py — Scheduler「事件→重激活桥」测试。

覆盖 `Scheduler._process_reactivation_events`：非运行态节点收到
事件 → 桥把它解释为「请求该节点行动/重激活」，复用激活引擎
`_activate_target` 的全部安全边界，形成反馈/协作闭环。

事件→状态机（新契约，Event 是独立于依赖终态的可执行信号）：
- WAITING   + Event → READY（注入增量上下文，绕过依赖门控）
- COMPLETED + Event → READY（进入 Revise，不重新生成 Main Plan）
- RUNNING   + Event → RUNNING（由运行中 Agent 自行消费，Scheduler 不重启）
- FAILED    + Event → FAILED（失败节点不自动复活，须人工 continue）

覆盖:
1. COMPLETED 节点收到事件 → 转 READY（activation_count+1 /
   pending_context 含 event_content/event_producer / routing_history 记录 from=producer）
2. WAITING 节点收到事件 → 转 READY（不等待依赖全部完成）
3. FAILED 节点收到事件 → 保持 FAILED（不自动复活，事件不消费）
4. 桥跳过 RUNNING / READY / PAUSED / STOPPED
5. 事件消费：转换后 inbox 事件文件被删除（delete-on-consume）
6. 预算：max_activations 已移除，事件重激活不拦截、不记升级
7. content 中的 mark_terminal 等字样不被解析为指令（纯文本注入）
8. producer 无法解析为节点 → 仍重激活（prevalidated 跳过 related 复查）
"""

import json
import os
import time

import pytest

from runtime.graph import Graph
from runtime.models import Edge, EdgeType, Node, NodeStatus
from runtime.scheduler import Scheduler
from executables.base import Executable, ExecutableContext
from executables.manager import Manager
from executables.registry import ExecutableRegistry


# ============================================================================
# Mocks
# ============================================================================

class MockDelegate:
    def __init__(self):
        self.completed_managers: list[str] = []
        self.status_changes: list[tuple[str, NodeStatus]] = []

    def on_manager_children_completed(self, manager_node_id: str) -> None:
        self.completed_managers.append(manager_node_id)

    def on_node_status_changed(self, node_id: str, status: NodeStatus) -> None:
        self.status_changes.append((node_id, status))

    def get_active_execution_count(self) -> int:
        return 0


class MockGateway:
    def submit_plan(self, node_id: str, plan) -> None:
        pass

    def update_node_status(self, node_id: str, status: NodeStatus, **updates) -> None:
        pass

    def get_node(self, node_id: str):
        return None

    def get_children(self, node_id: str):
        return []

    def read_artifact(self, node_id: str, path: str) -> str:
        return ""

    def register_external_input_wait(self, node_id: str, input_schema=None) -> str:
        return "token"

    def deliver_external_input(self, wait_token: str, input_data) -> None:
        pass


# ============================================================================
# Helpers
# ============================================================================

def _make_node(node_id: str, workspace_root: str, step_ref: str = None,
               parent_id: str = None, status: NodeStatus = NodeStatus.COMPLETED,
               activation_count: int = 0, max_activations: int = 3,
               exec_type: str = "worker", role: str = "dev",
               config_extra: dict = None) -> Node:
    cfg = {
        "_workspace_root": workspace_root,
        "_workspace_relpath": os.path.join("nodes", node_id),
        "events": {"subscribe": ["*"]},
    }
    if config_extra:
        cfg.update(config_extra)
    return Node(
        id=node_id,
        exec_type=exec_type,
        role=role,
        task=f"Task {node_id}",
        config=cfg,
        status=status,
        parent_id=parent_id,
        step_ref=step_ref or node_id,
        activation_count=activation_count,
        max_activations=max_activations,
    )


def _node_ws(root: str, node_id: str) -> str:
    """与 runtime.agent_events._node_workspace 同源的节点私有工作区。"""
    return os.path.realpath(os.path.join(root, "nodes", node_id))


def _write_event(root: str, node_id: str, event: dict) -> str:
    """把事件写入目标节点 inbox（模拟事件管理器投递）。"""
    ws = _node_ws(root, node_id)
    inbox = os.path.join(ws, "event_inbox")
    os.makedirs(inbox, exist_ok=True)
    path = os.path.join(inbox, f"evt_{event['event_id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(event, f, ensure_ascii=False)
    return path


class _BridgeHarness:
    """Graph: manager M1 → 两个 sibling（backend B1 / test T1）。"""

    def __init__(self, tmp_path, backend_status=NodeStatus.COMPLETED,
                 test_status=NodeStatus.COMPLETED):
        root = str(tmp_path)
        self.root = root
        self.graph = Graph()
        self.mgr = _make_node("M1", root, step_ref="mgr", exec_type="manager",
                              role="engineering_coord", status=NodeStatus.WAITING)
        self.backend = _make_node("B1", root, step_ref="backend", parent_id="M1",
                                  status=backend_status)
        self.test = _make_node("T1", root, step_ref="test", parent_id="M1",
                               status=test_status)
        for n in (self.mgr, self.backend, self.test):
            self.graph.add_node(n)
        for parent, child, eid in (("M1", "B1", "e1"), ("M1", "T1", "e2")):
            self.graph.add_edge(Edge(id=eid, type=EdgeType.PARENT,
                                     from_node=parent, to_node=child))
        self.delegate = MockDelegate()
        self.scheduler = Scheduler(
            graph=self.graph,
            registry=ExecutableRegistry(),
            delegate=self.delegate,
            runtime_gateway=MockGateway(),
            max_concurrency=4,
            # 单事件即触发：桥逻辑测试不关心合并阈值（合并行为由 TestBridgeBatching 单独覆盖）。
            event_batch_threshold=1,
        )

    def run_bridge(self) -> None:
        self.scheduler._process_reactivation_events()


def _feedback_event(producer: str, content: str = "") -> dict:
    """方案三事件：只含 {event_id, producer, content}（无 type/target/payload）。"""
    content = content or (
        "联调测试发现计算错误：multiply(2,3)=5, 应为 6。"
        "issues=[{'severity': 'high', 'description': 'multiply(2,3)=5, 应为 6'}]"
    )
    return {
        "event_id": "evt_abc",
        "producer": producer,
        "content": content,
    }


# ============================================================================
# Tests
# ============================================================================

def test_default_batch_waits_for_three_events(tmp_path):
    """The default policy preserves cost-saving three-event aggregation."""
    graph = Graph()
    target = _make_node("W", str(tmp_path))
    graph.add_node(target)
    # 图上还有运行中的节点（非空闲），才需要等凑批
    graph.add_node(_make_node("busy", str(tmp_path), status=NodeStatus.RUNNING))
    scheduler = Scheduler(graph, ExecutableRegistry(), MockDelegate(), MockGateway())
    try:
        for index in range(2):
            _write_event(
                str(tmp_path),
                "W",
                {**_feedback_event(f"P{index}"), "event_id": f"evt_{index}"},
            )
        scheduler._process_reactivation_events()
        assert target.status == NodeStatus.COMPLETED
        assert target.activation_count == 0
        assert len(scheduler._peek_inbox_events(target)) == 2

        _write_event(
            str(tmp_path),
            "W",
            {**_feedback_event("P2"), "event_id": "evt_2"},
        )
        scheduler._process_reactivation_events()
        assert target.status == NodeStatus.READY
        assert target.activation_count == 1
        assert not scheduler._peek_inbox_events(target)
    finally:
        scheduler.stop()


def test_batch_age_backstop_flushes_before_threshold(tmp_path):
    """A configured age backstop may flush a smaller batch than the threshold."""
    graph = Graph()
    target = _make_node("W", str(tmp_path))
    graph.add_node(target)
    scheduler = Scheduler(
        graph,
        ExecutableRegistry(),
        MockDelegate(),
        MockGateway(),
        event_batch_threshold=5,
        event_batch_max_age=0.01,
    )
    try:
        path = _write_event(str(tmp_path), "W", _feedback_event("P", "age flush"))
        old = time.time() - 1
        os.utime(path, (old, old))
        scheduler._process_reactivation_events()
        assert target.status == NodeStatus.READY
        assert target.activation_count == 1
        assert not os.path.exists(path)
    finally:
        scheduler.stop()


def test_paused_terminal_inbox_is_not_consumed(tmp_path):
    graph = Graph()
    node = _make_node("W", str(tmp_path))
    graph.add_node(node)
    path = _write_event(str(tmp_path), "W", _feedback_event("P"))
    scheduler = Scheduler(graph, ExecutableRegistry(), MockDelegate(), MockGateway())
    try:
        scheduler.pause()
        scheduler.start()
        scheduler._tick_thread.join(timeout=2)
        assert not scheduler.is_running
        assert node.status == NodeStatus.COMPLETED
        assert node.activation_count == 0
        assert os.path.exists(path)
    finally:
        scheduler.stop()


class TestBridgeReactivation:
    def test_terminal_node_target_event_reactivates(self, tmp_path):
        """终态 backend 收到 test 发的事件 → 转 READY（进入 Revise）+ 事件上下文注入。"""
        h = _BridgeHarness(tmp_path)
        h.graph.update_node("B1", artifact_path="deliverables/result.json")
        _write_event(h.root, "B1", _feedback_event("T1"))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY
        assert b.artifact_path == "deliverables/result.json"
        assert b.activation_count == 1
        assert "multiply(2,3)=5" in b.pending_context["event_content"]
        assert b.pending_context["event_producer"] == "T1"
        assert b.pending_context["activated_by"] == "T1"
        assert b.pending_context["activation_reason"].startswith("event evt_abc from T1")
        assert len(b.routing_history) == 1
        assert b.routing_history[0]["from"] == "T1"
        assert b.routing_history[0]["to"] == "B1"
        # Reactivation's intermediate COMPLETED → WAITING transition is silent;
        # only the event bridge's final WAITING → READY transition is published.
        assert h.delegate.status_changes == [("B1", NodeStatus.READY)]

    def test_reactivate_notification_is_explicit_for_real_transitions(self, tmp_path):
        h = _BridgeHarness(tmp_path)
        h.delegate.status_changes.clear()

        assert h.scheduler.reactivate(
            "B1", target_status=NodeStatus.WAITING, emit_notification=False
        )
        assert h.delegate.status_changes == []

        assert h.scheduler.reactivate(
            "B1", target_status=NodeStatus.READY, emit_notification=True
        )
        assert h.delegate.status_changes == [("B1", NodeStatus.READY)]

    def test_inbox_file_deleted_after_consume(self, tmp_path):
        """转换后被拦截/成功均删除 inbox 事件文件（delete-on-consume）。"""
        h = _BridgeHarness(tmp_path)
        path = _write_event(h.root, "B1", _feedback_event("T1"))
        assert os.path.exists(path)
        h.run_bridge()
        assert not os.path.exists(path), "转换后 inbox 事件文件必须删除"

    def test_producer_unresolvable_still_reactivates(self, tmp_path):
        """producer 不在图中（如已删除的节点）→ 仍重激活（prevalidated 兜底）。"""
        h = _BridgeHarness(tmp_path)
        _write_event(h.root, "B1", _feedback_event("ghost-producer"))
        h.run_bridge()
        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY
        assert b.activation_count == 1
        assert len(b.routing_history) == 1

    def test_inbox_event_processed_regardless_of_content(self, tmp_path):
        """方案三：批次中的 inbox 事件不解析 content 中的目标声明/指令。

        桥只消费 inbox 文件存在性，content 只是事件本身的描述——不按 target 跳过，
        也不把 content 中的"目标节点名"当路由指令解析。
        """
        h = _BridgeHarness(tmp_path)
        # 事件内容里提到其它节点名（如 "test"），但不构成路由指令 → 仍激活本节点
        _write_event(h.root, "B1", _feedback_event(
            "T1", "请 test 节点复验；本事件仍由桥投递给 backend 消费。"
        ))
        h.run_bridge()
        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY
        assert b.activation_count == 1


class TestBridgeStatusFilter:
    def test_skips_running(self, tmp_path):
        h = _BridgeHarness(tmp_path, backend_status=NodeStatus.RUNNING)
        _write_event(h.root, "B1", _feedback_event("T1", "backend"))
        h.run_bridge()
        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.RUNNING, "RUNNING 节点跳过（自行消费 inbox）"
        assert b.activation_count == 0

    def test_skips_ready(self, tmp_path):
        h = _BridgeHarness(tmp_path, backend_status=NodeStatus.READY)
        _write_event(h.root, "B1", _feedback_event("T1", "backend"))
        h.run_bridge()
        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY, "READY 节点跳过（避免双调度竞态）"
        assert b.activation_count == 0

    def test_skips_paused(self, tmp_path):
        h = _BridgeHarness(tmp_path, backend_status=NodeStatus.PAUSED)
        _write_event(h.root, "B1", _feedback_event("T1", "backend"))
        h.run_bridge()
        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.PAUSED, "PAUSED 节点跳过（尊重用户暂停）"
        assert b.activation_count == 0

    def test_skips_stopped(self, tmp_path):
        h = _BridgeHarness(tmp_path, backend_status=NodeStatus.STOPPED)
        _write_event(h.root, "B1", _feedback_event("T1", "backend"))
        h.run_bridge()
        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.STOPPED, "STOPPED 节点跳过"
        assert b.activation_count == 0


class TestBridgeBudgets:
    def test_max_activations_removed_no_blocking(self, tmp_path):
        """C2 已移除：事件重激活不再受 max_activations 拦截，也不记升级。"""
        h = _BridgeHarness(tmp_path, backend_status=NodeStatus.COMPLETED)
        h.graph.update_node("B1", activation_count=3, max_activations=3)
        _write_event(h.root, "B1", _feedback_event("T1"))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY, "无激活上限，事件应重激活目标节点"
        assert b.activation_count == 4, "激活计数应递增到 4"
        escalations = h.scheduler.get_escalations()
        assert not any(e["kind"] == "activation_budget_exceeded" for e in escalations)


class TestBridgeMarkTerminal:
    def test_content_mark_terminal_not_parsed_as_command(self, tmp_path):
        """方案三红线：content 只是事件描述，不被桥解析为 mark_terminal 等指令。

        即使事件内容中出现 "mark_terminal" 字样，Scheduler 也只把它当纯文本注入
        pending_context，不据此修改兄弟节点状态。
        """
        h = _BridgeHarness(tmp_path, test_status=NodeStatus.RUNNING)
        _write_event(h.root, "B1", _feedback_event(
            "T1", "请把 test 标记为终态（mark_terminal=test）；这是纯文本描述，不是指令。"
        ))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY, "事件仍激活目标节点"
        assert "mark_terminal=test" in b.pending_context["event_content"]
        t = h.graph.get_node("T1")
        assert t.status == NodeStatus.RUNNING, "content 中的 mark_terminal 文本不生效"


class TestBridgeCollaboration:
    def test_waiting_node_event_content_injected(self, tmp_path):
        """WAITING 节点收到事件 → 转 READY 并注入事件上下文（event_content/event_producer）。"""
        h = _BridgeHarness(tmp_path, backend_status=NodeStatus.WAITING)
        evt = {
            "event_id": "evt_collab",
            "producer": "M1",
            "content": "需求已评审通过：请按 requirements.md 的结论推进，期望 approve plan。",
        }
        _write_event(h.root, "B1", evt)
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY, "WAITING 节点收到事件应转 READY"
        assert b.activation_count == 1
        assert "需求已评审通过" in b.pending_context["event_content"]
        assert b.pending_context["event_producer"] == "M1"

    def test_event_content_carries_collab_intent(self, tmp_path):
        """协作意图由 Event.content 承载，Scheduler 不解析 request/respond。"""
        h = _BridgeHarness(tmp_path, backend_status=NodeStatus.WAITING)
        evt = {
            "event_id": "evt_resp",
            "producer": "T1",
            "content": "验收未通过：requirements not met，请调整后再提交。",
        }
        _write_event(h.root, "B1", evt)
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert "requirements not met" in b.pending_context["event_content"]
        assert b.pending_context["event_producer"] == "T1"
        # 无 collaboration_type/response 等结构化字段
        assert "collaboration_type" not in b.pending_context
        assert "response" not in b.pending_context


class TestBridgeStateMachine:
    """事件→状态机（新契约）：Event 是可执行信号，但仍遵守依赖门控。

    - WAITING + Event → 依赖未满足时保持 WAITING（等门控），满足时 READY
    - COMPLETED + Event → READY（进入 Revise，由 TestBridgeReactivation 覆盖）
    - RUNNING + Event → RUNNING（Scheduler 不重启、不消费 inbox）
    - FAILED + Event → FAILED（不自动复活、不消费 inbox）
    """

    def test_waiting_event_respects_dependency_gating(self, tmp_path):
        """WAITING 节点仍有 RUNNING 依赖时收到事件 → 保持 WAITING，不提前执行。"""
        h = _BridgeHarness(tmp_path, backend_status=NodeStatus.WAITING)
        # 给 B1 加一个仍在 RUNNING 的依赖 D1（B1 depends_on D1）
        dep = _make_node("D1", h.root, step_ref="dep", parent_id="M1",
                         status=NodeStatus.RUNNING)
        h.graph.add_node(dep)
        h.graph.add_edge(Edge(id="dep1", type=EdgeType.DEPENDENCY,
                              from_node="D1", to_node="B1"))

        # 依赖门控本应阻塞 B1：D1 未终态 → all_terminal == False
        deps = h.graph.dependencies("B1")
        assert len(deps) == 1 and deps[0].id == "D1"
        terminal_statuses = {NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.STOPPED}
        assert not all(d.status in terminal_statuses for d in deps)

        _write_event(h.root, "B1", _feedback_event("T1"))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.WAITING, "事件不得绕过依赖门控：依赖未满足时保持 WAITING"
        assert b.activation_count == 1  # 仍被激活（注入增量上下文），但不提前转 READY

    def test_waiting_dependency_batches_are_preserved_until_execution(self, tmp_path):
        """依赖等待期间到达的多批事件合并后一次交给下游输入。"""
        h = _BridgeHarness(tmp_path, backend_status=NodeStatus.WAITING)
        dep = _make_node(
            "D1", h.root, step_ref="dep", parent_id="M1", status=NodeStatus.RUNNING
        )
        h.graph.add_node(dep)
        h.graph.add_edge(Edge(
            id="dep-batch", type=EdgeType.DEPENDENCY,
            from_node="D1", to_node="B1",
        ))

        _write_event(h.root, "B1", {
            "event_id": "batch-a", "producer": "T1", "objective": "first batch",
        })
        h.run_bridge()
        assert h.graph.get_node("B1").status == NodeStatus.WAITING

        _write_event(h.root, "B1", {
            "event_id": "batch-b", "producer": "T1", "objective": "second batch",
        })
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.WAITING
        assert [event["event_id"] for event in b.pending_context["event_batch"]] == [
            "batch-a", "batch-b",
        ]
        assert b.pending_context["event_content"] == "first batch"
        # Manager 的真实事件上下文构造器应能看到合并后的完整输入。
        ctx = ExecutableContext(node=b, runtime=MockGateway(), config={})
        event_block = Manager._build_event_context_block(ctx)
        assert "first batch" in event_block
        assert "second batch" in event_block

        # 模拟下游已构造并消费本轮输入；后续批次不得重放旧事件。
        dep.status = NodeStatus.COMPLETED
        h.scheduler._process_waiting_nodes()
        b.pending_context = None
        b.status = NodeStatus.COMPLETED
        _write_event(h.root, "B1", {
            "event_id": "batch-c", "producer": "T1", "objective": "third batch",
        })
        h.run_bridge()
        assert [event["event_id"] for event in b.pending_context["event_batch"]] == [
            "batch-c",
        ]

    def test_failed_event_not_reactivated(self, tmp_path):
        """FAILED 节点收到事件 → 保持 FAILED，不自动复活，inbox 事件不消费。"""
        h = _BridgeHarness(tmp_path, backend_status=NodeStatus.FAILED)
        path = _write_event(h.root, "B1", _feedback_event("T1"))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.FAILED, "FAILED 节点不自动复活"
        assert b.activation_count == 0, "FAILED 节点不被事件桥激活"
        assert os.path.exists(path), "FAILED 节点的事件不消费（留待人工 continue）"

    def test_running_event_not_restarted(self, tmp_path):
        """RUNNING 节点收到事件 → 保持 RUNNING，Scheduler 不重启、不消费 inbox。"""
        h = _BridgeHarness(tmp_path, backend_status=NodeStatus.RUNNING)
        path = _write_event(h.root, "B1", _feedback_event("T1"))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.RUNNING, "RUNNING 节点自行消费 inbox，Scheduler 不重启"
        assert b.activation_count == 0
        assert os.path.exists(path), "RUNNING 节点的事件由 Agent 轮询消费，Scheduler 不删除"


@pytest.mark.parametrize("revise", [False, True])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_manager_run_receives_all_five_routed_events(tmp_path, revise, language):
    """Batch contents reach the AI on both initial Expand and incremental Revise."""
    graph = Graph()
    manager = _make_node("M", str(tmp_path), exec_type="manager")
    graph.add_node(manager)
    if revise:
        child = _make_node("C", str(tmp_path), parent_id="M")
        graph.add_node(child)
        graph.add_edge(Edge(id="parent", type=EdgeType.PARENT,
                            from_node="M", to_node="C"))

    class Gateway(MockGateway):
        def get_children(self, node_id):
            return graph.children(node_id)

        def get_node(self, node_id):
            return graph.get_node(node_id)

        def update_node_status(self, node_id, status, **updates):
            graph.update_node(node_id, status=status, **updates)

    class CapturingAI:
        # 非空计划：空 steps 在 Revise 语义下是「Manager 判定本轮无需行动」的显式
        # 判定（决策 0 ①，见 tests/test_root_revise.py::TestNoActionVerdict），
        # 本用例只关心事件是否送达 prompt，故给一份最小的真实计划走提交路径。
        _PLAN = {"steps": [{
            "step_id": "s1", "exec_type": "worker", "role": "dev",
            "task": "do the work", "task_summary": "do the work",
        }]}

        def __init__(self):
            self.calls = []

        def ask(self, prompt):
            self.calls.append((False, prompt))
            return dict(self._PLAN)

        def ask_incremental(self, prompt):
            self.calls.append((True, prompt))
            return dict(self._PLAN)

    events = [
        {"event_id": f"batch-{i}", "producer": f"producer-{i}",
         "objective": f"unique-objective-{i}", "intent": f"intent-{i}",
         "scope": "current", "action": f"action-{i}",
         "affected_roles": [{"role": f"role-{i}", "duty": f"duty-{i}"}]}
        for i in range(5)
    ]
    paths = [_write_event(str(tmp_path), "M", event) for event in events]
    gateway = Gateway()
    scheduler = Scheduler(graph, ExecutableRegistry(), MockDelegate(), gateway)
    try:
        scheduler._process_reactivation_events()
        assert manager.status == NodeStatus.READY
        assert manager.activation_count == 1
        assert manager.pending_revise == revise
        assert all(not os.path.exists(path) for path in paths)
        ai = CapturingAI()
        ctx = ExecutableContext(node=manager, runtime=gateway,
                                config={"language": language, "mode": "stub"})
        assert Manager(aicu_instance=ai).run(ctx) == NodeStatus.WAITING
        assert len(ai.calls) == 1
        incremental, prompt = ai.calls[0]
        # Revise 也是新会话下的完整首轮请求，不再走 ask_incremental。
        assert incremental is False
        for event in events:
            for key in ("event_id", "producer", "objective", "intent", "action"):
                assert event[key] in prompt
            assert prompt.count(event["objective"]) == 1
            assert event["affected_roles"][0]["role"] in prompt
            assert event["affected_roles"][0]["duty"] in prompt
    finally:
        scheduler.stop()


@pytest.mark.parametrize("snapshot_failure", [False, True])
def test_manager_expand_preserves_events_without_snapshot(tmp_path, monkeypatch, snapshot_failure):
    node = _make_node("M", str(tmp_path), exec_type="manager")
    node.pending_context = {"event_batch": [
        {"event_id": "evt_fallback", "objective": "must reach the manager"},
    ]}

    def snapshot(self, ctx):
        if snapshot_failure:
            raise RuntimeError("snapshot unavailable")
        return ""

    monkeypatch.setattr(Manager, "_build_execution_context_snapshot", snapshot)
    prompts = []

    class CapturingAI:
        def ask(self, prompt):
            prompts.append(prompt)
            return {"steps": []}

    ctx = ExecutableContext(node=node, runtime=MockGateway(), config={})
    assert Manager(aicu_instance=CapturingAI()).run(ctx) == NodeStatus.WAITING
    assert "must reach the manager" in prompts[0]
    assert node.task in prompts[0]


class TestBridgeManagerRevise:
    """COMPLETED 且已有子节点的 Manager 收到事件 → 进入 Revise（增量续跑）。

    状态机：COMPLETED + Event → READY（进入 Revise，不重新生成 Main Plan）。
    Manager 已有子节点时，重激活不能落到 manager_phase="review"（子节点可能尚未
    全部终态，且 review 会跳过「复用既有子节点」的 Revise 语义），也不能落回
    manager_phase=None 走全量 expand 重新生成 Main Plan。
    """

    def test_completed_manager_with_children_enters_revise(self, tmp_path):
        """COMPLETED 的父 Manager 收到事件 → READY + manager_phase="expand" + pending_revise。"""
        h = _BridgeHarness(tmp_path)
        # M1 已是 COMPLETED 且带子节点 backend/test（review 通过后 manager_phase=None）
        h.graph.update_node("M1", status=NodeStatus.COMPLETED, manager_phase=None)
        _write_event(h.root, "M1", {
            "event_id": "evt_mgr_revise",
            "producer": "T1",
            "content": "请审查需求清单并回发确认结论。",
        })
        h.run_bridge()

        m = h.graph.get_node("M1")
        assert m.status == NodeStatus.READY
        assert m.manager_phase == "expand", (
            "已有子节点的 Manager 收到事件应进入 expand（Revise），而非 review"
        )
        assert m.pending_revise is True, "Revise 增量续跑应置 pending_revise=True"
        assert "请审查需求清单" in m.pending_context["event_content"]
        assert m.pending_context["event_producer"] == "T1"


class TestBridgeScopeFilter:
    """Scheduler preserves the original deterministic current/subtree scope guard."""

    def _evt(self, producer, scope, event_id="evt_s"):
        return {
            "event_id": event_id,
            "producer": producer,
            "content": "越界/相关事件内容",
            "scope": scope,
        }

    @pytest.mark.parametrize("scope", ["current", "subtree"])
    def test_cross_scope_router_target_is_declined(self, tmp_path, scope):
        """A deterministic out-of-scope target is consumed without reactivation."""
        from runtime.models import EventMessage
        from runtime.router import AICURouter

        class RouterAI:
            def ask(self, prompt):
                return {"targets": ["B1"], "reason": "Sibling must respond"}

        h = _BridgeHarness(tmp_path)
        router = AICURouter(aicu_instance=RouterAI(), workspace_root=h.root)
        router.attach_graph(h.graph)
        event = self._evt("T1", scope)
        targets = router.route(EventMessage.from_dict(event))
        assert targets == ["B1"]
        paths = [_write_event(h.root, target, event) for target in targets]
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.COMPLETED
        assert b.activation_count == 0
        assert all(not os.path.exists(path) for path in paths)

    def test_current_scope_self_allowed(self, tmp_path):
        """scope=current 且节点==producer → 放行重激活。"""
        h = _BridgeHarness(tmp_path)
        _write_event(h.root, "B1", self._evt("B1", "current"))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY
        assert b.activation_count == 1

    def test_subtree_scope_inside_allowed(self, tmp_path):
        """scope=subtree 且节点在 producer（父 M1）子树内 → 放行重激活。"""
        h = _BridgeHarness(tmp_path)
        _write_event(h.root, "B1", self._evt("M1", "subtree"))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY, "B1 是 M1 子节点，应在子树内放行"
        assert b.activation_count == 1

    def test_related_scope_allowed(self, tmp_path):
        h = _BridgeHarness(tmp_path)
        _write_event(h.root, "B1", self._evt("T1", "related"))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY, "related 无法确定性判定，放行"
        assert b.activation_count == 1

    def test_global_scope_allowed(self, tmp_path):
        h = _BridgeHarness(tmp_path)
        _write_event(h.root, "B1", self._evt("T1", "global"))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY
        assert b.activation_count == 1

    def test_missing_scope_allowed(self, tmp_path):
        """无 scope 字段 → 放行（默认不确定，交给节点 CC 理解）。"""
        h = _BridgeHarness(tmp_path)
        _write_event(h.root, "B1", {
            "event_id": "evt_noscope",
            "producer": "T1",
            "content": "无 scope 字段的事件",
        })
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY


class TestBridgeBatching:
    """改动 3：event 合并触发（阈值 + 兜底 + 全量 drain）。"""

    def test_manager_renders_every_event_in_canonical_batch(self, tmp_path):
        node = _make_node("M", str(tmp_path), exec_type="manager")
        node.pending_context = {
            "event_batch": [
                {"event_id": f"evt_{i}", "producer": "P",
                 "objective": f"objective-{i}", "intent": "fix"}
                for i in range(5)
            ],
            # Legacy scalar fields remain available to older consumers.
            "event_content": "objective-0",
            "event_producer": "P",
        }
        ctx = ExecutableContext(node=node, runtime=MockGateway(), config={})
        block = Manager._build_event_context_block(ctx)
        for i in range(5):
            assert f"evt_{i}" in block
            assert f"objective-{i}" in block


    def _harness(self, tmp_path, threshold=3, max_age=1800.0):
        h = _BridgeHarness(tmp_path)
        h.scheduler.event_batch_threshold = threshold
        h.scheduler.event_batch_max_age = max_age
        return h

    def _evt(self, i):
        return {"event_id": f"evt_{i}", "producer": "T1", "content": f"msg{i}"}

    def _inbox_dir(self, h, node_id="B1"):
        return os.path.join(_node_ws(h.root, node_id), "event_inbox")

    def test_idle_graph_flushes_below_threshold(self, tmp_path):
        """图已空闲 → 不会再有新事件，未满阈值也直接放行。"""
        h = self._harness(tmp_path)
        h.graph.update_node("M1", status=NodeStatus.COMPLETED)
        for i in range(2):
            _write_event(h.root, "B1", self._evt(i))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY
        assert b.activation_count == 1
        assert not os.listdir(self._inbox_dir(h))

    def test_other_waiting_node_blocks_idle_flush(self, tmp_path):
        """还有别的 Waiting 节点（可能马上被放行产生新事件）→ 不算空闲，继续凑批。"""
        h = self._harness(tmp_path)
        h.graph.add_node(_make_node("downstream", str(tmp_path), status=NodeStatus.WAITING))
        for i in range(2):
            _write_event(h.root, "B1", self._evt(i))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.COMPLETED
        assert len(os.listdir(self._inbox_dir(h))) == 2

    def test_below_threshold_not_triggered(self, tmp_path):
        """< 阈值且未到兜底时限（图上仍有运行中节点）→ 不触发，事件留在 inbox、节点不重激活。"""
        h = self._harness(tmp_path)
        h.graph.add_node(_make_node("busy", str(tmp_path), status=NodeStatus.RUNNING))
        for i in range(2):
            _write_event(h.root, "B1", self._evt(i))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.COMPLETED, "未达阈值不重激活"
        assert b.activation_count == 0
        inbox = self._inbox_dir(h)
        assert len(os.listdir(inbox)) == 2, "事件未消费，留待下一 tick"

    def test_threshold_triggers_full_drain(self, tmp_path):
        """满 3 条 → 触发一次，全量 drain（不只是 3 条），合并为一次重激活。"""
        h = self._harness(tmp_path)
        for i in range(7):
            _write_event(h.root, "B1", self._evt(i))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY
        assert b.activation_count == 1, "N 条事件合并为一次重激活"
        batch = b.pending_context["event_batch"]
        assert len(batch) == 7, "全量 drain，不只阈值 3 条"
        assert b.pending_context["event_content"] == "msg0", "单值字段取首条（最旧）"
        assert b.pending_context["event_producer"] == "T1"
        assert len(os.listdir(self._inbox_dir(h))) == 0, "inbox 清空"

    def test_backstop_triggers_below_threshold(self, tmp_path):
        """事件数 < 阈值但最早事件滞留超兜底时限 → 兜底触发。"""
        h = self._harness(tmp_path)
        path = _write_event(h.root, "B1", self._evt(0))
        # 把 mtime 拨到 2 小时前，模拟长期滞留
        old = time.time() - 7200
        os.utime(path, (old, old))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY, "超时兜底应触发重激活"
        assert b.activation_count == 1
        assert len(os.listdir(self._inbox_dir(h))) == 0

    def test_batch_preserves_event_batch_structure(self, tmp_path):
        """合并后的 pending_context 含结构化 event_batch（objective/scope 等）。"""
        h = self._harness(tmp_path, threshold=2)
        _write_event(h.root, "B1", {
            "event_id": "e1", "producer": "T1", "objective": "修接口",
            "intent": "fix", "scope": "related", "action": "revise",
            "affected_roles": ["backend"],
        })
        _write_event(h.root, "B1", {
            "event_id": "e2", "producer": "T1", "objective": "补测试",
        })
        h.run_bridge()

        b = h.graph.get_node("B1")
        batch = b.pending_context["event_batch"]
        assert len(batch) == 2
        assert batch[0]["objective"] == "修接口"
        assert batch[0]["scope"] == "related"
        assert batch[0]["intent"] == "fix"
        assert batch[0]["affected_roles"] == ["backend"]
        assert batch[1]["objective"] == "补测试"

