"""Manage project-scoped skills exposed to execution workspaces."""

from __future__ import annotations

import io
import logging
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

_SKILL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
_MAX_CONTENT = 200_000
# 多文件导入时的总解压上限（防 zip bomb）。
_MAX_IMPORT_BYTES = 20 * 1024 * 1024


class SkillsManager:
    """Store skills as directories containing a SKILL.md entrypoint.

    Supports multiple source directories:
    - skills_dir: 主目录（可读写，新增/修改的技能写入此目录）
    - extra_dirs: 额外只读目录（如 ~/.claude/skills），仅用于列表和读取
    """

    def __init__(self, skills_dir: str, extra_dirs: list[str] | None = None):
        self.skills_dir = Path(skills_dir).resolve()
        self.extra_dirs = [Path(d).expanduser().resolve() for d in (extra_dirs or [])]

    def _all_dirs(self) -> list[Path]:
        """返回所有有效的技能目录（主目录 + 额外目录）。"""
        dirs: list[Path] = []
        if self.skills_dir.is_dir():
            dirs.append(self.skills_dir)
        for d in self.extra_dirs:
            if d.is_dir() and d not in dirs:
                dirs.append(d)
        return dirs

    def list_skills(self) -> list[dict[str, Any]]:
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for skills_dir in self._all_dirs():
            for path in sorted(skills_dir.iterdir(), key=lambda item: item.name):
                if path.name in seen:
                    continue
                entry = path / "SKILL.md"
                if not path.is_dir() or not entry.is_file():
                    continue
                seen.add(path.name)
                content = entry.read_text(encoding="utf-8")
                meta = _frontmatter_meta(content)
                result.append({
                    "id": path.name,
                    "name": meta.get("name") or _title(path.name),
                    "description": meta.get("description") or _description(content),
                    "license": meta.get("license"),
                    "allowed_tools": _allowed_tools(meta),
                    "metadata": meta.get("metadata"),
                    "size": len(content),
                    "file_count": _file_count(path),
                })
        return result

    def get_skill(self, skill_id: str) -> dict[str, Any]:
        self._validate_id(skill_id)
        # 优先在主目录查找，再查额外目录
        for skills_dir in self._all_dirs():
            path = skills_dir / skill_id
            entry = path / "SKILL.md"
            if entry.is_file():
                content = entry.read_text(encoding="utf-8")
                meta = _frontmatter_meta(content)
                return {
                    "id": path.name,
                    "name": meta.get("name") or _title(path.name),
                    "description": meta.get("description") or _description(content),
                    "license": meta.get("license"),
                    "allowed_tools": _allowed_tools(meta),
                    "metadata": meta.get("metadata"),
                    "content": content,
                    "files": _list_files(path),
                }
        raise KeyError(f"Skill '{skill_id}' not found")

    def save_skill(self, skill_id: str, content: str) -> dict[str, Any]:
        self._validate_id(skill_id)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Skill content cannot be empty")
        if len(content.encode("utf-8")) > _MAX_CONTENT:
            raise ValueError(f"Skill content exceeds {_MAX_CONTENT} bytes")
        path = self._skill_path(skill_id)
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(content, encoding="utf-8")
        return self.get_skill(skill_id)

    def delete_skill(self, skill_id: str) -> None:
        """删除主目录下的 skill 目录（额外只读目录中的 skill 不删除）。

        Raises:
            KeyError: skill 在主目录中不存在时。
        """
        self._validate_id(skill_id)
        path = self._skill_path(skill_id)
        if not path.is_dir():
            raise KeyError(f"Skill '{skill_id}' not found")
        shutil.rmtree(path)
        logger.info("Deleted skill directory: %s", path)

    def import_skill_zip(self, skill_id: str, zip_bytes: bytes) -> dict[str, Any]:
        """从 zip 归档导入完整的多文件 skill（保留脚本/模板/引用）。

        归档可包含顶层目录（如 ``my-skill/SKILL.md``）或直接以 SKILL.md 为根。
        必须有 SKILL.md 入口文件。所有条目做 zip-slip 防护。
        """
        self._validate_id(skill_id)
        path = self._skill_path(skill_id)
        if path.exists():
            raise ValueError(f"Skill '{skill_id}' already exists")
        try:
            archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile as error:
            raise ValueError("Invalid zip archive") from error

        names = [name for name in archive.namelist() if not name.endswith("/")]
        if not names:
            raise ValueError("Zip archive is empty")

        total = sum(info.file_size for info in archive.infolist())
        if total > _MAX_IMPORT_BYTES:
            raise ValueError(f"Skill archive exceeds {_MAX_IMPORT_BYTES} bytes")

        strip = _single_root_prefix(names)

        path.mkdir(parents=True, exist_ok=True)
        try:
            for name in names:
                self._assert_safe_name(name)
                rel = name[len(strip):] if strip and name.startswith(strip) else name
                if not rel:
                    continue
                dest = self._safe_dest(path, rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        except Exception:
            shutil.rmtree(path, ignore_errors=True)
            raise

        if not (path / "SKILL.md").is_file():
            shutil.rmtree(path, ignore_errors=True)
            raise ValueError("Skill archive must contain a SKILL.md entrypoint")
        return self.get_skill(skill_id)

    @staticmethod
    def _assert_safe_name(name: str) -> None:
        """拒绝归档条目名中的路径穿越（在 strip 前缀前校验原始条目名）。"""
        if name.startswith("/") or "\x00" in name or ".." in Path(name).parts:
            raise ValueError(f"Invalid entry path in archive: {name}")

    def _safe_dest(self, root: Path, rel: str) -> Path:
        """校验相对路径并返回 root 下的解析路径（zip-slip 防护）。"""
        parts = Path(rel).parts
        if not parts or rel.startswith("/") or ".." in parts or "\x00" in rel:
            raise ValueError(f"Invalid entry path in archive: {rel}")
        dest = (root / rel).resolve()
        if root not in dest.parents and dest != root:
            raise ValueError(f"Invalid entry path in archive: {rel}")
        return dest

    def skill_dir(self, skill_id: str, primary_only: bool = False) -> Optional[Path]:
        """按名字找 skill 目录（主目录优先，再额外目录）；找不到返回 None。

        ``primary_only=True`` 只看主目录 —— 角色运行时默认只从主目录解析 skill。
        """
        self._validate_id(skill_id)
        dirs = [self.skills_dir] if primary_only else self._all_dirs()
        for root in dirs:
            path = root / skill_id
            if path.is_dir() and (path / "SKILL.md").is_file():
                return path
        return None

    def _skill_path(self, skill_id: str) -> Path:
        self._validate_id(skill_id)
        path = (self.skills_dir / skill_id).resolve()
        if self.skills_dir not in path.parents:
            raise ValueError("Invalid skill path")
        return path

    @staticmethod
    def _validate_id(skill_id: str) -> None:
        if not isinstance(skill_id, str) or not _SKILL_ID_PATTERN.fullmatch(skill_id):
            raise ValueError("Skill id must match [a-z][a-z0-9_-]*")


def _title(skill_id: str) -> str:
    return skill_id.replace("-", " ").replace("_", " ").title()


def _allowed_tools(meta: dict[str, Any]) -> list[str]:
    """frontmatter 里 allowed-tools（连字符，Anthropic 规范）或 allowed_tools。"""
    value = meta.get("allowed-tools", meta.get("allowed_tools"))
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


def _description(content: str) -> str:
    _, body = _split_frontmatter(content)
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:160]
    return ""


def _frontmatter_meta(content: str) -> dict[str, Any]:
    """返回 SKILL.md 的 YAML frontmatter（无则空 dict）。"""
    frontmatter, _ = _split_frontmatter(content)
    return frontmatter


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """把 SKILL.md 拆成 (frontmatter dict, body)。无 frontmatter 返回 ({}, content)。"""
    if not content.startswith("---"):
        return {}, content
    lines = content.splitlines()
    end: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, content
    fm_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:])
    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}, content
    if not isinstance(parsed, dict):
        return {}, content
    return parsed, body


def _single_root_prefix(names: list[str]) -> str:
    """若所有条目共享同一顶层目录，则返回该目录前缀（含尾斜杠），否则返回空串。"""
    roots = {name.split("/", 1)[0] for name in names}
    if len(roots) == 1 and any("/" in name for name in names):
        return roots.pop() + "/"
    return ""


def _file_count(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file())


def _list_files(path: Path) -> list[str]:
    return sorted(str(item.relative_to(path)) for item in path.rglob("*") if item.is_file())
