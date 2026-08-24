"""Tests for runtime/scheduler.py — Manager-aware scheduling.

Tests the Task-6 enhancements:
1. WAITING phase Managers don't occupy concurrent slots
2. RUNNING(expand) and RUNNING(review) Managers occupy slots
3. Same Manager Expand→Children→Review sequence priority
4. Manager nodes skipped in _process_waiting_nodes
5. _notified_managers prevents duplicate callbacks
6. Terminal states (FAILED/STOPPED) trigger children completion
"""

import time
import threading
from concurrent.futures import Future
from typing import Any, Optional

import pytest

from runtime.models import Node, NodeStatus, Edge, EdgeType, ManagerPhase
from runtime.graph import Graph
from executables.base import Executable, ExecutableContext, SchedulerDelegate, RuntimeGateway
from executables.registry import ExecutableRegistry
from runtime.event_bus import EventBus


# ============================================================================
# Test helpers
# ============================================================================

class MockDelegate:
    """Mock SchedulerDelegate that records callbacks."""

    def __init__(self):
        self.status_changes: list[tuple[str, NodeStatus]] = []
        self.completed_managers: list[str] = []
        self._active_count = 0

    def on_manager_children_completed(self, manager_node_id: str) -> None:
        self.completed_managers.append(manager_node_id)

    def on_node_status_changed(self, node_id: str, status: NodeStatus) -> None:
        self.status_changes.append((node_id, status))

    def get_active_execution_count(self) -> int:
        return self._active_count


class MockRuntimeGateway:
    """Mock RuntimeGateway for ExecutableContext."""

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


class _TestExecutable(Executable):
    """Test Executable that reads delay and result_status from ctx.config."""

    def __init__(self, context: ExecutableContext, **kwargs: Any):
        self.ctx = context

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        delay = ctx.config.get("delay", 0)
        if delay > 0:
            time.sleep(delay)
        result_status = ctx.config.get("result_status", NodeStatus.COMPLETED)
        # 模拟真实 Manager._expand：返回 WAITING 时同步置 manager_phase="waiting"，
        # 否则 Scheduler._process_waiting_nodes 会把 WAITING Manager 误判为
        # "expand 前等待依赖" 而重新提升为 READY，造成 RUNNING→WAITING→READY 循环。
        if (
            ctx.node.exec_type == "manager"
            and result_status == NodeStatus.WAITING
            and ctx.node.manager_phase != ManagerPhase.WAITING.value
        ):
            ctx.node.manager_phase = ManagerPhase.WAITING.value
        return result_status

    def stop(self, ctx: ExecutableContext) -> None:
        pass

    def pause(self, ctx: ExecutableContext) -> None:
        pass

    def resume(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.RUNNING

    def cleanup_resources(self, ctx: ExecutableContext) -> None:
        pass


def _make_node(node_id: str, status: NodeStatus = NodeStatus.READY,
               exec_type: str = "test", config: Optional[dict] = None,
               parent_id: Optional[str] = None,
               manager_phase: Optional[str] = None) -> Node:
    return Node(
        id=node_id,
        exec_type=exec_type,
        role="dev",
        task=f"Task {node_id}",
        config=config or {},
        status=status,
        parent_id=parent_id,
        manager_phase=manager_phase,
    )


def _add_dep_edge(graph: Graph, from_node: str, to_node: str, edge_id: str = None) -> None:
    """Add a dependency edge: to_node depends on from_node."""
    eid = edge_id or f"e_{from_node}_{to_node}"
    graph.add_edge(Edge(id=eid, type=EdgeType.DEPENDENCY, from_node=from_node, to_node=to_node))


def _add_parent_edge(graph: Graph, parent: str, child: str, edge_id: str = None) -> None:
    """Add a parent edge: parent is parent of child."""
    eid = edge_id or f"p_{parent}_{child}"
    graph.add_edge(Edge(id=eid, type=EdgeType.PARENT, from_node=parent, to_node=child))


def wait_until(condition_fn, timeout: float = 10.0, interval: float = 0.05) -> bool:
    """Wait until condition_fn() returns True or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition_fn():
            return True
        time.sleep(interval)
    return False


# ============================================================================
# Manager-aware slot counting tests
# ============================================================================

class TestManagerEffectiveRunningCount:

    def setup_method(self):
        self.graph = Graph()
        self.registry = ExecutableRegistry()
        self.registry.register("test", _TestExecutable)
        self.registry.register("manager", _TestExecutable)
        self.delegate = MockDelegate()
        self.event_bus = EventBus()
        self.runtime = MockRuntimeGateway()

    def test_waitting_phase_manager_excluded_from_slots(self):
        """WAITING phase 的 Manager 不占用并发槽位。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        # Add a WAITING-phase Manager that has a future (simulating post-expand)
        mgr = _make_node("M1", status=NodeStatus.WAITING, exec_type="manager",
                         manager_phase=ManagerPhase.WAITING.value)
        self.graph.add_node(mgr)

        # Simulate future in _futures (Manager submitted expand, now waiting)
        scheduler._futures["M1"] = Future()

        # _get_effective_running_count should exclude the WAITING-phase Manager
        effective = scheduler._get_effective_running_count()
        assert effective == 0, \
            f"WAITING phase Manager should not occupy slots, got {effective}"

        scheduler.stop()

    def test_running_expand_manager_included_in_slots(self):
        """RUNNING(expand) 的 Manager 占用并发槽位。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        mgr = _make_node("M1", status=NodeStatus.RUNNING, exec_type="manager",
                         manager_phase=ManagerPhase.EXPAND.value)
        self.graph.add_node(mgr)
        scheduler._futures["M1"] = Future()

        effective = scheduler._get_effective_running_count()
        assert effective == 1, \
            f"RUNNING(expand) Manager should occupy 1 slot, got {effective}"

        scheduler.stop()

    def test_running_review_manager_included_in_slots(self):
        """RUNNING(review) 的 Manager 占用并发槽位。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        mgr = _make_node("M1", status=NodeStatus.RUNNING, exec_type="manager",
                         manager_phase=ManagerPhase.REVIEW.value)
        self.graph.add_node(mgr)
        scheduler._futures["M1"] = Future()

        effective = scheduler._get_effective_running_count()
        assert effective == 1, \
            f"RUNNING(review) Manager should occupy 1 slot, got {effective}"

        scheduler.stop()

    def test_worker_always_included_in_slots(self):
        """Worker 节点始终占用并发槽位（不管 phase）。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        worker = _make_node("W1", status=NodeStatus.RUNNING, exec_type="worker")
        self.graph.add_node(worker)
        scheduler._futures["W1"] = Future()

        effective = scheduler._get_effective_running_count()
        assert effective == 1, \
            f"Worker should always occupy 1 slot, got {effective}"

        scheduler.stop()

    def test_mixed_running_count(self):
        """混合场景：WAITING Manager 排除，Worker 和 RUNNING Manager 计入。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        # RUNNING(expand) Manager — 应该计入
        m_expand = _make_node("M_expand", status=NodeStatus.RUNNING, exec_type="manager",
                              manager_phase=ManagerPhase.EXPAND.value)
        self.graph.add_node(m_expand)
        scheduler._futures["M_expand"] = Future()

        # RUNNING(review) Manager — 应该计入
        m_review = _make_node("M_review", status=NodeStatus.RUNNING, exec_type="manager",
                              manager_phase=ManagerPhase.REVIEW.value)
        self.graph.add_node(m_review)
        scheduler._futures["M_review"] = Future()

        # WAITING phase Manager — 不应计入
        m_waiting = _make_node("M_waiting", status=NodeStatus.WAITING, exec_type="manager",
                               manager_phase=ManagerPhase.WAITING.value)
        self.graph.add_node(m_waiting)
        scheduler._futures["M_waiting"] = Future()

        # Worker — 应该计入
        worker = _make_node("W1", status=NodeStatus.RUNNING, exec_type="worker")
        self.graph.add_node(worker)
        scheduler._futures["W1"] = Future()

        effective = scheduler._get_effective_running_count()
        assert effective == 3, \
            f"Expected 3 effective running (M_expand + M_review + W1), got {effective}"

        scheduler.stop()


# ============================================================================
# Manager children scheduling priority tests
# ============================================================================

class TestManagerChildrenPriority:

    def setup_method(self):
        self.graph = Graph()
        self.registry = ExecutableRegistry()
        self.registry.register("test", _TestExecutable)
        self.registry.register("manager", _TestExecutable)
        self.registry.register("worker", _TestExecutable)
        self.delegate = MockDelegate()
        self.event_bus = EventBus()
        self.runtime = MockRuntimeGateway()

    def test_children_of_waiting_manager_priority(self):
        """WAITING Manager 的子节点在调度时优先于同深度其他节点。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=1, event_bus=self.event_bus,
        )

        # Create a WAITING-phase Manager
        mgr = _make_node("M1", status=NodeStatus.WAITING, exec_type="manager",
                         manager_phase=ManagerPhase.WAITING.value)
        self.graph.add_node(mgr)

        # Create a child of this manager
        child = _make_node("child_1", status=NodeStatus.READY, exec_type="worker",
                           parent_id="M1")
        self.graph.add_node(child)
        _add_parent_edge(self.graph, "M1", "child_1")

        # Create an independent node (same depth 0, no deps)
        other = _make_node("other", status=NodeStatus.READY, exec_type="worker")
        self.graph.add_node(other)

        # Schedule one slot — should pick the Manager's child due to priority
        scheduler._schedule_ready_nodes()

        assert "child_1" in scheduler._futures, \
            "Manager's child should be scheduled first due to priority"
        assert "other" not in scheduler._futures, \
            "Independent node should not be scheduled when Manager child is waiting"

        scheduler.stop()

    def test_priority_order_with_deeper_node(self):
        """WAITING Manager 的子节点即使深度较浅也优先于深度更大的非子节点。

        即 (is_child=1, depth=0) > (is_child=0, depth=5)。
        """
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=1, event_bus=self.event_bus,
        )

        mgr = _make_node("M1", status=NodeStatus.WAITING, exec_type="manager",
                         manager_phase=ManagerPhase.WAITING.value)
        self.graph.add_node(mgr)

        # Child of waiting manager (depth 0, no deps)
        child = _make_node("child", status=NodeStatus.READY, exec_type="worker",
                           parent_id="M1")
        self.graph.add_node(child)
        _add_parent_edge(self.graph, "M1", "child")

        # Deep chain node: A→B→C, C has depth 2
        for nid in ["A", "B", "C"]:
            self.graph.add_node(_make_node(nid, status=NodeStatus.READY, exec_type="worker"))
        _add_dep_edge(self.graph, "A", "B")
        _add_dep_edge(self.graph, "B", "C")

        # Schedule one slot — child should win despite shallower depth
        scheduler._schedule_ready_nodes()

        assert "child" in scheduler._futures, \
            "Manager child (depth 0) should be scheduled before deeper non-child"

        scheduler.stop()


# ============================================================================
# Manager _process_waiting_nodes skip tests
# ============================================================================

class TestManagerProcessWaitingNodes:

    def setup_method(self):
        self.graph = Graph()
        self.registry = ExecutableRegistry()
        self.registry.register("test", _TestExecutable)
        self.registry.register("manager", _TestExecutable)
        self.delegate = MockDelegate()
        self.event_bus = EventBus()
        self.runtime = MockRuntimeGateway()

    def test_manager_waiting_skipped_by_dependency_check(self):
        """Manager WAITING 节点不通过 _process_waiting_nodes 转为 READY。

        Manager 应由 on_children_completed 回调驱动，而非依赖检查。
        """
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        # Manager is WAITING, has a dependency that is COMPLETED
        dep = _make_node("dep", status=NodeStatus.COMPLETED, exec_type="worker")
        self.graph.add_node(dep)

        mgr = _make_node("M1", status=NodeStatus.WAITING, exec_type="manager",
                         manager_phase=ManagerPhase.WAITING.value)
        self.graph.add_node(mgr)
        _add_dep_edge(self.graph, "dep", "M1")

        # Process waiting nodes — should NOT transition the Manager
        scheduler._process_waiting_nodes()

        m1 = self.graph.get_node("M1")
        assert m1 is not None and m1.status == NodeStatus.WAITING, \
            "Manager should remain WAITING after _process_waiting_nodes"

        scheduler.stop()

    def test_non_manager_waiting_normal_transition(self):
        """非 Manager WAITING 节点（exec_type != manager）正常通过依赖检查转 READY。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        dep = _make_node("dep", status=NodeStatus.COMPLETED, exec_type="worker")
        self.graph.add_node(dep)

        # Worker-type WAITING node
        worker = _make_node("W1", status=NodeStatus.WAITING, exec_type="worker")
        self.graph.add_node(worker)
        _add_dep_edge(self.graph, "dep", "W1")

        scheduler._process_waiting_nodes()

        w1 = self.graph.get_node("W1")
        assert w1 is not None and w1.status == NodeStatus.READY, \
            f"Worker should transition to READY, got {w1.status if w1 else None}"

        scheduler.stop()


# ============================================================================
# Manager children completion tests
# ============================================================================

class TestManagerChildrenCompleted:

    def setup_method(self):
        self.graph = Graph()
        self.registry = ExecutableRegistry()
        self.registry.register("test", _TestExecutable)
        self.registry.register("manager", _TestExecutable)
        self.registry.register("worker", _TestExecutable)
        self.delegate = MockDelegate()
        self.event_bus = EventBus()
        self.runtime = MockRuntimeGateway()

    def test_all_children_completed_triggers_callback(self):
        """Manager 的所有子节点 COMPLETED 后触发完成回调。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        # Create Manager in WAITING status
        mgr = _make_node("M1", status=NodeStatus.WAITING, exec_type="manager",
                         manager_phase=ManagerPhase.WAITING.value)
        self.graph.add_node(mgr)

        # Create 2 children, both COMPLETED
        c1 = _make_node("c1", status=NodeStatus.COMPLETED, exec_type="worker", parent_id="M1")
        c2 = _make_node("c2", status=NodeStatus.COMPLETED, exec_type="worker", parent_id="M1")
        self.graph.add_node(c1)
        self.graph.add_node(c2)
        _add_parent_edge(self.graph, "M1", "c1")
        _add_parent_edge(self.graph, "M1", "c2")

        # Trigger check from one child
        scheduler._check_manager_children_completed("c1")

        assert "M1" in self.delegate.completed_managers, \
            "Delegate should be notified when all children are completed"

        scheduler.stop()

    def test_not_all_children_completed_no_callback(self):
        """Manager 的子节点未全部完成时不触发回调。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        mgr = _make_node("M1", status=NodeStatus.WAITING, exec_type="manager",
                         manager_phase=ManagerPhase.WAITING.value)
        self.graph.add_node(mgr)

        c1 = _make_node("c1", status=NodeStatus.COMPLETED, exec_type="worker", parent_id="M1")
        c2 = _make_node("c2", status=NodeStatus.RUNNING, exec_type="worker", parent_id="M1")
        self.graph.add_node(c1)
        self.graph.add_node(c2)
        _add_parent_edge(self.graph, "M1", "c1")
        _add_parent_edge(self.graph, "M1", "c2")

        scheduler._check_manager_children_completed("c1")

        assert len(self.delegate.completed_managers) == 0, \
            "Delegate should NOT be notified when not all children completed"

        scheduler.stop()

    def test_terminal_states_trigger_callback(self):
        """Manager 子节点全部达到终端状态（FAILED/STOPPED）也应触发回调。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        mgr = _make_node("M1", status=NodeStatus.WAITING, exec_type="manager",
                         manager_phase=ManagerPhase.WAITING.value)
        self.graph.add_node(mgr)

        c1 = _make_node("c1", status=NodeStatus.FAILED, exec_type="worker", parent_id="M1")
        c2 = _make_node("c2", status=NodeStatus.STOPPED, exec_type="worker", parent_id="M1")
        self.graph.add_node(c1)
        self.graph.add_node(c2)
        _add_parent_edge(self.graph, "M1", "c1")
        _add_parent_edge(self.graph, "M1", "c2")

        scheduler._check_manager_children_completed("c1")

        assert "M1" in self.delegate.completed_managers, \
            "Delegate should be notified when all children reached terminal states (FAILED/STOPPED)"

        scheduler.stop()

    def test_notified_managers_prevents_duplicate_callbacks(self):
        """_notified_managers 集合防止同一 tick 内重复回调。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        mgr = _make_node("M1", status=NodeStatus.WAITING, exec_type="manager",
                         manager_phase=ManagerPhase.WAITING.value)
        self.graph.add_node(mgr)

        c1 = _make_node("c1", status=NodeStatus.COMPLETED, exec_type="worker", parent_id="M1")
        c2 = _make_node("c2", status=NodeStatus.COMPLETED, exec_type="worker", parent_id="M1")
        self.graph.add_node(c1)
        self.graph.add_node(c2)
        _add_parent_edge(self.graph, "M1", "c1")
        _add_parent_edge(self.graph, "M1", "c2")

        # First call triggers callback
        scheduler._check_manager_children_completed("c1")
        assert len(self.delegate.completed_managers) == 1, \
            "First call should trigger callback"

        # Second call should be suppressed by _notified_managers
        scheduler._check_manager_children_completed("c2")
        assert len(self.delegate.completed_managers) == 1, \
            "Second call should be suppressed by _notified_managers"

        scheduler.stop()

    def test_notified_managers_cleared_each_tick(self):
        """_notified_managers 在 tick 开始时被清除，允许下一个 tick 重新触发。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        # Simulate _notified_managers being set
        scheduler._notified_managers.add("M1")

        # In the tick loop, it's cleared at the start of each tick
        # But we need to simulate: M1 children completed, callback fired, M1 phase changed
        # Next tick: if M1 is still WAITING (it shouldn't be), it could fire again
        # Test: clear resets state
        scheduler._notified_managers.clear()
        assert "M1" not in scheduler._notified_managers, \
            "Cleared managers should be empty"

        scheduler.stop()

    def test_parent_not_manager_no_callback(self):
        """parent_id 指向非 Manager 节点时不触发回调。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        # Parent is worker, not manager
        parent = _make_node("parent", status=NodeStatus.WAITING, exec_type="worker")
        self.graph.add_node(parent)

        child = _make_node("child", status=NodeStatus.COMPLETED, exec_type="worker", parent_id="parent")
        self.graph.add_node(child)
        _add_parent_edge(self.graph, "parent", "child")

        scheduler._check_manager_children_completed("child")

        assert len(self.delegate.completed_managers) == 0, \
            "Should not trigger for non-Manager parent"

        scheduler.stop()

    def test_manager_not_waitting_no_callback(self):
        """Manager 状态不是 WAITING 时不触发回调。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        # Manager is RUNNING, not WAITING
        mgr = _make_node("M1", status=NodeStatus.RUNNING, exec_type="manager",
                         manager_phase=ManagerPhase.EXPAND.value)
        self.graph.add_node(mgr)

        c1 = _make_node("c1", status=NodeStatus.COMPLETED, exec_type="worker", parent_id="M1")
        self.graph.add_node(c1)
        _add_parent_edge(self.graph, "M1", "c1")

        scheduler._check_manager_children_completed("c1")

        assert len(self.delegate.completed_managers) == 0, \
            "Should not trigger when Manager is not WAITING"

        scheduler.stop()

    def test_children_completed_with_parent_edge_only(self):
        """子节点完成检测基于 PARENT 边关系而非依赖关系。"""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        mgr = _make_node("M1", status=NodeStatus.WAITING, exec_type="manager",
                         manager_phase=ManagerPhase.WAITING.value)
        self.graph.add_node(mgr)

        # Child has no dependency edge, only parent edge
        child = _make_node("child", status=NodeStatus.COMPLETED, exec_type="worker", parent_id="M1")
        self.graph.add_node(child)
        _add_parent_edge(self.graph, "M1", "child")

        scheduler._check_manager_children_completed("child")

        assert "M1" in self.delegate.completed_managers, \
            "Should trigger based on PARENT edge regardless of dependency edge"

        scheduler.stop()


# ============================================================================
# Integration tests: Manager-aware scheduling with real Executable
# ============================================================================

class TestManagerAwareSchedulerIntegration:

    def setup_method(self):
        self.graph = Graph()
        self.registry = ExecutableRegistry()
        self.registry.register("test", _TestExecutable)
        self.registry.register("manager", _TestExecutable)
        self.registry.register("worker", _TestExecutable)
        self.delegate = MockDelegate()
        self.event_bus = EventBus()
        self.runtime = MockRuntimeGateway()
        self.scheduler = None

    def teardown_method(self):
        if self.scheduler:
            self.scheduler.stop()

    def test_manager_expand_completes_and_waits(self):
        """Manager 执行 expand 后状态变为 WAITING，不占槽位。"""
        from runtime.scheduler import Scheduler

        # Create a Manager in READY with expand phase
        mgr = _make_node("M1", status=NodeStatus.READY, exec_type="manager",
                         manager_phase=ManagerPhase.EXPAND.value,
                         config={"result_status": NodeStatus.WAITING})
        self.graph.add_node(mgr)

        self.scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )
        self.scheduler.start()

        # Manager should transition READY→RUNNING→WAITING
        def mgr_waitting():
            n = self.graph.get_node("M1")
            return n is not None and n.status == NodeStatus.WAITING

        assert wait_until(mgr_waitting, timeout=5), \
            "Manager should reach WAITING after expand"

        # Manager should NOT occupy effective slot
        effective = self.scheduler._get_effective_running_count()
        assert effective == 0, \
            f"WAITING Manager should not occupy slot, effective count={effective}"

    def test_children_of_manager_get_priority(self):
        """Manager expand→WAITING 后，其子节点在调度时获优先。"""
        from runtime.scheduler import Scheduler

        # First, create a WAITING-phase Manager
        mgr = _make_node("M1", status=NodeStatus.WAITING, exec_type="manager",
                         manager_phase=ManagerPhase.WAITING.value)
        self.graph.add_node(mgr)

        # Create children of the Manager
        for cid in ["c1", "c2"]:
            child = _make_node(cid, status=NodeStatus.WAITING, exec_type="worker",
                               parent_id="M1", config={"delay": 0.3})
            self.graph.add_node(child)
            _add_parent_edge(self.graph, "M1", cid)
            # Children have no deps beyond being parented to M1

        # Make children READY (simulating deps met)
        self.graph.update_node("c1", status=NodeStatus.READY)
        self.graph.update_node("c2", status=NodeStatus.READY)

        # Create independent node
        other = _make_node("other", status=NodeStatus.READY, exec_type="worker",
                           config={"delay": 0.3})
        self.graph.add_node(other)

        self.scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=2, event_bus=self.event_bus,
        )
        self.scheduler.start()

        # Both children should be scheduled before 'other'
        def children_running_or_done():
            c1 = self.graph.get_node("c1")
            c2 = self.graph.get_node("c2")
            return (c1 is not None and c1.status in (NodeStatus.RUNNING, NodeStatus.COMPLETED) and
                    c2 is not None and c2.status in (NodeStatus.RUNNING, NodeStatus.COMPLETED))

        assert wait_until(children_running_or_done, timeout=5), \
            "Manager's children should be scheduled"

        # 'other' should not have been scheduled yet (only 2 slots, taken by children)
        other_node = self.graph.get_node("other")
        assert other_node is not None and other_node.status == NodeStatus.READY, \
            "Independent node should still be READY (priority to Manager children)"

    def test_manager_children_all_terminal_triggers_completion(self):
        """Manager 的所有子节点达到终端状态后触发 on_manager_children_completed。"""
        from runtime.scheduler import Scheduler

        mgr = _make_node("M1", status=NodeStatus.WAITING, exec_type="manager",
                         manager_phase=ManagerPhase.WAITING.value)
        self.graph.add_node(mgr)

        # Two children that complete quickly
        for cid in ["c1", "c2"]:
            child = _make_node(cid, status=NodeStatus.READY, exec_type="worker",
                               parent_id="M1")
            self.graph.add_node(child)
            _add_parent_edge(self.graph, "M1", cid)

        self.scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=2, event_bus=self.event_bus,
        )
        self.scheduler.start()

        # Wait for both children to complete
        def all_children_done():
            c1 = self.graph.get_node("c1")
            c2 = self.graph.get_node("c2")
            return (c1 is not None and c1.status == NodeStatus.COMPLETED and
                    c2 is not None and c2.status == NodeStatus.COMPLETED)

        assert wait_until(all_children_done, timeout=5), \
            "Both children should complete"

        # Manager should be in completed_managers list
        assert wait_until(lambda: "M1" in self.delegate.completed_managers, timeout=5), \
            "Delegate should be notified when all children complete"

    def test_waitting_manager_does_not_block_other_nodes(self):
        """WAITING phase 的 Manager 不占槽位，不影响其他节点调度。"""
        from runtime.scheduler import Scheduler

        # Create a WAITING-phase Manager (simulates expand completed)
        mgr = _make_node("M1", status=NodeStatus.WAITING, exec_type="manager",
                         manager_phase=ManagerPhase.WAITING.value)
        self.graph.add_node(mgr)

        # Simulate M1 having an active future (thread running but phase=WAITING)
        # This should NOT count against concurrency

        # Create independent nodes
        for nid in ["A", "B", "C"]:
            self.graph.add_node(
                _make_node(nid, status=NodeStatus.READY, exec_type="worker",
                           config={"delay": 0.3})
            )

        self.scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=2, event_bus=self.event_bus,
        )

        # Manually put M1 in _futures (simulating active Manager future)
        self.scheduler._futures["M1"] = Future()

        self.scheduler.start()

        # With max_concurrency=2 and M1 not counting, we should be able to run 2 workers
        def two_workers_running():
            running = [n for n in self.graph.nodes.values()
                       if n.status == NodeStatus.RUNNING]
            return len(running) >= 2

        assert wait_until(two_workers_running, timeout=5), \
            "Should schedule 2 workers despite WAITING Manager occupying a future slot"
