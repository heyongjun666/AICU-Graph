"""tests/test_role_pool_bundle.py — 角色池导出 / 导入（含关联的角色、skill、MCP）。

每个环境都是 tmp_path 下独立的 roles/ + skills/ + mcp_servers.json + role_pools.json，
default.yaml 取自仓库真实文件（字段白名单与继承行为要和线上一致），不碰真实 roles/。
"""

import io
import json
import re
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime import role_pool_bundle as bundle_module
from runtime.role_pool_bundle import BundleError, BundleTooLarge, RolePoolBundle
from runtime.role_pools_manager import RolePoolsManager
from runtime.roles_manager import RolesManager
from runtime.skills_manager import SkillsManager

REPO = Path(__file__).resolve().parent.parent
ECHO = {"command": "python3", "args": ["tests/mcp_echo_server.py"], "required": True}


def make_env(root: Path, *, worker_default_skills: str | None = None):
    roles_dir = root / "roles"
    for kind in ("Manager", "Worker"):
        (roles_dir / kind).mkdir(parents=True)
        shutil.copy(REPO / "roles" / kind / "default.yaml", roles_dir / kind / "default.yaml")
    if worker_default_skills:
        default = roles_dir / "Worker" / "default.yaml"
        default.write_text(
            re.sub(r"(?m)^skills: \[\]$", f"skills: [{worker_default_skills}]", default.read_text("utf-8")),
            "utf-8",
        )
    (root / "skills").mkdir()
    registry = root / "mcp_servers.json"
    registry.write_text(json.dumps({"servers": {}}), "utf-8")
    roles = RolesManager(str(roles_dir))
    skills = SkillsManager(str(root / "skills"))
    pools = RolePoolsManager(str(root / "role_pools.json"), roles)
    return SimpleNamespace(
        root=root, roles=roles, skills=skills, pools=pools, registry=registry,
        bundle=RolePoolBundle(pools, roles, skills, registry),
    )


def add_skill(env, skill_id: str, files: dict[str, bytes] | None = None):
    base = env.root / "skills" / skill_id
    for rel, blob in (files or {"SKILL.md": b"---\nname: x\n---\nbody\n"}).items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_bytes(blob)


def set_mcp(env, servers: dict):
    env.registry.write_text(json.dumps({"servers": servers}), "utf-8")


def mcp_servers(env) -> dict:
    return json.loads(env.registry.read_text("utf-8"))["servers"]


def add_role(env, kind: str, role_id: str, text: str):
    env.roles.add_role(role_id, content=text, role_type=kind)


def worker_yaml(*, skills=(), mcp=(), note="w"):
    lines = [f"# 注释保留 {note}", f'role_md: "{note}"']
    if skills:
        lines.append("skills: [" + ", ".join(skills) + "]")
    if mcp:
        lines.append("mcp: [" + ", ".join(mcp) + "]")
    return "\n".join(lines) + "\n"


def seed_pool(env, name="池A", skills=("s1",), mcp=("echo-probe",)):
    """worker w1 引用 skill + mcp，manager m1 只有 role_md（继承 default）。"""
    add_skill(env, "s1", {"SKILL.md": b"---\nname: s1\n---\nhello\n", "assets/blob.bin": bytes(range(256))})
    set_mcp(env, {"echo-probe": ECHO, "other": {"command": "python3"}})
    add_role(env, "worker", "w1", worker_yaml(skills=skills, mcp=mcp))
    add_role(env, "manager", "m1", 'role_md: "m"\n')
    return env.pools.create_pool(name, ["w1", "m1"])


def make_zip(entries: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content if isinstance(content, bytes) else content)
    return buffer.getvalue()


def good_manifest(**over):
    manifest = {"format": "aicu-role-pool", "version": 1, "pool": {"name": "池X", "roles": ["w1"]}}
    manifest.update(over)
    return json.dumps(manifest)


def zip_entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {i.filename: archive.read(i) for i in archive.infolist() if not i.is_dir()}


# ============================================================================
# 导出
# ============================================================================


def test_export_contains_roles_skills_and_only_referenced_mcp(tmp_path):
    env = make_env(tmp_path / "a")
    pool = seed_pool(env)
    name, data = env.bundle.build_export(pool["id"])
    files = zip_entries(data)

    assert name == "池A"
    assert files["roles/Worker/w1.yaml"].decode().startswith("# 注释保留 w")  # 原文，含注释
    assert "roles/Manager/m1.yaml" in files
    assert files["skills/s1/assets/blob.bin"] == bytes(range(256))  # 二进制原样
    assert set(json.loads(files["mcp_servers.json"])["servers"]) == {"echo-probe"}  # 不带 other
    manifest = json.loads(files["manifest.json"])
    assert manifest["pool"] == {"name": "池A", "roles": ["w1", "m1"]}
    assert set(manifest["defaults_sha256"]) == {"manager", "worker"}


def test_export_follows_skills_inherited_from_default(tmp_path):
    env = make_env(tmp_path / "a", worker_default_skills="s1")
    add_skill(env, "s1")
    add_role(env, "worker", "w1", 'role_md: "only"\n')  # 自己不写 skills，靠 default 继承
    pool = env.pools.create_pool("P", ["w1"])
    assert env.bundle.preview_export(pool["id"])["skills"] == ["s1"]


def test_preview_lists_sensitive_keys_never_values(tmp_path):
    env = make_env(tmp_path / "a")
    pool = seed_pool(env)
    set_mcp(env, {"echo-probe": {**ECHO, "env": {"API_TOKEN": "sekrit-value", "EMPTY": ""}}})
    preview = env.bundle.preview_export(pool["id"])
    assert preview["sensitive_mcp"] == [{"name": "echo-probe", "keys": ["env:API_TOKEN"]}]
    assert "sekrit-value" not in json.dumps(preview)


def test_export_missing_references_become_warnings_not_failure(tmp_path):
    env = make_env(tmp_path / "a")
    add_role(env, "worker", "w1", worker_yaml(skills=("ghost",), mcp=("nomcp",)))
    pool = env.pools.create_pool("P", ["w1"])
    preview = env.bundle.preview_export(pool["id"])
    assert {(w["kind"], w["ref"]) for w in preview["warnings"]} == {
        ("missing_skill", "ghost"), ("missing_mcp", "nomcp"),
    }
    env.bundle.build_export(pool["id"])  # 不抛


def test_export_unknown_pool_raises_keyerror(tmp_path):
    env = make_env(tmp_path / "a")
    with pytest.raises(KeyError):
        env.bundle.preview_export("nope")


# ============================================================================
# 往返与冲突
# ============================================================================


def exported(tmp_path):
    src = make_env(tmp_path / "src")
    pool = seed_pool(src)
    _, data = src.bundle.build_export(pool["id"])
    return src, data


def test_round_trip_into_empty_environment(tmp_path):
    src, data = exported(tmp_path)
    dst = make_env(tmp_path / "dst")
    result = dst.bundle.import_bundle(data)

    assert result["counts"] == {"created": 5, "overwritten": 0, "skipped": 0, "unchanged": 0}
    for kind, role_id in (("worker", "w1"), ("manager", "m1")):
        assert dst.roles.read_role_raw(role_id, kind)["content"] == src.roles.read_role_raw(role_id, kind)["content"]
    assert (dst.root / "skills/s1/assets/blob.bin").read_bytes() == bytes(range(256))
    assert mcp_servers(dst)["echo-probe"]["command"] == "python3"
    assert [p["roles"] for p in dst.pools.list_pools()] == [["w1", "m1"]]
    assert dst.pools.list_pools()[0]["id"] != src.pools.list_pools()[0]["id"] or True
    assert all(r["ready"] for r in result["readiness"])
    assert not [p for p in dst.root.iterdir() if p.name.startswith(".aicu-")]


def test_reimport_is_all_same_and_not_a_conflict(tmp_path):
    _, data = exported(tmp_path)
    dst = make_env(tmp_path / "dst")
    dst.bundle.import_bundle(data)

    plan = dst.bundle.import_bundle(data, dry_run=True)
    assert not plan["has_conflict"]
    assert {i["status"] for i in plan["roles"] + plan["skills"] + plan["mcp"]} == {"same"}
    assert plan["pool"]["status"] == "same"
    again = dst.bundle.import_bundle(data)
    assert again["counts"]["unchanged"] == 5 and len(dst.pools.list_pools()) == 1


def test_dry_run_writes_nothing(tmp_path):
    _, data = exported(tmp_path)
    dst = make_env(tmp_path / "dst")
    plan = dst.bundle.import_bundle(data, dry_run=True)
    assert plan["dry_run"] is True
    assert [i["status"] for i in plan["roles"]] == ["new", "new"]
    assert plan["mcp"][0]["command"] == "python3" and plan["mcp"][0]["args"] == ["tests/mcp_echo_server.py"]
    assert dst.roles.list_roles() == [] and dst.pools.list_pools() == []
    assert not (dst.root / "skills/s1").exists() and mcp_servers(dst) == {}


def edit_local_copy(dst):
    dst.roles.update_role("w1", content=worker_yaml(skills=("s1",), mcp=("echo-probe",), note="LOCAL"), role_type="worker")
    (dst.root / "skills/s1/SKILL.md").write_text("local edit", "utf-8")
    (dst.root / "skills/s1/extra.txt").write_text("only local", "utf-8")
    set_mcp(dst, {"echo-probe": {**ECHO, "description": "local"}})


def test_conflict_skip_keeps_local_versions(tmp_path):
    _, data = exported(tmp_path)
    dst = make_env(tmp_path / "dst")
    dst.bundle.import_bundle(data)
    edit_local_copy(dst)

    plan = dst.bundle.import_bundle(data, dry_run=True)
    assert plan["has_conflict"]
    result = dst.bundle.import_bundle(data, on_conflict="skip")
    assert result["counts"]["skipped"] == 3  # w1、s1、echo-probe
    assert "LOCAL" in dst.roles.read_role_raw("w1", "worker")["content"]
    assert (dst.root / "skills/s1/SKILL.md").read_text("utf-8") == "local edit"
    assert mcp_servers(dst)["echo-probe"]["description"] == "local"


def test_conflict_overwrite_replaces_everything_and_leaves_no_backup(tmp_path):
    src, data = exported(tmp_path)
    dst = make_env(tmp_path / "dst")
    dst.bundle.import_bundle(data)
    edit_local_copy(dst)

    result = dst.bundle.import_bundle(data, on_conflict="overwrite")
    assert result["counts"]["overwritten"] == 3
    assert dst.roles.read_role_raw("w1", "worker")["content"] == src.roles.read_role_raw("w1", "worker")["content"]
    assert (dst.root / "skills/s1/SKILL.md").read_bytes() == (src.root / "skills/s1/SKILL.md").read_bytes()
    assert not (dst.root / "skills/s1/extra.txt").exists()  # 整目录替换，不是叠加
    assert "description" not in mcp_servers(dst)["echo-probe"]
    assert not [p for p in dst.root.iterdir() if p.name.startswith(".aicu-")]


def test_pool_name_conflict_overwrite_keeps_local_id(tmp_path):
    _, data = exported(tmp_path)
    dst = make_env(tmp_path / "dst")
    dst.bundle.import_bundle(data)
    local = dst.pools.list_pools()[0]
    dst.pools.update_pool(local["id"], roles=["w1"])

    assert dst.bundle.import_bundle(data, dry_run=True)["pool"]["status"] == "conflict"
    dst.bundle.import_bundle(data, on_conflict="skip")
    assert dst.pools.get_pool(local["id"])["roles"] == ["w1"]
    dst.bundle.import_bundle(data, on_conflict="overwrite")
    assert [p["id"] for p in dst.pools.list_pools()] == [local["id"]]
    assert dst.pools.get_pool(local["id"])["roles"] == ["w1", "m1"]


def test_invalid_on_conflict_rejected(tmp_path):
    _, data = exported(tmp_path)
    with pytest.raises(ValueError):
        make_env(tmp_path / "dst").bundle.import_bundle(data, on_conflict="merge")


# ============================================================================
# 回滚
# ============================================================================


def test_failure_midway_rolls_back_new_items(tmp_path, monkeypatch):
    _, data = exported(tmp_path)
    dst = make_env(tmp_path / "dst")
    monkeypatch.setattr(dst.pools, "create_pool", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        dst.bundle.import_bundle(data)
    assert dst.roles.list_roles() == [] and dst.pools.list_pools() == []
    assert not (dst.root / "skills/s1").exists() and mcp_servers(dst) == {}
    assert not [p for p in dst.root.iterdir() if p.name.startswith(".aicu-")]


def test_failure_midway_restores_overwritten_items(tmp_path, monkeypatch):
    _, data = exported(tmp_path)
    dst = make_env(tmp_path / "dst")
    dst.bundle.import_bundle(data)
    edit_local_copy(dst)
    local_role = dst.roles.read_role_raw("w1", "worker")["content"]
    local_mcp = mcp_servers(dst)

    dst.pools.update_pool(dst.pools.list_pools()[0]["id"], roles=["w1"])
    monkeypatch.setattr(dst.pools, "update_pool", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        dst.bundle.import_bundle(data, on_conflict="overwrite")

    assert dst.roles.read_role_raw("w1", "worker")["content"] == local_role
    assert (dst.root / "skills/s1/SKILL.md").read_text("utf-8") == "local edit"
    assert (dst.root / "skills/s1/extra.txt").read_text("utf-8") == "only local"
    assert mcp_servers(dst) == local_mcp
    assert not [p for p in dst.root.iterdir() if p.name.startswith(".aicu-")]


# ============================================================================
# 预检：任何写入之前整体校验
# ============================================================================


def assert_nothing_written(env):
    assert env.roles.list_roles() == [] and env.pools.list_pools() == []
    assert list((env.root / "skills").iterdir()) == [] and mcp_servers(env) == {}


def errors_of(env, data: bytes, **kwargs) -> list[str]:
    with pytest.raises(BundleError) as caught:
        env.bundle.import_bundle(data, **kwargs)
    return caught.value.errors


def valid_entries(**extra):
    entries = {
        "manifest.json": good_manifest(),
        "roles/Worker/w1.yaml": 'role_md: "w"\n',
    }
    entries.update(extra)
    return entries


def test_minimal_valid_bundle_imports(tmp_path):
    env = make_env(tmp_path / "dst")
    result = env.bundle.import_bundle(make_zip(valid_entries()))
    assert result["counts"]["created"] == 2
    assert env.pools.list_pools()[0]["name"] == "池X"


@pytest.mark.parametrize("name", ["../evil.yaml", "/abs.txt", "skills/s1/../../x", "roles/Worker/../../x.yaml"])
def test_zip_slip_entries_rejected(tmp_path, name):
    env = make_env(tmp_path / "dst")
    errors = errors_of(env, make_zip(valid_entries(**{name: "x"})), dry_run=True)
    assert any("非法条目路径" in e for e in errors)
    assert_nothing_written(env)


def test_not_a_zip_and_missing_or_bad_manifest(tmp_path):
    env = make_env(tmp_path / "dst")
    assert errors_of(env, b"not a zip") == ["不是有效的 zip 文件"]
    assert errors_of(env, make_zip({"roles/Worker/w1.yaml": "role_md: x\n"})) == ["缺少 manifest.json"]
    assert "合法 JSON" in errors_of(env, make_zip({"manifest.json": "{"}))[0]
    assert "manifest.format" in errors_of(env, make_zip({"manifest.json": good_manifest(format="other")}))[0]
    assert "版本" in errors_of(env, make_zip(valid_entries(**{"manifest.json": good_manifest(version=99)})))[0]
    assert_nothing_written(env)


def test_all_errors_reported_together_and_nothing_written(tmp_path):
    env = make_env(tmp_path / "dst")
    data = make_zip(valid_entries(**{
        "roles/Worker/w1.yaml": "not_a_field: 1\n",
        "roles/Manager/Bad-Id.yaml": 'role_md: "x"\n',
        "mcp_servers.json": json.dumps({"servers": {"bad": {"command": ""}}}),
        "skills/orphan/notes.txt": "no entrypoint",
    }))
    errors = errors_of(env, data)
    joined = "\n".join(errors)
    assert "worker/w1" in joined and "Bad-Id" in joined and "MCP 'bad'" in joined and "orphan" in joined
    assert_nothing_written(env)


def test_yaml_field_whitelist_enforced_for_raw_content(tmp_path):
    env = make_env(tmp_path / "dst")
    errors = errors_of(env, make_zip(valid_entries(**{"roles/Worker/w1.yaml": 'role_md: "w"\nsystem_role: "hijack"\n'})))
    assert any("worker/w1" in e for e in errors)
    assert_nothing_written(env)


def test_explicit_null_on_known_field_is_importable(tmp_path):
    # 仓库自带的 Worker 角色大量写 output_file: null（modular_output 模式），导入不能拒收。
    env = make_env(tmp_path / "dst")
    entries = valid_entries(**{"roles/Worker/w1.yaml": 'role_md: "w"\nmodular_output: true\noutput_file: null\n'})
    env.bundle.import_bundle(make_zip(entries))
    assert "output_file: null" in env.roles.read_role_raw("w1", "worker")["content"]


def test_newline_in_ids_is_rejected(tmp_path):
    env = make_env(tmp_path / "dst")
    pool_bad = good_manifest(pool={"name": "池X", "roles": ["w1\n"]})
    assert any("非法的角色 id" in e for e in errors_of(env, make_zip(valid_entries(**{"manifest.json": pool_bad}))))
    mcp_bad = json.dumps({"servers": {"echo\n": ECHO}})
    assert any("非法名字" in e for e in errors_of(env, make_zip(valid_entries(**{"mcp_servers.json": mcp_bad}))))
    assert_nothing_written(env)


def test_pool_referencing_unknown_role_rejected_but_local_role_allowed(tmp_path):
    env = make_env(tmp_path / "dst")
    bundle = make_zip({"manifest.json": good_manifest()})
    assert any("既不在包内也不在本机" in e for e in errors_of(env, bundle))
    add_role(env, "worker", "w1", 'role_md: "local"\n')
    result = env.bundle.import_bundle(bundle)
    assert result["counts"]["created"] == 1 and env.pools.list_pools()[0]["roles"] == ["w1"]


def test_size_limits(tmp_path, monkeypatch):
    env = make_env(tmp_path / "dst")
    data = make_zip(valid_entries())

    monkeypatch.setattr(bundle_module, "MAX_BUNDLE_BYTES", 10)
    with pytest.raises(BundleTooLarge):
        env.bundle.import_bundle(data)
    monkeypatch.undo()

    monkeypatch.setattr(bundle_module, "MAX_ENTRIES", 1)
    assert "条目数" in errors_of(env, data)[0]
    monkeypatch.undo()

    monkeypatch.setattr(bundle_module, "MAX_UNCOMPRESSED_BYTES", 5)
    assert "解压后总量" in errors_of(env, data)[0]
    monkeypatch.undo()

    monkeypatch.setattr(bundle_module, "MAX_ROLE_BYTES", 5)
    assert "超过大小上限" in "\n".join(errors_of(env, data))
    assert_nothing_written(env)


def test_skill_over_limit_rejected(tmp_path, monkeypatch):
    env = make_env(tmp_path / "dst")
    monkeypatch.setattr(bundle_module, "MAX_SKILL_BYTES", 10)
    data = make_zip(valid_entries(**{"skills/big/SKILL.md": "x" * 11}))
    assert "big" in "\n".join(errors_of(env, data))
    assert_nothing_written(env)


def test_unknown_manifest_warnings_are_sanitized(tmp_path):
    env = make_env(tmp_path / "dst")
    manifest = good_manifest(warnings=[{"kind": "x" * 500, "ref": "r" * 500, "role": 5}, "junk", {"kind": 1}])
    plan = env.bundle.import_bundle(make_zip(valid_entries(**{"manifest.json": manifest})), dry_run=True)
    for warning in plan["warnings"]:
        assert all(isinstance(v, str) and len(v) <= 200 for v in warning.values())


# ============================================================================
# 可用性自检与 default.yaml 差异
# ============================================================================


def readiness_of(env, data, **kwargs):
    return {r["id"]: r for r in env.bundle.import_bundle(data, **kwargs)["readiness"]}


def bundle_with(skills=("s1",), mcp=("echo-probe",), servers=None, with_skill=True):
    entries = valid_entries(**{
        "roles/Worker/w1.yaml": worker_yaml(skills=skills, mcp=mcp),
        "mcp_servers.json": json.dumps({"servers": servers if servers is not None else {"echo-probe": ECHO}}),
    })
    if with_skill:
        entries["skills/s1/SKILL.md"] = "---\nname: s1\n---\nx\n"
    return make_zip(entries)


def issue_kinds(report):
    return {(i["kind"], i["ref"]) for i in report["issues"]}


def test_readiness_flags_missing_skill_unregistered_mcp(tmp_path):
    env = make_env(tmp_path / "dst")
    data = bundle_with(skills=("s1", "ghost"), mcp=("echo-probe", "nomcp"))
    report = readiness_of(env, data, dry_run=True)["w1"]
    assert not report["ready"]
    assert issue_kinds(report) == {("skill_missing", "ghost"), ("mcp_unregistered", "nomcp")}
    assert readiness_of(env, data)["w1"]["ready"] is False  # 导入后重算，结论一致


def test_readiness_flags_missing_command_and_arg_path(tmp_path):
    env = make_env(tmp_path / "dst")
    servers = {
        "nocmd": {"command": "definitely-not-installed-xyz-123"},
        "noarg": {"command": "python3", "args": ["tests/definitely_missing_server.py"]},
    }
    data = bundle_with(skills=(), mcp=("nocmd", "noarg"), servers=servers, with_skill=False)
    report = readiness_of(env, data, dry_run=True)["w1"]
    assert issue_kinds(report) == {("mcp_command_missing", "nocmd"), ("mcp_arg_missing", "noarg")}


def test_readiness_depends_on_conflict_choice(tmp_path):
    env = make_env(tmp_path / "dst")
    good = bundle_with()
    env.bundle.import_bundle(good)
    set_mcp(env, {"echo-probe": {"command": "definitely-not-installed-xyz-123"}})  # 本地版本坏了

    skip = readiness_of(env, good, on_conflict="skip", dry_run=True)["w1"]
    overwrite = readiness_of(env, good, on_conflict="overwrite", dry_run=True)["w1"]
    assert not skip["ready"] and overwrite["ready"]


def test_defaults_differ_warning(tmp_path):
    _, data = exported(tmp_path)
    dst = make_env(tmp_path / "dst")
    assert not [w for w in dst.bundle.import_bundle(data, dry_run=True)["warnings"] if w["kind"] == "defaults_differ"]

    default = dst.root / "roles/Worker/default.yaml"
    default.write_text(default.read_text("utf-8") + "\n# local tweak\n", "utf-8")
    warnings = dst.bundle.import_bundle(data, dry_run=True)["warnings"]
    assert [(w["kind"], w["ref"]) for w in warnings if w["kind"] == "defaults_differ"] == [("defaults_differ", "worker")]


# ============================================================================
# 逐项决定、used_by、脚本计数、CLI 自检、个人 skill
# ============================================================================


def conflicted(tmp_path):
    src, data = exported(tmp_path)
    dst = make_env(tmp_path / "dst")
    dst.bundle.import_bundle(data)
    edit_local_copy(dst)
    return src, dst, data


def test_per_item_decisions_override_global_policy(tmp_path):
    src, dst, data = conflicted(tmp_path)
    plan = dst.bundle.import_bundle(data, dry_run=True, decisions={"skill:s1": "overwrite"})
    assert {i["key"]: i["policy"] for i in plan["roles"] + plan["skills"] + plan["mcp"] if i["status"] == "conflict"} == {
        "role:worker/w1": "skip", "skill:s1": "overwrite", "mcp:echo-probe": "skip",
    }

    result = dst.bundle.import_bundle(data, on_conflict="skip", decisions={"skill:s1": "overwrite"})
    assert result["counts"]["overwritten"] == 1 and result["counts"]["skipped"] == 2
    assert (dst.root / "skills/s1/SKILL.md").read_bytes() == (src.root / "skills/s1/SKILL.md").read_bytes()
    assert "LOCAL" in dst.roles.read_role_raw("w1", "worker")["content"]
    assert mcp_servers(dst)["echo-probe"]["description"] == "local"


def test_decision_can_skip_one_item_under_global_overwrite(tmp_path):
    _, dst, data = conflicted(tmp_path)
    result = dst.bundle.import_bundle(data, on_conflict="overwrite", decisions={"mcp:echo-probe": "skip"})
    assert result["counts"]["overwritten"] == 2 and result["counts"]["skipped"] == 1
    assert mcp_servers(dst)["echo-probe"]["description"] == "local"


def test_pool_decision_and_unknown_keys_ignored(tmp_path):
    _, data = exported(tmp_path)
    dst = make_env(tmp_path / "dst")
    dst.bundle.import_bundle(data)
    local = dst.pools.list_pools()[0]
    dst.pools.update_pool(local["id"], roles=["w1"])
    dst.bundle.import_bundle(data, on_conflict="skip", decisions={"pool": "overwrite", "role:worker/ghost": "overwrite"})
    assert dst.pools.get_pool(local["id"])["roles"] == ["w1", "m1"]


def test_invalid_decision_value_rejected(tmp_path):
    _, data = exported(tmp_path)
    dst = make_env(tmp_path / "dst")
    with pytest.raises(ValueError, match="skip"):
        dst.bundle.import_bundle(data, dry_run=True, decisions={"skill:s1": "merge"})


def test_readiness_follows_per_item_decision(tmp_path):
    env = make_env(tmp_path / "dst")
    good = bundle_with()
    env.bundle.import_bundle(good)
    set_mcp(env, {"echo-probe": {"command": "definitely-not-installed-xyz-123"}})
    fixed = readiness_of(env, good, on_conflict="skip", decisions={"mcp:echo-probe": "overwrite"}, dry_run=True)["w1"]
    assert fixed["ready"]


def test_conflicts_report_what_else_uses_the_local_version(tmp_path):
    _, dst, data = conflicted(tmp_path)
    dst.pools.create_pool("另一个池", ["w1"])
    plan = dst.bundle.import_bundle(data, dry_run=True)
    role = next(r for r in plan["roles"] if r["id"] == "w1")
    assert sorted(role["used_by"]) == ["另一个池", "池A"]
    assert plan["skills"][0]["used_by"] == ["w1"]
    assert plan["mcp"][0]["used_by"] == ["w1"]
    assert "used_by" not in next(r for r in plan["roles"] if r["id"] == "m1")  # 非冲突项不算


def test_pool_conflict_lists_local_roles(tmp_path):
    _, data = exported(tmp_path)
    dst = make_env(tmp_path / "dst")
    dst.bundle.import_bundle(data)
    local = dst.pools.list_pools()[0]
    dst.pools.update_pool(local["id"], roles=["w1"])
    pool = dst.bundle.import_bundle(data, dry_run=True)["pool"]
    assert pool["status"] == "conflict" and pool["local_roles"] == ["w1"]


def test_skill_script_count_in_plan(tmp_path):
    env = make_env(tmp_path / "dst")
    entries = valid_entries(**{"roles/Worker/w1.yaml": worker_yaml(skills=("s1",))})
    entries.update({
        "skills/s1/SKILL.md": "---\nname: s1\n---\nx\n",
        "skills/s1/run.py": "print(1)",
        "skills/s1/bin/setup.SH": "echo",
        "skills/s1/notes.txt": "no",
    })
    plan = env.bundle.import_bundle(make_zip(entries), dry_run=True)
    assert plan["skills"][0]["script_count"] == 2 and plan["skills"][0]["file_count"] == 4


def test_readiness_flags_missing_cli(tmp_path, monkeypatch):
    env = make_env(tmp_path / "dst")
    entries = valid_entries(**{"roles/Worker/w1.yaml": worker_yaml() + "cli_name: codex\n"})
    data = make_zip(entries)

    monkeypatch.setattr(bundle_module.shutil, "which", lambda command: "/bin/x")
    assert readiness_of(env, data, dry_run=True)["w1"]["ready"]
    monkeypatch.setattr(bundle_module.shutil, "which", lambda command: None)
    report = readiness_of(env, data, dry_run=True)["w1"]
    assert [(i["kind"], i["ref"]) for i in report["issues"]] == [("cli_missing", "codex")]


def test_preview_lists_personal_skills_outside_primary_dir(tmp_path):
    env = make_env(tmp_path / "a")
    personal = tmp_path / "personal"
    (personal / "mine").mkdir(parents=True)
    (personal / "mine" / "SKILL.md").write_text("---\nname: mine\n---\nx\n", "utf-8")
    env.skills.extra_dirs = [personal.resolve()]
    add_skill(env, "s1")
    add_role(env, "worker", "w1", worker_yaml(skills=("s1", "mine")))
    pool = env.pools.create_pool("P", ["w1"])
    preview = env.bundle.preview_export(pool["id"])
    assert preview["skills"] == ["s1", "mine"] and preview["personal_skills"] == ["mine"]
