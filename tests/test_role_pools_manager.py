"""runtime/role_pools_manager.py（会话候选角色池）单元测试。

覆盖：CRUD 往返、持久化重读、名称校验、角色 id 存在性校验、去重保序、
`default` 被拒、未知 id 抛错，以及损坏存储文件的容错。

全部使用 tmp_path 隔离，不触碰仓库 roles/ 目录（C6）。
"""

import json
import os

import pytest

from runtime.role_pools_manager import RolePoolsManager
from runtime.roles_manager import RolesManager


def _write_role_yaml(roles_dir, role_type, role_id, content="summary: S\n"):
    sub = os.path.join(roles_dir, role_type.capitalize())
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, f"{role_id}.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


@pytest.fixture
def roles_dir(tmp_path):
    """三个可选角色 + 一个不可入池的 default 基类。"""
    root = tmp_path / "roles"
    _write_role_yaml(str(root), "manager", "default", "role_md: base\n")
    _write_role_yaml(str(root), "manager", "engineering_coord")
    _write_role_yaml(str(root), "worker", "code")
    _write_role_yaml(str(root), "worker", "test")
    return root


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / "role_pools.json")


@pytest.fixture
def manager(store, roles_dir):
    return RolePoolsManager(store, RolesManager(str(roles_dir)))


class TestCrud:
    def test_create_then_list_and_get(self, manager):
        created = manager.create_pool("博客开发", ["code", "test"])

        assert created["name"] == "博客开发"
        assert created["roles"] == ["code", "test"]
        assert created["id"]
        assert manager.list_pools() == [created]
        assert manager.get_pool(created["id"]) == created

    def test_list_is_sorted_by_name(self, manager):
        manager.create_pool("Zeta", ["code"])
        manager.create_pool("Alpha", ["test"])

        assert [p["name"] for p in manager.list_pools()] == ["Alpha", "Zeta"]

    def test_list_returns_copies(self, manager):
        created = manager.create_pool("P", ["code"])
        manager.list_pools()[0]["roles"].append("test")

        assert manager.get_pool(created["id"])["roles"] == ["code"]

    def test_update_replaces_only_the_given_fields(self, manager):
        created = manager.create_pool("P", ["code"])

        renamed = manager.update_pool(created["id"], name="Q")
        assert renamed["name"] == "Q"
        assert renamed["roles"] == ["code"]

        rerolled = manager.update_pool(created["id"], roles=["test"])
        assert rerolled["name"] == "Q"
        assert rerolled["roles"] == ["test"]

    def test_update_keeps_the_id(self, manager):
        created = manager.create_pool("P", ["code"])
        updated = manager.update_pool(created["id"], name="Q")

        assert updated["id"] == created["id"]

    def test_delete_removes_the_pool(self, manager):
        created = manager.create_pool("P", ["code"])
        manager.delete_pool(created["id"])

        assert manager.list_pools() == []

    def test_name_must_be_unique_on_create(self, manager):
        manager.create_pool("P", ["code"])

        with pytest.raises(ValueError, match="already exists"):
            manager.create_pool("P", ["test"])

    def test_name_must_be_unique_on_update(self, manager):
        first = manager.create_pool("P", ["code"])
        manager.create_pool("Q", ["test"])

        with pytest.raises(ValueError, match="already exists"):
            manager.update_pool(first["id"], name="Q")

    def test_update_to_its_own_name_is_allowed(self, manager):
        created = manager.create_pool("P", ["code"])

        assert manager.update_pool(created["id"], name="P")["name"] == "P"

    def test_get_and_delete_missing_pool_raise_key_error(self, manager):
        with pytest.raises(KeyError):
            manager.get_pool("nope")
        with pytest.raises(KeyError):
            manager.delete_pool("nope")


class TestPersistence:
    def test_survives_a_new_instance(self, store, roles_dir):
        created = RolePoolsManager(store, RolesManager(str(roles_dir))).create_pool(
            "博客开发", ["code", "test"]
        )

        reloaded = RolePoolsManager(store, RolesManager(str(roles_dir)))
        assert reloaded.list_pools() == [created]

    def test_store_carries_a_version(self, store, roles_dir, manager):
        manager.create_pool("P", ["code"])

        with open(store, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["version"] == 1
        assert len(data["pools"]) == 1

    def test_missing_file_reads_as_empty(self, store, roles_dir):
        assert RolePoolsManager(store, RolesManager(str(roles_dir))).list_pools() == []

    @pytest.mark.parametrize(
        "content",
        ["{ not json", "[]", '{"pools": "nope"}', '{"pools": [{"id": "a"}, null]}'],
    )
    def test_corrupt_store_falls_back_to_empty(self, store, roles_dir, content):
        with open(store, "w", encoding="utf-8") as f:
            f.write(content)

        assert RolePoolsManager(store, RolesManager(str(roles_dir))).list_pools() == []


class TestValidation:
    @pytest.mark.parametrize("name", ["", "   ", None, 42])
    def test_name_must_be_a_non_empty_string(self, manager, name):
        with pytest.raises(ValueError):
            manager.create_pool(name, ["code"])

    def test_name_is_trimmed_and_length_capped(self, manager):
        assert manager.create_pool("  P  ", ["code"])["name"] == "P"

        with pytest.raises(ValueError, match="too long"):
            manager.create_pool("x" * 61, ["code"])

    def test_name_at_the_length_limit_is_accepted(self, manager):
        assert manager.create_pool("x" * 60, ["code"])["name"] == "x" * 60

    def test_roles_must_be_a_list(self, manager):
        with pytest.raises(ValueError, match="must be a list"):
            manager.create_pool("P", "code")

    def test_roles_must_contain_strings(self, manager):
        with pytest.raises(ValueError, match="list of strings"):
            manager.create_pool("P", ["code", 7])

    def test_unknown_role_id_is_rejected(self, manager):
        with pytest.raises(ValueError, match="Unknown role ids"):
            manager.create_pool("P", ["code", "ghost"])

    def test_empty_roles_are_rejected(self, manager):
        with pytest.raises(ValueError, match="at least one role"):
            manager.create_pool("P", [])

    def test_duplicate_role_ids_are_deduped_preserving_order(self, manager):
        created = manager.create_pool("P", ["test", "code", "test"])

        assert created["roles"] == ["test", "code"]

    def test_default_cannot_join_a_pool(self, manager):
        """default 是继承基类而非可选角色，天然不在合法 id 集合里。"""
        with pytest.raises(ValueError, match="Unknown role ids"):
            manager.create_pool("P", ["default"])

    def test_update_validates_too(self, manager):
        created = manager.create_pool("P", ["code"])

        with pytest.raises(ValueError, match="Unknown role ids"):
            manager.update_pool(created["id"], roles=["ghost"])
        with pytest.raises(ValueError):
            manager.update_pool(created["id"], name="")
        # 校验失败不得留下半写状态
        assert manager.get_pool(created["id"]) == created
