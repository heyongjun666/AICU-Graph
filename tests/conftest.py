"""Pytest fixtures for cc_0 integration tests."""

import os
import time
import tempfile
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from runtime.event_bus import EventBus, SSEEvent
from runtime.graph import Graph
from runtime.models import Node, NodeStatus, Edge, EdgeType, ExecutionPlan, Step
from runtime.persistence import Persistence
from runtime.runtime import Runtime
from runtime.scheduler import Scheduler
from runtime.server import create_app
from runtime.session_manager import SessionManager
from runtime.session_server import create_session_app
from runtime.sse_manager import SSEManager
from executables.base import Executable, ExecutableContext
from executables.registry import ExecutableRegistry


# ============================================================================
# Helper executables for testing
# ============================================================================


class _TestExecutable(Executable):
    """Minimal executable that auto-completes immediately."""

    def __init__(self, context: ExecutableContext, **kwargs):
        super().__init__()

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.COMPLETED


class _BlockingExecutable(Executable):
    """Executable that blocks until external input or stopped."""

    def __init__(self, context: ExecutableContext, **kwargs):
        super().__init__()

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.COMPLETED


def wait_until(condition, timeout=5.0, interval=0.1):
    """Wait until condition callable returns truthy."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def event_bus():
    """EventBus fixture."""
    return EventBus()


@pytest.fixture
def graph():
    """Graph fixture with one manager node."""
    g = Graph()
    manager = Node(
        id="manager_1",
        parent_id=None,
        exec_type="manager",
        role="coder",
        task="manager task",
        config={},
        status=NodeStatus.READY,
        manager_phase="expand",
    )
    g.add_node(manager)
    return g


@pytest.fixture
def runtime():
    """Runtime fixture with registered test executables."""
    rt = Runtime()
    rt.persistence.clear()
    rt.registry.register("manager", _TestExecutable)
    rt.registry.register("worker", _TestExecutable)
    rt.start()
    yield rt
    try:
        rt.shutdown()
        rt.persistence.clear()
    except Exception:
        pass


@pytest.fixture
def sse_manager(event_bus):
    """SSEManager fixture (not started by default)."""
    return SSEManager(event_bus=event_bus)


@pytest.fixture
def client(runtime):
    """FastAPI TestClient fixture with runtime and sse_manager injected."""
    sse = SSEManager(event_bus=runtime.event_bus)
    sse.start()
    app = create_app(runtime, sse)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def session_manager(runtime, tmp_path):
    manager = SessionManager(
        registry=runtime.registry,
        root_dir=str(tmp_path / "sessions"),
        workspace_root=str(tmp_path),
        restore=False,
    )
    yield manager
    manager.shutdown()


@pytest.fixture
def session_client(session_manager):
    app = create_session_app(session_manager)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def temp_state_file():
    """Fixture: temporary state file path."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


# ============================================================================
# Shared factory helpers
# ============================================================================


def make_manager_node(node_id: str, status: NodeStatus = NodeStatus.READY,
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


def make_worker_node(node_id: str, parent_id: str,
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


def make_simple_plan():
    """Create a simple ExecutionPlan with one worker step."""
    return ExecutionPlan(steps=[
        Step(
            step_id="step_1",
            exec_type="worker",
            role="coder",
            task="Write unit tests",
            config={},
            depends_on=[],
        ),
    ])
