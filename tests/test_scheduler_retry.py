"""Integration tests for Scheduler — Manager-aware scheduling and auto-retry.

Tests:
1. WAITING Manager doesn't occupy concurrent slots
2. Multiple Manager concurrency respects max_concurrency
3. Auto-retry Worker FAILED nodes in place (same node, no new retry node)
4. Auto-retry Manager FAILED nodes in place (re-expand), same as Worker
5. Max retries cap (no retry after max_iterations exhausted)
6. Retry interval control (retry_delay honored for dependency-free nodes)
"""

import time
from typing import Any, Optional

import pytest

from runtime.graph import Graph
from runtime.models import Node, NodeStatus, Edge, EdgeType, ManagerPhase
from runtime.planner import Planner
from executables.base import Executable, ExecutableContext, SchedulerDelegate, RuntimeGateway
from executables.registry import ExecutableRegistry


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


class _RetryAwareExecutable(Executable):
    """Executable that FAILs on first attempt (iteration_count=0), succeeds on retry.

    This enables testing the retry mechanism without infinite retry loops:
    - iteration_count == 0 → FAILED (triggers auto-retry)
    - iteration_count > 0  → COMPLETED (retry succeeds)
    """

    def __init__(self, context: ExecutableContext, **kwargs: Any):
        self.ctx = context

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        delay = ctx.config.get("delay", 0)
        if delay > 0:
            time.sleep(delay)
        if ctx.node.iteration_count == 0:
            return NodeStatus.FAILED
        return NodeStatus.COMPLETED

    def stop(self, ctx: ExecutableContext) -> None:
        pass

    def pause(self, ctx: ExecutableContext) -> None:
        pass

    def resume(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.RUNNING

    def cleanup_resources(self, ctx: ExecutableContext) -> None:
        pass


class _AlwaysFailExecutable(Executable):
    """Executable that always returns FAILED regardless of iteration count."""

    def __init__(self, context: ExecutableContext, **kwargs: Any):
        self.ctx = context

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        delay = ctx.config.get("delay", 0)
        if delay > 0:
            time.sleep(delay)
        return NodeStatus.FAILED

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
               manager_phase: Optional[str] = None,
               max_iterations: int = 3) -> Node:
    return Node(
        id=node_id,
        exec_type=exec_type,
        role="dev",
        task=f"Task {node_id}",
        config=config or {},
        status=status,
        parent_id=parent_id,
        manager_phase=manager_phase,
        max_iterations=max_iterations,
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
# Manager-aware scheduling tests
# ============================================================================

class TestManagerSlotUsage:

    def setup_method(self):
        self.graph = Graph()
        self.registry = ExecutableRegistry()
        self.registry.register("worker", _TestExecutable)
        self.registry.register("manager", _TestExecutable)
        self.delegate = MockDelegate()
        self.runtime = MockRuntimeGateway()

    def test_waiting_manager_does_not_occupy_slots(self):
        """WAITING Manager 不占用并发槽位，不影响其他 Worker 调度。"""
        from runtime.scheduler import Scheduler
        from concurrent.futures import Future

        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=2,
        )

        # Add a WAITING-phase Manager (simulating post-expand, children running)
        mgr = _make_node("M1", status=NodeStatus.WAITING, exec_type="manager",
                         manager_phase=ManagerPhase.WAITING.value)
        self.graph.add_node(mgr)

        # Simulate Manager's future still active (Manager thread alive)
        scheduler._futures["M1"] = Future()

        # Add 2 Workers that should be schedulable
        for wid in ["W1", "W2"]:
            self.graph.add_node(
                _make_node(wid, status=NodeStatus.READY, exec_type="worker",
                           config={"delay": 0.3})
            )

        scheduler.start()

        # Both workers should be scheduled (M1 doesn't count toward concurrency)
        def both_running_or_done():
            w1 = self.graph.get_node("W1")
            w2 = self.graph.get_node("W2")
            return (w1 is not None and w1.status in (NodeStatus.RUNNING, NodeStatus.COMPLETED) and
                    w2 is not None and w2.status in (NodeStatus.RUNNING, NodeStatus.COMPLETED))

        assert wait_until(both_running_or_done, timeout=5), \
            "Both workers should be scheduled despite WAITING Manager in _futures"

        scheduler.stop()

    def test_multiple_manager_concurrency(self):
        """多个 Manager 并发受 max_concurrency 限制。"""
        from runtime.scheduler import Scheduler

        # Create 3 Managers that run for a while
        for mid in ["M1", "M2", "M3"]:
            self.graph.add_node(
                _make_node(mid, status=NodeStatus.READY, exec_type="manager",
                           config={"delay": 0.5, "result_status": NodeStatus.COMPLETED})
            )

        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=2,
        )
        scheduler.start()

        # Wait briefly for scheduler to pick up nodes
        time.sleep(0.3)

        # At peak, at most 2 should be RUNNING
        max_running = 0
        for _ in range(15):
            running = sum(
                1 for n in self.graph.nodes.values()
                if n.status == NodeStatus.RUNNING
            )
            max_running = max(max_running, running)
            time.sleep(0.05)

        assert max_running <= 2, \
            f"At most 2 Managers should run concurrently, got {max_running}"

        scheduler.stop()


# ============================================================================
# Auto-retry tests
# ============================================================================

class TestAutoRetry:

    def setup_method(self):
        self.graph = Graph()
        self.planner = Planner(self.graph)
        self.registry = ExecutableRegistry()
        self.registry.register("worker", _TestExecutable)
        self.registry.register("manager", _TestExecutable)
        self.delegate = MockDelegate()
        self.runtime = MockRuntimeGateway()

    def test_auto_retry_worker_retried_in_place(self):
        """Worker FAILED 后原地重试：同节点 WAITING → 重跑成功，不创建新节点。"""
        from runtime.scheduler import Scheduler

        self.registry.register("worker", _RetryAwareExecutable)

        worker = _make_node("W1", status=NodeStatus.READY, exec_type="worker",
                            config={"delay": 0.05},
                            max_iterations=3)
        self.graph.add_node(worker)

        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=2, planner=self.planner, retry_delay=0.05,
        )
        scheduler.start()

        # W1 首跑失败（iteration_count=0），原地重试成功（iteration_count=1）后 COMPLETED
        def w1_completed():
            n = self.graph.get_node("W1")
            return n is not None and n.status == NodeStatus.COMPLETED
        assert wait_until(w1_completed, timeout=5), \
            "W1 should complete after in-place retry"

        # 原地重试：同一个节点，不创建 [RETRY: 新节点
        assert len(self.graph.nodes) == 1, "In-place retry must NOT create a new node"
        assert self.graph.get_node("W1").iteration_count == 1, \
            "W1 should have iteration_count=1 after one retry"
        assert scheduler._retry_attempts.get("W1", 0) == 1, \
            "W1 should have exactly 1 retry attempt"

        scheduler.stop()

    def test_auto_retry_manager_retried_in_place(self):
        """Manager FAILED 后同样原地重试（无子节点 → re-expand），与 Worker 一致。"""
        from runtime.scheduler import Scheduler

        self.registry.register("manager", _RetryAwareExecutable)

        mgr = _make_node("M1", status=NodeStatus.READY, exec_type="manager",
                         config={"delay": 0.05},
                         manager_phase=ManagerPhase.EXPAND.value,
                         max_iterations=3)
        self.graph.add_node(mgr)

        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=2, planner=self.planner, retry_delay=0.05,
        )
        scheduler.start()

        def m1_completed():
            n = self.graph.get_node("M1")
            return n is not None and n.status == NodeStatus.COMPLETED
        assert wait_until(m1_completed, timeout=5), \
            "M1 should complete after in-place retry"

        assert len(self.graph.nodes) == 1, "In-place retry must NOT create a new node"
        assert scheduler._retry_attempts.get("M1", 0) == 1, \
            "M1 should have exactly 1 retry attempt"

        scheduler.stop()

    def test_auto_retry_max_retries_cap(self):
        """重试超过 max_iterations 后不再重试，节点保持 FAILED。"""
        from runtime.scheduler import Scheduler

        self.registry.register("worker", _AlwaysFailExecutable)

        # max_iterations=1：只允许 1 次原地重试，随后耗尽
        worker = _make_node("W1", status=NodeStatus.READY, exec_type="worker",
                            config={"delay": 0.05},
                            max_iterations=1)
        self.graph.add_node(worker)

        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=2, planner=self.planner, retry_delay=0.05,
        )
        scheduler.start()

        # 首跑 + 1 次重试都失败，随后耗尽 max_iterations，W1 保持 FAILED
        def w1_exhausted():
            n = self.graph.get_node("W1")
            return (n is not None and n.status == NodeStatus.FAILED
                    and scheduler._retry_attempts.get("W1", 0) == 1)
        assert wait_until(w1_exhausted, timeout=5), \
            "W1 should end FAILED after retries exhausted"

        # 稳定后不再重试：确认 FAILED 保持、不创建新节点
        time.sleep(0.5)
        assert self.graph.get_node("W1").status == NodeStatus.FAILED, \
            "W1 should remain FAILED after max_iterations exhausted"
        assert len(self.graph.nodes) == 1, "In-place retry must NOT create a new node"

        scheduler.stop()

    def test_auto_retry_retry_succeeds(self):
        """原地重试成功后进入 COMPLETED，且 _retry_attempts 记录正确。"""
        from runtime.scheduler import Scheduler

        self.registry.register("worker", _RetryAwareExecutable)

        worker = _make_node("W1", status=NodeStatus.READY, exec_type="worker",
                            config={"delay": 0.1},
                            max_iterations=3)
        self.graph.add_node(worker)

        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=2, planner=self.planner, retry_delay=0.1,
        )
        scheduler.start()

        # 首跑失败（iteration_count=0）→ 原地重试成功（iteration_count=1）→ COMPLETED
        def w1_completed():
            n = self.graph.get_node("W1")
            return n is not None and n.status == NodeStatus.COMPLETED
        assert wait_until(w1_completed, timeout=5), \
            "W1 should eventually complete after retry"

        assert scheduler._retry_attempts.get("W1", 0) == 1, \
            f"Should have exactly 1 retry attempt, got {scheduler._retry_attempts.get('W1', 0)}"
        assert len(self.graph.nodes) == 1, "In-place retry must NOT create a new node"

        scheduler.stop()

    def test_auto_retry_interval_control(self):
        """重试间隔 retry_delay 被尊重：ready_time 到达前节点保持 WAITING，不提前重跑。"""
        from runtime.scheduler import Scheduler

        self.registry.register("worker", _AlwaysFailExecutable)

        worker = _make_node("W1", status=NodeStatus.READY, exec_type="worker",
                            config={"delay": 0.0},
                            max_iterations=3)
        self.graph.add_node(worker)

        RETRY_DELAY = 0.8
        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=2, planner=self.planner, retry_delay=RETRY_DELAY,
        )
        scheduler.start()

        # 首次失败后进入原地重试等待：_scheduled_retries 记录 ready_time，节点转 WAITING
        def retry_scheduled():
            return "W1" in scheduler._scheduled_retries
        assert wait_until(retry_scheduled, timeout=5), \
            "W1 should be placed in _scheduled_retries after first failure"
        ready_time = scheduler._scheduled_retries["W1"]
        assert ready_time - time.time() >= RETRY_DELAY - 0.3, \
            f"ready_time ({ready_time:.2f}) should be ~now + retry_delay ({RETRY_DELAY})"

        # 回归保护：ready_time 到达前，_process_waiting_nodes 不得把 W1 提前转为 READY
        promoted_early = False
        while time.time() < ready_time - 0.05:
            n = self.graph.get_node("W1")
            if n is None or n.status != NodeStatus.WAITING:
                promoted_early = True
                break
            time.sleep(0.02)
        assert not promoted_early, \
            "retry_delay bypassed: W1 left WAITING before ready_time"

        # 到期后正常转为 READY 并重跑（重跑后再次失败 → 进入下一轮等待）
        def w1_reruns():
            n = self.graph.get_node("W1")
            return n is not None and n.status in (
                NodeStatus.READY, NodeStatus.RUNNING,
                NodeStatus.FAILED, NodeStatus.COMPLETED,
            )
        assert wait_until(w1_reruns, timeout=RETRY_DELAY + 2), \
            "W1 should be re-scheduled after ready_time"

        scheduler.stop()


# ============================================================================
# End-to-end retry through Runtime
# ============================================================================

class TestRuntimeRetry:

    def setup_method(self):
        self.graph = Graph()
        self.planner = Planner(self.graph)
        self.registry = ExecutableRegistry()
        self.registry.register("worker", _TestExecutable)
        self.delegate = MockDelegate()
        self.runtime = MockRuntimeGateway()

    def test_runtime_auto_retry_flow(self):
        """Runtime 集成验证：Scheduler 通过 Runtime 传入的 Planner 执行原地自动重试。"""
        from runtime.scheduler import Scheduler

        self.registry.register("worker", _RetryAwareExecutable)

        worker = _make_node("W1", status=NodeStatus.READY, exec_type="worker",
                            config={"delay": 0.05},
                            max_iterations=2)
        self.graph.add_node(worker)

        scheduler = Scheduler(
            graph=self.graph, registry=self.registry,
            delegate=self.delegate, runtime_gateway=self.runtime,
            max_concurrency=2, planner=self.planner, retry_delay=0.05,
        )
        scheduler.start()

        # W1 首跑失败（iteration_count=0）→ 原地重试成功（iteration_count=1）→ COMPLETED
        def w1_completed():
            n = self.graph.get_node("W1")
            return n is not None and n.status == NodeStatus.COMPLETED
        assert wait_until(w1_completed, timeout=5), \
            "W1 should complete after in-place auto retry"

        assert len(self.graph.nodes) == 1, "In-place retry must NOT create a new node"
        assert scheduler._retry_attempts.get("W1", 0) == 1, \
            "W1 should have exactly 1 retry attempt"

        scheduler.stop()
