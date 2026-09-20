"""runtime/skills_manager.py（M5 能力配置：Skill 管理）单元测试。

覆盖：列表/读取/保存/删除、主目录优先去重、zip 导入（正常/已存在/无效/空/
无 SKILL.md/zip-slip/超限）、frontmatter 解析与回退、allowed-tools 变体。
全部使用 tmp_path 隔离，不触碰仓库 skills/ 目录（C6）。
"""

import io
import os
import zipfile

import pytest

from runtime.skills_manager import SkillsManager


def _write_skill(skills_dir, skill_id, content):
    d = os.path.join(str(skills_dir), skill_id)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "SKILL.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class TestListSkills:
    def test_lists_sorted_and_skips_missing_entrypoint(self, tmp_path):
        _write_skill(
            tmp_path, "dev-skill",
            "---\nname: Dev Skill\ndescription: 写代码\n"
            "allowed-tools: [bash, read]\n---\n# Dev\n正文\n",
        )
        _write_skill(tmp_path, "plain", "Intro line\n")
        os.makedirs(tmp_path / "noentry")  # 无 SKILL.md → 跳过

        skills = SkillsManager(str(tmp_path)).list_skills()
        assert [s["id"] for s in skills] == ["dev-skill", "plain"]
        dev = skills[0]
        assert dev["name"] == "Dev Skill"
        assert dev["description"] == "写代码"
        assert dev["allowed_tools"] == ["bash", "read"]
        # 无 frontmatter → name 回退标题、description 回退正文首行
        assert skills[1]["name"] == "Plain"
        assert skills[1]["description"] == "Intro line"

    def test_missing_dir_returns_empty(self, tmp_path):
        assert SkillsManager(str(tmp_path / "nope")).list_skills() == []


class TestCrud:
    def test_save_get_overwrite(self, tmp_path):
        sm = SkillsManager(str(tmp_path))
        r = sm.save_skill("my-skill", "---\nname: My Skill\n---\nBody\n")
        assert r["id"] == "my-skill"
        assert r["name"] == "My Skill"
        assert r["files"] == ["SKILL.md"]
        sm.save_skill("my-skill", "v2\n")
        assert sm.get_skill("my-skill")["content"] == "v2\n"

    def test_get_missing_raises_keyerror(self, tmp_path):
        sm = SkillsManager(str(tmp_path))
        with pytest.raises(KeyError):
            sm.get_skill("ghost")

    @pytest.mark.parametrize("bad", ["Bad", "1x", "a b", "", "../x", "x/../y", "/tmp/x"])
    def test_save_and_get_reject_invalid_id(self, tmp_path, bad):
        sm = SkillsManager(str(tmp_path))
        with pytest.raises(ValueError):
            sm.save_skill(bad, "ok\n")
        with pytest.raises(ValueError):
            sm.get_skill(bad)

    @pytest.mark.parametrize("content", ["", "   "])
    def test_save_rejects_empty_content(self, tmp_path, content):
        sm = SkillsManager(str(tmp_path))
        with pytest.raises(ValueError):
            sm.save_skill("x", content)

    def test_save_rejects_oversize_content(self, tmp_path):
        sm = SkillsManager(str(tmp_path))
        with pytest.raises(ValueError):
            sm.save_skill("x", "a" * 200_001)

    def test_delete(self, tmp_path):
        sm = SkillsManager(str(tmp_path))
        sm.save_skill("my-skill", "x\n")
        sm.delete_skill("my-skill")
        assert not (tmp_path / "my-skill").is_dir()
        with pytest.raises(KeyError):
            sm.delete_skill("ghost")


class TestExtraDirs:
    def test_main_dir_wins_and_delete_only_main(self, tmp_path):
        main = tmp_path / "main"
        extra = tmp_path / "extra"
        _write_skill(extra, "shared", "---\nname: ExtraShared\n---\nE\n")
        _write_skill(main, "shared", "---\nname: MainShared\n---\nM\n")

        sm = SkillsManager(str(main), extra_dirs=[str(extra)])
        skills = sm.list_skills()
        assert [s["id"] for s in skills] == ["shared"]
        assert skills[0]["name"] == "MainShared"  # 主目录优先去重
        assert sm.get_skill("shared")["name"] == "MainShared"

        sm.delete_skill("shared")  # 只删主目录
        assert not (main / "shared").is_dir()
        assert (extra / "shared" / "SKILL.md").is_file()


class TestImportZip:
    def _zip_bytes(self, entries, size=None):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in entries:
                z.writestr(name, data)
        buf.seek(0)
        return buf.read()

    def test_import_extracts_nested_files(self, tmp_path):
        sm = SkillsManager(str(tmp_path))
        r = sm.import_skill_zip(
            "tool",
            self._zip_bytes([
                ("tool/SKILL.md", "---\nname: Tool\n---\nT\n"),
                ("tool/run.sh", "#!/bin/sh\n"),
            ]),
        )
        assert r["id"] == "tool"
        assert r["files"] == ["SKILL.md", "run.sh"]
        assert (tmp_path / "tool" / "run.sh").is_file()

    def test_import_rejects_existing(self, tmp_path):
        sm = SkillsManager(str(tmp_path))
        data = self._zip_bytes([("tool/SKILL.md", "x")])
        sm.import_skill_zip("tool", data)
        with pytest.raises(ValueError):
            sm.import_skill_zip("tool", data)

    def test_import_rejects_invalid_and_empty_zip(self, tmp_path):
        sm = SkillsManager(str(tmp_path))
        with pytest.raises(ValueError):
            sm.import_skill_zip("bad", b"not a zip")
        with pytest.raises(ValueError):
            sm.import_skill_zip("empty", self._zip_bytes([]))

    def test_import_rejects_missing_skillmd_and_cleans_up(self, tmp_path):
        sm = SkillsManager(str(tmp_path))
        with pytest.raises(ValueError):
            sm.import_skill_zip(
                "noskill", self._zip_bytes([("noskill/readme.md", "x")]),
            )
        assert not (tmp_path / "noskill").is_dir()

    def test_import_rejects_zip_slip(self, tmp_path):
        sm = SkillsManager(str(tmp_path))
        with pytest.raises(ValueError):
            sm.import_skill_zip(
                "slip", self._zip_bytes([("../evil.txt", "x")]),
            )

    def test_import_rejects_oversize_archive(self, tmp_path):
        sm = SkillsManager(str(tmp_path))
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as z:
            z.writestr("big/SKILL.md", b"x" * (20 * 1024 * 1024 + 1))
        data.seek(0)
        with pytest.raises(ValueError):
            sm.import_skill_zip("big", data.read())


class TestFrontmatterHelpers:
    def test_allowed_tools_variants(self, tmp_path):
        _write_skill(tmp_path, "a", "---\nname: A\nallowed-tools: [bash]\n---\n# A\n")
        _write_skill(tmp_path, "b", "---\nname: B\nallowed_tools: bash\n---\nB\n")
        _write_skill(tmp_path, "c", "---\nname: C\nallowed-tools: 3\n---\nC\n")
        by_id = {s["id"]: s for s in SkillsManager(str(tmp_path)).list_skills()}
        assert by_id["a"]["allowed_tools"] == ["bash"]  # 连字符规范
        assert by_id["b"]["allowed_tools"] == ["bash"]  # 下划线变体
        assert by_id["c"]["allowed_tools"] == []  # 非 list/str → []

    def test_frontmatter_fallback(self, tmp_path):
        _write_skill(tmp_path, "d", "plain body\n")
        _write_skill(tmp_path, "e", "---\nnot: [valid\n---\nbody\n")
        _write_skill(tmp_path, "f", "---\n- a\n- b\n---\nbody\n")
        by_id = {s["id"]: s for s in SkillsManager(str(tmp_path)).list_skills()}
        assert by_id["d"]["name"] == "D"  # 无 frontmatter → 标题
        assert by_id["d"]["description"] == "plain body"
        assert by_id["e"]["name"] == "E"  # 损坏 frontmatter → 回退
        assert by_id["f"]["name"] == "F"  # 非 dict frontmatter → 回退
