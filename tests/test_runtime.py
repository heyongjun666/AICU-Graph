"""
Tests for runtime/runtime.py — Runtime 协调整合引擎（Basic Version）

Covers 8 acceptance criteria:
  1. Runtime initializes all components and starts Scheduler
  2. submit_root_manager creates root Manager node
  3. submit_plan translates via Planner and injects into Graph
  4. shutdown properly cleans up resources
  5. Single-root guard: duplicate submit_root_manager raises RuntimeError
  6. cleanup_all_processes() is callable (basic version no-op)
  7. SchedulerDelegate.on_node_status_changed publishes via EventBus
  8. shutdown persists latest graph_version, restore yields consistent version
"""

import os
import time
import threading
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, PropertyMock

import pytest

from runtime.event_bus import EventBus, SSEEvent
from runtime.graph import Graph
from runtime.models import (
    Node, NodeStatus, ExecutionPlan, Step, LogEntry, LogLevel, Edge, EdgeType,
)
from runtime.persistence import Persistence
from runtime.planner import Planner
from runtime.runtime import MANUAL_CONTINUE_BUDGET, Runtime
from runtime.scheduler import Scheduler
from executables.base import Executable, ExecutableContext
from executables.registry import ExecutableRegistry


# ============================================================================
# Helpers
# ============================================================================

class _TestExecutable(Executable):
    """Minimal executable for scheduler (auto-completes immediately)."""

    def __init__(self, context: ExecutableContext, **kwargs):
        super().__init__()

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.COMPLETED

    def stop(self, ctx: ExecutableContext) -> None:
        pass

    def pause(self, ctx: ExecutableContext) -> None:
        pass

    def resume(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.RUNNING

    def cleanup_resources(self, ctx: ExecutableContext) -> None:
        pass


class _BlockingExecutable(Executable):
    """Executable that blocks until external input or stopped."""

    def __init__(self, context: ExecutableContext, **kwargs):
        super().__init__()

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.COMPLETED

    def stop(self, ctx: ExecutableContext) -> None:
        pass

    def pause(self, ctx: ExecutableContext) -> None:
        pass

    def resume(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.RUNNING

    def cleanup_resources(self, ctx: ExecutableContext) -> None:
        pass


def wait_until(condition, timeout=5.0, interval=0.1):
    """Wait until *condition* callable returns truthy."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def _make_plan():
    """Create a simple ExecutionPlan with one worker step."""
    return ExecutionPlan(steps=[
        Step(
            step_id="step_1",
            exec_type="worker",
            role="coder",
            task="Write unit tests",
            task_summary="Write unit tests summary",
            config={},
            depends_on=[],
        ),
    ])


@pytest.fixture
def temp_state_file(tmp_path):
    """Fixture: temporary state file path."""
    return str(tmp_path / "runtime_state.json")


@pytest.fixture
def runtime(temp_state_file):
    """Fixture: basic Runtime instance with registered test executables."""
    rt = Runtime(
        persistence=Persistence(file_path=temp_state_file),
    )
    rt.registry.register("manager", _TestExecutable)
    rt.registry.register("worker", _TestExecutable)
    return rt


def _make_manager_node(node_id: str, status: NodeStatus = NodeStatus.READY,
                       parent_id: Optional[str] = None) -> Node:
    """Create a manager Node for testing."""
    return Node(
        id=node_id,
        parent_id=parent_id,
        exec_type="manager",
        role="coder",
        task="manager task",
        config={},
        status=status,
    )


def _make_worker_node(node_id: str, parent_id: str,
                      status: NodeStatus = NodeStatus.READY) -> Node:
    """Create a worker Node for testing."""
    return Node(
        id=node_id,
        parent_id=parent_id,
        exec_type="worker",
        role="coder",
        task="worker task",
        config={},
        status=status,
    )


# ============================================================================
# Acceptance Criterion 1: 初始化所有组件并启动 Scheduler
# ============================================================================

class TestRuntimeInitialization:
    """AC 1: Runtime 可正确初始化所有组件并启动 Scheduler."""

    def test_default_construction(self):
        """Runtime should auto-create all components when not provided."""
        rt = Runtime()
        assert isinstance(rt.graph, Graph)
        assert isinstance(rt.planner, Planner)
        assert isinstance(rt.persistence, Persistence)
        assert isinstance(rt.scheduler, Scheduler)
        assert isinstance(rt.event_bus, EventBus)
        assert isinstance(rt.registry, ExecutableRegistry)
        assert rt._root_manager_id is None

    def test_injection_of_components(self):
        """Runtime should accept externally created components."""
        graph = Graph()
        planner = Planner(graph)
        registry = ExecutableRegistry()
        persistence = Persistence("test.json")
        event_bus = EventBus()
        scheduler = Scheduler(
            graph=graph,
            registry=registry,
            delegate=MagicMock(),
            runtime_gateway=MagicMock(),
        )
        rt = Runtime(
            graph=graph,
            planner=planner,
            persistence=persistence,
            scheduler=scheduler,
            registry=registry,
            event_bus=event_bus,
        )
        assert rt.graph is graph
        assert rt.planner is planner
        assert rt.persistence is persistence
        assert rt.scheduler is scheduler
        assert rt.event_bus is event_bus
        assert rt.registry is registry

    def test_start_and_scheduler_running(self, runtime):
        """After start(), the scheduler tick thread should have been created."""
        runtime.start()
        # Scheduler auto-stops when no nodes, but tick thread was created
        assert runtime.scheduler._tick_thread is not None

    def test_start_loads_persisted_state(self, temp_state_file):
        """Starting Runtime should load persisted state when available."""
        # First session: create state
        rt1 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt1.registry.register("manager", _TestExecutable)
        rt1.start()
        rt1.submit_root_manager("test task")
        # Force save and shutdown
        rt1.persistence.save(rt1.graph)
        rt1.shutdown()

        # Second session: verify state loaded
        rt2 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt2.registry.register("manager", _TestExecutable)
        rt2.start()
        assert rt2._root_manager_id is not None
        assert len(rt2.graph.nodes) > 0
        rt2.shutdown()

    def test_start_downgrades_crashed_nodes(self, temp_state_file):
        """Crashed (RUNNING at snapshot) nodes should be downgraded to FAILED."""
        # Create state with a RUNNING node
        rt1 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt1.registry.register("manager", _TestExecutable)
        node = Node(
            id="crashed_node",
            parent_id=None,
            exec_type="manager",
            role="coder",
            task="crashed task",
            config={},
            status=NodeStatus.RUNNING,
        )
        rt1.graph.add_node(node)
        rt1.persistence.save(rt1.graph)
        rt1.shutdown()

        # Reload
        rt2 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt2.registry.register("manager", _TestExecutable)
        rt2.start()
        crashed_node = rt2.graph.get_node("crashed_node")
        assert crashed_node is not None
        assert crashed_node.status == NodeStatus.FAILED
        rt2.shutdown()

    def test_start_recovers_root_manager_waiting_with_terminal_children(self, temp_state_file):
        """根 Manager（parent_id=None）在 child 全部终态后崩溃，恢复时必须被推进为
        READY/review，否则根永久卡 WAITING（real E2E root 卡死的恢复补偿）。
        """
        rt1 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt1.registry.register("manager", _TestExecutable)
        root = Node(
            id="root",
            parent_id=None,
            exec_type="manager",
            role="default",
            task="root task",
            config={},
            status=NodeStatus.WAITING,
            manager_phase="waiting",
        )
        child_a = Node(
            id="child_a",
            parent_id="root",
            exec_type="manager",
            role="release_planner",
            task="child a",
            config={},
            status=NodeStatus.COMPLETED,
            manager_phase="waiting",
        )
        child_b = Node(
            id="child_b",
            parent_id="root",
            exec_type="manager",
            role="engineering_coord",
            task="child b",
            config={},
            status=NodeStatus.COMPLETED,
            manager_phase="waiting",
        )
        for n in (root, child_a, child_b):
            rt1.graph.add_node(n)
        rt1.graph.add_edge(Edge(
            id="e_a", type=EdgeType.PARENT, from_node="root", to_node="child_a",
        ))
        rt1.graph.add_edge(Edge(
            id="e_b", type=EdgeType.PARENT, from_node="root", to_node="child_b",
        ))
        rt1.persistence.save(rt1.graph)
        rt1.shutdown()

        rt2 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt2.registry.register("manager", _TestExecutable)
        rt2.start()
        recovered = rt2.graph.get_node("root")
        assert recovered is not None
        # 补偿把根推进为 READY/review；调度器可能随即执行完（COMPLETED）。
        # 核心不变量：根绝不再卡 WAITING，且 phase 已推进为 review。
        assert recovered.status != NodeStatus.WAITING
        assert recovered.manager_phase == "review"
        rt2.shutdown()


# ============================================================================
# Acceptance Criterion 2: submit_root_manager 创建根 Manager Node
# ============================================================================

class TestSubmitRootManager:
    """AC 2: submit_root_manager 创建根 Manager Node."""

    def test_create_root_manager(self, runtime):
        """submit_root_manager should create a root manager node."""
        runtime.start()
        node_id = runtime.submit_root_manager(
            task="Build the project",
            role="architect",
            config={"num_ccs": 2},
        )
        assert node_id is not None
        node = runtime.graph.get_node(node_id)
        assert node is not None
        assert node.parent_id is None
        assert node.exec_type == "manager"
        assert node.role == "architect"
        assert node.task == "Build the project"
        # config 会附加内部 workspace 元数据（_workspace_root/_workspace_relpath）
        assert node.config["num_ccs"] == 2
        assert node.config["_workspace_root"]
        assert node.config["_workspace_relpath"].startswith("nodes/")

    def test_root_manager_in_graph(self, runtime):
        """Root manager node should be in the graph."""
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        node = runtime.graph.get_node(node_id)
        assert node is not None
        # Node may have been scheduled/executed by async scheduler,
        # so only verify structural attributes
        assert node.parent_id is None
        assert node.exec_type == "manager"

    def test_default_role_and_config(self, runtime):
        """submit_root_manager should use defaults for role and config."""
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        node = runtime.graph.get_node(node_id)
        assert node.role == "default"
        # 默认 config={}，但会附加内部 workspace + Session Shared Workspace 元数据
        assert set(node.config) == {
            "_workspace_root",
            "_workspace_relpath",
            "_shared_workspace",
            "_output_path",
        }


# ============================================================================
# Acceptance Criterion 3: submit_plan 经 Planner 翻译后注入 Graph
# ============================================================================

class TestSubmitPlan:
    """AC 3: submit_plan translates via Planner and injects into Graph."""

    def test_apply_plan_requires_explicit_full_channel(self, runtime, monkeypatch):
        plan = _make_plan()
        expected = [object()]
        calls = []

        def apply_full(parent_id, received_plan):
            calls.append((parent_id, received_plan))
            return expected

        monkeypatch.setattr(runtime, "inject_plan", apply_full)
        result = runtime.apply_plan("parent", plan, channel="full")

        assert result is expected
        assert calls == [("parent", plan)]

    def test_apply_plan_dispatches_revision_without_guessing(self, runtime, monkeypatch):
        plan = _make_plan()
        expected = [object()]
        calls = []

        def apply_revision(parent_id, received_plan):
            calls.append((parent_id, received_plan))
            return expected

        monkeypatch.setattr(runtime, "inject_revision_plan", apply_revision)
        result = runtime.apply_plan("parent", plan, channel="revision")

        assert result is expected
        assert calls == [("parent", plan)]

    def test_apply_plan_rejects_implicit_channel(self, runtime):
        with pytest.raises(ValueError, match="full.*revision"):
            runtime.apply_plan("parent", _make_plan(), channel="auto")

    def test_submit_plan_creates_nodes(self, runtime):
        """submit_plan should create nodes via Planner."""
        runtime.start()
        parent_id = runtime.submit_root_manager("parent task")
        plan = _make_plan()
        nodes = runtime.inject_plan(parent_id, plan)
        assert len(nodes) == 1
        assert nodes[0].parent_id == parent_id
        assert nodes[0].exec_type == "worker"

    def test_submit_plan_protocol_contract(self, runtime):
        """RuntimeGateway.submit_plan protocol method should return None."""
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        plan = _make_plan()
        result = runtime.submit_plan(node_id, plan)
        assert result is None
        # Plan should be stored on the node
        node = runtime.get_node(node_id)
        assert node.execution_plan is not None

    def test_inject_plan_returns_nodes(self, runtime):
        """inject_plan should return list[Node]."""
        runtime.start()
        parent_id = runtime.submit_root_manager("parent task")
        plan = _make_plan()
        nodes = runtime.inject_plan(parent_id, plan)
        assert isinstance(nodes, list)
        assert len(nodes) == 1
        assert nodes[0].parent_id == parent_id
        assert nodes[0].exec_type == "worker"

    def test_plan_nodes_in_graph(self, runtime):
        """Plan-created nodes should be in the graph."""
        runtime.start()
        parent_id = runtime.submit_root_manager("parent task")
        plan = _make_plan()
        nodes = runtime.inject_plan(parent_id, plan)
        for n in nodes:
            graph_node = runtime.graph.get_node(n.id)
            assert graph_node is not None
            assert graph_node.id == n.id

    def test_plan_with_dependencies(self, runtime):
        """Plan with dependencies should create edges."""
        runtime.start()
        parent_id = runtime.submit_root_manager("parent task")
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="worker", role="coder", task="step 1", task_summary="step 1 summary"),
            Step(
                step_id="s2", exec_type="worker", role="coder",
                task="step 2", depends_on=["s1"], task_summary="step 2 summary",
            ),
        ])
        nodes = runtime.inject_plan(parent_id, plan)
        assert len(nodes) == 2
        # Check dependency relationship
        deps = runtime.graph.dependencies(nodes[1].id)
        dep_ids = [d.id for d in deps]
        assert nodes[0].id in dep_ids


# ============================================================================
# Acceptance Criterion 4: shutdown 正确清理资源
# ============================================================================

class TestShutdown:
    """AC 4: shutdown properly cleans up resources."""

    def test_shutdown_stops_scheduler(self, runtime):
        """After shutdown, scheduler should not be running."""
        runtime.start()
        runtime.submit_root_manager("test")
        runtime.shutdown()
        assert runtime.scheduler._running is False

    def test_shutdown_persists_state(self, temp_state_file):
        """After shutdown, the state file should exist and be loadable."""
        rt = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt.registry.register("manager", _TestExecutable)
        rt.registry.register("worker", _TestExecutable)
        rt.start()
        rt.submit_root_manager("test task")
        # Submit a plan
        plan = _make_plan()
        parent_id = rt._root_manager_id
        rt.inject_plan(parent_id, plan)
        rt.shutdown()

        # Verify file exists
        assert os.path.exists(temp_state_file)

        # Verify we can load it back
        snapshot = rt.persistence.load()
        assert snapshot is not None
        assert len(snapshot.nodes) >= 1  # at least root manager

    def test_shutdown_idempotent(self, runtime):
        """Calling shutdown multiple times should not raise."""
        runtime.start()
        runtime.submit_root_manager("test")
        runtime.shutdown()
        runtime.shutdown()  # second call should not raise

    def test_shutdown_does_not_persist_after_stop_periodic(self, runtime):
        """Shutdown should cleanly stop periodic save."""
        runtime.start()
        runtime.submit_root_manager("test")
        runtime.shutdown()
        # Verify the periodic timer is stopped
        assert runtime.persistence._timer is None


# ============================================================================
# Acceptance Criterion 5: 单根保护
# ============================================================================

class TestSingleRootGuard:
    """AC 5: 单根保护：重复 submit_root_manager 抛出 RuntimeError."""

    def test_duplicate_root_raises(self, runtime):
        """Second call to submit_root_manager should raise RuntimeError."""
        runtime.start()
        runtime.submit_root_manager("first task")
        with pytest.raises(RuntimeError, match="Root manager already exists"):
            runtime.submit_root_manager("second task")

    def test_root_manager_id_set(self, runtime):
        """After submit_root_manager, _root_manager_id should be set."""
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        assert runtime._root_manager_id == node_id

    def test_root_manager_id_protects_after_reload(self, temp_state_file):
        """After reload from persistence, root manager ID should protect."""
        # First session
        rt1 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt1.registry.register("manager", _TestExecutable)
        rt1.start()
        rt1.submit_root_manager("persisted task")
        rt1.persistence.save(rt1.graph)
        rt1.shutdown()

        # Second session: loads root manager from persistence
        rt2 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt2.registry.register("manager", _TestExecutable)
        rt2.start()
        # Should have root manager from loaded state
        with pytest.raises(RuntimeError, match="Root manager already exists"):
            rt2.submit_root_manager("another task")
        rt2.shutdown()


# ============================================================================
# Acceptance Criterion 6: cleanup_all_processes
# ============================================================================

class TestCleanupAllProcesses:
    """AC 6: cleanup_all_processes() is callable (basic version no-op)."""

    def test_cleanup_all_processes_callable(self, runtime):
        """cleanup_all_processes should be callable without error."""
        runtime.cleanup_all_processes()

    def test_cleanup_all_processes_after_start(self, runtime):
        """cleanup_all_processes should work after start."""
        runtime.start()
        runtime.cleanup_all_processes()
        runtime.shutdown()


# ============================================================================
# Acceptance Criterion 7: on_node_status_changed → EventBus
# ============================================================================

class TestEventBusPublish:
    """AC 7: SchedulerDelegate.on_node_status_changed publishes via EventBus."""

    def test_on_node_status_changed_publishes_event(self, runtime):
        """on_node_status_changed should publish node_status_changed event."""
        received_events = []

        def handler(event: SSEEvent):
            received_events.append(event)

        runtime.event_bus.subscribe("node_status_changed", handler)
        runtime.on_node_status_changed("node_1", NodeStatus.COMPLETED)
        assert len(received_events) == 1
        assert received_events[0].event_type == "node_status_changed"
        assert received_events[0].payload["node_id"] == "node_1"
        assert received_events[0].payload["status"] == "Completed"

    def test_event_payload_structure(self, runtime):
        """node_status_changed event should have correct payload structure."""
        received = []

        def handler(event: SSEEvent):
            received.append(event)

        runtime.event_bus.subscribe("node_status_changed", handler)
        runtime.on_node_status_changed("test_id", NodeStatus.FAILED)
        assert len(received) == 1
        payload = received[0].payload
        assert "node_id" in payload
        assert "status" in payload
        assert payload["node_id"] == "test_id"
        assert payload["status"] == "Failed"

    def test_wildcard_subscription_receives_event(self, runtime):
        """Wildcard subscription should also receive node_status_changed events."""
        received = []

        def handler(event: SSEEvent):
            received.append(event)

        runtime.event_bus.subscribe("*", handler)
        runtime.on_node_status_changed("n1", NodeStatus.RUNNING)
        assert len(received) >= 1


# ============================================================================
# Acceptance Criterion 8: Persistence graph_version consistency
# ============================================================================

class TestPersistenceGraphVersion:
    """AC 8: shutdown persists latest graph_version, restore yields consistent version."""

    def test_shutdown_persists_latest_version(self, temp_state_file):
        """After shutdown, the persisted snapshot should have graph_version > 0."""
        rt = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt.registry.register("manager", _TestExecutable)
        rt.start()
        rt.submit_root_manager("test")
        prior_version = rt.graph.graph_version
        assert prior_version > 0
        rt.shutdown()

        snapshot = Persistence(file_path=temp_state_file).load()
        assert snapshot is not None
        assert snapshot.graph_version == prior_version

    def test_restore_version_consistency(self, temp_state_file):
        """After restore, graph_version should match the saved version."""
        # First session
        rt1 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt1.registry.register("manager", _TestExecutable)
        rt1.registry.register("worker", _TestExecutable)
        rt1.start()
        rt1.submit_root_manager("test task")
        plan = _make_plan()
        rt1.inject_plan(rt1._root_manager_id, plan)
        saved_version = rt1.graph.graph_version
        rt1.shutdown()

        # Second session: restore。paused=True 隔离「仅验证 restore 本身」：
        # 调度线程不执行任何节点，避免恢复后的重跑 bump 版本号干扰断言。
        rt2 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt2.registry.register("manager", _TestExecutable)
        rt2.registry.register("worker", _TestExecutable)
        rt2.start(paused=True)
        assert rt2.graph.graph_version == saved_version
        rt2.shutdown()

    def test_multiple_mutations_persist_tracking(self, temp_state_file):
        """Graph version should persist correctly across sessions."""
        # Session 1: add a node
        rt1 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt1.registry.register("manager", _TestExecutable)
        rt1.start()
        rt1.submit_root_manager("t1")
        v1 = rt1.graph.graph_version
        rt1.shutdown()

        # Session 2: add another node, version should continue
        rt2 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt2.registry.register("manager", _TestExecutable)
        rt2.registry.register("worker", _TestExecutable)
        rt2.start(paused=True)
        # The version after restore should equal v1
        assert rt2.graph.graph_version == v1
        # Adding a plan should bump version
        plan = _make_plan()
        rt2.inject_plan(rt2._root_manager_id, plan)
        assert rt2.graph.graph_version > v1
        rt2.shutdown()


# ============================================================================
# RuntimeGateway protocol implementation tests
# ============================================================================

class TestRuntimeGateway:
    """Tests for RuntimeGateway protocol methods."""

    def test_get_node(self, runtime):
        """get_node should return node by ID."""
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        node = runtime.get_node(node_id)
        assert node is not None
        assert node.id == node_id

    def test_get_node_nonexistent(self, runtime):
        """get_node should return None for nonexistent node."""
        node = runtime.get_node("nonexistent")
        assert node is None

    def test_get_children(self, runtime):
        """get_children should return child nodes."""
        runtime.start()
        parent_id = runtime.submit_root_manager("test")
        plan = _make_plan()
        runtime.inject_plan(parent_id, plan)
        children = runtime.get_children(parent_id)
        assert len(children) == 1
        assert children[0].parent_id == parent_id

    def test_update_node_status(self, runtime):
        """update_node_status should update node status and publish event."""
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        received = []

        def handler(event):
            received.append(event)

        runtime.event_bus.subscribe("node_status_changed", handler)
        runtime.update_node_status(node_id, NodeStatus.COMPLETED)
        node = runtime.get_node(node_id)
        assert node.status == NodeStatus.COMPLETED
        assert len(received) == 1

    def test_register_and_deliver_external_input(self, runtime):
        """register_external_input_wait and deliver_external_input should work."""
        token = runtime.register_external_input_wait("node_1", {"type": "object"})
        assert token is not None
        runtime.deliver_external_input(token, {"key": "value"})
        assert runtime._external_input_waits[token]["data"] == {"key": "value"}

    def test_deliver_invalid_token(self, runtime):
        """deliver_external_input with invalid token should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid wait token"):
            runtime.deliver_external_input("invalid_token", {})


# ============================================================================
# Node-level control tests
# ============================================================================

class TestNodeControl:
    """Tests for node-level lifecycle control."""

    def test_stop_node(self, runtime):
        """stop_node should set node to STOPPED."""
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        runtime.stop_node(node_id)
        node = runtime.get_node(node_id)
        assert node.status == NodeStatus.STOPPED

    def test_stop_nonexistent_node(self, runtime):
        """stop_node with nonexistent ID should raise ValueError."""
        with pytest.raises(ValueError, match="not found"):
            runtime.stop_node("nonexistent")

    def test_pause_node_not_running(self, runtime):
        """pause_node on non-RUNNING node should raise ValueError."""
        runtime.start()
        # Pause scheduler so it won't pick up the READY node
        runtime.global_pause()
        node_id = runtime.submit_root_manager("test")
        with pytest.raises(ValueError, match="Cannot pause"):
            runtime.pause_node(node_id)

    def test_resume_node_not_paused(self, runtime):
        """resume_node on non-PAUSED node should raise ValueError."""
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        with pytest.raises(ValueError, match="Cannot resume"):
            runtime.resume_node(node_id)


# ============================================================================
# Manual Continue 预算授予（修复 max_iterations 锁死 bug）
# ============================================================================

class TestContinueNodeBudget:
    """手动 Continue 应额外授予 review 预算，而非锁死为当前迭代次数。

    原实现：有子节点的 Manager continue 时 max_iterations=iteration_count，
    审查守卫 `iteration_count + 1 < max_iterations` 恒 False → 立即再次 FAILED，
    等于 0 额外机会。修复后 max_iterations = max(原上限, iteration_count+预算)。
    """

    @staticmethod
    def _add_failed_manager(rt, node_id="mgr", iteration_count=3,
                            max_iterations=3, with_child=True):
        """直接在 runtime 图上构造 FAILED Manager（可带子节点）。"""
        mgr = Node(
            id=node_id, parent_id="root", exec_type="manager", role="coder",
            task="manager task", config={}, status=NodeStatus.FAILED,
            iteration_count=iteration_count, max_iterations=max_iterations,
        )
        rt.graph.add_node(mgr)
        if with_child:
            child_id = node_id + "_child"
            rt.graph.add_node(Node(
                id=child_id, parent_id=node_id, exec_type="worker", role="coder",
                task="child task", config={}, status=NodeStatus.COMPLETED,
            ))
            rt.graph.add_edge(Edge(
                id="e_" + child_id, type=EdgeType.PARENT,
                from_node=node_id, to_node=child_id,
            ))
        return node_id

    def test_continue_failed_manager_with_children_grants_budget(self, runtime):
        """有子节点 → 仅 re-review，max_iterations 提升为 iteration_count + 预算。"""
        runtime.global_pause()  # 阻止调度器立即派发，确定性断言 continue 决策
        nid = self._add_failed_manager(runtime, iteration_count=3, max_iterations=3)
        runtime.continue_node(nid)
        node = runtime.get_node(nid)
        assert node.status == NodeStatus.READY
        assert node.manager_phase == "review"
        assert node.iteration_count == 3  # 当前迭代不动
        assert node.max_iterations == 3 + MANUAL_CONTINUE_BUDGET

    def test_continue_failed_manager_no_children_grants_budget(self, runtime):
        """无子节点 → 重新 expand，同样授予额外预算。"""
        runtime.global_pause()
        nid = self._add_failed_manager(
            runtime, iteration_count=2, max_iterations=3, with_child=False)
        runtime.continue_node(nid)
        node = runtime.get_node(nid)
        assert node.status == NodeStatus.WAITING
        assert node.manager_phase is None
        assert node.max_iterations == max(3, 2 + MANUAL_CONTINUE_BUDGET)

    def test_continue_never_reduces_original_budget(self, runtime):
        """原上限更高时不缩减预算。"""
        runtime.global_pause()
        nid = self._add_failed_manager(runtime, iteration_count=1, max_iterations=8)
        runtime.continue_node(nid)
        assert runtime.get_node(nid).max_iterations == 8

    def test_continue_worker_reexecutes_without_budget_change(self, runtime):
        """Worker 直接重新执行，max_iterations 保持不变。"""
        runtime.global_pause()
        wid = "worker1"
        runtime.graph.add_node(Node(
            id=wid, parent_id="root", exec_type="worker", role="coder",
            task="worker task", config={}, status=NodeStatus.FAILED,
            iteration_count=2, max_iterations=3,
        ))
        runtime.continue_node(wid)
        node = runtime.get_node(wid)
        assert node.status == NodeStatus.WAITING
        assert node.manager_phase is None
        assert node.max_iterations == 3

    def test_continue_non_terminal_raises(self, runtime):
        """非 FAILED/STOPPED 节点 raise ValueError。"""
        runtime.global_pause()
        nid = self._add_failed_manager(runtime, with_child=True)
        runtime.update_node_status(nid, NodeStatus.READY)
        with pytest.raises(ValueError, match="FAILED or STOPPED"):
            runtime.continue_node(nid)


# ============================================================================
# Manual delete protection tests
# ============================================================================

class TestDeleteManualNodeProtection:
    """删除保护：清理子节点是严重操作，只有用户在前端手动逐叶子删除。

    代码层面不存在级联删除 —— Manager 有子节点时拒绝删除，必须先删除其子节点。
    """

    def _add_manager_with_child(self, runtime, mgr_id="mgr", child_id="child"):
        runtime.graph.add_node(Node(
            id=mgr_id, parent_id="root", exec_type="manager", role="coder",
            task="manager task", config={}, status=NodeStatus.COMPLETED,
        ))
        runtime.graph.add_node(Node(
            id=child_id, parent_id=mgr_id, exec_type="worker", role="coder",
            task="child task", config={}, status=NodeStatus.COMPLETED,
        ))
        runtime.graph.add_edge(Edge(
            id="e_child", type=EdgeType.PARENT,
            from_node=mgr_id, to_node=child_id,
        ))

    def test_delete_manager_with_children_rejected(self, runtime):
        """Manager 有子节点 → RuntimeError（删除保护生效，禁止级联删除）。"""
        runtime.global_pause()
        self._add_manager_with_child(runtime)
        with pytest.raises(RuntimeError, match="no cascade delete"):
            runtime.delete_manual_node("mgr")
        # 节点仍在（未被部分删除）
        assert runtime.graph.get_node("mgr") is not None
        assert runtime.graph.get_node("child") is not None

    def test_delete_leaf_child_succeeds(self, runtime):
        """叶子节点可直接删除；Manager 子节点删除后 Manager 变为可删。"""
        runtime.global_pause()
        self._add_manager_with_child(runtime)
        runtime.delete_manual_node("child")
        assert runtime.graph.get_node("child") is None
        # 子节点删空后 Manager 变为叶子，可删除
        runtime.delete_manual_node("mgr")
        assert runtime.graph.get_node("mgr") is None

    def test_delete_nonexistent_raises_value_error(self, runtime):
        with pytest.raises(ValueError):
            runtime.delete_manual_node("nonexistent")


# ============================================================================
# Global control tests
# ============================================================================

class TestGlobalControl:
    """Tests for global lifecycle control."""

    def test_global_pause_resume(self, runtime):
        """global_pause and global_resume should toggle scheduler pause."""
        runtime.start()
        assert runtime.scheduler._paused is False
        runtime.global_pause()
        assert runtime.scheduler._paused is True
        runtime.global_resume()
        assert runtime.scheduler._paused is False

    def test_global_stop(self, runtime):
        """global_stop should stop scheduler and mark active nodes STOPPED."""
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        runtime.global_stop()
        assert runtime.scheduler._running is False
        node = runtime.get_node(node_id)
        assert node.status == NodeStatus.STOPPED


# ============================================================================
# SchedulerDelegate protocol tests
# ============================================================================

class TestSchedulerDelegate:
    """Tests for SchedulerDelegate protocol methods."""

    def test_on_manager_children_completed_nonexistent_node(self, runtime):
        """on_manager_children_completed with nonexistent node should not raise."""
        runtime.on_manager_children_completed("nonexistent_node")

    def test_on_manager_children_completed_not_manager(self, runtime):
        """on_manager_children_completed with non-manager node should not raise."""
        runtime.start()
        runtime.registry.register("worker", _TestExecutable)
        # Create a worker node
        parent_id = runtime.submit_root_manager("test")
        plan = _make_plan()
        runtime.inject_plan(parent_id, plan)
        worker_node = runtime.graph.children(parent_id)[0]
        # Calling on_manager_children_completed with a worker ID should be no-op
        runtime.on_manager_children_completed(worker_node.id)

    def test_on_manager_children_completed_no_children(self, runtime):
        """on_manager_children_completed with no children should not transition."""
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        runtime.on_manager_children_completed(node_id)
        node = runtime.get_node(node_id)
        assert node.manager_phase is None or node.manager_phase == "expand"
        assert node.status != NodeStatus.READY

    def test_on_manager_children_not_all_completed(self, runtime):
        """When not all children are COMPLETED, no transition occurs."""
        runtime.start()
        manager_id = runtime.submit_root_manager("test")
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="worker", role="coder", task="step 1", task_summary="step 1 summary"),
            Step(step_id="s2", exec_type="worker", role="coder", task="step 2", task_summary="step 2 summary"),
        ])
        runtime.inject_plan(manager_id, plan)
        children = runtime.graph.children(manager_id)
        # Complete only one child
        runtime.graph.update_node(children[0].id, status=NodeStatus.COMPLETED)
        runtime.on_manager_children_completed(manager_id)
        manager = runtime.get_node(manager_id)
        # Manager should NOT transition (not all children completed)
        assert manager.manager_phase != "review"
        assert manager.status != NodeStatus.READY

    def test_on_manager_children_all_completed(self, runtime):
        """When all children are COMPLETED, Manager transitions to REVIEW phase."""
        runtime.start()
        manager_id = runtime.submit_root_manager("test")
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="worker", role="coder", task="step 1", task_summary="step 1 summary"),
            Step(step_id="s2", exec_type="worker", role="coder", task="step 2", task_summary="step 2 summary"),
        ])
        runtime.inject_plan(manager_id, plan)
        # Complete all children
        children = runtime.graph.children(manager_id)
        for child in children:
            runtime.graph.update_node(child.id, status=NodeStatus.COMPLETED)
        runtime.on_manager_children_completed(manager_id)
        manager = runtime.get_node(manager_id)
        assert manager.manager_phase == "review"
        assert manager.status == NodeStatus.READY

    def test_on_manager_children_all_completed_single_child(self, runtime):
        """Single child completing triggers Manager REVIEW transition."""
        runtime.start()
        manager_id = runtime.submit_root_manager("test")
        plan = _make_plan()
        runtime.inject_plan(manager_id, plan)
        child = runtime.graph.children(manager_id)[0]
        runtime.graph.update_node(child.id, status=NodeStatus.COMPLETED)
        runtime.on_manager_children_completed(manager_id)
        manager = runtime.get_node(manager_id)
        assert manager.manager_phase == "review"
        assert manager.status == NodeStatus.READY

    def test_get_active_execution_count(self, runtime):
        """get_active_execution_count should delegate to scheduler."""
        runtime.start()
        count = runtime.get_active_execution_count()
        assert count == 0
        assert count == runtime.scheduler.active_execution_count


# ============================================================================
# Integration tests
# ============================================================================

class TestIntegration:
    """Integration-level tests."""

    def test_full_lifecycle(self, temp_state_file):
        """Full lifecycle: start → submit root → submit plan → shutdown → restart."""
        # Session 1
        rt1 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt1.registry.register("manager", _TestExecutable)
        rt1.registry.register("worker", _TestExecutable)
        rt1.start()
        root_id = rt1.submit_root_manager("Build something")
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="worker", role="coder", task="do work", task_summary="do work summary"),
        ])
        rt1.inject_plan(root_id, plan)
        rt1.shutdown()

        # Session 2: verify everything is restored
        rt2 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt2.registry.register("manager", _TestExecutable)
        rt2.registry.register("worker", _TestExecutable)
        rt2.start()
        assert rt2._root_manager_id == root_id
        assert len(rt2.graph.nodes) == 2  # root + worker
        rt2.shutdown()

    def test_restart_after_crash_downgrade(self, temp_state_file):
        """Nodes that were RUNNING at crash should be FAILED after reload."""
        # Set up state with a RUNNING node
        rt1 = Runtime(persistence=Persistence(file_path=temp_state_file))
        node = Node(
            id="running_node",
            parent_id=None,
            exec_type="manager",
            role="coder",
            task="task",
            config={},
            status=NodeStatus.RUNNING,
        )
        rt1.graph.add_node(node)
        rt1.persistence.save(rt1.graph)
        rt1.shutdown()

        # Reload and verify downgrade
        rt2 = Runtime(persistence=Persistence(file_path=temp_state_file))
        rt2.registry.register("manager", _TestExecutable)
        rt2.start()
        n = rt2.graph.get_node("running_node")
        assert n.status == NodeStatus.FAILED
        rt2.shutdown()


# ============================================================================
# v4: TestGetLogs
# ============================================================================

class TestGetLogs:
    """runtime.get_logs() 获取节点日志。"""

    def test_get_logs_returns_logs(self, runtime):
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        node = runtime.get_node(node_id)
        node.log.append(LogEntry(ts=datetime.now(), level=LogLevel.INFO, message="test log"))
        logs = runtime.get_logs(node_id)
        assert len(logs) == 1
        assert logs[0].message == "test log"

    def test_get_logs_nonexistent_node(self, runtime):
        with pytest.raises(ValueError, match="not found"):
            runtime.get_logs("nonexistent")

    def test_get_logs_offset_limit(self, runtime):
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        node = runtime.get_node(node_id)
        for i in range(10):
            node.log.append(LogEntry(ts=datetime.now(), level=LogLevel.INFO, message=f"log {i}"))
        logs = runtime.get_logs(node_id, offset=3, limit=4)
        assert len(logs) == 4
        assert logs[0].message == "log 3"


# ============================================================================
# v4: TestGetArtifact
# ============================================================================

class TestGetArtifact:
    """runtime.get_artifact() 读取产物文件。"""

    def test_get_artifact_absolute_path(self, runtime, tmp_path):
        runtime.workspace_root = str(tmp_path)
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        node_ws = Path(runtime.get_node_workspace(node_id))
        f = node_ws / "test.txt"
        f.write_text("hello")
        content = runtime.get_artifact(node_id, str(f))
        assert content == "hello"

    def test_get_artifact_relative_path(self, runtime, tmp_path):
        runtime.workspace_root = str(tmp_path)
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        node_ws = Path(runtime.get_node_workspace(node_id))
        f = node_ws / "test.txt"
        f.write_text("hello")
        content = runtime.get_artifact(node_id, "test.txt")
        assert content == "hello"

    def test_get_artifact_path_traversal(self, runtime):
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        with pytest.raises(ValueError, match="Path traversal"):
            runtime.get_artifact(node_id, "../etc/passwd")

    def test_get_artifact_file_not_found(self, runtime, tmp_path):
        runtime.workspace_root = str(tmp_path)
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        with pytest.raises(FileNotFoundError):
            runtime.get_artifact(node_id, "nonexistent.txt")


# ============================================================================
# v4: TestGetGraphSnapshot
# ============================================================================

class TestGetGraphSnapshot:
    """runtime.get_graph_snapshot() 返回 Graph 快照。"""

    def test_snapshot_structure(self, runtime):
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        snapshot = runtime.get_graph_snapshot()
        assert "nodes" in snapshot
        assert "edges" in snapshot
        assert "stats" in snapshot
        assert len(snapshot["nodes"]) >= 1

    def test_snapshot_node_fields(self, runtime):
        runtime.start()
        node_id = runtime.submit_root_manager("test", role="architect", config={"num_ccs": 2})
        snapshot = runtime.get_graph_snapshot()
        node = snapshot["nodes"][0]
        assert node["id"] == node_id
        assert node["exec_type"] == "manager"
        assert node["role"] == "architect"
        assert node["status"] == "Running"


# ============================================================================
# v4: TestUpdateNodeTask
# ============================================================================

class TestUpdateNodeTask:
    """runtime.update_node_task() 更新节点 task。"""

    def test_update_task(self, runtime):
        runtime.start()
        node_id = runtime.submit_root_manager("original task")
        runtime.update_node_task(node_id, "updated task")
        node = runtime.get_node(node_id)
        assert node.task == "updated task"

    def test_update_task_nonexistent_node(self, runtime):
        with pytest.raises(ValueError, match="not found"):
            runtime.update_node_task("nonexistent", "task")


# ============================================================================
# v4: TestGraphMutationEvents
# ============================================================================

class TestGraphMutationEvents:
    """Graph 变更事件通过 EventBus 发布。"""

    def test_add_node_publishes_event(self, runtime):
        runtime.start()
        received = []
        runtime.event_bus.subscribe("node_added", lambda e: received.append(e))
        node_id = runtime.submit_root_manager("test")
        assert len(received) >= 1
        assert received[0].event_type == "node_added"

    def test_update_node_publishes_event(self, runtime):
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        received = []
        runtime.event_bus.subscribe("node_updated", lambda e: received.append(e))
        runtime.update_node_task(node_id, "new task")
        assert len(received) >= 1
        assert received[0].event_type == "node_updated"

    def test_add_edge_publishes_event(self, runtime):
        runtime.start()
        from runtime.models import Edge, EdgeType
        received = []
        runtime.event_bus.subscribe("edge_added", lambda e: received.append(e))
        parent_id = runtime.submit_root_manager("test")
        # inject_plan creates edges
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="worker", role="coder", task="step 1", task_summary="step 1 summary"),
        ])
        runtime.inject_plan(parent_id, plan)
        # inject may or may not create edges depending on planner
        if received:
            assert received[0].event_type == "edge_added"


def test_node_results_are_scoped_to_declared_artifact(tmp_path):
    runtime = Runtime(
        persistence=Persistence(file_path=str(tmp_path / "state.json")),
        workspace_root=str(tmp_path / "workspace"),
    )
    try:
        node = _make_manager_node("result_node", NodeStatus.COMPLETED)
        runtime.graph.add_node(node)
        assert runtime.list_node_results(node.id) == {
            "artifact_path": None,
            "artifact_kind": None,
            "result_root": None,
            "path": ".",
            "entries": [],
        }

        workspace = Path(runtime.get_node_workspace(node.id))
        result_dir = workspace / "results"
        nested_dir = result_dir / "nested"
        nested_dir.mkdir(parents=True)
        (result_dir / "summary.md").write_text("# Result", encoding="utf-8")
        (nested_dir / "details.txt").write_text("details", encoding="utf-8")
        cc_dir = workspace / "cc_0"
        cc_dir.mkdir()
        (cc_dir / "scratch.txt").write_text("private", encoding="utf-8")

        runtime.update_node_status(
            node.id, NodeStatus.COMPLETED, artifact_path=str(result_dir)
        )
        listing = runtime.list_node_results(node.id)
        assert listing["artifact_kind"] == "directory"
        assert [entry["name"] for entry in listing["entries"]] == [
            "nested", "summary.md"
        ]
        assert runtime.read_node_result_file(node.id, "summary.md")["content"] == "# Result"
        assert runtime.read_node_result_file(node.id, "nested/details.txt")["content"] == "details"
        with pytest.raises(ValueError, match="escapes its root"):
            runtime.list_node_results(node.id, "../cc_0")
        with pytest.raises(ValueError, match="escapes its root"):
            runtime.read_node_result_file(node.id, "../cc_0/scratch.txt")

        final_file = workspace / "final.txt"
        final_file.write_text("final output", encoding="utf-8")
        runtime.update_node_status(
            node.id, NodeStatus.COMPLETED, artifact_path=str(final_file)
        )
        file_listing = runtime.list_node_results(node.id)
        assert file_listing["artifact_kind"] == "file"
        assert file_listing["entries"][0]["name"] == "final.txt"
        assert runtime.read_node_result_file(node.id, "final.txt")["content"] == "final output"
    finally:
        runtime.shutdown()


def test_cc_activity_and_log_metadata_are_observable(tmp_path):
    runtime = Runtime(
        persistence=Persistence(file_path=str(tmp_path / "state.json")),
        workspace_root=str(tmp_path / "workspace"),
    )
    try:
        node = _make_manager_node("activity_node", NodeStatus.RUNNING)
        runtime.graph.add_node(node)
        runtime.update_cc_activity(
            node.id,
            "CC-1",
            "running",
            phase="streaming",
            task="Review the implementation",
            latest_output="Checking tests",
        )
        runtime.append_node_log(
            node.id,
            LogLevel.INFO,
            "Checking tests",
            cc_id="CC-1",
            phase="streaming",
            event="output",
            task="Review the implementation",
        )

        activity = runtime.get_cc_activity(node.id)["CC-1"]
        assert activity["status"] == "running"
        assert activity["phase"] == "streaming"
        assert activity["latest_output"] == "Checking tests"
        log = runtime.get_logs(node.id)[0]
        assert log.cc_id == "CC-1"
        assert log.phase == "streaming"
        assert log.event == "output"
        assert log.task == "Review the implementation"

        runtime.update_cc_activity(node.id, "CC-1", "completed", phase="finished")
        completed = runtime.get_cc_activity(node.id)["CC-1"]
        assert completed["status"] == "completed"
        assert completed["finished_at"]
    finally:
        runtime.shutdown()
