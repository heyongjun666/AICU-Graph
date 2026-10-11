"""Tests for runtime/server.py — FastAPI HTTP REST API + SSE endpoints."""

import json
import time
from unittest.mock import ANY

import pytest
from fastapi.testclient import TestClient

from runtime.server import create_app
from runtime.sse_manager import SSEManager, format_sse


# ============================================================================
# TestGraphEndpoints
# ============================================================================

class TestGraphEndpoints:
    """Graph-level REST API endpoints."""

    def test_get_graph(self, client):
        resp = client.get("/v1/graph")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert "stats" in data

    def test_create_manager(self, client):
        resp = client.post("/v1/graph/manager", json={
            "task": "Build the project",
            "role": "architect",
            "config": {"model": "custom-model"},
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "node_id" in data

    def test_create_manager_legacy_config_keys_ignored(self, client):
        """遗留 num_ccs/audit_rounds 静默剥除：不 4xx，也不进节点 config。"""
        resp = client.post("/v1/graph/manager", json={
            "task": "Build the project",
            "config": {"num_ccs": 2, "audit_rounds": 2},
        })
        assert resp.status_code == 201, resp.text
        graph = client.get("/v1/graph").json()
        node = next(n for n in graph["nodes"] if n["id"] == resp.json()["node_id"])
        assert "num_ccs" not in node["config"]
        assert "audit_rounds" not in node["config"]

    def test_create_manager_defaults(self, client):
        resp = client.post("/v1/graph/manager", json={"task": "test"})
        assert resp.status_code == 201
        data = resp.json()
        assert "node_id" in data

    def test_create_manager_duplicate_root(self, client):
        client.post("/v1/graph/manager", json={"task": "first"})
        resp = client.post("/v1/graph/manager", json={"task": "second"})
        assert resp.status_code == 409

    def test_create_manager_invalid_config(self, client):
        resp = client.post("/v1/graph/manager", json={
            "task": "test",
            "config": {"invalid_field": "value"},
        })
        assert resp.status_code == 422

    def test_create_manager_task_too_long(self, client):
        # _MAX_TASK_LENGTH = 5000（与前端 constants.ts 保持一致），超过才拒绝
        resp = client.post("/v1/graph/manager", json={
            "task": "x" * 5001,
        })
        assert resp.status_code == 422

    def test_submit_plan(self, client):
        resp = client.post("/v1/graph/manager", json={"task": "parent task"})
        manager_id = resp.json()["node_id"]
        resp = client.post("/v1/graph/plan", json={
            "parent_node_id": manager_id,
            "plan": {
                "steps": [{
                    "step_id": "s1",
                    "exec_type": "worker",
                    "role": "coder",
                    "task": "Write tests",
                    "task_summary": "Write tests",
                }],
            },
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "nodes" in data

    def test_submit_plan_empty_steps(self, client):
        resp = client.post("/v1/graph/plan", json={
            "parent_node_id": "manager_1",
            "plan": {"steps": []},
        })
        assert resp.status_code == 422

    def test_get_graph_stats(self, client):
        resp = client.get("/v1/graph/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_events" in data
        assert "has_overflowed" in data


# ============================================================================
# TestNodeEndpoints
# ============================================================================

class TestNodeEndpoints:
    """Node-level query endpoints."""

    @pytest.fixture(autouse=True)
    def _setup(self, client):
        resp = client.post("/v1/graph/manager", json={"task": "test"})
        self.node_id = resp.json()["node_id"]
        self.client = client

    def test_get_node(self):
        resp = self.client.get(f"/v1/nodes/{self.node_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == self.node_id
        assert data["exec_type"] == "manager"
        assert data["status"] is not None

    def test_get_node_not_found(self):
        resp = self.client.get("/v1/nodes/nonexistent")
        assert resp.status_code == 404

    def test_get_node_children(self):
        resp = self.client.get(f"/v1/nodes/{self.node_id}/children")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_node_children_not_found(self):
        resp = self.client.get("/v1/nodes/nonexistent/children")
        assert resp.status_code == 404

    def test_get_node_logs(self):
        resp = self.client.get(f"/v1/nodes/{self.node_id}/logs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_node_logs_not_found(self):
        resp = self.client.get("/v1/nodes/nonexistent/logs")
        assert resp.status_code == 404

    def test_get_node_artifact_not_found(self):
        resp = self.client.get(f"/v1/nodes/{self.node_id}/artifact", params={"path": "test.txt"})
        assert resp.status_code == 404


# ============================================================================
# TestNodeControlEndpoints
# ============================================================================

class TestNodeControlEndpoints:
    """Node-level control (mutation) endpoints."""

    @pytest.fixture(autouse=True)
    def _setup(self, client):
        resp = client.post("/v1/graph/manager", json={"task": "test"})
        self.node_id = resp.json()["node_id"]
        self.client = client

    def test_start_node_not_found(self):
        resp = self.client.post("/v1/nodes/nonexistent/start")
        assert resp.status_code == 404

    def test_pause_node_not_found(self):
        resp = self.client.post("/v1/nodes/nonexistent/pause")
        assert resp.status_code == 409

    def test_stop_node(self):
        resp = self.client.post(f"/v1/nodes/{self.node_id}/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"

    def test_stop_node_not_found(self):
        resp = self.client.post("/v1/nodes/nonexistent/stop")
        assert resp.status_code == 404

    def test_update_node_task(self):
        resp = self.client.put(f"/v1/nodes/{self.node_id}/task", json={"task": "new task"})
        assert resp.status_code == 200

    def test_update_node_task_not_found(self):
        resp = self.client.put("/v1/nodes/nonexistent/task", json={"task": "new task"})
        assert resp.status_code == 404

    def test_delete_node(self):
        """叶子节点删除成功。先等节点脱离 RUNNING（_TestExecutable 自动完成后）。"""
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.client.get(f"/v1/nodes/{self.node_id}").json().get("status") == "Completed":
                break
            time.sleep(0.1)
        resp = self.client.delete(f"/v1/nodes/{self.node_id}")
        assert resp.status_code == 200
        assert self.client.get(f"/v1/nodes/{self.node_id}").status_code == 404

    def test_delete_node_not_found(self):
        resp = self.client.delete("/v1/nodes/nonexistent")
        assert resp.status_code == 404

    def test_delete_running_node_rejected(self):
        """删除保护：RUNNING 节点拒绝删除（需先停止），409。"""
        deadline = time.time() + 2
        while time.time() < deadline:
            if self.client.get(f"/v1/nodes/{self.node_id}").json().get("status") == "Running":
                break
            time.sleep(0.01)
        resp = self.client.delete(f"/v1/nodes/{self.node_id}")
        assert resp.status_code == 409
        assert "Running" in resp.json()["detail"]


# ============================================================================
# TestGlobalLogs
# ============================================================================

class TestGlobalLogs:
    """Global logs endpoint."""

    def test_get_global_logs(self, client):
        resp = client.get("/v1/logs")
        assert resp.status_code == 200
        data = resp.json()
        # /v1/logs 返回分页信封 {logs, total}
        assert isinstance(data, dict)
        assert isinstance(data["logs"], list)
        assert isinstance(data["total"], int)

    def test_get_global_logs_with_pagination(self, client):
        resp = client.get("/v1/logs", params={"offset": 0, "limit": 10})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert isinstance(data["logs"], list)
        assert len(data["logs"]) <= 10
        # total 是全量日志数，分页截断后不应超过它
        assert data["total"] >= len(data["logs"])


# ============================================================================
# TestSSEEndpoints
# ============================================================================

class TestSSEEndpoints:_DUMMY = 1


# ============================================================================
# TestExternalInput
# ============================================================================

class TestExternalInput:
    """External input endpoint."""

    def test_external_input_invalid_token(self, client):
        resp = client.post("/v1/nodes/any-node-id/external-input", json={
            "wait_token": "invalid",
            "input_data": {"key": "value"},
        })
        assert resp.status_code == 404


# ============================================================================
# TestSSEManagerIntegration
# ============================================================================

class TestSSEManagerIntegration:
    """SSEManager integrated with server."""

    def test_events_after_manager_creation(self, runtime):
        sse_manager = SSEManager(event_bus=runtime.event_bus)
        sse_manager.start()
        runtime.start()
        node_id = runtime.submit_root_manager("test")
        time.sleep(0.3)
        events = sse_manager.get_events_since(0)
        # At least node_added and node_status_changed
        assert len(events) >= 2
        event_types = {e.event_type for e in events}
        assert "node_added" in event_types

    def test_snapshot_event_has_correct_structure(self, runtime, sse_manager):
        runtime.start()
        runtime.submit_root_manager("test")
        snapshot = sse_manager.get_snapshot_event(runtime)
        assert snapshot.event_type == "graph_updated"
        assert "nodes" in snapshot.payload
        assert "edges" in snapshot.payload
        assert "summary" in snapshot.payload
        assert snapshot.payload["summary"]["node_count"] >= 1
