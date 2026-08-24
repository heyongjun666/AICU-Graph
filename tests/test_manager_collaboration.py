"""tests/test_manager_collaboration.py — Manager 事件目标与事件上下文注入测试。

方案三（独立 AICU Router）：
- Router 目标可以是任意 Node（Worker→Worker / Worker→Manager / Manager→Manager），
  不再有 collaboration_type/required_role 等结构化协作字段——协作意图由 Event.content 承载。
- Scheduler 旧 routing 路径不校验目标角色/可达性（Router 统一决策，Scheduler 只做生命周期）。
- 被事件激活的 Manager 在 expand prompt 注入「待处理事件上下文」块
  （event_producer + event_content）。
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
from runtime.scheduler import Scheduler
from executables.base import ExecutableContext, RuntimeGateway
from executables.manager import Manager
from executables.registry import ExecutableRegistry


# ============================================================================
# Manager 侧 Gateway
# ============================================================================

class _Gateway(RuntimeGateway):
    """Manager 测试用最小 RuntimeGateway：记录状态更新 + 子节点/产物访问。"""

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


class _Stub:
    """可配置 ask() 返回的桩。"""

    def __init__(self, payload: dict):
        self._payload = payload
        self.calls = 0

    def ask(self, prompt: str) -> dict:
        self.calls += 1
        return self._payload


# ============================================================================
# Scheduler 旧 routing 路径：目标可为任意 Node（含 Manager），无可达性边界
# ============================================================================

class _SchedHarness:
    """Graph: root(R, manager) → 两个子 Manager(plan_mgr / req_mgr)。"""

    def __init__(self, planner: Optional[Any] = None):
        self.graph = Graph()
        self.root = Node(
            id="R", exec_type="manager", role="engineering_coord",
            task="root", config={}, status=NodeStatus.WAITING, step_ref="root",
        )
        self.plan_mgr = Node(
            id="P", exec_type="manager", role="architecture",
            task="plan", config={}, status=NodeStatus.COMPLETED,
            parent_id="R", step_ref="plan_mgr",
        )
        self.req_mgr = Node(
            id="Q", exec_type="manager", role="requirements",
            task="requirements", config={}, status=NodeStatus.COMPLETED,
            parent_id="R", step_ref="req_mgr",
        )
        for n in (self.root, self.plan_mgr, self.req_mgr):
            self.graph.add_node(n)
        for parent, child, eid in (("R", "P", "e1"), ("R", "Q", "e2")):
            self.graph.add_edge(Edge(id=eid, type=EdgeType.PARENT,
                                     from_node=parent, to_node=child))
        self.delegate = _MockDelegate()
        self.scheduler = Scheduler(
            graph=self.graph, registry=ExecutableRegistry(),
            delegate=self.delegate, runtime_gateway=_MockGateway(),
            max_concurrency=4, planner=planner,
        )


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


class TestSchedulerRoutingAnyNodeTarget:
    def test_manager_can_activate_manager_sibling(self):
        """A3 删除 Manager 目标拦截：Manager 可激活 Manager 兄弟。"""
        h = _SchedHarness()
        h.graph.update_node("P", pending_routing=RoutingDecision(
            activate=["req_mgr"],
            contexts={"req_mgr": {"event_content": "review plan", "event_producer": "P"}},
            reason="need review",
        ))
        h.scheduler._handle_routing("P")
        req = h.graph.get_node("Q")
        assert req.status == NodeStatus.WAITING, "Manager 目标不再被 A3 拦截"
        assert req.activation_count == 1
        assert req.pending_context["event_content"] == "review plan"

    def test_worker_can_route_to_manager_when_reachable(self):
        """Worker → Manager 目标同样允许（Router 目标可为任意 Node）。"""
        h = _SchedHarness()
        worker = Node(
            id="W", exec_type="worker", role="dev",
            task="w", config={}, status=NodeStatus.COMPLETED,
            parent_id="R", step_ref="worker",
        )
        h.graph.add_node(worker)
        h.graph.add_edge(Edge(id="e3", type=EdgeType.PARENT, from_node="R", to_node="W"))
        h.graph.update_node("W", pending_routing=RoutingDecision(
            activate=["req_mgr"],
            contexts={"req_mgr": {"event_content": "超出职责，需要 Manager 重新规划"}},
            reason="escalate",
        ))
        h.scheduler._handle_routing("W")
        req = h.graph.get_node("Q")
        assert req.status == NodeStatus.WAITING, "Worker 路由 Manager 目标不被拦截"
        assert req.activation_count == 1

    def test_routing_without_reachability_boundary(self):
        """可达性能力边界已删除：任意目标均可被激活（Router 统一决策）。"""
        h = _SchedHarness()
        h.graph.update_node("P", pending_routing=RoutingDecision(
            activate=["req_mgr"],
            contexts={"req_mgr": {"event_content": "t"}},
            reason="r",
        ))
        h.scheduler._handle_routing("P")
        req = h.graph.get_node("Q")
        assert req.status == NodeStatus.WAITING, "无可达性边界后目标被激活"
        assert req.activation_count == 1

    def test_manager_routes_back_to_requester_manager(self):
        """Manager 收到事件后回路由给请求方 Manager（无 respond 结构化字段）。"""
        h = _SchedHarness()
        h.graph.update_node("Q", pending_routing=RoutingDecision(
            activate=["plan_mgr"],
            contexts={"plan_mgr": {
                "event_content": "验收结论：需求未满足，请调整计划。", "event_producer": "Q",
            }},
            reason="adjust plan",
        ))
        h.scheduler._handle_routing("Q")
        plan = h.graph.get_node("P")
        assert plan.status == NodeStatus.WAITING, "回路由激活请求方 Manager"
        assert "需求未满足" in plan.pending_context["event_content"]
        assert plan.pending_context["event_producer"] == "Q"


# ============================================================================
# expand prompt 事件上下文注入
# ============================================================================

class TestEventContextBlock:
    def test_block_empty_without_event_context(self):
        node = _make_manager()
        gw = _Gateway("/tmp")
        gw.add_node(node)
        ctx = ExecutableContext(node=node, runtime=gw,
                                config={"workspace_dir": "/tmp", "mode": "stub"})
        assert Manager._build_event_context_block(ctx) == ""

    def test_block_injected_from_pending_context(self):
        node = _make_manager()
        node.pending_context = {
            "event_content": "review plan：请结合需求清单给出审查结论。",
            "event_producer": "other-node",
        }
        gw = _Gateway("/tmp")
        gw.add_node(node)
        ctx = ExecutableContext(node=node, runtime=gw,
                                config={"workspace_dir": "/tmp", "mode": "stub"})
        block = Manager._build_event_context_block(ctx)
        assert "## 📥 待处理事件上下文 (Event)" in block
        assert "review plan" in block
        assert "other-node" in block

    def test_expand_prompt_includes_event_context_block(self, tmp_path):
        node = _make_manager()
        node.pending_context = {
            "event_content": "review plan：请给出审查结论。",
            "event_producer": "other-node",
        }
        gw = _Gateway(str(tmp_path))
        gw.add_node(node)
        ctx = ExecutableContext(node=node, runtime=gw,
                                config={"workspace_dir": str(tmp_path), "mode": "stub"})
        mgr = Manager(context=ctx, stub=_Stub({"steps": []}))
        prompt = mgr._build_expand_prompt(ctx)
        assert "## 📥 待处理事件上下文 (Event)" in prompt
        assert "review plan" in prompt

    def test_expand_prompt_unchanged_without_event_context(self, tmp_path):
        """无待处理事件时 expand prompt 不注入事件上下文块。"""
        node = _make_manager()
        gw = _Gateway(str(tmp_path))
        gw.add_node(node)
        ctx = ExecutableContext(node=node, runtime=gw,
                                config={"workspace_dir": str(tmp_path), "mode": "stub"})
        mgr = Manager(context=ctx, stub=_Stub({"steps": []}))
        prompt = mgr._build_expand_prompt(ctx)
        assert "## 📥 待处理事件上下文 (Event)" not in prompt
        assert "## 你的任务(本节点原始 Task 描述)" in prompt

    def test_block_injected_from_event_inbox_fallback(self, tmp_path):
        """pending_context 为空但 inbox 有事件 → fallback 渲染事件上下文块。

        覆盖「目标先于事件 READY/RUNNING」竞态：桥跳过运行中节点不重激活，
        由运行中的 Manager CC 自行消费 inbox 事件渲染上下文。
        """
        node = _make_manager()
        node.pending_context = None  # 主路径（pending_context）为空，走 fallback
        inbox = os.path.join(str(tmp_path), "nodes", node.id, "event_inbox")
        os.makedirs(inbox, exist_ok=True)
        with open(os.path.join(inbox, "evt_collab.json"), "w", encoding="utf-8") as f:
            json.dump({
                "event_id": "evt_collab",
                "producer": "other-node",
                "content": "review plan：请结合需求清单给出审查结论，期望 approve plan。",
            }, f, ensure_ascii=False)
        gw = _Gateway(str(tmp_path))
        gw.add_node(node)
        ctx = ExecutableContext(node=node, runtime=gw,
                                config={"workspace_dir": str(tmp_path), "mode": "stub"})
        block = Manager._build_event_context_block(ctx)
        assert "## 📥 待处理事件上下文 (Event)" in block
        assert "review plan" in block
        assert "approve plan" in block
        assert "other-node" in block

    def test_block_empty_when_inbox_event_has_no_content(self, tmp_path):
        """inbox 事件缺 content（旧格式残留）→ 无事件上下文可渲染。"""
        node = _make_manager()
        node.pending_context = None
        inbox = os.path.join(str(tmp_path), "nodes", node.id, "event_inbox")
        os.makedirs(inbox, exist_ok=True)
        with open(os.path.join(inbox, "evt_legacy.json"), "w", encoding="utf-8") as f:
            json.dump({
                "event_id": "evt_legacy",
                "producer": "other-node",
                "payload": {"issues": [{"description": "fix it"}]},
            }, f, ensure_ascii=False)
        gw = _Gateway(str(tmp_path))
        gw.add_node(node)
        ctx = ExecutableContext(node=node, runtime=gw,
                                config={"workspace_dir": str(tmp_path), "mode": "stub"})
        block = Manager._build_event_context_block(ctx)
        assert block == ""
        assert "## 📥 待处理事件上下文 (Event)" not in block
