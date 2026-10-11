"""Tests for the multi-session graph API."""

import contextlib
import io
import json
import shutil
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from executables.base import Executable, ExecutableContext
from executables.manager import Manager
from executables.registry import ExecutableRegistry
from executables.worker import Worker
from runtime.models import LogLevel, NodeStatus
from runtime.repo_paths import REPO_ROOT
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
        detail = client.get("/v1/skills/focused-review")
        assert detail.status_code == 200
        assert detail.json()["referencing_roles"] == []
        assert client.get("/v1/skills/../bad").status_code in {400, 404}


def test_a_junk_role_filename_does_not_poison_unrelated_endpoints(
    session_manager, tmp_path
):
    """文件名不是合法 role_id 的角色，不该把「遍历全部角色」的那些接口带崩。

    出处是一次真实的故障：实验遗留的 ``roles/Worker/_mcp_lab_local.yaml`` 让
    ``/v1/mcp/servers`` 返 500、让**每一个** skill 的详情页返 400 —— 用户点开一个
    完全正常的 skill，读到的却是一句「Invalid role_id」。``_`` 开头正是
    ``build_available_roles_text`` 明确跳过的临时角色写法，所以它在
    ``list_roles()`` 这一步就该被挡住，而不是等到逐个读取时才炸。
    """
    roles_dir = tmp_path / "roles"
    worker_dir = roles_dir / "Worker"
    worker_dir.mkdir(parents=True)
    (worker_dir / "code.yaml").write_text("mcp: []\n", encoding="utf-8")
    (worker_dir / "_scratch.yaml").write_text("mcp: []\n", encoding="utf-8")
    registry = tmp_path / "mcp_servers.json"
    registry.write_text(
        json.dumps({"servers": {"keep": {"command": "node"}}}), encoding="utf-8"
    )

    app = create_session_app(
        session_manager,
        RolesManager(str(roles_dir)),
        SkillsManager(str(tmp_path / "skills")),
        mcp_registry_path=str(registry),
    )

    with TestClient(app) as client:
        created = client.post("/v1/skills", json={"skill_id": "tdd", "content": "# tdd\n"})
        assert created.status_code == 201, created.text

        # 非法文件名既不进列表，也不该被当成一个能读的角色
        assert [role["id"] for role in client.get("/v1/roles").json()["roles"]] == ["code"]

        registry_response = client.get("/v1/mcp/servers")
        assert registry_response.status_code == 200, registry_response.text
        assert registry_response.json()["servers"][0]["referencing_roles"] == []

        detail = client.get("/v1/skills/tdd")
        assert detail.status_code == 200, detail.text
        assert detail.json()["referencing_roles"] == []


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


# ---- 可浏览根：复制路径 / 拖入 / 拖出（V3-2.3） ----
#
# 八条路由共用一套落盘机制，所以每个边界只测一次、用 shared 那组做代表，
# 只在「根不同」这件事上（无产出目录 / 产出是文件 / 跨上下文隔离）才把两边都跑。


def _shared_root(session_manager, session_id) -> Path:
    return Path(session_manager.get_session(session_id).runtime.get_shared_workspace())


def _completed_node_with_artifact(runtime, root_id, tmp_path, artifact: str):
    """造一个已经跑完、并且声明了产出路径的节点。"""
    child = runtime.create_manual_node(task="Produce", parent_id=root_id)
    workspace = Path(runtime.get_node_workspace(child.id))
    target = workspace / artifact
    if artifact.endswith("/"):
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("done", encoding="utf-8")
    runtime.update_node_status(
        child.id, NodeStatus.COMPLETED, artifact_path=artifact.rstrip("/")
    )
    return child


def test_upload_to_shared_writes_exact_bytes(session_client, session_manager):
    session_id = _create_session(session_client)["id"]
    response = session_client.post(
        f"/v1/sessions/{session_id}/shared/upload",
        params={"path": "", "name": "notes.txt"},
        content=b"hello \xe4\xb8\xad\xe6\x96\x87",
    )
    assert response.status_code == 201, response.text
    assert response.json() == {
        "name": "notes.txt", "path": "notes.txt", "size": 12, "renamed": False,
    }
    assert (_shared_root(session_manager, session_id) / "notes.txt").read_bytes() == (
        b"hello \xe4\xb8\xad\xe6\x96\x87"
    )
    # 列表里看得见，且是刚写进去的那份
    listing = session_client.get(f"/v1/sessions/{session_id}/shared").json()
    assert [entry["name"] for entry in listing["entries"]] == ["notes.txt"]


def test_upload_creates_nested_directories(session_client, session_manager):
    session_id = _create_session(session_client)["id"]
    response = session_client.post(
        f"/v1/sessions/{session_id}/shared/upload",
        params={"path": "refs/2026/papers", "name": "a.pdf"},
        content=b"pdf",
    )
    assert response.status_code == 201, response.text
    assert response.json()["path"] == "refs/2026/papers/a.pdf"
    assert (_shared_root(session_manager, session_id) / "refs/2026/papers/a.pdf").read_bytes() == b"pdf"


def test_upload_same_name_renames_and_never_overwrites(session_client, session_manager):
    """覆盖是这里唯一不可接受的失败：用户拖进来的可能是他手上唯一的那份。"""
    session_id = _create_session(session_client)["id"]
    url = f"/v1/sessions/{session_id}/shared/upload"
    first = session_client.post(url, params={"name": "a.txt"}, content=b"first")
    second = session_client.post(url, params={"name": "a.txt"}, content=b"second")
    assert first.json()["name"] == "a.txt" and first.json()["renamed"] is False
    assert second.json()["name"] == "a-1.txt" and second.json()["renamed"] is True
    root = _shared_root(session_manager, session_id)
    assert (root / "a.txt").read_bytes() == b"first"
    assert (root / "a-1.txt").read_bytes() == b"second"


def test_upload_empty_file_is_not_an_error(session_client, session_manager):
    session_id = _create_session(session_client)["id"]
    response = session_client.post(
        f"/v1/sessions/{session_id}/shared/upload",
        params={"name": "empty.bin"}, content=b"",
    )
    assert response.status_code == 201
    assert response.json()["size"] == 0


@pytest.mark.parametrize("path, name", [
    ("../escape", "a.txt"),      # 越界
    ("/etc", "a.txt"),           # 绝对路径
    ("", "a/b.txt"),             # 文件名里带分隔符
    ("", "..\\b.txt"),           # 反斜杠分隔符
    ("sub/../../out", "a.txt"),  # 中间层越界
])
def test_upload_rejects_traversal(session_client, session_manager, path, name):
    session_id = _create_session(session_client)["id"]
    response = session_client.post(
        f"/v1/sessions/{session_id}/shared/upload",
        params={"path": path, "name": name}, content=b"x",
    )
    assert response.status_code == 400, response.text
    # 拒绝就是拒绝：根目录下不能因为这个请求多出任何东西
    root = _shared_root(session_manager, session_id)
    assert not root.exists() or list(root.rglob("*")) == []


def test_upload_into_framework_directory_is_refused(session_client, session_manager):
    """cc_0 / .claude 这类目录会被框架 rmtree 重建 —— 放进去等于静默丢失。"""
    session_id = _create_session(session_client)["id"]
    for path in ["cc_0", "deliverables", "refs/.git", "a/cc_1/b"]:
        response = session_client.post(
            f"/v1/sessions/{session_id}/shared/upload",
            params={"path": path, "name": "a.txt"}, content=b"x",
        )
        assert response.status_code == 400, f"{path} 应当被拒绝: {response.text}"


def test_upload_size_limit_returns_413_without_leaving_debris(
    session_client, session_manager, monkeypatch
):
    from runtime import file_transfer

    monkeypatch.setattr(file_transfer, "MAX_UPLOAD_BYTES", 16)
    session_id = _create_session(session_client)["id"]
    response = session_client.post(
        f"/v1/sessions/{session_id}/shared/upload",
        params={"name": "big.bin"}, content=b"x" * 64,
    )
    assert response.status_code == 413, response.text
    root = _shared_root(session_manager, session_id)
    assert list(root.iterdir()) == []  # 连 .part 都不留


def test_download_round_trips_and_encodes_non_ascii_filename(
    session_client, session_manager
):
    session_id = _create_session(session_client)["id"]
    payload = b"\x00\x01binary\xff"
    session_client.post(
        f"/v1/sessions/{session_id}/shared/upload",
        params={"name": "报告.pdf"}, content=payload,
    )
    response = session_client.get(
        f"/v1/sessions/{session_id}/shared/download", params={"path": "报告.pdf"},
    )
    assert response.status_code == 200
    assert response.content == payload
    disposition = response.headers["content-disposition"]
    # attachment 是拖出成立的前提；中文名必须走 RFC 5987 那一支
    assert disposition.startswith("attachment")
    assert "filename*=utf-8''" in disposition


def test_download_directory_is_rejected(session_client, session_manager):
    session_id = _create_session(session_client)["id"]
    session_client.post(
        f"/v1/sessions/{session_id}/shared/upload",
        params={"path": "refs", "name": "a.txt"}, content=b"x",
    )
    response = session_client.get(
        f"/v1/sessions/{session_id}/shared/download", params={"path": "refs"},
    )
    assert response.status_code == 400, response.text


def test_resolve_path_returns_a_real_absolute_path(session_client, session_manager):
    """「复制路径」要的就是这个 —— 且这是唯一交出真实根的地方。"""
    session_id = _create_session(session_client)["id"]
    session_client.post(
        f"/v1/sessions/{session_id}/shared/upload",
        params={"path": "refs", "name": "a.txt"}, content=b"x",
    )
    root = _shared_root(session_manager, session_id)

    # path="" 合法 —— 「复制当前目录路径」是这里最常用的一条
    assert session_client.get(
        f"/v1/sessions/{session_id}/shared/path"
    ).json()["abs_path"] == str(root)
    info = session_client.get(
        f"/v1/sessions/{session_id}/shared/path", params={"path": "refs/a.txt"},
    ).json()
    assert info == {
        "path": "refs/a.txt",
        "abs_path": str(root / "refs/a.txt"),
        "name": "a.txt",
        "type": "file",
    }
    assert session_client.get(
        f"/v1/sessions/{session_id}/shared/path", params={"path": "../x"},
    ).status_code == 400


# ---------------------------------------------------------------------------
# 共享根「还不存在」这一状态：两个读端点口径必须一致
#
# 起因：新建会话后点共享区面包屑上的「复制路径」→ 后端回 404，而同一时刻列目录
# 明明显示着一个空目录。根因是共享根原先只在「节点开跑」（runtime._prepare_node_config）
# 和「连终端」（PTY 端点）时被懒创建，而**列目录**与**解析路径**对「根还没建」
# 给出了相反的答案：前者按注释里点名的「空白图会话从未创建节点」返回空树，后者 404。
#
# 现在由 _build_handle 保证会话一建立共享根就在，下面这两条读路径只做兜底。
# ---------------------------------------------------------------------------


def test_a_new_session_already_has_its_shared_root(session_client, session_manager):
    """会话一建立，派生的共享根就在 —— 不必等第一个节点开跑。"""
    session_id = _create_session(session_client)["id"]

    assert _shared_root(session_manager, session_id).is_dir()


def test_resolve_path_still_answers_when_the_shared_root_is_gone(
    session_client, session_manager
):
    """根不在时（老会话、或外部项目目录被删）复制路径仍要给出路径，而不是 404 ——
    与 list_session_shared 对同一状态返回空树保持同一个口径。"""
    session_id = _create_session(session_client)["id"]
    root = _shared_root(session_manager, session_id)
    # ignore_errors：这一步只是把场景布置成「根不在」，不该成为断言失败的原因。
    shutil.rmtree(root, ignore_errors=True)

    response = session_client.get(f"/v1/sessions/{session_id}/shared/path")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "path": ".",
        "abs_path": str(root),
        "name": root.name,
        "type": "directory",
    }
    # 解析不该顺手把目录造出来：这是读端点，建目录的职责在会话建立/保存设置那两处。
    assert not root.exists()


def test_resolve_path_still_404s_for_a_missing_subpath(session_client):
    """只有**根本身**享受这个待遇：子路径不存在就是真的找不到。"""
    session_id = _create_session(session_client)["id"]

    response = session_client.get(
        f"/v1/sessions/{session_id}/shared/path", params={"path": "nope.txt"},
    )

    assert response.status_code == 404


def test_clearing_the_project_dir_creates_the_fallback_shared_root(
    session_client, session_manager, tmp_path
):
    """清除外部项目目录 → 落回派生路径 {workspace_root}/shared，那个目录也得在。

    这条分支原先没人建目录：显式那一支有 makedirs，回到默认这一支没有。
    """
    session_id = _create_session(session_client)["id"]
    derived = _shared_root(session_manager, session_id)
    external = tmp_path / "external-project"

    session_client.patch(
        f"/v1/sessions/{session_id}/settings",
        json={"config": {}, "shared_workspace": str(external)},
    )
    assert external.is_dir()
    shutil.rmtree(derived, ignore_errors=True)  # 派生路径这期间被删了（或从未建过）

    response = session_client.patch(
        f"/v1/sessions/{session_id}/settings",
        json={"config": {}, "shared_workspace": None},
    )

    assert response.status_code == 200, response.text
    assert derived.is_dir()


def test_changing_workspace_root_creates_the_shared_root_under_the_new_one(
    session_client, tmp_path
):
    """改工作区根会让派生共享根整个挪地方 —— 新落点同样得建。"""
    session_id = _create_session(session_client)["id"]
    moved = tmp_path / "moved"

    response = session_client.patch(
        f"/v1/sessions/{session_id}/settings",
        json={"config": {}, "workspace_root": str(moved)},
    )

    assert response.status_code == 200, response.text
    assert (moved / "shared").is_dir()


def test_explicit_project_dir_is_created_on_creation(session_client, tmp_path):
    """建会话时显式指定的项目目录，按 validate_shared_workspace 的约定由调用方创建。"""
    external = tmp_path / "external-project"

    session_client.post("/v1/sessions", json={
        "task": "t", "config": {"shared_workspace": str(external)},
    })

    assert external.is_dir()


def test_restore_does_not_resurrect_an_explicit_project_dir(runtime, tmp_path):
    """_build_handle 只在**派生**路径上建目录；显式配置的外部项目目录不碰。

    它是每个历史会话在服务启动时都会跑一遍的路径：外部目录若只是暂时不可达
    （网络盘没挂上），在它的位置造一个空目录会把真正的挂载点盖住，还会把
    「路径配错了」伪装成「建好了」。
    """
    root_dir = tmp_path / "sessions"
    external = tmp_path / "external-project"
    manager = SessionManager(
        registry=runtime.registry, root_dir=str(root_dir),
        workspace_root=str(tmp_path), restore=False,
    )
    try:
        session_id = manager.create_session(
            "t", config={"shared_workspace": str(external)}
        )["id"]
        assert external.is_dir()
        shutil.rmtree(external)
    finally:
        manager.shutdown()

    restored = SessionManager(
        registry=runtime.registry, root_dir=str(root_dir),
        workspace_root=str(tmp_path), restore=True,
    )
    try:
        assert restored.get_session(
            session_id
        ).runtime.get_shared_workspace() == str(external)
        assert not external.exists()
    finally:
        restored.shutdown()


def test_reveal_is_refused_off_macos_without_shelling_out(
    session_client, session_manager, monkeypatch
):
    calls = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("runtime.runtime.subprocess.run", lambda *a, **k: calls.append(a))
    session_id = _create_session(session_client)["id"]
    response = session_client.post(f"/v1/sessions/{session_id}/shared/reveal")
    assert response.status_code == 501, response.text
    assert calls == []  # 不支持的平台上一次子进程都不该起


def test_reveal_on_macos_calls_open_with_an_argv_list(
    session_client, session_manager, monkeypatch
):
    calls = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "runtime.runtime.subprocess.run",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )
    session_id = _create_session(session_client)["id"]
    session_client.post(
        f"/v1/sessions/{session_id}/shared/upload",
        params={"name": "a.txt"}, content=b"x",
    )
    response = session_client.post(
        f"/v1/sessions/{session_id}/shared/reveal", params={"path": "a.txt"},
    )
    assert response.status_code == 200, response.text
    root = _shared_root(session_manager, session_id)
    argv, kwargs = calls[0]
    assert argv == ["open", "-R", str(root / "a.txt")]
    assert "shell" not in kwargs  # list 形式，不经 shell，没有注入面


def test_node_results_upload_lands_in_the_declared_output_dir(
    session_client, session_manager
):
    session_id = _create_session(session_client)["id"]
    runtime = session_manager.get_session(session_id).runtime
    root = runtime.create_manual_node(task="Root", exec_type="manager")
    child = _completed_node_with_artifact(runtime, root.id, None, "out/")
    response = session_client.post(
        f"/v1/sessions/{session_id}/nodes/{child.id}/results/upload",
        params={"name": "extra.txt"}, content=b"reference",
    )
    assert response.status_code == 201, response.text
    assert response.json()["path"] == "extra.txt"
    workspace = Path(runtime.get_node_workspace(child.id))
    assert (workspace / "out" / "extra.txt").read_bytes() == b"reference"


def test_node_results_upload_refuses_when_there_is_nowhere_to_land(
    session_client, session_manager
):
    """没产出目录 / 产出是个文件 —— 两条都**不**退回到别处去落。"""
    session_id = _create_session(session_client)["id"]
    runtime = session_manager.get_session(session_id).runtime
    root = runtime.create_manual_node(task="Root", exec_type="manager")

    never_ran = runtime.create_manual_node(task="Pending", parent_id=root.id)
    response = session_client.post(
        f"/v1/sessions/{session_id}/nodes/{never_ran.id}/results/upload",
        params={"name": "a.txt"}, content=b"x",
    )
    assert response.status_code == 404, response.text

    single_file = _completed_node_with_artifact(runtime, root.id, None, "report.md")
    response = session_client.post(
        f"/v1/sessions/{session_id}/nodes/{single_file.id}/results/upload",
        params={"name": "a.txt"}, content=b"x",
    )
    assert response.status_code == 400, response.text
    # 那个文件本身一个字节都没被碰
    workspace = Path(runtime.get_node_workspace(single_file.id))
    assert (workspace / "report.md").read_text(encoding="utf-8") == "done"


def test_shared_and_node_results_roots_do_not_leak_into_each_other(
    session_client, session_manager
):
    """两条入口各写各的根 —— 写串了就是「东西放到了你没想到的地方」。"""
    session_id = _create_session(session_client)["id"]
    runtime = session_manager.get_session(session_id).runtime
    root = runtime.create_manual_node(task="Root", exec_type="manager")
    child = _completed_node_with_artifact(runtime, root.id, None, "out/")
    workspace = Path(runtime.get_node_workspace(child.id))

    session_client.post(
        f"/v1/sessions/{session_id}/shared/upload",
        params={"name": "shared-only.txt"}, content=b"x",
    )
    session_client.post(
        f"/v1/sessions/{session_id}/nodes/{child.id}/results/upload",
        params={"name": "node-only.txt"}, content=b"x",
    )

    assert not (_shared_root(session_manager, session_id) / "node-only.txt").exists()
    assert not (workspace / "node-only.txt").exists()
    assert not (workspace / "out" / "shared-only.txt").exists()
    assert (workspace / "out" / "node-only.txt").exists()


def test_upload_to_another_sessions_node_is_rejected(session_client, session_manager):
    """别把「这个节点不在这条会话里」当成 404 —— 它和同族的
    ``read_node_result_file`` / ``list_node_results`` 一样走 ``_require_node`` 的
    ValueError → 400。同一个「节点找不到」在读写两条路上必须报同一件事。"""
    first = _create_session(session_client)["id"]
    second = _create_session(session_client)["id"]
    runtime = session_manager.get_session(first).runtime
    root = runtime.create_manual_node(task="Root", exec_type="manager")
    child = _completed_node_with_artifact(runtime, root.id, None, "out/")
    response = session_client.post(
        f"/v1/sessions/{second}/nodes/{child.id}/results/upload",
        params={"name": "a.txt"}, content=b"x",
    )
    assert response.status_code == 400
    # 而且什么都没写进去
    assert list((Path(runtime.get_node_workspace(child.id)) / "out").iterdir()) == []


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


def test_artifact_endpoint_lists_directory_and_reads_file(session_client, session_manager):
    """产物是文件时返回内容；是目录时返回一层清单而不是 500。

    Manager 的 artifact_path 就是它的 workspace（目录），此前 open() 抛
    IsADirectoryError 一路冒成 500，前端拿到的是「服务端错误」。
    """
    session = _create_session(session_client, "Inspect artifact")
    session_id = session["id"]
    node = session_client.post(
        f"/v1/sessions/{session_id}/nodes",
        json={"task": "t", "exec_type": "worker"},
    ).json()
    node_id = node["id"]
    runtime = session_manager.get_session(session_id).runtime

    # 未产出：形状不变
    assert session_client.get(
        f"/v1/sessions/{session_id}/nodes/{node_id}/artifact"
    ).json() == {"path": None, "content": None}

    output_dir = Path(runtime.get_node_workspace(node_id)) / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.md").write_text("# Report", encoding="utf-8")
    node = runtime.get_node(node_id)

    # 目录：目录清单，条目口径与 /results 一致
    runtime.update_node_status(node_id, node.status, artifact_path=str(output_dir))
    # 反证：get_artifact 面对目录本来就是 IsADirectoryError（旧端点未捕获 → 500），
    # 所以端点必须先判目录，而不是指望读文件成功。
    with pytest.raises(IsADirectoryError):
        runtime.get_artifact(node_id, runtime.get_node(node_id).artifact_path)
    assert runtime.list_artifact_dir(node_id, "output") is not None
    assert runtime.list_artifact_dir(node_id, "output/report.md") is None
    listed = session_client.get(
        f"/v1/sessions/{session_id}/nodes/{node_id}/artifact"
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["path"] == runtime.get_node(node_id).artifact_path == "output"
    assert body["content"] is None
    assert body["artifact_kind"] == "directory"
    assert [entry["name"] for entry in body["entries"]] == ["report.md"]
    assert body["entries"][0]["path"] == "report.md"

    # 文件：响应逐字不变，仍然是 {path, content}
    runtime.update_node_status(node_id, node.status, artifact_path=str(output_dir / "report.md"))
    read = session_client.get(
        f"/v1/sessions/{session_id}/nodes/{node_id}/artifact"
    )
    assert read.status_code == 200, read.text
    assert read.json() == {
        "path": runtime.get_node(node_id).artifact_path,
        "content": "# Report",
    }
    assert read.json()["path"] == "output/report.md"


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
        # 遗留 num_ccs（default.yaml 里仍写着）不进 defaults / 字段目录
        assert "num_ccs" not in worker_schema.json()["defaults"]
        worker_fields = {field["name"]: field for field in worker_schema.json()["fields"]}
        assert worker_fields["main_doc"]["has_default"] is False
        assert "num_ccs" not in worker_fields
        assert "index_file" not in worker_fields
        assert worker_fields["description"]["type"] == "text"
        manager_schema = client.get("/v1/roles/schema/manager")
        assert manager_schema.json()["defaults"]["model"] == "manager-default"
        manager_fields = {field["name"]: field for field in manager_schema.json()["fields"]}
        assert "num_ccs" not in manager_schema.json()["defaults"]
        assert "num_ccs" not in manager_fields
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
        # 遗留 num_ccs 被静默剥除，不落盘、不回显
        assert role["config"] == {
            "main_doc": "README.md",
            "role_md": "Write focused output.",
        }
        assert "num_ccs" not in (
            roles_dir / "Worker" / "focused_writer.yaml"
        ).read_text(encoding="utf-8")
        assert role["effective_config"]["model"] == "worker-default"
        assert "model:" not in (
            roles_dir / "Worker" / "focused_writer.yaml"
        ).read_text(encoding="utf-8")

        invalid_type = client.post(
            "/v1/roles",
            json={
                "role_id": "invalid_type",
                "role_type": "worker",
                "config": {"main_doc": 5},
            },
        )
        assert invalid_type.status_code == 400
        # 遗留键即使值非法也直接被忽略，不触发类型校验
        legacy_only = client.post(
            "/v1/roles",
            json={
                "role_id": "legacy_only",
                "role_type": "worker",
                "config": {"num_ccs": "many", "audit_rounds": "x", "main_doc": "README.md"},
            },
        )
        assert legacy_only.status_code == 201, legacy_only.text
        assert "num_ccs" not in legacy_only.json()["config"]
        assert "audit_rounds" not in legacy_only.json()["config"]
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


def test_effective_base_url_follows_the_model(session_client):
    """连接表单显示「生效地址」用的端点：换模型就换地址。

    传的是**表单里当前的值**（可能还没保存），所以只走 query，不读全局配置。
    """
    resp = session_client.get(
        "/v1/effective-base-url",
        params={"cli_name": "claude", "model": "glm-5.2", "api_base_url": ""},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "source": "model_vendor",
    }

    resp = session_client.get(
        "/v1/effective-base-url",
        params={"cli_name": "claude", "model": "kimi-k3", "api_base_url": ""},
    )
    assert resp.json() == {
        "base_url": "https://api.moonshot.cn/anthropic",
        "source": "model_vendor",
    }


def test_effective_base_url_uses_provider_route(session_client):
    for cli_name, expected_url in (
        ("claude", "https://openrouter.ai/api"),
        ("codex", "https://openrouter.ai/api/v1"),
        ("codebuddy", "https://openrouter.ai/api/v1"),
    ):
        resp = session_client.get(
            "/v1/effective-base-url",
            params={
                "cli_name": cli_name,
                "model": "anthropic/claude-sonnet-4.5",
                "api_base_url": "https://openrouter.ai/api/v1",
                "provider_id": "openrouter",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"base_url": expected_url, "source": "configured"}


def test_effective_base_url_keeps_user_gateway(session_client):
    """用户手填的网关不被改写 —— 换模型也不动。"""
    params = {"cli_name": "codex", "model": "gpt-5.6", "api_base_url": "https://my-gw.corp/v1"}
    resp = session_client.get("/v1/effective-base-url", params=params)
    assert resp.json() == {"base_url": "https://my-gw.corp/v1", "source": "configured"}

    params["model"] = "deepseek-v4-pro"
    resp = session_client.get("/v1/effective-base-url", params=params)
    assert resp.json() == {"base_url": "https://my-gw.corp/v1", "source": "configured"}


def test_effective_base_url_missing_params_degrade_quietly(session_client):
    """表单刚打开、什么都还没填时不能 500。"""
    resp = session_client.get("/v1/effective-base-url")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"base_url": None, "source": "cli_default"}


# ============================================================================
def test_provider_id_persists_and_is_inherited_by_new_session(session_client, session_manager):
    resp = session_client.patch(
        "/v1/config",
        json={
            "config": {
                "cli_name": "claude",
                "model": "anthropic/claude-sonnet-4.5",
                "provider_id": "openrouter",
            }
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["provider_id"] == "openrouter"
    assert session_client.get("/v1/config").json()["config"]["provider_id"] == "openrouter"

    session_id = _create_session(session_client)["id"]
    assert session_manager.get_settings(session_id)["config"]["provider_id"] == "openrouter"


# ============================================================================
# 遗留全局配置键（dynamic_num_ccs / default_num_ccs）静默丢弃
# ============================================================================

def test_legacy_num_ccs_global_config_keys_are_silently_dropped(session_client, session_manager):
    """旧前端/旧脚本仍可能 PATCH 这两个键：必须 2xx，且不出现在返回值或存储里。"""
    before = session_client.get("/v1/config").json()["config"]
    assert "dynamic_num_ccs" not in before
    assert "default_num_ccs" not in before

    resp = session_client.patch(
        "/v1/config",
        json={"config": {"dynamic_num_ccs": False, "default_num_ccs": 3}},
    )
    assert resp.status_code == 200, resp.text
    assert "dynamic_num_ccs" not in resp.json()["config"]
    assert "default_num_ccs" not in resp.json()["config"]

    stored = session_client.get("/v1/config").json()["config"]
    assert "dynamic_num_ccs" not in stored
    assert "default_num_ccs" not in stored
    assert "dynamic_num_ccs" not in session_manager.get_global_config()
    assert "default_num_ccs" not in session_manager.get_global_config()


def test_legacy_num_ccs_global_keys_do_not_block_valid_fields(session_client):
    """遗留键与合法键同批提交：合法键照常生效，遗留键即使值非法（0/"abc"/None）也不 4xx。"""
    resp = session_client.patch(
        "/v1/config",
        json={"config": {"default_num_ccs": 0, "dynamic_num_ccs": "abc", "max_concurrency": 3}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["max_concurrency"] == 3
    assert "default_num_ccs" not in resp.json()["config"]
    assert "dynamic_num_ccs" not in resp.json()["config"]

    resp = session_client.patch("/v1/config", json={"config": {"default_num_ccs": None}})
    assert resp.status_code == 200, resp.text
    assert session_client.get("/v1/config").json()["config"]["max_concurrency"] == 3


def test_legacy_num_ccs_in_old_config_file_is_not_loaded(session_client, tmp_path):
    """旧 config.json 里残留的遗留键重新加载后不会出现在全局配置里。"""
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
        assert "dynamic_num_ccs" not in config
        assert "default_num_ccs" not in config
    finally:
        reloaded.shutdown()


def test_node_config_num_ccs_is_silently_stripped(session_client, session_manager):
    """创建节点时带遗留 num_ccs/audit_rounds：201，且不存入 node.config。"""
    session_id = _create_session(session_client)["id"]
    runtime = session_manager.get_session(session_id).runtime

    resp = session_client.post(
        f"/v1/sessions/{session_id}/nodes",
        json={
            "task": "t", "exec_type": "worker",
            "config": {"num_ccs": 3, "audit_rounds": 2},
        },
    )
    assert resp.status_code == 201, resp.text
    assert "num_ccs" not in resp.json()["config"]
    assert "audit_rounds" not in resp.json()["config"]
    assert "num_ccs" not in runtime.get_node(resp.json()["id"]).config


def test_global_config_rejects_unknown_keys(session_client):
    """白名单仍然生效：未登记的键不能借遗留键的静默丢弃口子进来。"""
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
        assert client.patch(url, json={"config": {"num_ccs": 2}}).status_code == 200  # 遗留键，静默忽略
        assert client.get(url).json()["config"]["candidate_roles"] == ["code"]
        assert session_manager.get_session(session_id).runtime.candidate_roles == ["code"]

        # 空列表 = 解除绑定（归一为 None → Manager 看到全部角色）
        assert client.patch(url, json={"config": {"candidate_roles": []}}).status_code == 200
        assert session_manager.get_session(session_id).runtime.candidate_roles is None


def test_candidate_roles_can_be_bound_when_the_session_is_created(session_manager, tmp_path):
    """建会话时直接带 candidate_roles：一步到位，Runtime 上的候选集立即生效。"""
    roles_manager = _seed_roles(tmp_path / "roles")
    app = create_session_app(session_manager, roles_manager)

    with TestClient(app) as client:
        created = client.post(
            "/v1/sessions",
            json={"task": "", "title": "bound", "config": {"candidate_roles": ["code", "test", "code"]}},
        )
        assert created.status_code == 201
        session_id = created.json()["id"]

        assert client.get(f"/v1/sessions/{session_id}/settings").json()["config"]["candidate_roles"] == ["code", "test", "code"]
        # 重复 id 在装入 Runtime 时归一（去重保序）
        assert session_manager.get_session(session_id).runtime.candidate_roles == ["code", "test"]


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


# =============================================================================
# MCP 注册表：设置页「MCP」标签页背后的增删改查与连通性测试
#
# 隔离手法与角色/技能一致：注册表指向 tmp_path 里的一份临时文件，绝不碰仓库根那份真的
# mcp_servers.json —— 它随 git 跟踪，被测试写坏会直接进 diff。
# =============================================================================

#: 仓库自带的探针 server（注册表里的 echo-probe）：真起一次子进程就能验「测试连接」。
ECHO_SPEC = {"name": "echo-probe", "command": "python3", "args": ["tests/mcp_echo_server.py"]}


def _mcp_app(session_manager, tmp_path: Path, servers: dict | None = None, roles=()):
    """建一个注册表与角色目录都隔离的 app，返回 ``(app, 注册表路径)``。

    ``roles`` 是 ``(role_type, role_id, config)`` 序列，用来测「谁引用了这个 server」。
    """
    registry = tmp_path / "mcp_servers.json"
    registry.write_text(
        json.dumps(
            {"_comment": ["keep me"], "servers": servers or {}}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    roles_dir = tmp_path / "roles"
    for role_type, role_id, config in roles:
        target = roles_dir / role_type.capitalize() / f"{role_id}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    app = create_session_app(
        session_manager,
        RolesManager(str(roles_dir)),
        SkillsManager(str(tmp_path / "skills")),
        mcp_registry_path=str(registry),
    )
    return app, registry


def _servers(client) -> list[dict]:
    return client.get("/v1/mcp/servers").json()["servers"]


class TestMcpRegistryEndpoints:
    def test_crud_round_trip(self, session_manager, tmp_path):
        app, _ = _mcp_app(session_manager, tmp_path, {"keep": {"command": "node"}})

        with TestClient(app) as client:
            created = client.post("/v1/mcp/servers", json={**ECHO_SPEC, "description": "探针"})
            assert created.status_code == 201, created.text
            assert created.json()["required"] is True

            assert [item["name"] for item in _servers(client)] == ["echo-probe", "keep"]

            updated = client.patch(
                "/v1/mcp/servers/echo-probe", json={**ECHO_SPEC, "description": "探针", "required": False}
            )
            assert updated.status_code == 200
            assert updated.json()["required"] is False

            assert client.delete("/v1/mcp/servers/echo-probe").status_code == 204
            assert [item["name"] for item in _servers(client)] == ["keep"]

    def test_duplicate_name_conflicts(self, session_manager, tmp_path):
        app, _ = _mcp_app(session_manager, tmp_path, {"echo-probe": {"command": "node"}})
        with TestClient(app) as client:
            assert client.post("/v1/mcp/servers", json=ECHO_SPEC).status_code == 409

    def test_bad_entry_can_be_repaired_and_new_create_preserves_it(
        self, session_manager, tmp_path
    ):
        """设置页必须能在其它坏条目存在时创建新条目并修复坏条目。"""
        app, registry = _mcp_app(
            session_manager, tmp_path, {"broken": {"command": ""}}
        )
        with TestClient(app) as client:
            created = client.post(
                "/v1/mcp/servers", json={**ECHO_SPEC, "name": "fresh"}
            )
            assert created.status_code == 201, created.text
            raw_after_create = json.loads(registry.read_text(encoding="utf-8"))
            assert raw_after_create["servers"]["broken"] == {"command": ""}
            assert "fresh" in raw_after_create["servers"]

            repaired = client.patch(
                "/v1/mcp/servers/broken",
                json={**ECHO_SPEC, "name": "broken"},
            )
            assert repaired.status_code == 200, repaired.text

        raw_after_repair = json.loads(registry.read_text(encoding="utf-8"))
        assert raw_after_repair["servers"]["broken"]["command"] == "python3"
        assert raw_after_repair["servers"]["broken"]["args"] == ECHO_SPEC["args"]

    def test_duplicate_409_does_not_rewrite_a_bad_entry(self, session_manager, tmp_path):
        app, registry = _mcp_app(
            session_manager, tmp_path, {"broken": {"command": "", "keep": "me"}}
        )
        before = registry.read_bytes()
        with TestClient(app) as client:
            rejected = client.post(
                "/v1/mcp/servers",
                json={**ECHO_SPEC, "name": "broken"},
            )
        assert rejected.status_code == 409
        assert registry.read_bytes() == before

    def test_malformed_registry_is_400_for_read_create_and_update_without_rewrite(
        self, session_manager, tmp_path
    ):
        registry = tmp_path / "mcp_servers.json"
        registry.write_text("{ not json", encoding="utf-8")
        before = registry.read_bytes()
        app = create_session_app(
            session_manager,
            RolesManager(str(tmp_path / "roles")),
            SkillsManager(str(tmp_path / "skills")),
            mcp_registry_path=str(registry),
        )
        with TestClient(app) as client:
            responses = [
                client.get("/v1/mcp/servers"),
                client.post("/v1/mcp/servers", json=ECHO_SPEC),
                client.patch("/v1/mcp/servers/echo-probe", json=ECHO_SPEC),
            ]
        assert [response.status_code for response in responses] == [400, 400, 400]
        assert registry.read_bytes() == before

    def test_name_cannot_be_changed(self, session_manager, tmp_path):
        """名字是注册表的键、也是角色 yaml 里的引用，改它要同步重写所有引用方。

        与角色 id 同一惯例：不可改，要改就是新建 + 删旧。
        """
        app, _ = _mcp_app(session_manager, tmp_path, {"echo-probe": {"command": "node"}})
        with TestClient(app) as client:
            renamed = client.patch("/v1/mcp/servers/echo-probe", json={**ECHO_SPEC, "name": "other"})
            assert renamed.status_code == 400

    def test_updating_or_deleting_unknown_name_is_404(self, session_manager, tmp_path):
        app, _ = _mcp_app(session_manager, tmp_path)
        with TestClient(app) as client:
            assert client.patch(
                "/v1/mcp/servers/nope", json={**ECHO_SPEC, "name": "nope"}
            ).status_code == 404
            assert client.delete("/v1/mcp/servers/nope").status_code == 404

    def test_invalid_name_is_rejected_before_it_reaches_the_registry(
        self, session_manager, tmp_path
    ):
        """名字最终会变成 CLI 的配置键（grok 的 TOML 表名、codex 的 -c 键路径）。

        带 `.` 的写法在入口就该挡掉 —— 放过它，错误会延后到某个 CLI 报一句与 MCP 无关的话。
        """
        app, registry = _mcp_app(session_manager, tmp_path)
        with TestClient(app) as client:
            rejected = client.post("/v1/mcp/servers", json={**ECHO_SPEC, "name": "echo.probe"})
            assert rejected.status_code == 422
        assert json.loads(registry.read_text(encoding="utf-8"))["servers"] == {}

    def test_blank_command_is_rejected_by_the_business_layer(self, session_manager, tmp_path):
        """Pydantic 的 min_length 挡不住全空白串，那要业务层的 validate_server 挡。"""
        app, registry = _mcp_app(session_manager, tmp_path)
        with TestClient(app) as client:
            rejected = client.post("/v1/mcp/servers", json={**ECHO_SPEC, "command": "   "})
            assert rejected.status_code == 400
        assert json.loads(registry.read_text(encoding="utf-8"))["servers"] == {}

    def test_saving_does_not_rewrite_relative_paths(self, session_manager, tmp_path):
        """在界面上保存一次，不能把注册表里的相对路径换成写死在本机的绝对路径。

        相对路径是「clone 下来即可用」这条约定的载体；被换成绝对路径后功能照常，换台机器
        才炸。顺带守住 _comment —— 那是写给改这个文件的人看的说明。
        """
        app, registry = _mcp_app(session_manager, tmp_path)
        with TestClient(app) as client:
            assert client.post("/v1/mcp/servers", json=ECHO_SPEC).status_code == 201

        text = registry.read_text(encoding="utf-8")
        assert "tests/mcp_echo_server.py" in text
        assert str(REPO_ROOT) not in text, "相对路径被写成了本机绝对路径"
        assert json.loads(text)["_comment"] == ["keep me"]

    def test_broken_registry_is_a_readable_error_not_a_crash(self, session_manager, tmp_path):
        registry = tmp_path / "mcp_servers.json"
        registry.write_text("{ not json", encoding="utf-8")
        app = create_session_app(
            session_manager,
            RolesManager(str(tmp_path / "roles")),
            SkillsManager(str(tmp_path / "skills")),
            mcp_registry_path=str(registry),
        )
        with TestClient(app) as client:
            assert client.get("/v1/mcp/servers").status_code == 400


class TestMcpRegistryReferencingRoles:
    """删除确认框里那行「还有谁在用」就是这里返回的东西。"""

    def test_roles_declaring_the_name_are_reported(self, session_manager, tmp_path):
        app, _ = _mcp_app(
            session_manager,
            tmp_path,
            {"echo-probe": {"command": "node"}},
            roles=[
                ("worker", "mcp_probe", {"mcp": ["echo-probe"]}),
                ("worker", "plain", {"skills": []}),
            ],
        )
        with TestClient(app) as client:
            assert _servers(client)[0]["referencing_roles"] == [
                {"id": "mcp_probe", "type": "worker"}
            ]

    def test_mcp_declared_in_default_yaml_counts_for_every_role_of_that_type(
        self, session_manager, tmp_path
    ):
        """在 default.yaml 里声明 mcp 是最常见的用法 —— 那样该类型下每个角色都在引用。

        只看角色自己那份 yaml 会漏掉全部引用者，用户就会以为这个 server 没人用，删掉。
        """
        app, _ = _mcp_app(
            session_manager,
            tmp_path,
            {"echo-probe": {"command": "node"}},
            roles=[
                ("worker", "default", {"mcp": ["echo-probe"]}),
                ("worker", "dev", {}),
                ("manager", "boss", {}),
            ],
        )
        with TestClient(app) as client:
            # default 本身不是可选角色（list_roles 跳过），被算作引用者的是继承了它的 dev；
            # manager 那边没有 default.yaml，不该被算进来。
            assert _servers(client)[0]["referencing_roles"] == [{"id": "dev", "type": "worker"}]


class TestMcpProbeEndpoint:
    def test_reports_the_tools_the_server_exposes(self, session_manager, tmp_path):
        """真起一次子进程 —— 「测试连接」按钮背后就是这一条。"""
        app, _ = _mcp_app(session_manager, tmp_path)
        with TestClient(app) as client:
            probed = client.post("/v1/mcp/probe", json=ECHO_SPEC)
        assert probed.status_code == 200, probed.text
        assert "probe_echo" in probed.json()["tools"]

    def test_probes_the_submitted_values_not_the_saved_ones(self, session_manager, tmp_path):
        """改完还没保存就该能测。

        注册表里存的是坏的、请求体里给的是好的，探测必须按请求体走 —— 否则用户每次都得先
        存下一份自己都不确定对不对的定义，才轮到测。
        """
        app, _ = _mcp_app(
            session_manager,
            tmp_path,
            {"echo-probe": {"command": "definitely-not-a-command"}},
        )
        with TestClient(app) as client:
            assert client.post("/v1/mcp/probe", json=ECHO_SPEC).status_code == 200

    def test_reports_an_unstartable_command(self, session_manager, tmp_path):
        """错误原句来自 probe_server（含命令名与 stderr 尾部），前端不重写。"""
        app, _ = _mcp_app(session_manager, tmp_path)
        with TestClient(app) as client:
            failed = client.post(
                "/v1/mcp/probe", json={**ECHO_SPEC, "command": "definitely-not-a-command"}
            )
        assert failed.status_code == 400
        assert "definitely-not-a-command" in failed.json()["detail"]

    def test_required_false_does_not_silence_a_manual_test(self, session_manager, tmp_path):
        """required 只决定节点启动时要不要因它失败，手动点「测试」永远要看到错误。

        走 probe_servers 的话这里会返回 200 空结果，用户读到的就是「测过了，没事」。
        """
        app, _ = _mcp_app(session_manager, tmp_path)
        with TestClient(app) as client:
            failed = client.post(
                "/v1/mcp/probe",
                json={**ECHO_SPEC, "command": "definitely-not-a-command", "required": False},
            )
        assert failed.status_code == 400


# ---- 远程（Streamable HTTP）条目 ----

REMOTE_SPEC = {
    "name": "remote-probe",
    "url": "https://mcp.example.com/mcp",
    "headers": {"Authorization": "Bearer t"},
}


@contextlib.contextmanager
def _fake_remote(tools=("remote_echo",)):
    """一个只在本机、端口由系统分配的假远程 MCP 端点。

    与 tests/test_mcp_client.py 里那个同源，这里要验的是**端点这一层**把注册表里的
    一条远程定义原样送到了预检手里，所以额外记下收到的请求头 —— 「headers 有没有真的
    发出去」只有真端点看得见。

    用端口 0 而不是写死：本仓库有一条硬约束，8080 上跑着常驻的 AICU 服务，测试不许碰它。
    """
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {}
            seen.append({"headers": dict(self.headers), "body": body})
            method = body.get("method")
            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "1"},
                }
            elif method == "tools/list":
                result = {"tools": [{"name": name} for name in tools]}
            else:
                result = None
            payload = (
                {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
                if result is not None
                else {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601}}
            )
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/mcp", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestMcpRegistryRemoteEntries:
    """远程条目与本地条目共用同一套端点，差别全在 validate_server 那一层。"""

    def test_remote_spec_round_trip(self, session_manager, tmp_path):
        app, registry = _mcp_app(session_manager, tmp_path)
        with TestClient(app) as client:
            created = client.post("/v1/mcp/servers", json=REMOTE_SPEC)
            assert created.status_code == 201, created.text
            assert created.json()["url"] == REMOTE_SPEC["url"]

            listed = _servers(client)[0]
            assert listed["url"] == REMOTE_SPEC["url"]
            assert listed["headers"] == REMOTE_SPEC["headers"]

        # 落到文件里的只有 url/headers —— 不写一个空的 command，否则下次读到的人会以为
        # 这条定义了命令却没填（_parse_entry 也正是按「有没有 url」判传输）。
        on_disk = json.loads(registry.read_text(encoding="utf-8"))["servers"]["remote-probe"]
        assert on_disk["url"] == REMOTE_SPEC["url"]
        assert "command" not in on_disk and "args" not in on_disk

    def test_remote_and_local_entries_coexist(self, session_manager, tmp_path):
        app, _ = _mcp_app(session_manager, tmp_path, {"echo-probe": {"command": "node"}})
        with TestClient(app) as client:
            assert client.post("/v1/mcp/servers", json=REMOTE_SPEC).status_code == 201
            by_name = {item["name"]: item for item in _servers(client)}

        assert by_name["remote-probe"]["url"] == REMOTE_SPEC["url"]
        # 本地条目不带 url/headers：多两个恒为空的键只会让人以为要填。
        assert "url" not in by_name["echo-probe"]

    def test_remote_entries_can_be_referenced_by_roles(self, session_manager, tmp_path):
        """「还有谁在用」对远程条目同样要算 —— 否则删除确认框会少报引用者。"""
        app, _ = _mcp_app(
            session_manager,
            tmp_path,
            {"remote-probe": {"url": REMOTE_SPEC["url"]}},
            roles=[("worker", "mcp_probe", {"mcp": ["remote-probe"]})],
        )
        with TestClient(app) as client:
            assert _servers(client)[0]["referencing_roles"] == [
                {"id": "mcp_probe", "type": "worker"}
            ]

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"command": "python3"}, id="url-and-command"),
            pytest.param({"url": None}, id="neither"),
            pytest.param({"args": ["--x"]}, id="url-and-args"),
            pytest.param({"env": {"A": "1"}}, id="url-and-env"),
            pytest.param({"headers": {"X": "1"}, "url": None}, id="headers-without-url"),
            pytest.param({"url": "ftp://example.com/mcp"}, id="ftp-scheme"),
            pytest.param({"url": "   "}, id="blank-url"),
        ],
    )
    def test_contradictory_remote_specs_are_rejected(
        self, session_manager, tmp_path, overrides
    ):
        """混着给必须**报错**，不能静默丢掉其中一组。

        静默丢掉的后果是「存进去了但没生效」，用户要到节点起不来时才发现 —— 与本轮
        修的 _resolve_path 把 `@scope/pkg` 改坏是同一类坏法。

        ``url`` 给 None 是「不传」的意思（pydantic 默认值就是 None），不是空串。
        """
        spec = {**REMOTE_SPEC, **overrides}
        if spec.get("url") is None:
            spec.pop("url")
        app, _ = _mcp_app(session_manager, tmp_path)
        with TestClient(app) as client:
            rejected = client.post("/v1/mcp/servers", json=spec)
        assert rejected.status_code == 400, rejected.text

    def test_header_injection_is_refused_with_our_own_message(self, session_manager, tmp_path):
        """换行会把一个 header 变成两个 —— 错误必须是我们可读的那句，不是 urllib 的原话。"""
        app, _ = _mcp_app(session_manager, tmp_path)
        with TestClient(app) as client:
            rejected = client.post(
                "/v1/mcp/servers",
                json={**REMOTE_SPEC, "headers": {"X-Token": "a\r\nX-Evil: 1"}},
            )
        assert rejected.status_code == 400, rejected.text
        assert "换行" in rejected.json()["detail"]


class TestMcpProbeEndpointRemote:
    """「测试连接」按钮对远程条目也要真连一次，并且带上注册表里的 headers。"""

    def test_reports_the_tools_the_remote_server_exposes(self, session_manager, tmp_path):
        app, _ = _mcp_app(session_manager, tmp_path)
        with _fake_remote(tools=("remote_echo", "other")) as (url, _):
            with TestClient(app) as client:
                probed = client.post("/v1/mcp/probe", json={**REMOTE_SPEC, "url": url})
        assert probed.status_code == 200, probed.text
        assert probed.json()["tools"] == ["remote_echo", "other"]

    def test_the_submitted_headers_reach_the_remote_end(self, session_manager, tmp_path):
        """测的就是请求体里的那份定义 —— headers 没发出去的话，远端会 401，而用户
        只会看到「连不上」，永远猜不到是凭据没带上。"""
        app, _ = _mcp_app(session_manager, tmp_path)
        with _fake_remote() as (url, seen):
            with TestClient(app) as client:
                probed = client.post(
                    "/v1/mcp/probe",
                    json={**REMOTE_SPEC, "url": url, "headers": {"Authorization": "Bearer abc"}},
                )
        assert probed.status_code == 200, probed.text
        assert seen and all(r["headers"].get("Authorization") == "Bearer abc" for r in seen)

    def test_probes_the_submitted_url_not_the_saved_one(self, session_manager, tmp_path):
        app, _ = _mcp_app(
            session_manager, tmp_path, {"remote-probe": {"url": "http://127.0.0.1:1/mcp"}}
        )
        with _fake_remote() as (url, _):
            with TestClient(app) as client:
                assert client.post(
                    "/v1/mcp/probe", json={**REMOTE_SPEC, "url": url}
                ).status_code == 200

    def test_an_unreachable_remote_is_a_readable_error(self, session_manager, tmp_path):
        """错误原句来自 probe_server（含地址与原因），前端不重写。"""
        app, _ = _mcp_app(session_manager, tmp_path)
        with TestClient(app) as client:
            failed = client.post(
                "/v1/mcp/probe", json={**REMOTE_SPEC, "url": "http://127.0.0.1:1/mcp"}
            )
        assert failed.status_code == 400
        assert "127.0.0.1:1" in failed.json()["detail"]


# --------------------------------------------------------------------------
# 角色池导入 / 导出
# --------------------------------------------------------------------------


def _pool_bundle_app(session_manager, root: Path):
    """隔离的 roles/ + skills/ + mcp_servers.json + role_pools.json；default.yaml 取自仓库。"""
    repo = Path(__file__).resolve().parent.parent
    roles_dir = root / "roles"
    for kind in ("Manager", "Worker"):
        (roles_dir / kind).mkdir(parents=True, exist_ok=True)
        shutil.copy(repo / "roles" / kind / "default.yaml", roles_dir / kind / "default.yaml")
    (root / "skills").mkdir(exist_ok=True)
    registry = root / "mcp_servers.json"
    registry.write_text(json.dumps({"servers": {}}), encoding="utf-8")
    roles_manager = RolesManager(str(roles_dir))
    app = create_session_app(
        session_manager,
        roles_manager,
        SkillsManager(str(root / "skills")),
        RolePoolsManager(str(root / "role_pools.json"), roles_manager),
        mcp_registry_path=str(registry),
    )
    return app, roles_manager, registry


def _seed_exportable_pool(client, root: Path, name="博客开发"):
    (root / "skills" / "s1").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "s1" / "SKILL.md").write_text("---\nname: s1\n---\nhi\n", encoding="utf-8")
    (root / "mcp_servers.json").write_text(
        json.dumps({"servers": {"echo-probe": {"command": "python3", "args": ["tests/mcp_echo_server.py"],
                                               "env": {"API_TOKEN": "sekrit-value"}}}}),
        encoding="utf-8",
    )
    role = client.post("/v1/roles", json={
        "role_id": "w1", "role_type": "worker",
        "content": 'role_md: "w"\nskills: [s1]\nmcp: [echo-probe]\n',
    })
    assert role.status_code in (200, 201), role.text
    created = client.post("/v1/role-pools", json={"name": name, "roles": ["w1"]})
    assert created.status_code == 201, created.text
    return created.json()


class TestRolePoolBundleEndpoints:
    def test_preview_and_download(self, session_manager, tmp_path):
        app, _, _ = _pool_bundle_app(session_manager, tmp_path / "src")
        with TestClient(app) as client:
            pool = _seed_exportable_pool(client, tmp_path / "src")

            preview = client.get(f"/v1/role-pools/{pool['id']}/export/preview")
            assert preview.status_code == 200, preview.text
            body = preview.json()
            assert body["skills"] == ["s1"] and body["mcp"] == ["echo-probe"]
            assert body["sensitive_mcp"] == [{"name": "echo-probe", "keys": ["env:API_TOKEN"]}]
            assert "sekrit-value" not in preview.text

            download = client.get(f"/v1/role-pools/{pool['id']}/export")
            assert download.status_code == 200
            assert download.headers["content-type"] == "application/zip"
            disposition = download.headers["content-disposition"]
            assert disposition.startswith("attachment;")
            assert "filename*=UTF-8''" in disposition and "%E5%8D%9A%E5%AE%A2" in disposition
            with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
                assert "manifest.json" in archive.namelist()

            assert client.get("/v1/role-pools/nope/export/preview").status_code == 404
            assert client.get("/v1/role-pools/nope/export").status_code == 404

    def test_import_dry_run_then_real_import_and_conflicts(self, session_manager, tmp_path):
        src_app, _, _ = _pool_bundle_app(session_manager, tmp_path / "src")
        with TestClient(src_app) as client:
            pool = _seed_exportable_pool(client, tmp_path / "src")
            data = client.get(f"/v1/role-pools/{pool['id']}/export").content

        dst_app, dst_roles, dst_registry = _pool_bundle_app(session_manager, tmp_path / "dst")
        with TestClient(dst_app) as client:
            plan = client.post("/v1/role-pools/import?dry_run=true", content=data)
            assert plan.status_code == 200, plan.text
            assert plan.json()["dry_run"] is True
            assert plan.json()["mcp"][0]["command"] == "python3"
            assert client.get("/v1/role-pools").json() == {"pools": []}
            assert not (tmp_path / "dst" / "skills" / "s1").exists()

            done = client.post("/v1/role-pools/import", content=data)
            assert done.status_code == 200, done.text
            assert done.json()["counts"]["created"] == 4
            assert [p["name"] for p in client.get("/v1/role-pools").json()["pools"]] == ["博客开发"]

            dst_roles.update_role("w1", content='role_md: "LOCAL"\n', role_type="worker")
            skipped = client.post("/v1/role-pools/import?on_conflict=skip", content=data)
            assert skipped.json()["counts"]["skipped"] >= 1
            assert "LOCAL" in dst_roles.read_role_raw("w1", "worker")["content"]
            overwritten = client.post("/v1/role-pools/import?on_conflict=overwrite", content=data)
            assert overwritten.json()["counts"]["overwritten"] >= 1
            assert "LOCAL" not in dst_roles.read_role_raw("w1", "worker")["content"]

            assert client.post("/v1/role-pools/import?on_conflict=merge", content=data).status_code in (400, 422)

            dst_roles.update_role("w1", content='role_md: "LOCAL2"\n', role_type="worker")
            plan = client.post(
                "/v1/role-pools/import?dry_run=true&on_conflict=skip&decide=role:worker/w1=overwrite", content=data
            ).json()
            assert next(r for r in plan["roles"] if r["id"] == "w1")["policy"] == "overwrite"
            assert client.post("/v1/role-pools/import?decide=role:worker/w1=merge", content=data).status_code == 400
            assert client.post("/v1/role-pools/import?decide=nonsense", content=data).status_code == 400
            client.post("/v1/role-pools/import?decide=role:worker/w1=overwrite", content=data)
            assert "LOCAL2" not in dst_roles.read_role_raw("w1", "worker")["content"]

    def test_import_rejects_bad_and_oversized_bodies(self, session_manager, tmp_path, monkeypatch):
        from runtime import session_server as server_module

        app, _, _ = _pool_bundle_app(session_manager, tmp_path / "dst")
        with TestClient(app) as client:
            bad = client.post("/v1/role-pools/import", content=b"not a zip")
            assert bad.status_code == 400
            assert "zip" in bad.text

            monkeypatch.setattr(server_module, "MAX_BUNDLE_BYTES", 10)
            assert client.post("/v1/role-pools/import", content=b"x" * 11).status_code == 413
