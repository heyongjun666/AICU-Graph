"""Regression coverage for the approved runtime/session-manager fixes."""

from __future__ import annotations

import threading

import pytest

from executables.registry import ExecutableRegistry
from runtime.graph import Graph
from runtime.models import Edge, EdgeType, Node, NodeStatus
from runtime.persistence import Persistence
from runtime.runtime import Runtime
from runtime.session_manager import SessionManager


def _node(node_id: str, *, parent_id: str | None, role: str, status: NodeStatus) -> Node:
    return Node(
        id=node_id,
        parent_id=parent_id,
        exec_type="worker",
        role=role,
        task=node_id,
        config={},
        status=status,
        step_ref=node_id,
    )


def _edge(edge_id: str, edge_type: EdgeType, source: str, target: str) -> Edge:
    return Edge(id=edge_id, type=edge_type, from_node=source, to_node=target)


def test_fix_role_replacement_reroutes_both_dependency_directions(tmp_path):
    graph = Graph()
    graph.add_node(Node(
        id="root", parent_id=None, exec_type="manager", role="manager",
        task="root", config={}, status=NodeStatus.READY, manager_phase="review",
    ))
    graph.add_node(_node("upstream", parent_id="root", role="coder", status=NodeStatus.COMPLETED))
    graph.add_node(_node("old", parent_id="root", role="coder", status=NodeStatus.FAILED))
    graph.add_node(_node("downstream", parent_id="root", role="coder", status=NodeStatus.WAITING))
    for edge in (
        _edge("p-up", EdgeType.PARENT, "root", "upstream"),
        _edge("p-old", EdgeType.PARENT, "root", "old"),
        _edge("p-down", EdgeType.PARENT, "root", "downstream"),
        _edge("d-in", EdgeType.DEPENDENCY, "upstream", "old"),
        _edge("d-out", EdgeType.DEPENDENCY, "old", "downstream"),
    ):
        graph.add_edge(edge)

    runtime = Runtime(
        graph=graph,
        persistence=Persistence(str(tmp_path / "graph.json")),
        workspace_root=str(tmp_path),
    )
    try:
        replacements = runtime.submit_fix_plan(
            "root",
            [{
                "step_ref": "old",
                "reason": "needs specialist",
                "fix": {
                    "role": "tester",
                    "task": "repair output",
                    "task_summary": "repair output",
                },
            }],
            iteration_count=2,
        )
        replacement = replacements[0]

        assert graph.get_node("old").status == NodeStatus.STOPPED
        assert graph.get_node("old").parent_id == "root"
        assert replacement.parent_id == "root"
        assert replacement.role == "tester"
        assert {node.id for node in graph.dependencies(replacement.id)} == {"upstream"}
        assert {node.id for node in graph.dependents(replacement.id)} == {"downstream"}
        assert graph.dependencies("old") == []
        assert graph.dependents("old") == []
        assert {node.id for node in graph.children("root")} == {
            "upstream", "old", "downstream", replacement.id,
        }
    finally:
        runtime.shutdown()


def test_global_config_propagation_is_serialized_without_manager_lock(tmp_path):
    manager = SessionManager(
        registry=ExecutableRegistry(),
        root_dir=str(tmp_path / "sessions"),
        workspace_root=str(tmp_path / "workspace"),
        restore=False,
    )
    try:
        session = manager.create_session("concurrency")
        handle = manager.get_session(session["id"])
        original = handle.runtime.set_max_concurrency
        entered = threading.Event()
        release = threading.Event()
        seen: list[int] = []

        def setter(value: int) -> None:
            # This call must be able to acquire SessionManager._lock while the
            # global update is in its propagation phase.
            manager.get_session(handle.metadata.id)
            seen.append(value)
            entered.set()
            release.wait(2)
            original(value)

        handle.runtime.set_max_concurrency = setter
        first_done = threading.Event()

        def first_update() -> None:
            manager.update_global_config({"max_concurrency": 6})
            first_done.set()

        thread = threading.Thread(target=first_update)
        thread.start()
        assert entered.wait(2)

        second_done = threading.Event()

        def second_update() -> None:
            manager.update_global_config({"max_concurrency": 7})
            second_done.set()

        second = threading.Thread(target=second_update)
        second.start()
        # The dedicated config lock keeps the second patch behind propagation
        # rather than racing with the first runtime setter.
        assert not second_done.wait(0.1)
        release.set()
        thread.join(2)
        second.join(2)
        assert first_done.is_set() and second_done.is_set()
        assert seen == [6, 7]
        assert manager.get_global_config()["max_concurrency"] == 7
    finally:
        manager.shutdown()


def test_constructor_concurrency_is_used_when_config_has_no_persisted_limit(tmp_path):
    manager = SessionManager(
        registry=ExecutableRegistry(),
        root_dir=str(tmp_path / "sessions"),
        workspace_root=str(tmp_path / "workspace"),
        max_concurrency=6,
        restore=False,
    )
    try:
        assert manager.get_global_config()["max_concurrency"] == 6
        session = manager.create_session("fallback")
        assert manager.get_session(session["id"]).runtime.max_concurrency == 6
    finally:
        manager.shutdown()


def test_global_config_rolls_back_when_persistence_fails(tmp_path, monkeypatch):
    manager = SessionManager(
        registry=ExecutableRegistry(),
        root_dir=str(tmp_path / "sessions"),
        workspace_root=str(tmp_path / "workspace"),
        restore=False,
    )
    try:
        before = manager.get_global_config()

        def fail_save() -> None:
            raise OSError("disk full")

        monkeypatch.setattr(manager, "_save_global_config", fail_save)
        with pytest.raises(OSError, match="disk full"):
            manager.update_global_config({"max_concurrency": 6, "language": "en"})
        assert manager.get_global_config() == before
    finally:
        manager.shutdown()
