"""Persistence compatibility tests for historical and forward-versioned graphs."""

import json

import pytest

from runtime.graph import Graph
from runtime.models import Edge, EdgeType, Node, NodeStatus
from runtime.persistence import Persistence


def _node_payload(**overrides):
    node = Node(
        id="n1",
        exec_type="worker",
        role="code",
        task="Inspect the project",
        config={},
        status=NodeStatus.COMPLETED,
    )
    payload = json.loads(node.to_json())
    payload.update(overrides)
    return payload


def test_node_from_json_ignores_unknown_and_legacy_fields():
    payload = _node_payload(
        legacy_delivery_marker="validated",
        future_scheduler_hint={"priority": 10},
    )

    with pytest.warns(RuntimeWarning, match="unknown persisted Node fields"):
        restored = Node.from_json(json.dumps(payload))

    assert restored.id == "n1"
    assert restored.status is NodeStatus.COMPLETED
    assert not hasattr(restored, "legacy_delivery_marker")
    assert not hasattr(restored, "future_scheduler_hint")


def test_node_from_json_preserves_current_fields_that_may_be_removed_later():
    payload = _node_payload(
        max_activations=7,
        pending_routing=None,
    )

    restored = Node.from_json(json.dumps(payload))

    assert restored.max_activations == 7
    assert restored.pending_routing is None


def test_node_from_json_missing_required_field_fails_explicitly():
    payload = _node_payload()
    del payload["id"]

    with pytest.raises(TypeError, match="missing.*id|required positional argument"):
        Node.from_json(json.dumps(payload))


def test_persistence_restores_graph_snapshot_with_legacy_fields(tmp_path):
    graph = Graph()
    node = Node(
        id="root",
        exec_type="manager",
        role="manager",
        task="Build feature",
        config={},
        status=NodeStatus.WAITING,
        manager_phase="waiting",
    )
    child = Node(
        id="child",
        exec_type="worker",
        role="code",
        task="Implement feature",
        config={},
        status=NodeStatus.COMPLETED,
        parent_id="root",
    )
    graph.add_node(node)
    graph.add_node(child)
    graph.add_edge(Edge(
        id="edge-root-child",
        type=EdgeType.PARENT,
        from_node="root",
        to_node="child",
    ))

    persistence = Persistence(file_path=str(tmp_path / "graph.json"))
    persistence.save(graph)

    raw = json.loads((tmp_path / "graph.json").read_text(encoding="utf-8"))
    raw["nodes"]["root"]["removed_from_future_node_model"] = True
    (tmp_path / "graph.json").write_text(
        json.dumps(raw), encoding="utf-8"
    )

    with pytest.warns(RuntimeWarning, match="unknown persisted Node fields"):
        restored = persistence.load()

    assert restored is not None
    assert set(restored.nodes) == {"root", "child"}
    assert restored.nodes["root"].manager_phase == "waiting"
    assert len(restored.edges) == 1
    assert restored.edges["edge-root-child"].type is EdgeType.PARENT


def test_persistence_rejects_missing_required_node_field(tmp_path):
    payload = {
        "nodes": {"n1": _node_payload()},
        "edges": {},
        "crashed": [],
        "graph_version": 1,
    }
    del payload["nodes"]["n1"]["task"]
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert Persistence(file_path=str(path)).load() is None
