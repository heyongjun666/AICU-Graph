"""runtime/roles_manager.py（M5 能力配置：角色管理）单元测试。

覆盖：列表/新增/读取/更新/删除、合法 id 校验、config 校验、产物契约解析与
默认回退、schema 字段类型推导与隐藏字段。全部使用 tmp_path 隔离，不触碰
仓库 roles/ 目录（C6）。
"""

import os

import pytest

from runtime.roles_manager import RolesManager


def _write_role_yaml(roles_dir, role_type, role_id, content):
    sub = os.path.join(roles_dir, role_type.capitalize())
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, f"{role_id}.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class TestListRoles:
    def test_lists_sorted_skips_default_and_non_yaml(self, tmp_path):
        _write_role_yaml(
            tmp_path, "manager", "default", "summary: base\n",
        )
        _write_role_yaml(
            tmp_path, "manager", "lead",
            "role_md: |\n  # Mission\n  Lead Manager 负责规划\n",
        )
        _write_role_yaml(tmp_path, "worker", "backend", "summary: B\n")
        (tmp_path / "Worker" / "notes.txt").write_text("ignored", encoding="utf-8")

        roles = RolesManager(str(tmp_path)).list_roles()
        assert [(r["id"], r["type"]) for r in roles] == [
            ("lead", "manager"),
            ("backend", "worker"),
        ]
        assert roles[0]["display_name"] == "Lead"
        # 跳过单词标题（# Mission），取首个有意义行
        assert roles[0]["description"] == "Lead Manager 负责规划"
        assert roles[1]["description"] == ""

    def test_missing_dir_returns_empty(self, tmp_path):
        assert RolesManager(str(tmp_path / "nope")).list_roles() == []

    def test_description_truncated_to_80(self, tmp_path):
        _write_role_yaml(
            tmp_path, "worker", "long",
            "role_md: |\n  " + ("长" * 90) + "\n",
        )
        roles = RolesManager(str(tmp_path)).list_roles()
        assert roles[0]["description"] == ("长" * 80)

    def test_skips_filenames_that_cannot_be_a_role_id(self, tmp_path):
        """文件名不是一个合法 role_id 的，不列为角色。

        这类文件列出来也打不开（``_role_path`` 判非法、接口返 400），却会被
        ``iter_role_configs`` 拿去逐个读 —— 于是一个临时文件就能把「谁引用了这个
        mcp / skill」这类整轮遍历的查询带崩。``_`` 开头是其中最常见的一种：
        ``build_available_roles_text`` 也是明确跳过它们的。
        """
        _write_role_yaml(tmp_path, "worker", "code", "summary: 正常角色\n")
        _write_role_yaml(tmp_path, "worker", "_scratch", "summary: 临时文件\n")
        _write_role_yaml(tmp_path, "manager", "bad-name", "summary: 连字符\n")

        roles = RolesManager(str(tmp_path)).list_roles()
        assert [(r["id"], r["type"]) for r in roles] == [("code", "worker")]


class TestSkillReferences:
    def test_reports_effective_skill_references_and_explicit_empty_override(self, tmp_path):
        _write_role_yaml(tmp_path, "manager", "default", "skills: [shared]\n")
        _write_role_yaml(tmp_path, "worker", "default", "skills: [shared]\n")
        _write_role_yaml(tmp_path, "worker", "plain", "role_md: plain\n")
        _write_role_yaml(tmp_path, "worker", "empty", "skills: []\n")
        _write_role_yaml(tmp_path, "manager", "same", "skills: [other]\n")
        refs = RolesManager(str(tmp_path)).roles_referencing_skill("shared")
        assert refs == [
            {"id": "default", "type": "manager"},
            {"id": "default", "type": "worker"},
            {"id": "plain", "type": "worker"},
        ]

    def test_a_junk_role_filename_does_not_break_the_whole_pass(self, tmp_path):
        """一个读不出来的角色文件，不该把其余角色的答案一起吞掉。

        这条与 ``iter_role_configs`` 已有的「坏 YAML 只跳过这一个」是同一个口径；
        ``_`` 开头的文件走的是 id 校验那条路，症状是**整轮查询抛异常**（而不是少一条）。
        """
        _write_role_yaml(tmp_path, "worker", "code", "skills: [tdd]\n")
        _write_role_yaml(tmp_path, "worker", "_scratch", "skills: [tdd]\n")
        assert RolesManager(str(tmp_path)).roles_referencing_skill("tdd") == [
            {"id": "code", "type": "worker"}
        ]


class TestAddGetRole:
    def test_add_with_content(self, tmp_path):
        rm = RolesManager(str(tmp_path))
        r = rm.add_role("dev", "summary: dev\n", role_type="worker")
        assert r["id"] == "dev"
        assert r["type"] == "worker"
        assert r["config"]["summary"] == "dev"
        assert os.path.isfile(str(tmp_path / "Worker" / "dev.yaml"))

    def test_add_with_config_requires_known_fields(self, tmp_path):
        _write_role_yaml(
            tmp_path, "worker", "default",
            "summary: S\nmodel: m\nnum_ccs: 2\n",
        )
        rm = RolesManager(str(tmp_path))
        r = rm.add_role(
            "lead", role_type="worker", config={"summary": "L", "num_ccs": 3},
        )
        assert r["config"]["summary"] == "L"
        assert r["effective_config"]["summary"] == "L"
        assert "schema" in r

    def test_add_with_config_rejects_unknown_field(self, tmp_path):
        rm = RolesManager(str(tmp_path))  # 无 default.yaml → 目录为空
        with pytest.raises(ValueError):
            rm.add_role("x", role_type="worker", config={"nope": 1})

    @pytest.mark.parametrize("bad", ["Dev", "1x", "_x", ""])
    def test_add_rejects_invalid_id(self, tmp_path, bad):
        rm = RolesManager(str(tmp_path))
        with pytest.raises(ValueError):
            rm.add_role(bad, role_type="worker")

    def test_add_rejects_invalid_type_and_duplicate_and_bad_yaml(self, tmp_path):
        rm = RolesManager(str(tmp_path))
        with pytest.raises(ValueError):
            rm.add_role("ok", role_type="system")
        rm.add_role("dev", "summary: x\n", role_type="worker")
        with pytest.raises(ValueError):
            rm.add_role("dev", role_type="worker")  # 重复
        with pytest.raises(ValueError):
            rm.add_role("bad", "not: [valid\n", role_type="worker")

    def test_get_missing_raises_keyerror(self, tmp_path):
        rm = RolesManager(str(tmp_path))
        with pytest.raises(KeyError):
            rm.get_role("ghost", "worker")


class TestUpdateDeleteRole:
    def test_update_content_and_config(self, tmp_path):
        _write_role_yaml(tmp_path, "worker", "default", "summary: S\n")
        rm = RolesManager(str(tmp_path))
        rm.add_role("dev", "summary: v1\n", role_type="worker")
        assert rm.update_role("dev", "summary: v2\n", role_type="worker")[
            "config"
        ]["summary"] == "v2"
        assert rm.update_role(
            "dev", role_type="worker", config={"summary": "v3"},
        )["config"]["summary"] == "v3"

    def test_update_errors(self, tmp_path):
        rm = RolesManager(str(tmp_path))
        rm.add_role("dev", "summary: x\n", role_type="worker")
        with pytest.raises(ValueError):  # 无内容
            rm.update_role("dev", role_type="worker")
        with pytest.raises(ValueError):  # 非法 YAML
            rm.update_role("dev", "not: [valid\n", role_type="worker")
        with pytest.raises(KeyError):  # 缺失
            rm.update_role("ghost", "x\n", role_type="worker")

    def test_delete(self, tmp_path):
        rm = RolesManager(str(tmp_path))
        rm.add_role("dev", "summary: x\n", role_type="worker")
        rm.delete_role("dev", role_type="worker")
        assert not (tmp_path / "Worker" / "dev.yaml").exists()
        with pytest.raises(ValueError):  # default 不可删
            rm.delete_role("default", role_type="worker")
        with pytest.raises(KeyError):  # 缺失
            rm.delete_role("ghost", role_type="worker")


class TestRoleSchema:
    def test_field_type_derivation_and_hidden_fields(self, tmp_path):
        _write_role_yaml(
            tmp_path, "worker", "default",
            "summary: S\nmodel: m\nnum_ccs: 2\nskills: [a]\n"
            "index_file: _INDEX.md\n",
        )
        _write_role_yaml(tmp_path, "worker", "dev", "role_md: |\n  Dev\n")
        schema = RolesManager(str(tmp_path)).get_role_schema("worker")
        assert schema["role_type"] == "worker"
        by_name = {f["name"]: f for f in schema["fields"]}
        assert by_name["num_ccs"]["type"] == "number"
        assert by_name["model"]["type"] == "text"
        assert by_name["skills"]["type"] == "list"
        assert by_name["role_md"]["type"] == "multiline"
        assert by_name["summary"]["has_default"] is True
        assert by_name["summary"]["default"] == "S"
        assert "index_file" not in by_name  # 隐藏字段

    def test_manager_schema_hides_protocol_fields(self, tmp_path):
        _write_role_yaml(
            tmp_path, "manager", "default",
            "summary: M\ngenerate_instruction: X\nreview_checklist: []\n",
        )
        schema = RolesManager(str(tmp_path)).get_role_schema("manager")
        names = {f["name"] for f in schema["fields"]}
        assert "generate_instruction" not in names
        assert "review_checklist" not in names
        assert "summary" in names

    def test_mcp_is_a_first_class_role_config_field(self, tmp_path):
        _write_role_yaml(
            tmp_path, "worker", "default",
            "summary: base\nmcp: []\n",
        )
        rm = RolesManager(str(tmp_path))
        schema = rm.get_role_schema("worker")
        fields = {field["name"]: field for field in schema["fields"]}
        assert fields["mcp"]["type"] == "list"

        created = rm.add_role(
            "mcp_probe", role_type="worker",
            config={"summary": "Uses the probe", "mcp": ["echo-probe"]},
        )
        assert created["config"]["mcp"] == ["echo-probe"]
        assert rm.get_role("mcp_probe", "worker")["config"]["mcp"] == ["echo-probe"]
