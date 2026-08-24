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

import pytest

from runtime.graph import Graph
from runtime.models import Edge, EdgeType, Node, NodeStatus
from runtime.scheduler import Scheduler
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

class TestBridgeReactivation:
    def test_terminal_node_target_event_reactivates(self, tmp_path):
        """终态 backend 收到 test 发的事件 → 转 READY（进入 Revise）+ 事件上下文注入。"""
        h = _BridgeHarness(tmp_path)
        _write_event(h.root, "B1", _feedback_event("T1"))
        h.run_bridge()

        b = h.graph.get_node("B1")
        assert b.status == NodeStatus.READY
        assert b.activation_count == 1
        assert "multiply(2,3)=5" in b.pending_context["event_content"]
        assert b.pending_context["event_producer"] == "T1"
        assert b.pending_context["activated_by"] == "T1"
        assert b.pending_context["activation_reason"].startswith("event evt_abc from T1")
        assert len(b.routing_history) == 1
        assert b.routing_history[0]["from"] == "T1"
        assert b.routing_history[0]["to"] == "B1"

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
        """方案三：inbox 有事件即处理，不解析 content 中的任何目标声明/指令。

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
    """事件→状态机（新契约）：Event 是独立于依赖终态的可执行信号。

    - WAITING + Event → READY（绕过依赖门控）
    - COMPLETED + Event → READY（进入 Revise，由 TestBridgeReactivation 覆盖）
    - RUNNING + Event → RUNNING（Scheduler 不重启、不消费 inbox）
    - FAILED + Event → FAILED（不自动复活、不消费 inbox）
    """

    def test_waiting_event_bypasses_dependency_gating(self, tmp_path):
        """WAITING 节点仍有 RUNNING 依赖时收到事件 → 直接 READY，不等依赖完成。"""
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
        assert b.status == NodeStatus.READY, "事件应绕过依赖门控直接转 READY"
        assert b.activation_count == 1

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

