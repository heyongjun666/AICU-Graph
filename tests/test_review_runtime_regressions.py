"""Regression coverage for the approved runtime/session-manager fixes."""

from __future__ import annotations

import pytest

from runtime.graph import Graph
from runtime.models import Edge, EdgeType, Node, NodeStatus
from runtime.persistence import Persistence
from runtime.runtime import Runtime


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
