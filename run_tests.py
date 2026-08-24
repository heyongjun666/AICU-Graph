#!/usr/bin/env python3
"""
run_tests.py — v4 Integrated System Self-Contained Test Script

Run with:  python run_tests.py

Covers:
  - Models (serialization, validation, enums)
  - Graph (CRUD, indexing, snapshots)
  - StateMachine (transitions, Manager rules)
  - Planner (validation, translation, retry)
  - Persistence (save/load, periodic, crash recovery)
  - Integration (cross-module data flow)
"""

import contextlib
import copy
import io
import json
import logging
import os
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime
from typing import Any, Optional

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

# Ensure the solution root is on sys.path
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# ============================================================================
# Imports
# ============================================================================

from runtime.models import (
    Node, NodeStatus, Edge, EdgeType, ExecutionPlan, Step,
    LogEntry, LogLevel, ErrorObject, ManagerPhase,
)
from runtime.graph import Graph, DependencyError, GraphSnapshot
from runtime.state_machine import StateMachine, IllegalTransitionError
from runtime.planner import Planner, PlanValidationError
from runtime.persistence import Persistence
from runtime.event_bus import EventBus, SSEEvent
from runtime.sse_manager import SSEManager, format_sse
from executables.registry import ExecutableRegistry
from executables.base import Executable, ExecutableContext

# ============================================================================
# Test Infrastructure
# ============================================================================

_passed = 0
_failed = 0
_errors = []
_test_start_time = 0.0


def _reset_counts():
    global _passed, _failed, _errors, _test_start_time
    _passed = 0
    _failed = 0
    _errors = []
    _test_start_time = time.time()


def test(name: str):
    """Decorator that registers a test case with structured error capture."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            global _passed, _failed
            try:
                fn(*args, **kwargs)
                _passed += 1
                status = "PASS"
            except Exception as e:
                _failed += 1
                tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
                _errors.append({"test": name, "error": str(e), "traceback": tb})
                status = "FAIL"
            _print_result(name, status)
        wrapper._test_name = name
        wrapper._test_fn = fn
        return wrapper
    return decorator


def _print_result(name: str, status: str):
    max_len = 90
    dots = max(1, max_len - len(name) - len(status) - 2)
    print(f"  {name} {'·' * dots} {status}")


def assert_raises(exc_type, callable, *args, **kwargs):
    """Assert that a callable raises a specific exception type."""
    try:
        callable(*args, **kwargs)
        raise AssertionError(
            f"Expected {exc_type.__name__} but no exception was raised"
        )
    except exc_type:
        pass  # Expected
    except Exception as e:
        raise AssertionError(
            f"Expected {exc_type.__name__} but got {type(e).__name__}: {e}"
        )


# ============================================================================
# Helper Factories
# ============================================================================

def _make_node(node_id: str = None, exec_type: str = "worker",
               status: NodeStatus = NodeStatus.WAITING,
               parent_id: str = None) -> Node:
    return Node(
        id=node_id or str(uuid.uuid4()),
        parent_id=parent_id,
        exec_type=exec_type,
        role="coder",
        task="test task",
        config={},
        status=status,
    )


def _make_edge(edge_id: str = None, edge_type: EdgeType = EdgeType.PARENT,
               from_node: str = "a", to_node: str = "b") -> Edge:
    return Edge(
        id=edge_id or str(uuid.uuid4()),
        type=edge_type,
        from_node=from_node,
        to_node=to_node,
    )


def _make_plan(steps: list = None) -> ExecutionPlan:
    if steps is None:
        steps = [Step(step_id="s1", exec_type="worker", role="coder", task="do work", task_summary="do work summary")]
    return ExecutionPlan(steps=steps)


# ============================================================================
# 1. Models Tests
# ============================================================================

class TestModels:
    """Models — 序列化/反序列化、字段校验、枚举边界"""

    @test("Models: Node terminal state transition blocked")
    def test_node_terminal_blocked():
        n = _make_node("n1", status=NodeStatus.COMPLETED)
        assert_raises(ValueError, lambda: setattr(n, "status", NodeStatus.RUNNING))

    @test("Models: Node initial assignment allowed")
    def test_node_initial_allowed():
        n = Node(id="n1", exec_type="worker", role="c", task="t",
                 config={}, status="Waiting")
        assert n.status == NodeStatus.WAITING

    @test("Models: Node self-transition allowed (idempotent)")
    def test_node_self_transition():
        n = _make_node("n1", status=NodeStatus.COMPLETED)
        n.status = NodeStatus.COMPLETED  # same value → no-op

    @test("Models: Node serialization round-trip")
    def test_node_serialize():
        n = _make_node("n1", exec_type="manager", status=NodeStatus.RUNNING)
        json_str = n.to_json()
        n2 = Node.from_json(json_str)
        assert n2.id == n.id
        assert n2.exec_type == n.exec_type
        assert n2.status == n.status

    @test("Models: Node set_status method")
    def test_node_set_status():
        n = _make_node("n1", status=NodeStatus.WAITING)
        n.set_status(NodeStatus.READY)
        assert n.status == NodeStatus.READY

    @test("Models: Node set_status blocks terminal transition")
    def test_node_set_status_terminal():
        n = _make_node("n1", status=NodeStatus.FAILED)
        assert_raises(ValueError, n.set_status, NodeStatus.RUNNING)

    @test("Models: Edge serialization round-trip")
    def test_edge_serialize():
        e = _make_edge("e1", EdgeType.PARENT, "a", "b")
        json_str = e.to_json()
        e2 = Edge.from_json(json_str)
        assert e2.id == e.id
        assert e2.type == e.type
        assert e2.from_node == e.from_node
        assert e2.to_node == e.to_node

    @test("Models: Edge invalid type raises ValueError")
    def test_edge_invalid_type():
        assert_raises(ValueError, Edge, id="e1", type="invalid", from_node="a", to_node="b")

    @test("Models: ExecutionPlan to_json/from_json round-trip")
    def test_plan_roundtrip():
        plan = _make_plan()
        json_str = plan.to_json()
        plan2 = ExecutionPlan.from_json(json_str)
        assert len(plan2.steps) == 1
        assert plan2.steps[0].step_id == "s1"

    @test("Models: ExecutionPlan empty steps")
    def test_plan_empty():
        plan = ExecutionPlan(steps=[])
        json_str = plan.to_json()
        plan2 = ExecutionPlan.from_json(json_str)
        assert len(plan2.steps) == 0

    @test("Models: Step task truncation at 5000")
    def test_step_task_truncation():
        long_task = "x" * 6000
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            step = Step(step_id="s1", exec_type="worker", role="c",
                        task=long_task, config={})
        assert len(step.task) == 5000

    @test("Models: Step num_ccs out of range raises ValueError")
    def test_step_num_ccs_invalid():
        assert_raises(ValueError, Step, step_id="s1", exec_type="worker",
                      role="c", task="t", config={"num_ccs": 0})

    @test("Models: Step num_ccs at minimum boundary (1)")
    def test_step_num_ccs_min():
        step = Step(step_id="s1", exec_type="worker", role="c",
                    task="t", config={"num_ccs": 1})
        assert step.config["num_ccs"] == 1

    @test("Models: Step num_ccs at maximum boundary (10)")
    def test_step_num_ccs_max():
        step = Step(step_id="s1", exec_type="worker", role="c",
                    task="t", config={"num_ccs": 10})
        assert step.config["num_ccs"] == 10

    @test("Models: Step audit_rounds out of range raises ValueError")
    def test_step_audit_rounds_invalid():
        assert_raises(ValueError, Step, step_id="s1", exec_type="worker",
                      role="c", task="t", config={"audit_rounds": -1})

    @test("Models: Step audit_rounds at minimum boundary (0)")
    def test_step_audit_rounds_min():
        step = Step(step_id="s1", exec_type="worker", role="c",
                    task="t", config={"audit_rounds": 0})
        assert step.config["audit_rounds"] == 0

    @test("Models: LogEntry invalid level raises ValueError")
    def test_logentry_invalid_level():
        assert_raises(ValueError, LogEntry, ts=datetime.now(), level="INVALID", message="x")

    @test("Models: LogEntry serialization to_json")
    def test_logentry_to_json():
        entry = LogEntry(ts=datetime(2026, 1, 1), level=LogLevel.INFO, message="test")
        s = entry.to_json()
        data = json.loads(s)
        assert data["level"] == "INFO"
        assert data["message"] == "test"

    @test("Models: ErrorObject factory methods")
    def test_error_factories():
        e1 = ErrorObject.create_aicu_error()
        assert "AICU" in e1.message
        e2 = ErrorObject.create_max_iteration_exceeded()
        assert "iterations" in e2.message
        e3 = ErrorObject.create_worker_execution_error()
        assert "Worker" in e3.message
        e4 = ErrorObject.create_manager_execution_error()
        assert "Manager" in e4.message

    @test("Models: ErrorObject format_for_frontend debug toggle")
    def test_error_format():
        e = ErrorObject(message="m", cause="c", suggestion="s", debug_traceback="tb")
        result = e.format_for_frontend(debug=False)
        assert "debug_traceback" not in result
        result_debug = e.format_for_frontend(debug=True)
        assert result_debug["debug_traceback"] == "tb"

    @test("Models: Node with full fields serialization round-trip")
    def test_node_full_roundtrip():
        log_entry = LogEntry(ts=datetime.now(), level=LogLevel.INFO, message="started")
        error = ErrorObject(message="err", cause="cause", suggestion="fix")
        n = Node(
            id="full_node", parent_id="p1", exec_type="manager", role="architect",
            task="complex task", config={"num_ccs": 3}, status=NodeStatus.FAILED,
            artifact_path="/tmp/out", step_ref="step_1", manager_phase="review",
            iteration_count=2, max_iterations=5, log=[log_entry], error=error,
        )
        json_str = n.to_json()
        n2 = Node.from_json(json_str)
        assert n2.id == "full_node"
        assert n2.exec_type == "manager"
        assert n2.status == NodeStatus.FAILED
        assert n2.manager_phase == "review"
        assert n2.iteration_count == 2
        assert n2.max_iterations == 5
        assert len(n2.log) == 1
        assert n2.log[0].level == LogLevel.INFO
        assert n2.error is not None
        assert n2.error.message == "err"

    @test("Models: Node string status auto-conversion")
    def test_node_string_status():
        n = Node(id="str_status", exec_type="worker", role="c", task="t",
                 config={}, status="Running")
        assert isinstance(n.status, NodeStatus)
        assert n.status == NodeStatus.RUNNING

    @test("Models: Edge string type auto-conversion")
    def test_edge_string_type():
        e = Edge(id="e_str", type="parent", from_node="a", to_node="b")
        assert isinstance(e.type, EdgeType)
        assert e.type == EdgeType.PARENT

    @test("Models: ManagerPhase enum values")
    def test_manager_phase():
        assert ManagerPhase.EXPAND.value == "expand"
        assert ManagerPhase.REVIEW.value == "review"
        assert ManagerPhase.WAITING.value == "waiting"

    @test("Models: NodeStatus enum values")
    def test_node_status_values():
        assert NodeStatus.WAITING.value == "Waiting"
        assert NodeStatus.READY.value == "Ready"
        assert NodeStatus.RUNNING.value == "Running"
        assert NodeStatus.COMPLETED.value == "Completed"
        assert NodeStatus.FAILED.value == "Failed"
        assert NodeStatus.STOPPED.value == "Stopped"
        assert NodeStatus.PAUSED.value == "Paused"

    @test("Models: Step with num_ccs as negative raises ValueError")
    def test_step_num_ccs_negative():
        assert_raises(ValueError, Step, step_id="s1", exec_type="worker",
                      role="c", task="t", config={"num_ccs": -5})

    @test("Models: Step with num_ccs exceeded max raises ValueError")
    def test_step_num_ccs_exceeded():
        assert_raises(ValueError, Step, step_id="s1", exec_type="worker",
                      role="c", task="t", config={"num_ccs": 99})

    @test("Models: Step with audit_rounds exceeded max raises ValueError")
    def test_step_audit_rounds_exceeded():
        assert_raises(ValueError, Step, step_id="s1", exec_type="worker",
                      role="c", task="t", config={"audit_rounds": 99})

    @test("Models: Node with execution_plan serialization")
    def test_node_with_plan():
        plan = _make_plan()
        n = _make_node("plan_node", exec_type="manager", status=NodeStatus.WAITING)
        n.execution_plan = plan
        json_str = n.to_json()
        n2 = Node.from_json(json_str)
        assert n2.execution_plan is not None
        assert len(n2.execution_plan.steps) == 1
        assert n2.execution_plan.steps[0].step_id == "s1"

    @test("Models: ExecutionPlan from_json with complex steps")
    def test_plan_from_json_complex():
        plan_dict = {
            "steps": [
                {"step_id": "s1", "exec_type": "worker", "role": "coder",
                 "task": "step 1", "config": {}, "depends_on": []},
                {"step_id": "s2", "exec_type": "worker", "role": "reviewer",
                 "task": "step 2", "config": {"num_ccs": 2}, "depends_on": ["s1"]},
            ]
        }
        plan = ExecutionPlan.from_json(json.dumps(plan_dict))
        assert len(plan.steps) == 2
        assert plan.steps[1].depends_on == ["s1"]
        assert plan.steps[1].config["num_ccs"] == 2


# ============================================================================
# 2. Graph Tests
# ============================================================================

class TestGraph:
    """Graph — CRUD, indexing, snapshots, boundaries"""

    @test("Graph: add_node and get_node")
    def test_add_get_node():
        g = Graph()
        n = _make_node("n1")
        g.add_node(n)
        assert g.get_node("n1") is n

    @test("Graph: update_node modifies fields")
    def test_update_node():
        g = Graph()
        n = _make_node("n1", status=NodeStatus.WAITING)
        g.add_node(n)
        g.update_node("n1", status=NodeStatus.READY)
        assert g.get_node("n1").status == NodeStatus.READY

    @test("Graph: update_node nonexistent raises ValueError")
    def test_update_nonexistent():
        g = Graph()
        assert_raises(ValueError, g.update_node, "bad_id", status=NodeStatus.READY)

    @test("Graph: remove_node removes node and edges")
    def test_remove_node():
        g = Graph()
        n1 = _make_node("a")
        n2 = _make_node("b")
        g.add_node(n1)
        g.add_node(n2)
        e = _make_edge("e1", EdgeType.PARENT, "a", "b")
        g.add_edge(e)
        g.remove_node("b")
        assert g.get_node("b") is None
        assert g.get_node("a") is not None
        # Edge e1 should also be gone (cascade)
        assert "e1" not in g.edges

    @test("Graph: remove_node nonexistent raises ValueError")
    def test_remove_nonexistent():
        g = Graph()
        assert_raises(ValueError, g.remove_node, "bad_id")

    @test("Graph: remove_node with dependents raises DependencyError")
    def test_remove_with_dependents():
        g = Graph()
        n1 = _make_node("a")
        n2 = _make_node("b")
        g.add_node(n1)
        g.add_node(n2)
        e = _make_edge("e1", EdgeType.DEPENDENCY, "a", "b")  # b depends on a
        g.add_edge(e)
        assert_raises(DependencyError, g.remove_node, "a")

    @test("Graph: children returns empty list for leaf node")
    def test_children_empty():
        g = Graph()
        n = _make_node("leaf")
        g.add_node(n)
        assert g.children("leaf") == []

    @test("Graph: children O(1) lookup")
    def test_children_lookup():
        g = Graph()
        g.add_node(_make_node("parent"))
        g.add_node(_make_node("child"))
        g.add_edge(_make_edge("e1", EdgeType.PARENT, "parent", "child"))
        kids = g.children("parent")
        assert len(kids) == 1
        assert kids[0].id == "child"

    @test("Graph: dependencies returns empty for no-deps node")
    def test_dependencies_empty():
        g = Graph()
        g.add_node(_make_node("alone"))
        assert g.dependencies("alone") == []

    @test("Graph: dependents returns correct list")
    def test_dependents():
        g = Graph()
        g.add_node(_make_node("a"))
        g.add_node(_make_node("b"))
        g.add_edge(_make_edge("e1", EdgeType.DEPENDENCY, "a", "b"))
        deps = g.dependents("a")
        assert len(deps) == 1
        assert deps[0].id == "b"

    @test("Graph: add_edge with nonexistent from_node raises ValueError")
    def test_add_edge_bad_from():
        g = Graph()
        g.add_node(_make_node("b"))
        assert_raises(ValueError, g.add_edge, _make_edge("e1", EdgeType.PARENT, "a", "b"))

    @test("Graph: add_edge with nonexistent to_node raises ValueError")
    def test_add_edge_bad_to():
        g = Graph()
        g.add_node(_make_node("a"))
        assert_raises(ValueError, g.add_edge, _make_edge("e1", EdgeType.PARENT, "a", "b"))

    @test("Graph: remove_edge nonexistent raises ValueError")
    def test_remove_edge_nonexistent():
        g = Graph()
        assert_raises(ValueError, g.remove_edge, "bad_edge")

    @test("Graph: graph_version starts at 0 and bumps on mutate")
    def test_version_bump():
        g = Graph()
        assert g.graph_version == 0
        g.add_node(_make_node("n1"))
        assert g.graph_version > 0

    @test("Graph: snapshot includes crashed (RUNNING) nodes")
    def test_snapshot_crashed():
        g = Graph()
        g.add_node(_make_node("running_n", status=NodeStatus.RUNNING))
        g.add_node(_make_node("completed_n", status=NodeStatus.COMPLETED))
        snap = g.snapshot()
        assert len(snap.crashed) == 1
        assert snap.crashed[0].id == "running_n"

    @test("Graph: snapshot deep copies data")
    def test_snapshot_deep_copy():
        g = Graph()
        g.add_node(_make_node("n1"))
        snap = g.snapshot()
        snap.nodes["n1"].task = "modified"
        assert g.get_node("n1").task == "test task"

    @test("Graph: get_stats per-status count")
    def test_get_stats():
        g = Graph()
        g.add_node(_make_node("n1", status=NodeStatus.RUNNING))
        g.add_node(_make_node("n2", status=NodeStatus.RUNNING))
        g.add_node(_make_node("n3", status=NodeStatus.COMPLETED))
        stats = g.get_stats()
        assert stats.get("Running") == 2
        assert stats.get("Completed") == 1

    @test("Graph: is_dirty tracking")
    def test_is_dirty():
        g = Graph()
        assert not g.is_dirty
        g.add_node(_make_node("n1"))
        assert g.is_dirty
        g.mark_clean()
        assert not g.is_dirty
        g.add_node(_make_node("n2"))
        assert g.is_dirty

    @test("Graph: mark_clean_if only when version matches")
    def test_mark_clean_if():
        g = Graph()
        g.add_node(_make_node("n1"))
        v1 = g.graph_version
        g.add_node(_make_node("n2"))
        g.mark_clean_if(v1)  # version changed, should NOT clear
        assert g.is_dirty

    @test("Graph: mutation callback fires on add_node")
    def test_mutation_callback():
        g = Graph()
        events = []
        g.set_on_mutation(lambda event, payload: events.append((event, payload)))
        g.add_node(_make_node("n1"))
        assert len(events) == 1
        assert events[0][0] == "add_node"

    @test("Graph: mutation callback fires on update_node")
    def test_mutation_callback_update():
        g = Graph()
        g.add_node(_make_node("n1"))
        events = []
        g.set_on_mutation(lambda event, payload: events.append((event, payload)))
        g.update_node("n1", task="new")
        assert len(events) >= 1
        assert events[0][0] == "update_node"

    @test("Graph: mutation callback fires on remove_node")
    def test_mutation_callback_remove():
        g = Graph()
        g.add_node(_make_node("n1"))
        events = []
        g.set_on_mutation(lambda event, payload: events.append((event, payload)))
        g.remove_node("n1")
        assert len(events) >= 1
        assert events[0][0] == "remove_node"

    @test("Graph: multiple edges indexing consistency")
    def test_multi_edge_indexing():
        g = Graph()
        for nid in ["a", "b", "c"]:
            g.add_node(_make_node(nid))
        g.add_edge(_make_edge("e1", EdgeType.DEPENDENCY, "a", "b"))
        g.add_edge(_make_edge("e2", EdgeType.DEPENDENCY, "a", "c"))
        assert len(g.dependents("a")) == 2
        assert len(g.dependencies("b")) == 1
        assert len(g.dependencies("c")) == 1

    @test("Graph: snapshot returns correct graph_version")
    def test_snapshot_version():
        g = Graph()
        g.add_node(_make_node("n1"))
        v = g.graph_version
        snap = g.snapshot()
        assert snap.graph_version == v

    @test("Graph: get_stats empty graph returns empty dict")
    def test_stats_empty():
        g = Graph()
        stats = g.get_stats()
        assert stats == {}


# ============================================================================
# 3. StateMachine Tests
# ============================================================================

class TestStateMachine:
    """StateMachine — 状态转换校验，Manager 规则，边界"""

    @test("StateMachine: Waiting → Ready legal")
    def test_waiting_ready():
        assert StateMachine.validate_transition(
            NodeStatus.WAITING, None, NodeStatus.READY, None, "worker")

    @test("StateMachine: Ready → Running legal")
    def test_ready_running():
        assert StateMachine.validate_transition(
            NodeStatus.READY, None, NodeStatus.RUNNING, None, "worker")

    @test("StateMachine: Running → Completed legal")
    def test_running_completed():
        assert StateMachine.validate_transition(
            NodeStatus.RUNNING, None, NodeStatus.COMPLETED, None, "worker")

    @test("StateMachine: Running → Failed legal")
    def test_running_failed():
        assert StateMachine.validate_transition(
            NodeStatus.RUNNING, None, NodeStatus.FAILED, None, "worker")

    @test("StateMachine: Running → Stopped legal")
    def test_running_stopped():
        assert StateMachine.validate_transition(
            NodeStatus.RUNNING, None, NodeStatus.STOPPED, None, "worker")

    @test("StateMachine: Running → Paused legal")
    def test_running_paused():
        assert StateMachine.validate_transition(
            NodeStatus.RUNNING, None, NodeStatus.PAUSED, None, "worker")

    @test("StateMachine: Paused → Running legal")
    def test_paused_running():
        assert StateMachine.validate_transition(
            NodeStatus.PAUSED, None, NodeStatus.RUNNING, None, "worker")

    @test("StateMachine: Waiting → Stopped legal")
    def test_waiting_stopped():
        assert StateMachine.validate_transition(
            NodeStatus.WAITING, None, NodeStatus.STOPPED, None, "worker")

    @test("StateMachine: Failed → Ready legal (retry)")
    def test_failed_ready():
        assert StateMachine.validate_transition(
            NodeStatus.FAILED, None, NodeStatus.READY, None, "worker")

    @test("StateMachine: Stopped → Ready legal (restart)")
    def test_stopped_ready():
        assert StateMachine.validate_transition(
            NodeStatus.STOPPED, None, NodeStatus.READY, None, "worker")

    @test("StateMachine: Manager Running(expand) → Waiting legal")
    def test_manager_expand_waiting():
        assert StateMachine.validate_transition(
            NodeStatus.RUNNING, "expand", NodeStatus.WAITING, None, "manager")

    @test("StateMachine: Manager Running(review) → Running(expand) legal (iterate)")
    def test_manager_review_expand():
        assert StateMachine.validate_transition(
            NodeStatus.RUNNING, "review", NodeStatus.RUNNING, "expand", "manager")

    @test("StateMachine: Manager Running(expand) → Completed illegal")
    def test_manager_expand_completed_illegal():
        assert_raises(IllegalTransitionError,
                      StateMachine.validate_transition,
                      NodeStatus.RUNNING, "expand", NodeStatus.COMPLETED, None, "manager")

    @test("StateMachine: Waiting → Running illegal (must go through Ready)")
    def test_waiting_running_illegal():
        assert_raises(IllegalTransitionError,
                      StateMachine.validate_transition,
                      NodeStatus.WAITING, None, NodeStatus.RUNNING, None, "worker")

    @test("StateMachine: Failed → Running illegal (must go through Ready)")
    def test_failed_running_illegal():
        assert_raises(IllegalTransitionError,
                      StateMachine.validate_transition,
                      NodeStatus.FAILED, None, NodeStatus.RUNNING, None, "worker")

    @test("StateMachine: Stopped → Running illegal (must go through Ready)")
    def test_stopped_running_illegal():
        assert_raises(IllegalTransitionError,
                      StateMachine.validate_transition,
                      NodeStatus.STOPPED, None, NodeStatus.RUNNING, None, "worker")

    @test("StateMachine: Paused → Completed illegal")
    def test_paused_completed_illegal():
        assert_raises(IllegalTransitionError,
                      StateMachine.validate_transition,
                      NodeStatus.PAUSED, None, NodeStatus.COMPLETED, None, "worker")

    @test("StateMachine: Paused → Failed illegal")
    def test_paused_failed_illegal():
        assert_raises(IllegalTransitionError,
                      StateMachine.validate_transition,
                      NodeStatus.PAUSED, None, NodeStatus.FAILED, None, "worker")

    @test("StateMachine: Completed → Running illegal (terminal, non-reactive)")
    def test_completed_any_illegal():
        # 终态 COMPLETED → WAITING/READY 允许（路由重激活/原地重试）；
        # 但 COMPLETED → 非重激活态 RUNNING 仍被拒（屏障保留）
        assert_raises(IllegalTransitionError,
                      StateMachine.validate_transition,
                      NodeStatus.COMPLETED, None, NodeStatus.RUNNING, None, "worker")

    @test("StateMachine: Failed → Completed illegal (terminal)")
    def test_failed_completed_illegal():
        assert_raises(IllegalTransitionError,
                      StateMachine.validate_transition,
                      NodeStatus.FAILED, None, NodeStatus.COMPLETED, None, "worker")

    @test("StateMachine: Stopped → Completed illegal (terminal)")
    def test_stopped_completed_illegal():
        assert_raises(IllegalTransitionError,
                      StateMachine.validate_transition,
                      NodeStatus.STOPPED, None, NodeStatus.COMPLETED, None, "worker")

    @test("StateMachine: get_allowed_transitions for Worker Ready")
    def test_allowed_worker_ready():
        result = StateMachine.get_allowed_transitions(NodeStatus.READY, None, "worker")
        statuses = [r["to_status"] for r in result]
        assert NodeStatus.RUNNING in statuses
        assert NodeStatus.STOPPED in statuses
        assert NodeStatus.COMPLETED not in statuses

    @test("StateMachine: get_allowed_transitions for Manager WAITING")
    def test_allowed_manager_waiting():
        result = StateMachine.get_allowed_transitions(NodeStatus.WAITING, "waiting", "manager")
        statuses = [r["to_status"] for r in result]
        assert NodeStatus.READY in statuses
        assert NodeStatus.STOPPED in statuses

    @test("StateMachine: get_allowed_transitions for Manager Running(expand)")
    def test_allowed_manager_running_expand():
        result = StateMachine.get_allowed_transitions(NodeStatus.RUNNING, "expand", "manager")
        statuses = [r["to_status"] for r in result]
        assert NodeStatus.WAITING in statuses  # expand → waiting

    @test("StateMachine: get_allowed_transitions for Manager Running(review)")
    def test_allowed_manager_running_review():
        result = StateMachine.get_allowed_transitions(NodeStatus.RUNNING, "review", "manager")
        statuses = [r["to_status"] for r in result]
        assert NodeStatus.COMPLETED in statuses  # review → completed (via generic)
        assert NodeStatus.RUNNING in statuses  # review → expand (iterate)

    @test("StateMachine: get_allowed_transitions does not include Running→Running for Worker")
    def test_allowed_worker_no_self():
        result = StateMachine.get_allowed_transitions(NodeStatus.RUNNING, None, "worker")
        statuses = [r["to_status"] for r in result]
        assert NodeStatus.RUNNING not in statuses

    @test("StateMachine: Paused → Ready illegal")
    def test_paused_ready_illegal():
        assert_raises(IllegalTransitionError,
                      StateMachine.validate_transition,
                      NodeStatus.PAUSED, None, NodeStatus.READY, None, "worker")

    @test("StateMachine: Paused → Waiting illegal")
    def test_paused_waiting_illegal():
        assert_raises(IllegalTransitionError,
                      StateMachine.validate_transition,
                      NodeStatus.PAUSED, None, NodeStatus.WAITING, None, "worker")

    @test("StateMachine: Running → Waiting illegal for Worker")
    def test_running_waiting_worker_illegal():
        assert_raises(IllegalTransitionError,
                      StateMachine.validate_transition,
                      NodeStatus.RUNNING, None, NodeStatus.WAITING, None, "worker")

    @test("StateMachine: WAITING→READY for Manager (via waiting phase) legal")
    def test_manager_waiting_ready():
        assert StateMachine.validate_transition(
            NodeStatus.WAITING, "waiting", NodeStatus.READY, None, "manager")


# ============================================================================
# 4. Planner Tests
# ============================================================================

class TestPlanner:
    """Planner — ExecutionPlan 校验、翻译、重试"""

    @test("Planner: validate_plan valid plan returns empty errors")
    def test_validate_valid():
        g = Graph()
        p = Planner(g)
        plan = _make_plan()
        errors = p.validate_plan(plan)
        assert errors == []

    @test("Planner: validate_plan multi-step valid")
    def test_validate_multi():
        g = Graph()
        p = Planner(g)
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="worker", role="coder", task="t1", task_summary="t1 summary"),
            Step(step_id="s2", exec_type="manager", role="reviewer", task="t2", depends_on=["s1"], task_summary="t2 summary"),
        ])
        errors = p.validate_plan(plan)
        assert errors == []

    @test("Planner: validate_plan empty steps returns error")
    def test_validate_empty():
        g = Graph()
        p = Planner(g)
        plan = ExecutionPlan(steps=[])
        errors = p.validate_plan(plan)
        assert len(errors) > 0

    @test("Planner: validate_plan missing step_id returns error")
    def test_validate_missing_step_id():
        g = Graph()
        p = Planner(g)
        plan = ExecutionPlan(steps=[
            Step(step_id="", exec_type="worker", role="coder", task="t"),
        ])
        errors = p.validate_plan(plan)
        assert len(errors) > 0

    @test("Planner: validate_plan missing exec_type returns error")
    def test_validate_missing_exec_type():
        g = Graph()
        p = Planner(g)
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="", role="coder", task="t"),
        ])
        errors = p.validate_plan(plan)
        assert len(errors) > 0

    @test("Planner: validate_plan invalid exec_type returns error")
    def test_validate_invalid_exec_type():
        g = Graph()
        p = Planner(g)
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="invalid_type", role="coder", task="t"),
        ])
        errors = p.validate_plan(plan)
        assert len(errors) > 0

    @test("Planner: validate_plan missing role returns error")
    def test_validate_missing_role():
        g = Graph()
        p = Planner(g)
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="worker", role="", task="t"),
        ])
        errors = p.validate_plan(plan)
        assert len(errors) > 0

    @test("Planner: validate_plan missing task returns error")
    def test_validate_missing_task():
        g = Graph()
        p = Planner(g)
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="worker", role="coder", task=""),
        ])
        errors = p.validate_plan(plan)
        assert len(errors) > 0

    @test("Planner: validate_plan duplicate step_id returns error")
    def test_validate_duplicate():
        g = Graph()
        p = Planner(g)
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="worker", role="coder", task="t1"),
            Step(step_id="s1", exec_type="worker", role="coder", task="t2"),
        ])
        errors = p.validate_plan(plan)
        assert len(errors) > 0
        assert any("Duplicate" in e for e in errors)

    @test("Planner: validate_plan depends_on unknown step returns error")
    def test_validate_bad_dep():
        g = Graph()
        p = Planner(g)
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="worker", role="coder", task="t",
                 depends_on=["nonexistent"]),
        ])
        errors = p.validate_plan(plan)
        assert len(errors) > 0
        assert any("depends_on" in e for e in errors)

    @test("Planner: translate creates nodes and injects into Graph")
    def test_translate():
        g = Graph()
        g.add_node(_make_node("parent", exec_type="manager", status=NodeStatus.READY))
        p = Planner(g)
        plan = _make_plan()
        nodes = p.translate(plan, "parent")
        assert len(nodes) == 1
        assert g.get_node(nodes[0].id) is not None
        assert nodes[0].parent_id == "parent"

    @test("Planner: translate multi-step with dependencies")
    def test_translate_with_deps():
        g = Graph()
        g.add_node(_make_node("parent", exec_type="manager", status=NodeStatus.READY))
        p = Planner(g)
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="worker", role="coder", task="t1", task_summary="t1 summary"),
            Step(step_id="s2", exec_type="worker", role="coder", task="t2", depends_on=["s1"], task_summary="t2 summary"),
        ])
        nodes = p.translate(plan, "parent")
        assert len(nodes) == 2
        deps = g.dependencies(nodes[1].id)
        assert len(deps) == 1
        assert deps[0].id == nodes[0].id

    @test("Planner: translate with iteration_count appends suffix")
    def test_translate_iteration():
        g = Graph()
        g.add_node(_make_node("parent", exec_type="manager", status=NodeStatus.READY))
        p = Planner(g)
        plan = _make_plan()
        nodes = p.translate(plan, "parent", iteration_count=2)
        assert nodes[0].step_ref == "s1_v2"

    @test("Planner: translate invalid plan raises PlanValidationError")
    def test_translate_invalid():
        g = Graph()
        g.add_node(_make_node("parent", exec_type="manager", status=NodeStatus.READY))
        p = Planner(g)
        plan = ExecutionPlan(steps=[])
        assert_raises(PlanValidationError, p.translate, plan, "parent")

    @test("Planner: translate preserves config on nodes")
    def test_translate_config():
        g = Graph()
        g.add_node(_make_node("parent", exec_type="manager", status=NodeStatus.READY))
        p = Planner(g)
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="worker", role="coder", task="t",
                 config={"num_ccs": 4}, task_summary="t summary"),
        ])
        nodes = p.translate(plan, "parent")
        assert nodes[0].config.get("num_ccs") == 4

    @test("Planner: create_retry_node inherits properties")
    def test_create_retry():
        g = Graph()
        g.add_node(_make_node("parent", exec_type="manager", status=NodeStatus.READY))
        p = Planner(g)
        original = _make_node("orig", exec_type="worker", status=NodeStatus.FAILED,
                              parent_id="parent")
        g.add_node(original)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            retry = p.create_retry_node(original, reason="test retry")
        assert retry.parent_id == original.parent_id
        assert retry.exec_type == original.exec_type
        assert "[RETRY:" in retry.task
        assert retry.status == NodeStatus.WAITING

    @test("Planner: create_retry_node preserves dependencies")
    def test_retry_preserves_deps():
        g = Graph()
        g.add_node(_make_node("parent", exec_type="manager", status=NodeStatus.READY))
        g.add_node(_make_node("dep", status=NodeStatus.COMPLETED))
        g.add_node(_make_node("orig", exec_type="worker", status=NodeStatus.FAILED,
                              parent_id="parent"))
        p = Planner(g)
        g.add_edge(_make_edge("e1", EdgeType.DEPENDENCY, "dep", "orig"))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            retry = p.create_retry_node(g.get_node("orig"), reason="retry")
        retry_deps = g.dependencies(retry.id)
        assert len(retry_deps) == 1
        assert retry_deps[0].id == "dep"

    @test("Planner: create_retry_node with iteration appends suffix")
    def test_retry_suffix():
        g = Graph()
        g.add_node(_make_node("parent", exec_type="manager", status=NodeStatus.READY))
        original = _make_node("orig", exec_type="worker", status=NodeStatus.FAILED,
                              parent_id="parent")
        original.iteration_count = 3
        original.step_ref = "step_1"
        g.add_node(original)
        p = Planner(g)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            retry = p.create_retry_node(original, reason="retry")
        assert "_v3" in retry.step_ref

    @test("Planner: restore loads nodes/edges into Graph")
    def test_restore():
        g = Graph()
        p = Planner(g)
        nodes = [_make_node("n1", status=NodeStatus.COMPLETED)]
        edges = []
        p.restore(nodes, edges)
        assert g.get_node("n1") is not None
        assert g.get_node("n1").status == NodeStatus.COMPLETED

    @test("Planner: validate_plan manager exec_type is valid")
    def test_validate_manager_type():
        g = Graph()
        p = Planner(g)
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="manager", role="architect", task="design", task_summary="design summary"),
        ])
        errors = p.validate_plan(plan)
        assert errors == []


# ============================================================================
# 5. Persistence Tests
# ============================================================================

class TestPersistence:
    """Persistence — 原子写盘、双文件容错、周期性保存"""

    @test("Persistence: save → load round-trip")
    def test_save_load():
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            g = Graph()
            g.add_node(_make_node("n1", exec_type="manager", status=NodeStatus.RUNNING))
            pers = Persistence(path)
            pers.save(g)
            loaded = pers.load()
            assert loaded is not None
            assert "n1" in loaded.nodes
            assert loaded.nodes["n1"].exec_type == "manager"
        finally:
            _cleanup_file(path)

    @test("Persistence: clear removes files")
    def test_clear():
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            g = Graph()
            g.add_node(_make_node("n1"))
            pers = Persistence(path)
            pers.save(g)
            assert os.path.exists(path)
            pers.clear()
            assert not os.path.exists(path)
        finally:
            _cleanup_file(path)

    @test("Persistence: load from empty path returns None")
    def test_load_empty():
        pers = Persistence("/tmp/nonexistent_file_xyz.json")
        result = pers.load()
        assert result is None

    @test("Persistence: dual-file fallback from corrupted .json to .tmp")
    def test_dual_fallback():
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        tmp_path = path + ".tmp"
        try:
            # Write corrupted primary file
            with open(path, "w") as f:
                f.write("NOT VALID JSON")
            # Write valid .tmp backup
            g = Graph()
            g.add_node(_make_node("n1"))
            pers = Persistence(path)
            pers.save(g)  # This writes to .tmp then renames
            # Now corrupt primary again and save .tmp only
            with open(path, "w") as f:
                f.write("NOT VALID JSON")
            # Manually create .tmp
            with open(tmp_path, "w") as f:
                json.dump({"graph_version": 5, "nodes": {}, "edges": {}, "crashed": []}, f)
            result = pers.load()
            assert result is not None
            assert result.graph_version == 5
        finally:
            _cleanup_file(path)
            _cleanup_file(tmp_path)

    @test("Persistence: both files corrupted returns None")
    def test_both_corrupted():
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        tmp_path = path + ".tmp"
        try:
            with open(path, "w") as f:
                f.write("corrupted")
            with open(tmp_path, "w") as f:
                f.write("corrupted too")
            pers = Persistence(path)
            result = pers.load()
            assert result is None
        finally:
            _cleanup_file(path)
            _cleanup_file(tmp_path)

    @test("Persistence: periodic_save starts and auto-saves")
    def test_periodic_save():
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            g = Graph()
            g.add_node(_make_node("n1"))
            pers = Persistence(path)
            pers.start_periodic_save(g, interval=0.3)
            time.sleep(0.5)
            loaded = pers.load()
            pers.stop_periodic_save()
            assert loaded is not None
            assert "n1" in loaded.nodes
        finally:
            _cleanup_file(path)

    @test("Persistence: stop_periodic_save stops timer")
    def test_stop_periodic():
        pers = Persistence("/tmp/test_stop.json")
        g = Graph()
        g.add_node(_make_node("n1"))
        pers.start_periodic_save(g, interval=0.3)
        assert pers._timer is not None
        pers.stop_periodic_save()
        assert pers._timer is None

    @test("Persistence: save with empty graph")
    def test_save_empty():
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            g = Graph()
            pers = Persistence(path)
            pers.save(g)
            loaded = pers.load()
            assert loaded is not None
            assert len(loaded.nodes) == 0
        finally:
            _cleanup_file(path)

    @test("Persistence: crashed nodes list in snapshot")
    def test_snapshot_crashed_persist():
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            g = Graph()
            g.add_node(_make_node("running1", status=NodeStatus.RUNNING))
            g.add_node(_make_node("completed1", status=NodeStatus.COMPLETED))
            pers = Persistence(path)
            pers.save(g)
            loaded = pers.load()
            assert loaded is not None
            assert len(loaded.crashed) == 1
            assert loaded.crashed[0].id == "running1"
        finally:
            _cleanup_file(path)

    @test("Persistence: mark_clean_if prevents dirty clear on version mismatch")
    def test_mark_clean_if_version():
        g = Graph()
        g.add_node(_make_node("n1"))
        g.mark_clean()
        assert not g.is_dirty
        g.add_node(_make_node("n2"))  # bumps version
        v1 = g.graph_version
        g.mark_clean_if(v1 - 1)  # wrong version
        assert g.is_dirty


# ============================================================================
# 6. EventBus Tests
# ============================================================================

class TestEventBus:
    """EventBus — 发布/订阅、通配符、线程安全"""

    @test("EventBus: publish delivers to matching subscriber")
    def test_publish():
        bus = EventBus()
        received = []
        bus.subscribe("test.event", lambda e: received.append(e))
        event = SSEEvent(event_type="test.event", sequence_id=0, timestamp=time.time(), payload={"k": "v"})
        bus.publish(event)
        assert len(received) == 1
        assert received[0].event_type == "test.event"

    @test("EventBus: wildcard * matches all")
    def test_wildcard_all():
        bus = EventBus()
        received = []
        bus.subscribe("*", lambda e: received.append(e))
        bus.publish(SSEEvent("any.event", 0, time.time(), {}))
        bus.publish(SSEEvent("another.event", 0, time.time(), {}))
        assert len(received) == 2

    @test("EventBus: prefix.* matches prefix events")
    def test_prefix_wildcard():
        bus = EventBus()
        received = []
        bus.subscribe("node.*", lambda e: received.append(e))
        bus.publish(SSEEvent("node.added", 0, time.time(), {}))
        bus.publish(SSEEvent("node.removed", 0, time.time(), {}))
        bus.publish(SSEEvent("other.event", 0, time.time(), {}))
        assert len(received) == 2

    @test("EventBus: unsubscribe removes subscription")
    def test_unsubscribe():
        bus = EventBus()
        received = []
        sid = bus.subscribe("*", lambda e: received.append(e))
        bus.unsubscribe(sid)
        bus.publish(SSEEvent("test", 0, time.time(), {}))
        assert len(received) == 0

    @test("EventBus: sequence_id increments monotonically")
    def test_sequence_id():
        bus = EventBus()
        events = []
        bus.subscribe("*", lambda e: events.append(e))
        bus.publish(SSEEvent("e1", 0, time.time(), {}))
        bus.publish(SSEEvent("e2", 0, time.time(), {}))
        assert events[0].sequence_id == 1
        assert events[1].sequence_id == 2

    @test("EventBus: get_last_sequence_id returns latest")
    def test_last_seq():
        bus = EventBus()
        assert bus.get_last_sequence_id() == 0
        bus.publish(SSEEvent("e1", 0, time.time(), {}))
        assert bus.get_last_sequence_id() == 1

    @test("EventBus: subscriber exception isolation")
    def test_exception_isolation():
        bus = EventBus()
        good_received = []
        bus.subscribe("*", lambda e: (_ for _ in ()).throw(ValueError("bad handler")))
        bus.subscribe("*", lambda e: good_received.append(e))
        bus.publish(SSEEvent("test", 0, time.time(), {}))
        assert len(good_received) == 1

    @test("EventBus: exact match does not match different event")
    def test_exact_match():
        bus = EventBus()
        received = []
        bus.subscribe("node.added", lambda e: received.append(e))
        bus.publish(SSEEvent("node.updated", 0, time.time(), {}))
        assert len(received) == 0

    @test("EventBus: multiple subscribers all receive event")
    def test_multiple_subscribers():
        bus = EventBus()
        r1, r2 = [], []
        bus.subscribe("*", lambda e: r1.append(e))
        bus.subscribe("*", lambda e: r2.append(e))
        bus.publish(SSEEvent("test", 0, time.time(), {}))
        assert len(r1) == 1
        assert len(r2) == 1

    @test("EventBus: unsubscribe nonexistent ID is no-op")
    def test_unsubscribe_nonexistent():
        bus = EventBus()
        bus.unsubscribe(99999)  # should not raise


# ============================================================================
# 7. SSEManager Tests
# ============================================================================

class TestSSEManager:
    """SSEManager — 环形缓冲区、微批合并、快照"""

    @test("SSEManager: start subscribes to EventBus")
    def test_start():
        bus = EventBus()
        mgr = SSEManager(bus)
        mgr.start()
        assert mgr._subscriber_id is not None
        mgr.stop()

    @test("SSEManager: events are stored in buffer")
    def test_event_storage():
        bus = EventBus()
        mgr = SSEManager(bus)
        mgr.start()
        bus.publish(SSEEvent("test.event", 0, time.time(), {"k": "v"}))
        time.sleep(0.05)
        events = mgr.get_events_since(0)
        assert len(events) >= 1
        assert events[0].event_type == "test.event"
        mgr.stop()

    @test("SSEManager: format_sse produces correct format")
    def test_format_sse():
        event = SSEEvent("node_added", 42, time.time(), {"node_id": "n1"})
        output = format_sse(event)
        assert "event: node_added" in output
        assert "id: 42" in output
        assert "data:" in output

    @test("SSEManager: get_stats returns correct info")
    def test_get_stats():
        bus = EventBus()
        mgr = SSEManager(bus)
        mgr.start()
        bus.publish(SSEEvent("test.event", 0, time.time(), {}))
        time.sleep(0.05)
        stats = mgr.get_stats()
        assert stats["total_events"] >= 1
        assert stats["latest_sequence_id"] >= 1
        mgr.stop()

    @test("SSEManager: get_events_since returns empty for future seq")
    def test_events_since_future():
        bus = EventBus()
        mgr = SSEManager(bus)
        mgr.start()
        bus.publish(SSEEvent("e1", 0, time.time(), {}))
        time.sleep(0.05)
        events = mgr.get_events_since(99999)
        assert events == []
        mgr.stop()

    @test("SSEManager: get_snapshot_event generates graph snapshot")
    def test_snapshot_event():
        bus = EventBus()
        mgr = SSEManager(bus)
        # Mock runtime with graph
        class MockRuntime:
            class MockGraph:
                def __init__(self):
                    self.nodes = {}
                    self.edges = {}
                def get_stats(self):
                    return {"total": 0}
                def all_nodes(self):
                    return list(self.nodes.values())
                def all_edges(self):
                    return list(self.edges.values())
            graph = MockGraph()
        runtime = MockRuntime()
        event = mgr.get_snapshot_event(runtime)
        assert event.event_type == "graph_updated"
        assert "nodes" in event.payload
        assert "edges" in event.payload
        assert "stats" in event.payload


# ============================================================================
# 8. ExecutableRegistry Tests
# ============================================================================

class TestExecutableRegistry:
    """ExecutableRegistry — 注册/创建/类型管理"""

    @test("Registry: register and create executable")
    def test_register_create():
        class TestEx(Executable):
            def run(self, ctx): return NodeStatus.COMPLETED
            def stop(self, ctx): pass
            def pause(self, ctx): pass
            def resume(self, ctx): return NodeStatus.RUNNING
            def cleanup_resources(self, ctx): pass
        reg = ExecutableRegistry()
        reg.register("test_type", TestEx)
        ctx = ExecutableContext(node=_make_node(), runtime=None)
        instance = reg.create("test_type", ctx)
        assert isinstance(instance, TestEx)

    @test("Registry: get_types returns registered types")
    def test_get_types():
        reg = ExecutableRegistry()
        assert "manager" not in reg.get_types()
        reg.register("custom", type("CE", (Executable,), {
            "run": lambda s, c: NodeStatus.COMPLETED,
            "stop": lambda s, c: None,
            "pause": lambda s, c: None,
            "resume": lambda s, c: NodeStatus.RUNNING,
            "cleanup_resources": lambda s, c: None,
        }))
        assert "custom" in reg.get_types()

    @test("Registry: unregister removes type")
    def test_unregister():
        reg = ExecutableRegistry()
        reg.unregister("manager")
        assert "manager" not in reg.get_types()


# ============================================================================
# 9. Integration Tests
# ============================================================================

class TestIntegration:
    """Integration — 跨模块数据流场景"""

    @test("Integration: Planner → Graph → query consistency")
    def test_planner_graph_consistency():
        g = Graph()
        g.add_node(_make_node("parent", exec_type="manager", status=NodeStatus.READY))
        p = Planner(g)
        plan = ExecutionPlan(steps=[
            Step(step_id="s1", exec_type="worker", role="coder", task="t1", task_summary="t1 summary"),
            Step(step_id="s2", exec_type="worker", role="coder", task="t2", depends_on=["s1"], task_summary="t2 summary"),
        ])
        nodes = p.translate(plan, "parent")
        # Graph query consistency
        assert len(g.children("parent")) == 2
        assert len(g.dependencies(nodes[1].id)) == 1
        assert g.dependencies(nodes[0].id) == []
        assert g.dependents(nodes[0].id)[0].id == nodes[1].id

    @test("Integration: StateMachine + Node dual barrier")
    def test_dual_barrier():
        # 终态 → WAITING/READY 允许（路由重激活/原地重试），两层保持一致
        n = _make_node("n1", status=NodeStatus.COMPLETED)
        n.status = NodeStatus.READY  # Node.__setattr__ 不应抛 ValueError
        assert StateMachine.validate_transition(
            NodeStatus.COMPLETED, None, NodeStatus.READY, None, "worker")
        # 终态 → 非重激活态仍被双层屏障拒绝
        n2 = _make_node("n2", status=NodeStatus.COMPLETED)
        assert_raises(ValueError, lambda: setattr(n2, "status", NodeStatus.RUNNING))
        assert_raises(IllegalTransitionError,
                      StateMachine.validate_transition,
                      NodeStatus.COMPLETED, None, NodeStatus.RUNNING, None, "worker")

    @test("Integration: Persistence save → load → Graph restore")
    def test_persistence_graph_restore():
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            # Create graph with data
            g = Graph()
            g.add_node(_make_node("n1", status=NodeStatus.COMPLETED))
            g.add_node(_make_node("n2", exec_type="manager", status=NodeStatus.WAITING,
                                  parent_id="n1"))
            # Save
            pers = Persistence(path)
            pers.save(g)
            # Load
            snapshot = pers.load()
            assert snapshot is not None
            # Restore into new graph
            g2 = Graph()
            from runtime.planner import Planner
            p2 = Planner(g2)
            p2.restore(list(snapshot.nodes.values()), list(snapshot.edges.values()))
            assert g2.get_node("n1") is not None
            assert g2.get_node("n2") is not None
            assert g2.get_node("n1").status == NodeStatus.COMPLETED
        finally:
            _cleanup_file(path)

    @test("Integration: EventBus → SSEManager pipeline")
    def test_eventbus_sse_pipeline():
        bus = EventBus()
        mgr = SSEManager(bus)
        mgr.start()
        bus.publish(SSEEvent("node_added", 0, time.time(), {"node_id": "n1"}))
        time.sleep(0.05)
        events = mgr.get_events_since(0)
        assert len(events) >= 1
        assert events[0].event_type == "node_added"
        assert events[0].payload["node_id"] == "n1"
        mgr.stop()

    @test("Integration: Graph mutation → EventBus bridge")
    def test_graph_event_bridge():
        bus = EventBus()
        g = Graph()
        # Wire up mutation → EventBus
        received = []
        bus.subscribe("node_added", lambda e: received.append(e))
        bus.subscribe("edge_added", lambda e: received.append(e))
        # Set up mutation bridge
        def bridge(event_type, payload):
            if event_type == "add_node":
                bus.publish(SSEEvent("node_added", 0, time.time(), payload))
            elif event_type == "add_edge":
                bus.publish(SSEEvent("edge_added", 0, time.time(), payload))
        g.set_on_mutation(bridge)
        # Trigger mutations
        g.add_node(_make_node("n1"))
        assert len(received) >= 1
        assert received[0].event_type == "node_added"

    @test("Integration: ExecutableRegistry creates and runs stub Executable")
    def test_registry_executable():
        class StubExec(Executable):
            def run(self, ctx):
                return NodeStatus.COMPLETED
            def stop(self, ctx): pass
            def pause(self, ctx): pass
            def resume(self, ctx): return NodeStatus.RUNNING
            def cleanup_resources(self, ctx): pass
        reg = ExecutableRegistry()
        reg.register("stub", StubExec)
        ctx = ExecutableContext(node=_make_node("test_node"), runtime=None)
        ex = reg.create("stub", ctx)
        result = ex.run(ctx)
        assert result == NodeStatus.COMPLETED

    @test("Integration: Graph stats with mixed node statuses")
    def test_graph_stats_mixed():
        g = Graph()
        for status in [NodeStatus.RUNNING, NodeStatus.COMPLETED,
                       NodeStatus.RUNNING, NodeStatus.FAILED, NodeStatus.WAITING]:
            g.add_node(_make_node(status=status))
        stats = g.get_stats()
        assert stats.get("Running") == 2
        assert stats.get("Completed") == 1
        assert stats.get("Failed") == 1
        assert stats.get("Waiting") == 1

    @test("Integration: Planner translate + retry preserves graph integrity")
    def test_planner_retry_integrity():
        g = Graph()
        g.add_node(_make_node("parent", exec_type="manager", status=NodeStatus.READY))
        g.add_node(_make_node("dep_node", status=NodeStatus.COMPLETED))
        p = Planner(g)
        original = _make_node("orig", exec_type="worker", status=NodeStatus.FAILED,
                              parent_id="parent")
        g.add_node(original)
        g.add_edge(_make_edge("e1", EdgeType.DEPENDENCY, "dep_node", "orig"))
        # Create retry (deprecated API, but verify backward compat)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            retry = p.create_retry_node(original, reason="failure detected")
        assert retry.parent_id == "parent"
        assert retry.exec_type == "worker"
        # Dependency preserved
        retry_deps = g.dependencies(retry.id)
        assert len(retry_deps) == 1
        # Graph has original + retry
        assert g.get_node("orig") is not None
        assert g.get_node(retry.id) is not None

    @test("Integration: StateMachine + Scheduler-compatible transition set")
    def test_scheduler_transition_set():
        """Verify all transitions used by Scheduler are legal."""
        # Scheduler uses: WAITING→READY, READY→RUNNING, RUNNING→COMPLETED/FAILED
        assert StateMachine.validate_transition(NodeStatus.WAITING, None, NodeStatus.READY, None, "worker")
        assert StateMachine.validate_transition(NodeStatus.READY, None, NodeStatus.RUNNING, None, "worker")
        assert StateMachine.validate_transition(NodeStatus.RUNNING, None, NodeStatus.COMPLETED, None, "worker")
        assert StateMachine.validate_transition(NodeStatus.RUNNING, None, NodeStatus.FAILED, None, "worker")

    @test("Integration: ErrorObject → Node error round-trip")
    def test_error_roundtrip():
        error = ErrorObject.create_worker_execution_error()
        n = _make_node("err_node", status=NodeStatus.FAILED)
        n.error = error
        json_str = n.to_json()
        n2 = Node.from_json(json_str)
        assert n2.error is not None
        assert n2.error.message == "Worker execution failed"
        assert n2.error.cause is not None


# ============================================================================
# Utilities
# ============================================================================

def _cleanup_file(path):
    """Remove a file if it exists."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


import warnings  # noqa: E402 (needed for Step task truncation test)


# ============================================================================
# Main — Run all tests
# ============================================================================

def main():
    _reset_counts()

    # Collect all test methods from test classes
    test_cases = []
    for cls in [TestModels, TestGraph, TestStateMachine, TestPlanner,
                TestPersistence, TestEventBus, TestSSEManager,
                TestExecutableRegistry, TestIntegration]:
        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if hasattr(attr, "_test_name"):
                test_cases.append(attr)

    total = len(test_cases)

    print("=" * 70)
    print("  v4 Integrated System — Test Suite")
    print(f"  {total} test cases collected")
    print("=" * 70)
    print()

    # Group by module
    current_module = None
    module_tests = {}
    for fn in test_cases:
        # Extract module name from test name prefix (e.g. "Models:" from "Models: Node...")
        test_name = fn._test_name
        module = test_name.split(":")[0].strip()
        if module not in module_tests:
            module_tests[module] = []
        module_tests[module].append(fn)

    for module, tests in module_tests.items():
        print(f"[{module}]")
        for fn in tests:
            fn()
        print()

    # Summary
    duration = time.time() - _test_start_time
    print("=" * 70)
    print(f"  Results: {_passed} passed, {_failed} failed, {total} total")
    print(f"  Duration: {duration:.2f}s")
    print("=" * 70)

    if _errors:
        print()
        print("FAILURES:")
        print("-" * 70)
        for err in _errors:
            print(f"  [{err['test']}]")
            print(f"    Error: {err['error']}")
            print(f"    Traceback (short):")
            tb_lines = err['traceback'].split('\n')
            # Print last 5 lines of traceback
            for line in tb_lines[-7:]:
                print(f"      {line}")
            print()

    # Write test report
    _write_report(total, duration)

    return 0 if _failed == 0 else 1


def _write_report(total: int, duration: float):
    """Generate test_report.md."""
    lines = []
    lines.append("# Test Report — v4 Integrated Test Suite")
    lines.append("")
    lines.append(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**Duration**: {duration:.2f}s")
    lines.append(f"**Python**: {sys.version.split()[0]}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Total Test Cases | {total} |")
    lines.append(f"| Passed | {_passed} |")
    lines.append(f"| Failed | {_failed} |")
    lines.append("")

    # Module coverage summary
    lines.append("## Module Coverage Summary")
    lines.append("")
    lines.append("| Module | Tests | Status |")
    lines.append("|--------|-------|--------|")
    modules = ["Models", "Graph", "StateMachine", "Planner", "Persistence",
               "EventBus", "SSEManager", "Registry", "Integration"]
    for mod in modules:
        mod_fns = []
        for _cls in [TestModels, TestGraph, TestStateMachine, TestPlanner,
                     TestPersistence, TestEventBus, TestSSEManager,
                     TestExecutableRegistry, TestIntegration]:
            for a in dir(_cls):
                fn = getattr(_cls, a)
                if hasattr(fn, "_test_name") and fn._test_name.startswith(mod):
                    mod_fns.append(fn)
        count = len(mod_fns)
        failed_in_mod = sum(1 for e in _errors if e["test"].startswith(mod))
        status = "PASS" if failed_in_mod == 0 else f"FAIL ({failed_in_mod})"
        lines.append(f"| {mod} | {count} | {status} |")
    lines.append("")

    # Coverage analysis
    lines.append("## Coverage Analysis")
    lines.append("")
    lines.append("### Key Coverage Highlights")
    lines.append("")
    lines.append("- **Normal paths**: All core workflows covered (serialization, CRUD, transitions)")
    lines.append("- **Boundary values**: Empty lists, 0 values, min/max ranges (num_ccs: 1-10, audit_rounds: 0-10, task length: 5000)")
    lines.append("- **Exception paths**: ValueError, IllegalTransitionError, PlanValidationError, DependencyError")
    lines.append("- **Integration**: Cross-module data flow (Planner→Graph, EventBus→SSEManager, StateMachine+Node)")
    lines.append("")

    # Defect log
    lines.append("## Defect Log")
    lines.append("")
    lines.append("| ID | Module | Description | Severity | Status |")
    lines.append("|----|--------|-------------|----------|--------|")
    for i, err in enumerate(_errors, 1):
        mod = err["test"].split(":")[0].strip()
        lines.append(f"| {i} | {mod} | {err['test']}: {err['error']} | FAIL | Open |")
    if not _errors:
        lines.append("| — | — | No defects found | — | — |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*Report generated by run_tests.py*")
    lines.append("")

    report_path = os.path.join(_SCRIPT_DIR, "test_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Test report written to: {report_path}")


if __name__ == "__main__":
    sys.exit(main())
