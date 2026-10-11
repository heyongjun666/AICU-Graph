"""Global (system-level) concurrency settings; all state lives under pytest tmp_path.

``max_concurrency`` is a global config key, alongside ``dynamic_num_ccs`` /
``default_num_ccs``: new sessions inherit it at creation, and changing it via
``update_global_config`` pushes the new limit to every already-running session's
Runtime (``set_max_concurrency`` does not interrupt in-flight nodes). Sessions no
longer hold their own limit, so a session-level ``max_concurrency`` is a no-op.
"""

import json
import threading
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from executables.registry import ExecutableRegistry
from runtime.concurrency import DEFAULT_MAX_CONCURRENCY
from runtime.session_manager import SessionManager
from runtime.session_server import _merge_session_connection, create_session_app


INVALID_LIMITS = [0, -1, 17, 1.5, 4.0, "4", True, False, None, [], {}]


@pytest.fixture
def manager_factory(tmp_path):
    managers = []

    def make(**kwargs):
        kwargs.setdefault("root_dir", str(tmp_path / f"sessions-{len(managers)}"))
        kwargs.setdefault("workspace_root", str(tmp_path / "workspace"))
        kwargs.setdefault("restore", False)
        manager = SessionManager(registry=ExecutableRegistry(), **kwargs)
        managers.append(manager)
        return manager

    yield make
    for manager in reversed(managers):
        manager.shutdown()


def _create(manager, **config):
    session = manager.create_session("Concurrency settings", config=config)
    return manager.get_session(session["id"])


def _assert_global(manager, expected):
    assert manager.get_global_config()["max_concurrency"] == expected


def _assert_session(manager, handle, expected):
    # 会话级 config 不再持有并发上限（get_settings / metadata.config 都没有它）。
    assert "max_concurrency" not in handle.metadata.config
    assert "max_concurrency" not in manager.get_settings(handle.metadata.id)["config"]
    assert handle.runtime.max_concurrency == expected
    assert handle.runtime.scheduler.max_concurrency == expected


def test_default_global_value_and_new_session_inheritance(manager_factory):
    manager = manager_factory()
    _assert_global(manager, DEFAULT_MAX_CONCURRENCY)
    handle = _create(manager)
    _assert_session(manager, handle, DEFAULT_MAX_CONCURRENCY)


@pytest.mark.parametrize("limit", [1, 16])
def test_update_global_config_pushes_to_every_session(manager_factory, limit):
    manager = manager_factory()
    first = _create(manager)
    second = _create(manager)
    _assert_session(manager, first, DEFAULT_MAX_CONCURRENCY)
    _assert_session(manager, second, DEFAULT_MAX_CONCURRENCY)

    result = manager.update_global_config({"max_concurrency": limit})
    assert result["max_concurrency"] == limit
    _assert_global(manager, limit)
    # 已存在会话即时跟随，新会话继承新值。
    _assert_session(manager, first, limit)
    _assert_session(manager, second, limit)
    _assert_session(manager, _create(manager), limit)


def test_session_level_max_concurrency_is_ignored(manager_factory):
    manager = manager_factory()
    # 创建时带 max_concurrency：被白名单丢弃，不报错，会话跟随全局默认。
    handle = _create(manager, max_concurrency=9, num_ccs=2)
    _assert_session(manager, handle, DEFAULT_MAX_CONCURRENCY)
    assert handle.metadata.config.get("num_ccs") == 2
    # 会话级 PATCH 带 max_concurrency：同样静默丢弃。
    manager.update_settings(handle.metadata.id, {"max_concurrency": 3})
    _assert_session(manager, handle, DEFAULT_MAX_CONCURRENCY)


@pytest.mark.parametrize("value", INVALID_LIMITS)
def test_invalid_global_value_is_rejected(manager_factory, value):
    manager = manager_factory()
    handle = _create(manager)
    before = asdict(handle.metadata)
    with pytest.raises(ValueError, match="max_concurrency"):
        manager.update_global_config({"max_concurrency": value})
    _assert_global(manager, DEFAULT_MAX_CONCURRENCY)
    _assert_session(manager, handle, DEFAULT_MAX_CONCURRENCY)
    assert asdict(handle.metadata) == before


def test_global_concurrency_persists_across_restart(manager_factory):
    manager = manager_factory()
    manager.update_global_config({"max_concurrency": 6})
    handle = _create(manager)
    _assert_session(manager, handle, 6)
    manager.shutdown()

    restored = manager_factory(root_dir=manager.root_dir, restore=True)
    _assert_global(restored, 6)
    _assert_session(restored, restored.get_session(handle.metadata.id), 6)


def test_legacy_session_concurrency_cleaned_up_on_restore(manager_factory):
    manager = manager_factory()
    handle = _create(manager)
    record = asdict(handle.metadata)
    manager.shutdown()
    # 旧会话级实现写进 sessions.json 的 max_concurrency：恢复时被清掉，会话跟随全局值。
    record["config"]["max_concurrency"] = 16
    Path(manager._index_path).write_text(json.dumps([record]))

    restored = manager_factory(root_dir=manager.root_dir, restore=True)
    restored_handle = restored.get_session(record["id"])
    assert "max_concurrency" not in restored_handle.metadata.config
    assert restored_handle.runtime.max_concurrency == DEFAULT_MAX_CONCURRENCY


def test_unrestorable_record_survives_index_rewrite(manager_factory):
    manager = manager_factory()
    handle = _create(manager)
    record = asdict(handle.metadata)
    manager.shutdown()
    broken = {"id": "from-a-newer-version", "unknown_field": 1}
    Path(manager._index_path).write_text(json.dumps([record, broken]))

    restored = manager_factory(root_dir=manager.root_dir, restore=True)
    assert restored.get_session(record["id"]) is not None
    ids = [item["id"] for item in json.loads(Path(restored._index_path).read_text())]
    assert "from-a-newer-version" in ids


def test_corrupt_index_leaves_a_copy(manager_factory):
    manager = manager_factory()
    manager.shutdown()
    Path(manager._index_path).write_text("{not json")

    manager_factory(root_dir=manager.root_dir, restore=True)
    assert Path(manager._index_path + ".corrupt").read_text() == "{not json"


def test_global_push_does_not_hold_manager_lock(manager_factory, monkeypatch):
    manager = manager_factory()
    handle = _create(manager)
    original = handle.runtime.set_max_concurrency
    acquired = threading.Event()

    def probe(value):
        # 若推送在 self._lock 内执行，这个 get_session（拿 self._lock）会一直阻塞，
        # acquired 超时即说明死锁风险仍在。
        def read_manager():
            manager.get_session(handle.metadata.id)
            acquired.set()

        reader = threading.Thread(target=read_manager, daemon=True)
        reader.start()
        assert acquired.wait(2), "runtime setter ran under the global manager lock"
        reader.join(2)
        original(value)

    monkeypatch.setattr(handle.runtime, "set_max_concurrency", probe)
    manager.update_global_config({"max_concurrency": 6})
    _assert_session(manager, handle, 6)


def test_api_round_trip_global(manager_factory):
    manager = manager_factory()
    with TestClient(create_session_app(manager)) as client:
        assert client.get("/v1/config").json()["config"]["max_concurrency"] == DEFAULT_MAX_CONCURRENCY

        created = client.post("/v1/sessions", json={})
        assert created.status_code == 201
        sid = created.json()["id"]

        # 会话 settings 不再含 max_concurrency。
        settings = client.get(f"/v1/sessions/{sid}/settings").json()
        assert "max_concurrency" not in settings["config"]

        # PATCH /v1/config 改并发 → 200，已存在会话即时跟随。
        for limit in (1, 16):
            response = client.patch("/v1/config", json={"config": {"max_concurrency": limit}})
            assert response.status_code == 200, response.text
            assert response.json()["config"]["max_concurrency"] == limit
            assert client.get("/v1/config").json()["config"]["max_concurrency"] == limit
            assert manager.get_session(sid).runtime.max_concurrency == limit

        # 非法全局值 → 400。
        for invalid in INVALID_LIMITS:
            response = client.patch("/v1/config", json={"config": {"max_concurrency": invalid}})
            assert response.status_code == 400, response.text
            assert "max_concurrency" in response.json()["detail"]

        # 会话级 PATCH 带 max_concurrency：静默丢弃，仍 200。
        response = client.patch(
            f"/v1/sessions/{sid}/settings", json={"config": {"max_concurrency": 3}}
        )
        assert response.status_code == 200, response.text
        assert "max_concurrency" not in client.get(f"/v1/sessions/{sid}/settings").json()["config"]


def test_concurrency_not_forwarded_to_node_config(session_client, session_manager):
    created = session_client.post("/v1/sessions", json={})
    assert created.status_code == 201
    sid = created.json()["id"]
    assert session_client.post(f"/v1/sessions/{sid}/graph/pause").status_code == 200
    node = session_client.post(
        f"/v1/sessions/{sid}/nodes",
        json={"task": "No external execution", "exec_type": "worker", "config": {"num_ccs": 2}},
    )
    assert node.status_code == 201, node.text
    assert "max_concurrency" not in node.json()["config"]
    runtime = session_manager.get_session(sid).runtime
    assert "max_concurrency" not in runtime.get_node(node.json()["id"]).config
    # 会话级来源的 max_concurrency 也不该漏进节点连接合并。
    assert _merge_session_connection({"max_concurrency": 8, "model": "deepseek-chat"}, {}) == {
        "_session_model": "deepseek-chat"
    }
