"""Tests for runtime/scheduler.py — Scheduler single-layer graph scheduling engine."""

import time
import threading
from typing import Any, Optional

import pytest

from runtime.models import Node, NodeStatus, Edge, EdgeType
from runtime.graph import Graph
from executables.base import Executable, ExecutableContext, SchedulerDelegate, RuntimeGateway
from executables.registry import ExecutableRegistry
from runtime.event_bus import EventBus, SSEEvent


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
        return ctx.config.get("result_status", NodeStatus.COMPLETED)

    def stop(self, ctx: ExecutableContext) -> None:
        pass

    def pause(self, ctx: ExecutableContext) -> None:
        pass

    def resume(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.RUNNING

    def cleanup_resources(self, ctx: ExecutableContext) -> None:
        pass


def _make_node(node_id: str, status: NodeStatus = NodeStatus.READY,
               exec_type: str = "test", config: Optional[dict] = None) -> Node:
    return Node(
        id=node_id,
        exec_type=exec_type,
        role="dev",
        task=f"Task {node_id}",
        config=config or {},
        status=status,
    )


def _add_dep_edge(graph: Graph, from_node: str, to_node: str, edge_id: str = None) -> None:
    """Add a dependency edge: to_node depends on from_node."""
    eid = edge_id or f"e_{from_node}_{to_node}"
    graph.add_edge(Edge(id=eid, type=EdgeType.DEPENDENCY, from_node=from_node, to_node=to_node))


def wait_until(condition_fn, timeout: float = 10.0, interval: float = 0.05) -> bool:
    """Wait until condition_fn() returns True or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition_fn():
            return True
        time.sleep(interval)
    return False


# ============================================================================
# Scheduler tests
# ============================================================================

class TestSchedulerConstruction:

    def setup_method(self):
        self.graph = Graph()
        self.registry = ExecutableRegistry()
        self.delegate = MockDelegate()
        self.event_bus = EventBus()
        self.runtime = MockRuntimeGateway()

    def test_thread_pool_size(self):
        """线程池大小 = max_concurrency + 2."""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph,
            registry=self.registry,
            delegate=self.delegate,
            runtime_gateway=self.runtime,
            max_concurrency=4,
            event_bus=self.event_bus,
        )
        assert scheduler._thread_pool._max_workers == 6  # 4 + 2

    def test_default_max_concurrency(self):
        """默认 max_concurrency = 4."""
        from runtime.scheduler import Scheduler
        scheduler = Scheduler(
            graph=self.graph,
            registry=self.registry,
            delegate=self.delegate,
            runtime_gateway=self.runtime,
            event_bus=self.event_bus,
        )
        assert scheduler.max_concurrency == 4


class TestSchedulerExecution:

    def setup_method(self):
        self.graph = Graph()
        self.registry = ExecutableRegistry()
        self.registry.register("test", _TestExecutable)
        self.delegate = MockDelegate()
        self.event_bus = EventBus()
        self.runtime = MockRuntimeGateway()
        self.scheduler = None

    def teardown_method(self):
        if self.scheduler:
            self.scheduler.stop()

    # ----------------------------------------------------------
    # Acceptance 1: Concurrency — 3 independent nodes all Running
    # ----------------------------------------------------------

    def test_three_independent_nodes_all_run_concurrently(self):
        """3 Worker 无依赖→3 个同时 Running（并发≥3）。"""
        from runtime.scheduler import Scheduler

        for nid in ["A", "B", "C"]:
            self.graph.add_node(_make_node(nid, status=NodeStatus.READY, config={"delay": 0.3}))

        self.scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )
        self.scheduler.start()

        # Wait until all 3 have transitioned to RUNNING
        def all_running():
            for nid in ["A", "B", "C"]:
                node = self.graph.get_node(nid)
                if node is None or node.status != NodeStatus.RUNNING:
                    return False
            return True

        assert wait_until(all_running, timeout=5), \
            "All 3 nodes should transition to RUNNING concurrently"

        # At the peak, all 3 should be in _futures
        peak = self.scheduler.active_execution_count
        assert peak >= 3, f"Expected >= 3 concurrent executions, got {peak}"

    # ----------------------------------------------------------
    # Acceptance 2: Serial dependency — A→B→C
    # ----------------------------------------------------------

    def test_serial_dependency_chain(self):
        """3 Worker 串行依赖 A→B→C → B 在 A Completed 前保持 Waiting。"""
        from runtime.scheduler import Scheduler

        # Setup: A→B→C  (C depends on B, B depends on A)
        nodes = {
            "A": _make_node("A", status=NodeStatus.READY, config={}),
            "B": _make_node("B", status=NodeStatus.WAITING, config={}),
            "C": _make_node("C", status=NodeStatus.WAITING, config={}),
        }
        for n in nodes.values():
            self.graph.add_node(n)
        _add_dep_edge(self.graph, "A", "B", "e_ab")
        _add_dep_edge(self.graph, "B", "C", "e_bc")

        self.scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )
        self.scheduler.start()

        # A should complete quickly (no delay)
        def a_done():
            n = self.graph.get_node("A")
            return n is not None and n.status == NodeStatus.COMPLETED
        assert wait_until(a_done, timeout=5), "A should complete"

        # B should eventually become READY then RUNNING then COMPLETED
        def b_done():
            n = self.graph.get_node("B")
            return n is not None and n.status == NodeStatus.COMPLETED
        assert wait_until(b_done, timeout=5), "B should complete after A"

        # C should eventually complete
        def c_done():
            n = self.graph.get_node("C")
            return n is not None and n.status == NodeStatus.COMPLETED
        assert wait_until(c_done, timeout=5), "C should complete after B"

    # ----------------------------------------------------------
    # Acceptance 3: Concurrency limit — max_concurrency=2
    # ----------------------------------------------------------

    def test_concurrency_limit_respected(self):
        """并发上限=2 时 Running 节点不超过 2。"""
        from runtime.scheduler import Scheduler

        for nid in ["A", "B", "C", "D"]:
            self.graph.add_node(_make_node(nid, status=NodeStatus.READY, config={"delay": 0.4}))

        self.scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=2, event_bus=self.event_bus,
        )
        self.scheduler.start()

        # Wait a bit for the scheduler to pick up nodes
        time.sleep(0.3)

        # Check that at most 2 are RUNNING at any snapshot
        running_count = lambda: sum(
            1 for n in self.graph.nodes.values()
            if n.status == NodeStatus.RUNNING
        )
        # It may spike briefly, but after stabilization max should be 2
        max_running = 0
        for _ in range(20):
            max_running = max(max_running, running_count())
            time.sleep(0.05)

        assert max_running <= 2, f"Should have at most 2 running, got {max_running}"

    # ----------------------------------------------------------
    # Acceptance 4: Auto-stop — tick loop stops when no Ready/Running
    # ----------------------------------------------------------

    def test_tick_loop_auto_stops(self):
        """tick 循环在无 Ready/Running 节点时自动停止。"""
        from runtime.scheduler import Scheduler

        node = _make_node("A", status=NodeStatus.READY, config={})
        self.graph.add_node(node)

        self.scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )
        self.scheduler.start()

        # Wait for A to complete
        def done():
            n = self.graph.get_node("A")
            return n is not None and n.status == NodeStatus.COMPLETED
        assert wait_until(done, timeout=5), "A should complete"

        # Tick loop should auto-stop
        assert wait_until(lambda: not self.scheduler._running, timeout=5), \
            "Scheduler should auto-stop when no Ready/Running nodes"

    # ----------------------------------------------------------
    # Acceptance 5: DFS — deeper nodes prioritized
    # ----------------------------------------------------------

    def test_dfs_depth_first_selection(self):
        """DFS 优先调度树深度更大的节点。"""
        from runtime.scheduler import Scheduler

        # Chain A→B→C (C depends on B, B depends on A)
        # Depths: A=0, B=1, C=2
        # All start READY; scheduler should schedule deepest first
        for nid in ["A", "B", "C"]:
            self.graph.add_node(_make_node(nid, status=NodeStatus.READY, config={}))
        _add_dep_edge(self.graph, "A", "B", "e_ab")
        _add_dep_edge(self.graph, "B", "C", "e_bc")

        self.scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=1, event_bus=self.event_bus,
        )

        # Run one tick manually
        self.scheduler._process_waiting_nodes()
        self.scheduler._schedule_ready_nodes()

        # With max_concurrency=1, only the deepest (C, depth 2) should be scheduled
        assert "C" in self.scheduler._futures, "C (depth 2) should be scheduled first"
        assert "B" not in self.scheduler._futures, \
            "B (depth 1) should not be scheduled when C is deeper"
        assert "A" not in self.scheduler._futures, \
            "A (depth 0) should not be scheduled when C is deeper"

    # ----------------------------------------------------------
    # Acceptance 6: Thread pool size = concurrency + 2
    # ----------------------------------------------------------

    def test_thread_pool_size_from_concurrency(self):
        """验证不同 max_concurrency 下线程池大小正确。"""
        from runtime.scheduler import Scheduler

        for conc in [1, 2, 4, 8]:
            s = Scheduler(
                graph=self.graph, registry=self.registry,
                delegate=self.delegate, runtime_gateway=self.runtime,
                max_concurrency=conc, event_bus=self.event_bus,
            )
            assert s._thread_pool._max_workers == conc + 2, \
                f"Expected {conc + 2} workers for concurrency={conc}"
            s.stop()

    # ----------------------------------------------------------
    # Acceptance 7: Delegate on_node_status_changed fires correctly
    # ----------------------------------------------------------

    def test_delegate_notified_on_status_changes(self):
        """SchedulerDelegate.on_node_status_changed 每次状态变更正确触发。"""
        from runtime.scheduler import Scheduler

        node = _make_node("A", status=NodeStatus.READY, config={})
        self.graph.add_node(node)

        self.scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )
        self.scheduler.start()

        # Wait for A to complete
        def done():
            n = self.graph.get_node("A")
            return n is not None and n.status == NodeStatus.COMPLETED
        assert wait_until(done, timeout=5), "A should complete"

        self.scheduler.stop()

        # Check delegate received status changes
        assert len(self.delegate.status_changes) >= 2, \
            f"Should have at least 2 status changes, got {len(self.delegate.status_changes)}"

        # Should see READY→RUNNING and RUNNING→COMPLETED
        changes = self.delegate.status_changes
        a_changes = [(nid, s) for nid, s in changes if nid == "A"]
        assert ("A", NodeStatus.RUNNING) in a_changes, \
            "Should have RUNNING transition"
        assert ("A", NodeStatus.COMPLETED) in a_changes, \
            "Should have COMPLETED transition"


class TestSchedulerWaitingToReady:

    def setup_method(self):
        self.graph = Graph()
        self.registry = ExecutableRegistry()
        self.registry.register("test", _TestExecutable)
        self.delegate = MockDelegate()
        self.event_bus = EventBus()
        self.runtime = MockRuntimeGateway()

    def test_waiting_to_ready_when_deps_complete(self):
        """WAITING 节点在依赖全部 COMPLETED 后转为 READY。"""
        from runtime.scheduler import Scheduler

        # A is READY, B depends on A and is WAITING
        self.graph.add_node(_make_node("A", status=NodeStatus.READY, config={}))
        self.graph.add_node(_make_node("B", status=NodeStatus.WAITING, config={}))
        _add_dep_edge(self.graph, "A", "B", "e_ab")

        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        # Manually complete A
        self.graph.update_node("A", status=NodeStatus.COMPLETED)

        # Process waiting nodes
        scheduler._process_waiting_nodes()

        # B should now be READY
        b_node = self.graph.get_node("B")
        assert b_node is not None and b_node.status == NodeStatus.READY, \
            f"B should be READY after A completes, got {b_node.status if b_node else None}"
        scheduler.stop()

    def test_waiting_stays_waiting_when_deps_not_done(self):
        """WAITING 节点在依赖未全部完成时保持 WAITING。"""
        from runtime.scheduler import Scheduler

        # A is READY but not done, B depends on A
        self.graph.add_node(_make_node("A", status=NodeStatus.READY, config={}))
        self.graph.add_node(_make_node("B", status=NodeStatus.WAITING, config={}))
        _add_dep_edge(self.graph, "A", "B", "e_ab")

        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        # Process waiting nodes (A is still READY, not COMPLETED)
        scheduler._process_waiting_nodes()

        # B should remain WAITING
        assert self.graph.get_node("B").status == NodeStatus.WAITING
        scheduler.stop()

    def test_waiting_stays_waiting_when_dep_failed(self):
        """依赖 FAILED 时下游必须保持 WAITING，不得被提升为 READY 运行。"""
        from runtime.scheduler import Scheduler

        # A FAILED（终态但非完成），B 依赖 A 且 WAITING
        self.graph.add_node(_make_node("A", status=NodeStatus.FAILED, config={}))
        self.graph.add_node(_make_node("B", status=NodeStatus.WAITING, config={}))
        _add_dep_edge(self.graph, "A", "B", "e_ab")

        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        scheduler._process_waiting_nodes()

        b_node = self.graph.get_node("B")
        assert b_node is not None and b_node.status == NodeStatus.WAITING, \
            f"B should stay WAITING when dep A FAILED, got {b_node.status if b_node else None}"

        # 重复 tick 也不得放行（FAILED 依赖不因时间推移解锁）
        scheduler._process_waiting_nodes()
        assert self.graph.get_node("B").status == NodeStatus.WAITING
        scheduler.stop()

    def test_waiting_stays_waiting_when_dep_stopped(self):
        """依赖 STOPPED 时下游必须保持 WAITING，不得被提升为 READY 运行。"""
        from runtime.scheduler import Scheduler

        self.graph.add_node(_make_node("A", status=NodeStatus.STOPPED, config={}))
        self.graph.add_node(_make_node("B", status=NodeStatus.WAITING, config={}))
        _add_dep_edge(self.graph, "A", "B", "e_ab")

        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        scheduler._process_waiting_nodes()

        assert self.graph.get_node("B").status == NodeStatus.WAITING, \
            "B should stay WAITING when dep A STOPPED"
        scheduler.stop()


class TestSchedulerPauseResume:

    def setup_method(self):
        self.graph = Graph()
        self.registry = ExecutableRegistry()
        self.registry.register("test", _TestExecutable)
        self.delegate = MockDelegate()
        self.event_bus = EventBus()
        self.runtime = MockRuntimeGateway()
        self.scheduler = None

    def teardown_method(self):
        if self.scheduler:
            self.scheduler.stop()

    def test_pause_prevents_scheduling(self):
        """暂停后不再调度新节点。"""
        from runtime.scheduler import Scheduler

        for nid in ["A", "B"]:
            self.graph.add_node(_make_node(nid, status=NodeStatus.READY, config={"delay": 0.3}))

        self.scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        # Pause before starting
        self.scheduler.pause()
        self.scheduler.start()

        time.sleep(0.5)

        # No node should be RUNNING since scheduler is paused
        running = [n for n in self.graph.nodes.values() if n.status == NodeStatus.RUNNING]
        assert len(running) == 0, "No node should run while paused"

    def test_resume_after_pause(self):
        """恢复后继续调度。"""
        from runtime.scheduler import Scheduler

        for nid in ["A", "B"]:
            self.graph.add_node(
                _make_node(nid, status=NodeStatus.READY, config={"delay": 0.2})
            )

        self.scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

        self.scheduler.pause()
        self.scheduler.start()
        time.sleep(0.3)

        # No nodes should be running
        assert not any(
            n.status == NodeStatus.RUNNING for n in self.graph.nodes.values()
        )

        # Resume
        self.scheduler.resume()
        time.sleep(0.5)

        # Nodes should now be running or completed
        any_progress = any(
            n.status in (NodeStatus.RUNNING, NodeStatus.COMPLETED)
            for n in self.graph.nodes.values()
        )
        assert any_progress, "Nodes should progress after resume"


class TestSchedulerWatchdog:

    def test_watchdog_records_retries(self):
        """Watchdog 达到最大重试次数后停止 scheduler。"""
        from runtime.scheduler import Scheduler

        graph = Graph()
        registry = ExecutableRegistry()
        delegate = MockDelegate()
        runtime = MockRuntimeGateway()

        scheduler = Scheduler(
            graph=graph, registry=registry,
            delegate=delegate, runtime_gateway=runtime,
            max_concurrency=4,
        )
        scheduler._running = True  # simulate started state

        # watchdog 重试 < max 时不停止
        scheduler._watchdog_retries = 1
        scheduler._last_watchdog_time = 0.0  # ensure interval check passes
        scheduler._handle_watchdog_error(Exception("test"))
        assert scheduler._running is True, "Scheduler should keep running after 1 retry"
        assert scheduler._watchdog_retries == 2, "Retry count should increment"

        # 模拟触发到 max
        scheduler._last_watchdog_time = 0.0
        scheduler._handle_watchdog_error(Exception("test"))
        assert scheduler._running is True, "Scheduler should keep running after 2 retries"

        scheduler._last_watchdog_time = 0.0
        scheduler._handle_watchdog_error(Exception("test"))
        assert scheduler._running is False, "Scheduler should stop after max retries"

        scheduler.stop()

    def test_watchdog_min_interval_enforced(self):
        """Watchdog 最小间隔内不重复计数。"""
        from runtime.scheduler import Scheduler

        graph = Graph()
        registry = ExecutableRegistry()
        delegate = MockDelegate()
        runtime = MockRuntimeGateway()

        scheduler = Scheduler(
            graph=graph, registry=registry,
            delegate=delegate, runtime_gateway=runtime,
            max_concurrency=4,
        )
        scheduler._running = True  # simulate started state

        scheduler._watchdog_retries = 0
        scheduler._last_watchdog_time = time.time()  # now
        retries_before = scheduler._watchdog_retries
        scheduler._handle_watchdog_error(Exception("test"))
        assert scheduler._watchdog_retries == retries_before, \
            "Retry count should not increment within min interval"
        assert scheduler._running is True, "Scheduler should still be running"

    def test_watchdog_pool_shutdown_recreates_pool(self):
        """Watchdog 在线程池已关闭时重新创建线程池。"""
        from runtime.scheduler import Scheduler

        graph = Graph()
        registry = ExecutableRegistry()
        delegate = MockDelegate()
        runtime = MockRuntimeGateway()

        scheduler = Scheduler(
            graph=graph, registry=registry,
            delegate=delegate, runtime_gateway=runtime,
            max_concurrency=4,
        )
        scheduler._running = True
        scheduler._watchdog_retries = 0
        scheduler._last_watchdog_time = 0.0

        # 先关闭线程池
        scheduler._pool_shutdown = True
        old_pool = scheduler._thread_pool

        scheduler._handle_watchdog_error(Exception("test"))

        # 线程池应被重新创建
        assert scheduler._thread_pool is not old_pool, "Thread pool should be recreated"
        assert scheduler._pool_shutdown is False, "Pool shutdown flag should be reset"


class TestSchedulerCycleDetection:

    def test_compute_depth_with_cycle(self):
        """环状依赖不导致栈溢出，稳定返回 0。"""
        from runtime.scheduler import Scheduler

        graph = Graph()
        # A→B→A 构成环
        graph.add_node(_make_node("A", status=NodeStatus.READY))
        graph.add_node(_make_node("B", status=NodeStatus.WAITING))
        _add_dep_edge(graph, "A", "B", "e_ab")
        _add_dep_edge(graph, "B", "A", "e_ba")

        scheduler = Scheduler(
            graph=graph, registry=ExecutableRegistry(),
            delegate=MockDelegate(), runtime_gateway=MockRuntimeGateway(),
            max_concurrency=4,
        )

        # 不应栈溢出
        depth_a = scheduler._compute_depth("A")
        depth_b = scheduler._compute_depth("B")
        assert isinstance(depth_a, int), "Depth should be an int"
        assert isinstance(depth_b, int), "Depth should be an int"
        assert depth_a >= 0, "Depth should be non-negative"
        assert depth_b >= 0, "Depth should be non-negative"

    def test_compute_depth_with_deep_chain(self):
        """长链依赖下深度计算正确且不超时。"""
        from runtime.scheduler import Scheduler

        graph = Graph()
        n_nodes = 20
        for i in range(n_nodes):
            nid = chr(ord('A') + i)
            graph.add_node(_make_node(nid, status=NodeStatus.READY))
        for i in range(n_nodes - 1):
            from_n = chr(ord('A') + i)
            to_n = chr(ord('A') + i + 1)
            _add_dep_edge(graph, from_n, to_n, f"e_{from_n}_{to_n}")

        scheduler = Scheduler(
            graph=graph, registry=ExecutableRegistry(),
            delegate=MockDelegate(), runtime_gateway=MockRuntimeGateway(),
            max_concurrency=4,
        )

        # A 的深度应该是 0（无入边），最深的节点深度为 n_nodes-1
        assert scheduler._compute_depth("A") == 0
        assert scheduler._compute_depth(chr(ord('A') + n_nodes - 1)) == n_nodes - 1


class TestSchedulerEventBus:

    def setup_method(self):
        self.graph = Graph()
        self.registry = ExecutableRegistry()
        self.registry.register("test", _TestExecutable)
        self.delegate = MockDelegate()
        self.event_bus = EventBus()
        self.runtime = MockRuntimeGateway()

    def test_tick_event_published(self):
        """Scheduler 在 tick 时发布 scheduler_tick 事件。"""
        from runtime.scheduler import Scheduler

        received_events = []

        def handler(event):
            received_events.append(event)

        self.event_bus.subscribe("scheduler_tick", handler)

        node = _make_node("A", status=NodeStatus.READY, config={})
        self.graph.add_node(node)

        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )
        scheduler.start()

        # Wait for A to complete
        def done():
            n = self.graph.get_node("A")
            return n is not None and n.status == NodeStatus.COMPLETED
        assert wait_until(done, timeout=5), "A should complete"

        scheduler.stop()

        # Should have received tick events
        assert len(received_events) > 0, "Should have received tick events"
        assert any(e.event_type == "scheduler_tick" for e in received_events)


# ============================================================================
# Manager children-completed 推进（pending_revise 死锁回归）
# ============================================================================

class TestManagerChildrenCompleted:

    def setup_method(self):
        self.graph = Graph()
        self.registry = ExecutableRegistry()
        self.delegate = MockDelegate()
        self.event_bus = EventBus()
        self.runtime = MockRuntimeGateway()

    def _make_scheduler(self):
        from runtime.scheduler import Scheduler
        return Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=4, event_bus=self.event_bus,
        )

    def _add_manager_with_children(self, manager_phase: str, pending_revise: bool):
        """构造 M1（manager）+ A/B（terminal workers）的父子图。"""
        mgr = Node(
            id="M1", exec_type="manager", role="coord", task="t",
            config={}, status=NodeStatus.WAITING,
            manager_phase=manager_phase, pending_revise=pending_revise,
        )
        self.graph.add_node(mgr)
        for cid in ("A", "B"):
            child = Node(
                id=cid, exec_type="test", role="dev", task=f"Task {cid}",
                config={}, status=NodeStatus.COMPLETED, parent_id="M1",
            )
            self.graph.add_node(child)
            self.graph.add_edge(Edge(
                id=f"e_m1_{cid}", type=EdgeType.PARENT,
                from_node="M1", to_node=cid,
            ))
        return mgr

    def test_waiting_manager_with_residual_pending_revise_advances_to_review(self):
        """回归（会话 f0c4d831 死锁）：残留 pending_revise 不再堵死 children-completed。

        事件重激活/自动重试路径可能把 Manager 停在
        WAITING + manager_phase="waiting" + pending_revise=True（expand 已完成但
        标记残留）。此前 _check_manager_children_completed 因 pending_revise 提前
        return，Manager 永久卡 Waiting；修复后判据只看 manager_phase，waiting
        （expand 已完成）即放行。
        """
        self._add_manager_with_children(manager_phase="waiting", pending_revise=True)
        scheduler = self._make_scheduler()

        scheduler._check_manager_children_completed("B")

        assert "M1" in self.delegate.completed_managers, \
            "waiting + 残留 pending_revise 的 Manager 应正常推进 review"

    def test_expanding_manager_with_terminal_old_children_does_not_advance(self):
        """守卫保留：expand 中的 Manager 不能因旧子节点终态提前 review。"""
        self._add_manager_with_children(manager_phase="expand", pending_revise=True)
        scheduler = self._make_scheduler()

        scheduler._check_manager_children_completed("B")

        assert "M1" not in self.delegate.completed_managers, \
            "expand 尚未完成的 Manager 不得推进 review"
