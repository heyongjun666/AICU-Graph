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
    """Mock RuntimeGateway for ExecutableContext.

    携带 graph 引用后，revise_worker_node 会镜像 runtime 的关键副作用
    （FAILED→WAITING + pending_revise + pending_context），使 Worker 自动重试
    的 Revise 路径可端到端验证。不弹 Worker._aicu_cache、不删 artifact（保留产物）。
    """

    def __init__(self, graph: Optional[Graph] = None):
        self.graph = graph
        self.revise_calls: list[tuple[str, str, str]] = []

    def submit_plan(self, node_id: str, plan: Any) -> None:
        pass

    def update_node_status(self, node_id: str, status: NodeStatus, **updates: Any) -> None:
        pass

    def get_node(self, node_id: str) -> Optional[Node]:
        if self.graph is not None:
            return self.graph.get_node(node_id)
        return None

    def get_children(self, node_id: str) -> list:
        return []

    def read_artifact(self, node_id: str, path: str) -> str:
        return ""

    def register_external_input_wait(self, node_id: str, input_schema: Optional[dict] = None) -> str:
        return "token"

    def deliver_external_input(self, wait_token: str, input_data: Any) -> None:
        pass

    def revise_worker_node(
        self, node_id: str, task: str, config: Optional[dict] = None,
        *, reason: str = "revise",
    ) -> str:
        """Mock of runtime.revise_worker_node：记录调用，节点 FAILED→WAITING 待续跑。

        保留缓存与产物（不弹 Worker._aicu_cache、不删 artifact），只镜像
        _reactivate_reused_worker 的状态簿记；iteration_count 由调度器回写。
        """
        self.revise_calls.append((node_id, task, reason))
        if self.graph is not None:
            node = self.graph.get_node(node_id)
            if node is not None:
                self.graph.update_node(
                    node_id,
                    status=NodeStatus.WAITING,
                    pending_revise=True,
                    pending_context={
                        "collaboration_type": "revise",
                        "activated_by": node_id,
                        "activation_reason": "auto-retry-after-failure",
                        "incremental_task": task,
                        "prior_artifacts": node.artifact_path,
                    },
                )
        return node_id


class _TestExecutable(Executable):
    """Test Executable that reads delay and result_status from ctx.config."""

    def __init__(self, context: ExecutableContext, **kwargs: Any):
        self.ctx = context

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        delay = ctx.config.get("delay", 0)
        if delay > 0:
            time.sleep(delay)
        return ctx.config.get("result_status", NodeStatus.COMPLETED)


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


class _AlwaysFailExecutable(Executable):
    """Executable that always returns FAILED regardless of iteration count."""

    def __init__(self, context: ExecutableContext, **kwargs: Any):
        self.ctx = context

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        delay = ctx.config.get("delay", 0)
        if delay > 0:
            time.sleep(delay)
        return NodeStatus.FAILED


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
        self.runtime = MockRuntimeGateway(self.graph)

    def test_auto_retry_worker_retried_in_place(self):
        """Worker FAILED 后原地重试：走 Revise 续跑而非全量重跑，同节点 WAITING → 成功。

        断言（与全量重跑路径的差异）：
        - 调用 runtime.revise_worker_node，任务携带失败原因反馈
        - pending_context.collaboration_type == 'revise'、pending_revise=True
        - 保留 Worker._aicu_cache（不弹缓存）、不删磁盘产物
        - iteration_count 回写为尝试数、_retry_attempts 递增
        - 同一个节点，不创建新节点
        """
        import tempfile
        import os
        from runtime.scheduler import Scheduler
        from executables.worker import Worker

        self.registry.register("worker", _RetryAwareExecutable)

        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = os.path.join(tmpdir, "design.md")
            with open(artifact, "w", encoding="utf-8") as f:
                f.write("# draft design\n")

            worker = _make_node("W1", status=NodeStatus.READY, exec_type="worker",
                                config={"delay": 0.05},
                                max_iterations=3)
            worker.artifact_path = artifact
            self.graph.add_node(worker)

            # 预置缓存的 CC 会话：revise 路径必须保留它（全量重跑会 pop）
            Worker._aicu_cache["W1"] = "cached-cc-session"
            try:
                scheduler = Scheduler(
                    graph=self.graph, registry=self.registry,
                    delegate=self.delegate, runtime_gateway=self.runtime,
                    max_concurrency=2, planner=self.planner, retry_delay=0.05,
                )
                scheduler.start()

                # W1 首跑失败（iteration_count=0），自动重试成功（iteration_count=1）后 COMPLETED
                def w1_completed():
                    n = self.graph.get_node("W1")
                    return n is not None and n.status == NodeStatus.COMPLETED
                assert wait_until(w1_completed, timeout=5), \
                    "W1 should complete after in-place retry"

                # 走 Revise：调用 runtime.revise_worker_node 且任务携带失败原因
                assert len(self.runtime.revise_calls) == 1, \
                    "Worker auto-retry should call runtime.revise_worker_node exactly once"
                called_id, called_task, called_reason = self.runtime.revise_calls[0]
                assert called_id == "W1"
                assert "失败原因" in called_task and "不要重头开始" in called_task, \
                    "revise task should carry failure feedback and forbid full restart"
                # reason 必须与用户手点的 Revise 区分开：界面上「你上一轮执行失败了…」
                # 这条任务得说清是谁写进来的（否则看的人以为是用户自己写的）。
                assert called_reason == "retry", \
                    "auto-retry must tag the revise as 'retry', not 'revise'"

                # pending_context 标记为 revise 增量续跑
                node = self.graph.get_node("W1")
                assert node.pending_context is not None, "pending_context should be set"
                assert node.pending_context.get("collaboration_type") == "revise", \
                    "pending_context.collaboration_type should be 'revise'"
                assert node.pending_revise is True, "pending_revise should be True"

                # 原地重试：同一个节点，不创建新节点；iteration_count / 重试次数回写
                assert len(self.graph.nodes) == 1, "In-place retry must NOT create a new node"
                assert node.iteration_count == 1, \
                    "W1 should have iteration_count=1 after one retry"
                assert scheduler._retry_attempts.get("W1", 0) == 1, \
                    "W1 should have exactly 1 retry attempt"

                # 缓存保留（revise 不弹 Worker._aicu_cache）与产物保留（不删 artifact）
                assert Worker._aicu_cache.get("W1") == "cached-cc-session", \
                    "revise path must NOT evict Worker._aicu_cache"
                assert os.path.exists(artifact), \
                    "revise path must NOT delete the artifact file"

                scheduler.stop()
            finally:
                Worker._aicu_cache.pop("W1", None)

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
        self.runtime = MockRuntimeGateway(self.graph)

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
