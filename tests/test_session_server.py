"""Tests for the multi-session graph API."""

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from executables.base import Executable, ExecutableContext
from executables.manager import Manager
from executables.registry import ExecutableRegistry
from executables.worker import Worker
from runtime.models import LogLevel, NodeStatus
from runtime.role_pools_manager import RolePoolsManager
from runtime.roles_manager import RolesManager
from runtime.session_manager import SessionManager
from runtime.session_server import create_session_app
from runtime.skills_manager import SkillsManager


def _create_session(client, task="Build a feature", title=None):
    payload = {"task": task}
    if title:
        payload["title"] = title
    response = client.post("/v1/sessions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class _BlockingExecutable(Executable):
    started = threading.Event()
    release = threading.Event()

    def __init__(self, context: ExecutableContext, **_kwargs):
        super().__init__(context)

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        self.started.set()
        if not self.release.wait(timeout=5):
            return NodeStatus.FAILED
        return NodeStatus.COMPLETED

    def stop(self, ctx: ExecutableContext) -> None:
        self.release.set()

    def pause(self, ctx: ExecutableContext) -> None:
        self.release.set()

    def resume(self, ctx: ExecutableContext) -> NodeStatus:
        return self.run(ctx)

    def cleanup_resources(self, ctx: ExecutableContext) -> None:
        pass


def _wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_session_starts_blank_and_is_isolated(session_client):
    first = _create_session(session_client, "First task", "First")
    second = _create_session(session_client, "Second task", "Second")

    first_graph = session_client.get(f"/v1/sessions/{first['id']}/graph").json()
    second_graph = session_client.get(f"/v1/sessions/{second['id']}/graph").json()

    # 空白图：新会话不自动创建/执行根节点
    assert first_graph["nodes"] == []
    assert second_graph["nodes"] == []

    # 会话隔离：给 first 手动添加节点，不影响 second
    added = session_client.post(
        f"/v1/sessions/{first['id']}/nodes",
        json={"task": "first", "exec_type": "manager"},
    )
    assert added.status_code == 201, added.text
    first_graph = session_client.get(f"/v1/sessions/{first['id']}/graph").json()
    second_graph = session_client.get(f"/v1/sessions/{second['id']}/graph").json()
    assert len(first_graph["nodes"]) == 1
    assert second_graph["nodes"] == []


def test_session_crud_and_not_found(session_client):
    session = _create_session(session_client, "Original task")

    response = session_client.patch(
        f"/v1/sessions/{session['id']}", json={"title": "Renamed session"}
    )
    assert response.status_code == 200
    assert response.json()["title"] == "Renamed session"

    response = session_client.delete(f"/v1/sessions/{session['id']}")
    assert response.status_code == 204
    assert session_client.get(f"/v1/sessions/{session['id']}").status_code == 404


def test_manual_graph_editing_is_scoped_to_session(session_client):
    first = _create_session(session_client, "Editable graph")
    second = _create_session(session_client, "Untouched graph")
    first_id = first["id"]

    # 暂停图避免新建节点被调度器自动执行（保持 WAITING 可编辑）
    assert session_client.post(f"/v1/sessions/{first_id}/graph/pause").status_code == 200
    root_response = session_client.post(
        f"/v1/sessions/{first_id}/nodes",
        json={"task": "root", "exec_type": "manager"},
    )
    assert root_response.status_code == 201, root_response.text
    root_id = root_response.json()["id"]

    child_response = session_client.post(
        f"/v1/sessions/{first_id}/nodes",
        json={
            "task": "Inspect inputs",
            "role": "default",
            "exec_type": "worker",
            "parent_id": root_id,
        },
    )
    assert child_response.status_code == 201, child_response.text
    child_id = child_response.json()["id"]

    peer_response = session_client.post(
        f"/v1/sessions/{first_id}/nodes",
        json={"task": "Produce output", "exec_type": "worker"},
    )
    assert peer_response.status_code == 201, peer_response.text
    peer_id = peer_response.json()["id"]

    update_response = session_client.patch(
        f"/v1/sessions/{first_id}/nodes/{peer_id}",
        json={"task": "Produce reviewed output", "role": "reviewer"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["task"] == "Produce reviewed output"

    edge_response = session_client.post(
        f"/v1/sessions/{first_id}/edges",
        json={"from_node": child_id, "to_node": peer_id},
    )
    assert edge_response.status_code == 201, edge_response.text
    edge_id = edge_response.json()["id"]

    first_graph = session_client.get(f"/v1/sessions/{first_id}/graph").json()
    second_graph = session_client.get(f"/v1/sessions/{second['id']}/graph").json()
    assert len(first_graph["nodes"]) == 3
    assert len(first_graph["edges"]) == 2
    assert len(second_graph["nodes"]) == 0
    assert len(second_graph["edges"]) == 0

    assert session_client.delete(
        f"/v1/sessions/{first_id}/edges/{edge_id}"
    ).status_code == 204
    assert session_client.delete(
        f"/v1/sessions/{first_id}/nodes/{peer_id}"
    ).status_code == 204


def test_running_or_completed_node_cannot_be_edited(session_client, session_manager):
    session = _create_session(session_client, "Immutable active node")
    sid = session["id"]
    node = session_client.post(
        f"/v1/sessions/{sid}/nodes",
        json={"task": "t", "exec_type": "worker"},
    ).json()
    # 显式置为非 WAITING（running/completed 均不可编辑），避免依赖调度器时序
    session_manager.get_session(sid).runtime.graph.update_node(
        node["id"], status=NodeStatus.RUNNING
    )
    response = session_client.patch(
        f"/v1/sessions/{sid}/nodes/{node['id']}",
        json={"task": "t"},
    )
    assert response.status_code == 409


def test_graph_level_controls_are_session_scoped(session_client):
    first = _create_session(session_client, "Pause this")
    second = _create_session(session_client, "Keep this independent")

    assert session_client.post(
        f"/v1/sessions/{first['id']}/graph/pause"
    ).status_code == 200
    assert _wait_for(
        lambda: session_client.get(f"/v1/sessions/{first['id']}").json()[
            "lifecycle_state"
        ]
        == "paused"
    )
    assert session_client.post(
        f"/v1/sessions/{first['id']}/graph/resume"
    ).status_code == 200

    sessions = session_client.get("/v1/sessions").json()["sessions"]
    assert {item["id"] for item in sessions} == {first["id"], second["id"]}


def test_graph_pause_immediate_exposes_lifecycle_and_resume_contract(session_client):
    session = _create_session(session_client, "Interrupt this")

    paused = session_client.post(
        f"/v1/sessions/{session['id']}/graph/pause",
        json={"mode": "immediate"},
    )
    assert paused.status_code == 200, paused.text
    payload = paused.json()
    assert payload["lifecycle_state"] in {"pausing", "paused"}
    assert payload["pause_mode"] == "immediate"
    assert isinstance(payload["interrupted_node_ids"], list)
    assert _wait_for(
        lambda: session_client.get(
            f"/v1/sessions/{session['id']}"
        ).json()["lifecycle_state"]
        == "paused"
    )

    resumed = session_client.post(
        f"/v1/sessions/{session['id']}/graph/resume"
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["lifecycle_state"] == "running"


def test_auto_recover_from_failed_revives_stopped_session(
    session_client, session_manager
):
    """continue/revise 对 stopped 会话应自动恢复到可恢复的 paused，而非死锁。

    Bug: `_auto_recover_from_failed` 原先只处理 failed/recovery_required，
    stopped 会话被 continue 置 READY 后调度器仍 paused 且 resume 拒绝 stopped，
    导致节点永远卡在 Ready 无法派发。修复：stopped 也转为 paused（可 resume）。
    """
    session = _create_session(session_client, "Continue after stop")
    sid = session["id"]

    # 用户主动 stopped 会话（硬停）
    assert session_client.post(f"/v1/sessions/{sid}/graph/stop").status_code == 200
    assert _wait_for(
        lambda: session_client.get(
            f"/v1/sessions/{sid}"
        ).json()["lifecycle_state"] == "stopped"
    )

    # continue/revise 触发的自动恢复：stopped -> paused（resume 前置条件）
    session_manager._auto_recover_from_failed(sid)
    assert _wait_for(
        lambda: session_client.get(
            f"/v1/sessions/{sid}"
        ).json()["lifecycle_state"] == "paused"
    )

    # paused 会话可正常 resume（此前 stopped 会被 409 拒绝）
    resumed = session_client.post(f"/v1/sessions/{sid}/graph/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["lifecycle_state"] == "running"


def test_drain_pause_waits_for_running_node_and_becomes_restart_safe(tmp_path):
    _BlockingExecutable.started = threading.Event()
    _BlockingExecutable.release = threading.Event()
    registry = ExecutableRegistry()
    registry.register("manager", _BlockingExecutable)
    registry.register("worker", _BlockingExecutable)
    manager = SessionManager(
        registry=registry,
        root_dir=str(tmp_path / "drain-sessions"),
        workspace_root=str(tmp_path),
        restore=False,
    )
    try:
        session = manager.create_session("Drain this session")
        handle = manager.get_session(session["id"])
        handle.runtime.create_manual_node(task="t", exec_type="worker")
        assert _BlockingExecutable.started.wait(timeout=3)
        assert _wait_for(
            lambda: manager.get_session(session["id"]).runtime.scheduler.active_execution_count
            == 1
        )

        pausing = manager.pause_session(session["id"], "drain")
        assert pausing["lifecycle_state"] == "pausing"
        assert pausing["running_count"] == 1
        assert pausing["restart_safe"] is False

        _BlockingExecutable.release.set()
        assert _wait_for(
            lambda: manager.get_session(session["id"]).metadata.lifecycle_state
            == "paused"
        )
        paused = manager.list_sessions()[0]
        assert paused["running_count"] == 0
        assert paused["restart_safe"] is True
        assert paused["interrupted_node_ids"] == []
    finally:
        _BlockingExecutable.release.set()
        manager.shutdown()


def test_immediate_pause_waits_for_execution_thread_to_exit(tmp_path):
    _BlockingExecutable.started = threading.Event()
    _BlockingExecutable.release = threading.Event()
    registry = ExecutableRegistry()
    registry.register("manager", _BlockingExecutable)
    registry.register("worker", _BlockingExecutable)
    manager = SessionManager(
        registry=registry,
        root_dir=str(tmp_path / "immediate-sessions"),
        workspace_root=str(tmp_path),
        restore=False,
    )
    try:
        session = manager.create_session("Interrupt this running session")
        handle = manager.get_session(session["id"])
        node = handle.runtime.create_manual_node(
            task="t", exec_type="worker"
        )
        assert _BlockingExecutable.started.wait(timeout=3)
        assert _wait_for(
            lambda: manager.get_session(session["id"]).runtime.scheduler.active_execution_count
            == 1
        )

        pausing = manager.pause_session(session["id"], "immediate")
        assert pausing["lifecycle_state"] == "pausing"
        assert pausing["restart_safe"] is False
        assert pausing["running_count"] == 1
        assert pausing["interrupted_node_ids"] == [node.id]

        _BlockingExecutable.release.set()
        assert _wait_for(
            lambda: manager.get_session(session["id"]).metadata.lifecycle_state
            == "paused"
        )
        paused = manager.list_sessions()[0]
        assert paused["restart_safe"] is True
        assert paused["interrupted_node_ids"] == [node.id]
    finally:
        _BlockingExecutable.release.set()
        manager.shutdown()


def test_graph_resume_does_not_resume_manually_paused_node(session_manager):
    session = session_manager.create_session("Keep manual pause isolated")
    handle = session_manager.get_session(session["id"])
    session_manager.pause_session(session["id"])
    assert _wait_for(
        lambda: session_manager.get_session(session["id"]).metadata.lifecycle_state
        == "paused"
    )
    manual_node = handle.runtime.create_manual_node(
        task="Remain manually paused",
        role="default",
        exec_type="worker",
    )
    handle.runtime.graph.update_node(manual_node.id, status=NodeStatus.PAUSED)
    handle.metadata.interrupted_node_ids = []

    resumed = session_manager.resume_session(session["id"])

    node = handle.runtime.graph.get_node(manual_node.id)
    assert resumed["lifecycle_state"] == "running"
    assert node is not None
    assert node.status == NodeStatus.PAUSED


def test_running_session_with_stopped_scheduler_resumes_pending_work(session_manager):
    session = session_manager.create_session("Resume pending revise")
    handle = session_manager.get_session(session["id"])
    root = handle.runtime.create_manual_node(
        task="t", exec_type="manager"
    )
    assert root is not None
    runtime = handle.runtime
    scheduler = runtime.scheduler

    # Reproduce the lifecycle/Scheduler split left by an idle scheduler after Revise.
    handle.runtime.scheduler.stop()
    handle.runtime.graph.update_node(
        root.id,
        status=NodeStatus.WAITING,
        manager_phase="expand",
        pending_revise=True,
    )
    handle.metadata.lifecycle_state = "running"
    resume_calls: list[list[str] | None] = []
    handle.runtime.global_resume = (  # type: ignore[method-assign]
        lambda interrupted=None: resume_calls.append(interrupted)
    )

    resumed = session_manager.resume_session(session["id"])

    assert resumed["lifecycle_state"] == "running"
    assert resume_calls == [[]]
    assert handle.runtime is runtime
    assert handle.runtime.scheduler is scheduler


def test_running_session_with_paused_scheduler_resumes_dispatch(session_manager):
    session = session_manager.create_session("Resume paused dispatch")
    handle = session_manager.get_session(session["id"])
    root = handle.runtime.create_manual_node(
        task="t", exec_type="manager"
    )
    assert root is not None
    runtime = handle.runtime
    scheduler = runtime.scheduler

    scheduler.pause()
    handle.runtime.graph.update_node(
        root.id,
        status=NodeStatus.WAITING,
        manager_phase="expand",
        pending_revise=True,
    )
    handle.metadata.lifecycle_state = "running"
    resume_calls: list[list[str] | None] = []
    handle.runtime.global_resume = (  # type: ignore[method-assign]
        lambda interrupted=None: resume_calls.append(interrupted)
    )

    resumed = session_manager.resume_session(session["id"])

    assert resumed["lifecycle_state"] == "running"
    assert resume_calls == [[]]
    assert handle.runtime is runtime
    assert handle.runtime.scheduler is scheduler


def test_stopped_session_with_waiting_work_can_resume(session_manager):
    session = session_manager.create_session("Resume stopped pending work")
    handle = session_manager.get_session(session["id"])
    root = handle.runtime.create_manual_node(
        task="t", exec_type="manager"
    )
    assert root is not None
    runtime = handle.runtime
    scheduler = runtime.scheduler

    scheduler.stop()
    handle.runtime.graph.update_node(
        root.id,
        status=NodeStatus.WAITING,
        manager_phase="expand",
        pending_revise=True,
    )
    handle.metadata.lifecycle_state = "stopped"
    resume_calls: list[list[str] | None] = []
    handle.runtime.global_resume = (  # type: ignore[method-assign]
        lambda interrupted=None: resume_calls.append(interrupted)
    )

    resumed = session_manager.resume_session(session["id"])

    assert resumed["lifecycle_state"] == "running"
    assert resume_calls == [[]]
    assert handle.runtime is runtime
    assert handle.runtime.scheduler is scheduler


def test_paused_session_restores_without_automatic_execution(tmp_path):
    _BlockingExecutable.started = threading.Event()
    _BlockingExecutable.release = threading.Event()
    root_dir = tmp_path / "persisted-sessions"
    registry = ExecutableRegistry()
    registry.register("manager", _BlockingExecutable)
    registry.register("worker", _BlockingExecutable)
    manager = SessionManager(
        registry=registry,
        root_dir=str(root_dir),
        workspace_root=str(tmp_path),
        restore=False,
    )
    session = manager.create_session("Persist this pause")
    manager.get_session(session["id"]).runtime.create_manual_node(
        task="t", exec_type="worker"
    )
    assert _BlockingExecutable.started.wait(timeout=3)
    assert _wait_for(
        lambda: manager.get_session(session["id"]).runtime.scheduler.active_execution_count
        == 1
    )
    manager.pause_session(session["id"], "immediate")
    _BlockingExecutable.release.set()
    assert _wait_for(
        lambda: manager.get_session(session["id"]).metadata.lifecycle_state
        == "paused"
    )
    manager.shutdown()

    restored = SessionManager(
        registry=registry,
        root_dir=str(root_dir),
        workspace_root=str(tmp_path),
        restore=True,
    )
    try:
        summary = restored.list_sessions()[0]
        assert summary["lifecycle_state"] == "paused"
        assert summary["restart_safe"] is True
        assert summary["running_count"] == 0
    finally:
        restored.shutdown()


def test_running_session_restores_to_recovery_required(runtime, tmp_path):
    root_dir = tmp_path / "recovery-sessions"
    manager = SessionManager(
        registry=runtime.registry,
        root_dir=str(root_dir),
        workspace_root=str(tmp_path),
        restore=False,
    )
    session = manager.create_session("Recover this session")
    runtime = manager.get_session(session["id"]).runtime
    node = runtime.create_manual_node(task="t", exec_type="worker")
    runtime.graph.update_node(node.id, status=NodeStatus.RUNNING)
    manager.shutdown()

    restored = SessionManager(
        registry=runtime.registry,
        root_dir=str(root_dir),
        workspace_root=str(tmp_path),
        restore=True,
    )
    try:
        summary = restored.list_sessions()[0]
        assert summary["lifecycle_state"] == "recovery_required"
        assert summary["recovery_required"] is True
        assert summary["running_count"] == 0
        assert summary["interrupted_node_ids"]
    finally:
        restored.shutdown()


def test_sessions_restore_from_independent_state_files(session_manager):
    created = session_manager.create_session(task="Persist this graph", title="Persistent")
    node = session_manager.get_session(created["id"]).runtime.create_manual_node(
        task="t", exec_type="worker"
    )
    root_dir = session_manager.root_dir
    registry = session_manager.registry
    workspace_root = session_manager.workspace_root
    session_manager.shutdown()

    restored = SessionManager(
        registry=registry,
        root_dir=root_dir,
        workspace_root=workspace_root,
        restore=True,
    )
    try:
        sessions = restored.list_sessions()
        assert [item["id"] for item in sessions] == [created["id"]]
        graph = restored.get_session(created["id"]).runtime.get_graph_snapshot()
        assert [n["id"] for n in graph["nodes"]] == [node.id]
    finally:
        restored.shutdown()


def test_role_and_skill_crud_use_isolated_managers(session_manager, tmp_path):
    roles_manager = RolesManager(str(tmp_path / "roles"))
    skills_manager = SkillsManager(str(tmp_path / "skills"))
    app = create_session_app(session_manager, roles_manager, skills_manager)

    with TestClient(app) as client:
        role_payload = {
            "role_id": "code_reviewer",
            "role_type": "worker",
            "content": "role_md: |\n  Review code for correctness.\n",
        }
        created_role = client.post("/v1/roles", json=role_payload)
        assert created_role.status_code == 201, created_role.text
        assert created_role.json()["id"] == "code_reviewer"
        assert client.get("/v1/roles/worker/code_reviewer").json()["content"] == role_payload["content"]

        changed_role = dict(role_payload, content="role_md: |\n  Review tests and code.\n")
        updated_role = client.patch(
            "/v1/roles/worker/code_reviewer", json=changed_role
        )
        assert updated_role.status_code == 200
        assert "Review tests" in updated_role.json()["content"]
        assert client.patch(
            "/v1/roles/worker/code_reviewer",
            json=dict(changed_role, role_id="other_role"),
        ).status_code == 400

        skill_payload = {
            "skill_id": "focused-review",
            "content": "# Focused review\nCheck correctness and tests.\n",
        }
        created_skill = client.post("/v1/skills", json=skill_payload)
        assert created_skill.status_code == 201, created_skill.text
        assert client.post("/v1/skills", json=skill_payload).status_code == 409
        updated_skill = client.patch(
            "/v1/skills/focused-review",
            json=dict(skill_payload, content="# Focused review\nCheck security.\n"),
        )
        assert updated_skill.status_code == 200
        assert "security" in updated_skill.json()["content"]
        assert [item["id"] for item in client.get("/v1/skills").json()["skills"]] == [
            "focused-review"
        ]


def test_session_settings_filter_secrets_and_switch_new_workspace(
    session_client, session_manager, tmp_path, monkeypatch
):
    session = _create_session(session_client)
    session_id = session["id"]
    original_root = session_manager.get_session(session_id).metadata.workspace_root
    # 切换工作区前先创建一个节点：其 _workspace_root 在创建时即注入旧工作区根
    pre_switch = session_client.post(
        f"/v1/sessions/{session_id}/nodes",
        json={"task": "t", "exec_type": "worker"},
    )
    assert pre_switch.status_code == 201, pre_switch.text
    node_id = pre_switch.json()["id"]
    custom_root = tmp_path / "custom-output"
    monkeypatch.setenv("SESSION_TEST_KEY", "secret-value")

    assert session_client.patch(
        f"/v1/sessions/{session_id}/settings",
        json={"config": {"api_key": "must-not-persist"}},
    ).status_code == 400

    response = session_client.patch(
        f"/v1/sessions/{session_id}/settings",
        json={
            "config": {
                "cli_name": "claude",
                "model": "claude-sonnet-4-6",
                "api_key_env": "SESSION_TEST_KEY",
                "unknown": "ignored",
            },
            "workspace_root": str(custom_root),
        },
    )
    assert response.status_code == 200, response.text

    settings = session_client.get(
        f"/v1/sessions/{session_id}/settings"
    ).json()
    assert settings["workspace_root"] == str(custom_root)
    assert settings["credential_configured"] is True
    assert settings["config"] == {
        "cli_name": "claude",
        "model": "claude-sonnet-4-6",
        "api_key_env": "SESSION_TEST_KEY",
        "language": "zh",
    }
    node = session_manager.get_session(session_id).runtime.get_node(node_id)
    assert node.config["_workspace_root"] == original_root


def test_nested_workspace_browsing_and_path_isolation(
    session_client, session_manager, tmp_path
):
    session = _create_session(session_client)
    session_id = session["id"]
    runtime = session_manager.get_session(session_id).runtime
    root = runtime.create_manual_node(task="Root", exec_type="manager")
    root_id = root.id
    child = runtime.create_manual_node(
        task="Produce nested files", parent_id=root_id
    )
    root_workspace = Path(runtime.get_node_workspace(root_id))
    child_workspace = Path(runtime.get_node_workspace(child.id))
    assert child_workspace.parent == root_workspace / "children"

    nested_dir = child_workspace / "reports"
    nested_dir.mkdir(parents=True)
    (nested_dir / "summary.txt").write_text("ready", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (child_workspace / "outside-link").symlink_to(outside)

    listing = session_client.get(
        f"/v1/sessions/{session_id}/nodes/{child.id}/workspace"
    )
    assert listing.status_code == 200
    assert [item["name"] for item in listing.json()["entries"]] == ["reports"]

    preview = session_client.get(
        f"/v1/sessions/{session_id}/nodes/{child.id}/workspace/file",
        params={"path": "reports/summary.txt"},
    )
    assert preview.status_code == 200
    assert preview.json()["content"] == "ready"
    assert session_client.get(
        f"/v1/sessions/{session_id}/nodes/{child.id}/workspace/file",
        params={"path": "../../outside.txt"},
    ).status_code == 400
    assert session_client.get(
        f"/v1/sessions/{session_id}/nodes/{child.id}/workspace/file",
        params={"path": "outside-link"},
    ).status_code == 400


def test_dependency_candidates_and_cycle_rejection(session_client):
    session = _create_session(session_client)
    session_id = session["id"]
    root = session_client.post(
        f"/v1/sessions/{session_id}/nodes",
        json={"task": "Root", "exec_type": "manager"},
    ).json()
    first = session_client.post(
        f"/v1/sessions/{session_id}/nodes",
        json={"task": "First", "parent_id": root["id"]},
    ).json()
    second = session_client.post(
        f"/v1/sessions/{session_id}/nodes",
        json={"task": "Second", "parent_id": root["id"]},
    ).json()

    edge = session_client.post(
        f"/v1/sessions/{session_id}/edges",
        json={"from_node": first["id"], "to_node": second["id"]},
    )
    assert edge.status_code == 201, edge.text
    assert session_client.post(
        f"/v1/sessions/{session_id}/edges",
        json={"from_node": second["id"], "to_node": first["id"]},
    ).status_code == 400

    candidates = session_client.get(
        f"/v1/sessions/{session_id}/graph/link-candidates",
        params={"node_id": first["id"]},
    ).json()["candidates"]
    second_candidate = next(item for item in candidates if item["id"] == second["id"])
    assert second_candidate["eligible_as_dependency"] is False


def test_node_result_and_activity_endpoints(session_client, session_manager):
    session = _create_session(session_client, "Inspect node output")
    session_id = session["id"]
    node = session_client.post(
        f"/v1/sessions/{session_id}/nodes",
        json={"task": "t", "exec_type": "worker"},
    ).json()
    node_id = node["id"]
    runtime = session_manager.get_session(session_id).runtime

    empty = session_client.get(
        f"/v1/sessions/{session_id}/nodes/{node_id}/results"
    )
    assert empty.status_code == 200
    assert empty.json()["artifact_path"] is None

    output_dir = Path(runtime.get_node_workspace(node_id)) / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.md").write_text("# Report", encoding="utf-8")
    cc_dir = Path(runtime.get_node_workspace(node_id)) / "cc_0"
    cc_dir.mkdir(exist_ok=True)
    (cc_dir / "notes.txt").write_text("internal", encoding="utf-8")
    node = runtime.get_node(node_id)
    runtime.update_node_status(
        node_id, node.status, artifact_path=str(output_dir)
    )
    runtime.update_cc_activity(
        node_id,
        "CC-0",
        "running",
        phase="streaming",
        task="Write report",
        latest_output="Drafting",
    )
    runtime.append_node_log(
        node_id,
        LogLevel.INFO,
        "Drafting",
        cc_id="CC-0",
        phase="streaming",
        event="output",
        task="Write report",
    )

    listing = session_client.get(
        f"/v1/sessions/{session_id}/nodes/{node_id}/results"
    )
    assert listing.status_code == 200, listing.text
    assert [entry["name"] for entry in listing.json()["entries"]] == ["report.md"]
    preview = session_client.get(
        f"/v1/sessions/{session_id}/nodes/{node_id}/results/file",
        params={"path": "report.md"},
    )
    assert preview.status_code == 200
    assert preview.json()["content"] == "# Report"
    assert session_client.get(
        f"/v1/sessions/{session_id}/nodes/{node_id}/results",
        params={"path": "../cc_0"},
    ).status_code == 400

    activity = session_client.get(
        f"/v1/sessions/{session_id}/nodes/{node_id}/activity"
    )
    assert activity.status_code == 200
    assert activity.json()["activities"]["CC-0"]["task"] == "Write report"
    logs = session_client.get(
        f"/v1/sessions/{session_id}/nodes/{node_id}/logs"
    ).json()
    streamed = next(item for item in logs if item["message"] == "Drafting")
    assert streamed["cc_id"] == "CC-0"
    assert streamed["phase"] == "streaming"


def test_structured_role_schema_defaults_and_validation(session_manager, tmp_path):
    roles_dir = tmp_path / "structured_roles"
    (roles_dir / "Manager").mkdir(parents=True)
    (roles_dir / "Worker").mkdir(parents=True)
    (roles_dir / "Manager" / "default.yaml").write_text(
        "num_ccs: 2\nmodel: manager-default\ncli_name: null\nskills: []\nrole_md: manager\n"
        "generate_instruction: build a plan\nreview_checklist: []\noutput_file: plan\n",
        encoding="utf-8",
    )
    (roles_dir / "Worker" / "default.yaml").write_text(
        "num_ccs: 4\nmodel: worker-default\ncli_name: null\nskills: []\nrole_md: worker\n",
        encoding="utf-8",
    )
    (roles_dir / "Manager" / "existing_manager.yaml").write_text(
        "summary: Coordinates delivery.\nsuitable_for:\n  - multi-team work\nnot_for: []\n",
        encoding="utf-8",
    )
    (roles_dir / "Worker" / "existing_worker.yaml").write_text(
        "description: Produces documentation.\nmain_doc: README.md\nindex_file: index.md\n",
        encoding="utf-8",
    )
    app = create_session_app(
        session_manager,
        RolesManager(str(roles_dir)),
        SkillsManager(str(tmp_path / "structured_skills")),
    )

    with TestClient(app) as client:
        worker_schema = client.get("/v1/roles/schema/worker")
        assert worker_schema.status_code == 200
        assert worker_schema.json()["defaults"]["model"] == "worker-default"
        worker_fields = {field["name"]: field for field in worker_schema.json()["fields"]}
        assert worker_fields["main_doc"]["has_default"] is False
        assert "index_file" not in worker_fields
        assert worker_fields["description"]["type"] == "text"
        manager_schema = client.get("/v1/roles/schema/manager")
        assert manager_schema.json()["defaults"]["model"] == "manager-default"
        manager_fields = {field["name"]: field for field in manager_schema.json()["fields"]}
        assert manager_fields["summary"]["has_default"] is False
        assert manager_fields["suitable_for"]["type"] == "list"
        # Manager 表单隐藏协议/样板字段（Decision Policy 生成指令、审查/审计、产出/交付），
        # 只保留身份字段（summary/suitable_for/not_for/role_md）+ 简单执行配置。
        for hidden in (
            "generate_instruction", "review_checklist", "output_file",
        ):
            assert hidden not in manager_fields

        created = client.post(
            "/v1/roles",
            json={
                "role_id": "focused_writer",
                "role_type": "worker",
                "config": {
                    "num_ccs": 1,
                    "main_doc": "README.md",
                    "role_md": "Write focused output.",
                },
            },
        )
        assert created.status_code == 201, created.text
        role = created.json()
        assert role["config"] == {
            "num_ccs": 1,
            "main_doc": "README.md",
            "role_md": "Write focused output.",
        }
        assert role["effective_config"]["model"] == "worker-default"
        assert "model:" not in (
            roles_dir / "Worker" / "focused_writer.yaml"
        ).read_text(encoding="utf-8")

        invalid_type = client.post(
            "/v1/roles",
            json={
                "role_id": "invalid_count",
                "role_type": "worker",
                "config": {"num_ccs": "many"},
            },
        )
        assert invalid_type.status_code == 400
        unknown_field = client.post(
            "/v1/roles",
            json={
                "role_id": "invalid_field",
                "role_type": "worker",
                "config": {"unknown": True},
            },
        )
        assert unknown_field.status_code == 400


# ============================================================================
# Session Outputs 冒烟（V3-2.2）
# ============================================================================

def test_session_shared_empty_tree(session_client):
    """空会话（无 shared 产出）→ 200 + 空树，不含 shared_root。"""
    session = _create_session(session_client, "outputs")
    resp = session_client.get(f"/v1/sessions/{session['id']}/shared")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "."
    assert body["entries"] == []
    assert body["truncated"] is False
    assert "shared_root" not in body


def test_session_shared_invalid_path_400(session_client):
    """越界 path → 400（ValueError → HTTP 400）。"""
    session = _create_session(session_client, "outputs2")
    resp = session_client.get(
        f"/v1/sessions/{session['id']}/shared", params={"path": "../x"}
    )
    assert resp.status_code == 400, resp.text


# ============================================================================
# Agent 事件日志（方案三 Router 路由记录 + 生命周期状态）
# ============================================================================

def test_agent_events_empty_then_published(session_client, session_manager):
    """事件日志端点：未发布 → 空；发布后返回事件（含解析出的 producer/目标标签）。"""
    session = _create_session(session_client, "agent events")
    sid = session["id"]

    # 初始为空
    resp = session_client.get(f"/v1/sessions/{sid}/agent-events")
    assert resp.status_code == 200, resp.text
    assert resp.json()["events"] == []

    # 发布两个事件（经 Runtime.publish_events → AgentEventManager 摄取 + 路由）
    runtime = session_manager.get_session(sid).runtime
    root_id = runtime.create_manual_node(
        task="t", exec_type="worker"
    ).id

    class _NoRouteAICU:
        def ask(self, prompt):
            return {"targets": []}

    # The production Router is always real; inject a deterministic test fake explicitly.
    runtime.router._aicu = _NoRouteAICU()
    runtime.publish_events(
        root_id,
        [{"objective": "hello world"}, {"objective": "second event"}],
    )

    resp = session_client.get(f"/v1/sessions/{sid}/agent-events")
    assert resp.status_code == 200, resp.text
    events = resp.json()["events"]
    assert len(events) == 2

    for event in events:
        assert event["event_id"].startswith("evt_")
        assert event["producer"] == root_id
        assert event["producer_label"]  # 已解析节点标签
        assert event["objective"] in {"hello world", "second event"}
        assert event["status"] in {"routed", "delivered", "consumed"}
        assert set(event) >= {
            "event_id", "producer", "producer_label", "objective",
            "intent", "affected_roles", "scope", "action",
            "created_at", "status", "routed_to", "targets",
        }
        # targets 与 routed_to 一一对应（可能为空数组）
        assert [t["node_id"] for t in event["targets"]] == event["routed_to"]


def test_agent_events_unknown_session_404(session_client):
    """未知会话 → 404（与其他会话端点一致）。"""
    resp = session_client.get("/v1/sessions/nope/agent-events")
    assert resp.status_code == 404, resp.text


# ============================================================================
# API Key 凭据端点（/v1/config/api-key）
# ============================================================================

def test_api_key_save_and_echo_by_vendor(session_client):
    """sk 按模型厂商保存，同厂商模型切换时回显同一 sk。"""
    resp = session_client.put(
        "/v1/config/api-key", json={"model": "deepseek-v4-pro", "api_key": "sk-test-123"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"vendor": "deepseek", "saved": True}

    # 同厂商（deepseek）不同模型回显同一 sk
    resp = session_client.get("/v1/config/api-key", params={"model": "deepseek-v4-flash"})
    assert resp.status_code == 200
    assert resp.json() == {"vendor": "deepseek", "api_key": "sk-test-123"}

    # 未保存的厂商返回 api_key=null
    resp = session_client.get("/v1/config/api-key", params={"model": "gpt-5.6"})
    assert resp.json() == {"vendor": "openai", "api_key": None}


def test_api_key_internal_model_returns_null_vendor(session_client):
    """内部模型（auto/*-内部）无需 sk，vendor=null。"""
    resp = session_client.get("/v1/config/api-key", params={"model": "auto-内部"})
    assert resp.status_code == 200
    assert resp.json() == {"vendor": None, "api_key": None}


def test_api_key_unknown_model_put_400(session_client):
    """无法推导厂商的模型 PUT → 400。"""
    resp = session_client.put(
        "/v1/config/api-key", json={"model": "auto", "api_key": "x"}
    )
    assert resp.status_code == 400


def test_api_key_empty_value_deletes(session_client):
    """空 api_key 视为删除该厂商凭据。"""
    session_client.put(
        "/v1/config/api-key", json={"model": "deepseek-v4-pro", "api_key": "sk-test-123"}
    )
    resp = session_client.put(
        "/v1/config/api-key", json={"model": "deepseek-v4-pro", "api_key": ""}
    )
    assert resp.status_code == 200
    assert resp.json() == {"vendor": "deepseek", "saved": False}
    assert session_client.get(
        "/v1/config/api-key", params={"model": "deepseek-v4-pro"}
    ).json()["api_key"] is None


def test_api_key_delete_by_vendor(session_client):
    """按厂商显式删除。"""
    session_client.put(
        "/v1/config/api-key", json={"model": "deepseek-v4-pro", "api_key": "sk-test-123"}
    )
    resp = session_client.delete("/v1/config/api-key", params={"vendor": "deepseek"})
    assert resp.status_code == 204
    assert session_client.get(
        "/v1/config/api-key", params={"model": "deepseek-v4-pro"}
    ).json()["api_key"] is None


def test_internal_username_save_and_echo(session_client):
    resp = session_client.put("/v1/config/internal-username", json={"username": "  v-user  "})
    assert resp.status_code == 200
    assert resp.json() == {"username": "v-user", "saved": True}
    assert session_client.get("/v1/config/internal-username").json() == {"username": "v-user"}


def test_internal_username_empty_value_deletes(session_client):
    session_client.put("/v1/config/internal-username", json={"username": "v-user"})
    resp = session_client.put("/v1/config/internal-username", json={"username": "  "})
    assert resp.status_code == 200
    assert resp.json() == {"username": None, "saved": False}
    assert session_client.get("/v1/config/internal-username").json() == {"username": None}


# ============================================================================
# 单节点 agent 数量策略（dynamic_num_ccs / default_num_ccs）
# ============================================================================

def test_num_ccs_policy_defaults_and_roundtrip(session_client):
    """默认 = 动态 + 1；关闭后必须回 false 而不是被兜底吞成 null。"""
    config = session_client.get("/v1/config").json()["config"]
    assert config["dynamic_num_ccs"] is True
    assert config["default_num_ccs"] == 1

    resp = session_client.patch(
        "/v1/config",
        json={"config": {"dynamic_num_ccs": False, "default_num_ccs": 2}},
    )
    assert resp.status_code == 200, resp.text
    # 关键回归：`value or None` 会把 False 吞成 None，此断言专门守这个坑。
    assert resp.json()["config"]["dynamic_num_ccs"] is False
    assert resp.json()["config"]["default_num_ccs"] == 2

    config = session_client.get("/v1/config").json()["config"]
    assert config["dynamic_num_ccs"] is False
    assert config["default_num_ccs"] == 2


def test_num_ccs_default_out_of_range_rejected(session_client):
    for bad in (0, 5, -1, "abc", None):
        resp = session_client.patch(
            "/v1/config", json={"config": {"default_num_ccs": bad}}
        )
        assert resp.status_code == 400, f"{bad!r} should be rejected"
    # 拒绝后不得污染已存配置
    assert session_client.get("/v1/config").json()["config"]["default_num_ccs"] == 1


def test_num_ccs_policy_persists_across_manager_reload(session_client, tmp_path):
    """持久化：重新加载 global config 后仍是 false（老 config.json 自动补默认）。"""
    session_client.patch(
        "/v1/config", json={"config": {"dynamic_num_ccs": False, "default_num_ccs": 3}}
    )
    from runtime.session_manager import SessionManager
    reloaded = SessionManager(
        registry=None,
        root_dir=str(tmp_path / "sessions"),
        workspace_root=str(tmp_path),
        restore=False,
    )
    try:
        config = reloaded.get_global_config()
        assert config["dynamic_num_ccs"] is False
        assert config["default_num_ccs"] == 3
    finally:
        reloaded.shutdown()


def test_num_ccs_policy_applies_to_new_session_nodes(session_client, session_manager):
    """开关关闭 → 新会话里 Manager 规划的 num_ccs 被压成默认值。"""
    session_client.patch(
        "/v1/config", json={"config": {"dynamic_num_ccs": False, "default_num_ccs": 1}}
    )
    session_id = _create_session(session_client)["id"]

    runtime = session_manager.get_session(session_id).runtime
    assert runtime.dynamic_num_ccs is False
    assert runtime.default_num_ccs == 1

    resp = session_client.post(
        f"/v1/sessions/{session_id}/nodes",
        json={"task": "t", "exec_type": "worker", "config": {"num_ccs": 3}},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["config"]["num_ccs"] == 1


def test_num_ccs_policy_pushed_to_existing_sessions(session_client, session_manager):
    """已存在的会话即时收到策略更新；已建节点不回改。"""
    session_id = _create_session(session_client)["id"]
    runtime = session_manager.get_session(session_id).runtime
    assert runtime.dynamic_num_ccs is True

    pre = session_client.post(
        f"/v1/sessions/{session_id}/nodes",
        json={"task": "pre", "exec_type": "worker", "config": {"num_ccs": 3}},
    )
    assert pre.status_code == 201, pre.text
    pre_node_id = pre.json()["id"]
    assert pre.json()["config"]["num_ccs"] == 3

    session_client.patch(
        "/v1/config", json={"config": {"dynamic_num_ccs": False, "default_num_ccs": 1}}
    )

    assert runtime.dynamic_num_ccs is False
    assert runtime.default_num_ccs == 1
    # 已建节点保持原值（不回溯）
    assert runtime.get_node(pre_node_id).config["num_ccs"] == 3
    # 此后新建的节点按新策略
    post = session_client.post(
        f"/v1/sessions/{session_id}/nodes",
        json={"task": "post", "exec_type": "worker", "config": {"num_ccs": 3}},
    )
    assert post.status_code == 201, post.text
    assert post.json()["config"]["num_ccs"] == 1


def test_num_ccs_policy_rejects_unknown_keys(session_client):
    """白名单仍然生效：未登记的键不能借这个口子进来。"""
    resp = session_client.patch(
        "/v1/config", json={"config": {"totally_unknown_key": 1}}
    )
    assert resp.status_code == 400


def _seed_roles(roles_dir):
    """写入两个 worker 角色，供角色池的存在性校验使用。"""
    worker_dir = roles_dir / "Worker"
    worker_dir.mkdir(parents=True, exist_ok=True)
    for role_id in ("code", "test"):
        (worker_dir / f"{role_id}.yaml").write_text(
            f"summary: {role_id}\n", encoding="utf-8"
        )
    return RolesManager(str(roles_dir))


def test_role_pool_crud_is_isolated_and_persisted(session_manager, tmp_path):
    roles_manager = _seed_roles(tmp_path / "roles")
    store = tmp_path / "pools" / "role_pools.json"
    app = create_session_app(
        session_manager,
        roles_manager,
        role_pools_manager=RolePoolsManager(str(store), roles_manager),
    )

    with TestClient(app) as client:
        assert client.get("/v1/role-pools").json() == {"pools": []}

        created = client.post(
            "/v1/role-pools", json={"name": "博客开发", "roles": ["code", "test"]}
        )
        assert created.status_code == 201, created.text
        pool = created.json()
        assert pool["name"] == "博客开发"
        assert pool["roles"] == ["code", "test"]
        assert client.get("/v1/role-pools").json()["pools"] == [pool]

        # 重名 → 409；未知角色 id / 空名 → 400
        assert client.post(
            "/v1/role-pools", json={"name": "博客开发", "roles": ["code"]}
        ).status_code == 409
        assert client.post(
            "/v1/role-pools", json={"name": "P", "roles": ["ghost"]}
        ).status_code == 400
        assert client.post("/v1/role-pools", json={"name": "", "roles": ["code"]}).status_code == 422

        patched = client.patch(f"/v1/role-pools/{pool['id']}", json={"roles": ["test"]})
        assert patched.status_code == 200, patched.text
        assert patched.json() == {"id": pool["id"], "name": "博客开发", "roles": ["test"]}

        assert client.patch("/v1/role-pools/nope", json={"name": "X"}).status_code == 404
        assert client.delete("/v1/role-pools/nope").status_code == 404
        assert client.delete(f"/v1/role-pools/{pool['id']}").status_code == 204
        assert client.get("/v1/role-pools").json() == {"pools": []}

    # 落盘：新管理器重读同一文件仍能看到（先重建一条，再校验重读）
    reloaded_roles = _seed_roles(tmp_path / "roles")
    fresh = RolePoolsManager(str(store), reloaded_roles)
    assert fresh.list_pools() == []


def test_session_binding_to_candidate_roles_reaches_the_runtime(session_manager, tmp_path):
    roles_manager = _seed_roles(tmp_path / "roles")
    app = create_session_app(
        session_manager,
        roles_manager,
        role_pools_manager=RolePoolsManager(
            str(tmp_path / "role_pools.json"), roles_manager
        ),
    )

    with TestClient(app) as client:
        session_id = _create_session(client)["id"]
        url = f"/v1/sessions/{session_id}/settings"

        assert client.patch(url, json={"config": {"candidate_roles": ["code"]}}).status_code == 200
        # 回显只能证明白名单放行；真正的生效判据是 Runtime 上的候选集
        assert client.get(url).json()["config"]["candidate_roles"] == ["code"]
        assert session_manager.get_session(session_id).runtime.candidate_roles == ["code"]

        # 后续 PATCH 未带该键 → 不得把已有绑定清空
        assert client.patch(url, json={"config": {"num_ccs": 2}}).status_code == 200
        assert client.get(url).json()["config"]["candidate_roles"] == ["code"]
        assert session_manager.get_session(session_id).runtime.candidate_roles == ["code"]

        # 空列表 = 解除绑定（归一为 None → Manager 看到全部角色）
        assert client.patch(url, json={"config": {"candidate_roles": []}}).status_code == 200
        assert session_manager.get_session(session_id).runtime.candidate_roles is None


def test_deleting_a_pool_leaves_bound_sessions_untouched(session_manager, tmp_path):
    """会话存的是角色 id 快照，不是池 id —— 删池不得影响已绑定会话。"""
    roles_manager = _seed_roles(tmp_path / "roles")
    app = create_session_app(
        session_manager,
        roles_manager,
        role_pools_manager=RolePoolsManager(
            str(tmp_path / "role_pools.json"), roles_manager
        ),
    )

    with TestClient(app) as client:
        pool = client.post(
            "/v1/role-pools", json={"name": "纯编码", "roles": ["code", "test"]}
        ).json()
        session_id = _create_session(client)["id"]
        url = f"/v1/sessions/{session_id}/settings"
        client.patch(url, json={"config": {"candidate_roles": pool["roles"]}})

        assert client.delete(f"/v1/role-pools/{pool['id']}").status_code == 204

        assert client.get(url).json()["config"]["candidate_roles"] == ["code", "test"]
        assert session_manager.get_session(session_id).runtime.candidate_roles == ["code", "test"]
