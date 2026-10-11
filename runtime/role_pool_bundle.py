"""角色池的打包导出 / 导入（连同池内角色引用的 skill 与 MCP 定义）。

包是一个 zip::

    manifest.json              格式版本、池名与角色列表、发送方 default.yaml 指纹、导出时的告警
    roles/{Manager,Worker}/<id>.yaml
    skills/<id>/...            整个 skill 目录（含二进制）
    mcp_servers.json           {"servers": {仅被引用的条目，原样}}

导入先把整个包校验完再写任何东西；写入中途失败会逆序回滚，不留半个池。

包带不走的东西（可用性自检只能提示，不能替用户补齐）：角色从 default.yaml
继承来的字段、MCP 命令在接收方机器上是否存在、skill 内部调用的外部程序。
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

import yaml

from runtime import mcp_manager
from runtime.repo_paths import resolve_repo_relative

logger = logging.getLogger(__name__)

FORMAT = "aicu-role-pool"
VERSION = 1

MAX_BUNDLE_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_SKILL_BYTES = 20 * 1024 * 1024
MAX_ROLE_BYTES = 200_000
MAX_ENTRIES = 5000

_ROLE_TYPES = ("manager", "worker")
_ROLE_ID = re.compile(r"[a-z][a-z0-9_]*")
_SKILL_ID = re.compile(r"[a-z][a-z0-9_-]*")
_MCP_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".ts", ".rb", ".pl", ".ps1"}
_SKIP_FILES = {".DS_Store"}
_SKIP_DIRS = {"__pycache__"}

# 导入会读改写 mcp_servers.json，而该文件的其它写入方没有锁；这里至少让导入之间串行。
_APPLY_LOCK = threading.Lock()


class BundleError(ValueError):
    """包校验失败；``errors`` 列出全部问题（调用方据此一次性展示）。"""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class BundleTooLarge(BundleError):
    """包体超过上限（路由映射为 413）。"""


def _safe_entry_name(name: str) -> bool:
    if not name or name.startswith("/") or "\x00" in name or "\\" in name or "//" in name:
        return False
    return ".." not in PurePosixPath(name).parts


def _norm_text(text: str) -> str:
    return text.replace("\r\n", "\n")


def _str_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _sensitive_keys(spec: Any) -> list[str]:
    """带非空值的 env / headers 键名（只给键名，绝不带值）。"""
    if not isinstance(spec, dict):
        return []
    keys: list[str] = []
    for field, prefix in (("env", "env"), ("headers", "header")):
        table = spec.get(field)
        if isinstance(table, dict):
            keys.extend(f"{prefix}:{key}" for key, value in table.items() if value not in (None, ""))
    return keys


def _walk_skill_files(root: Path):
    """遍历 skill 目录下的普通文件（跳过符号链接与缓存），产出 (相对 posix 路径, 绝对路径)。"""
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not os.path.islink(os.path.join(current, d))
        )
        for filename in sorted(filenames):
            full = os.path.join(current, filename)
            if filename in _SKIP_FILES or os.path.islink(full):
                continue
            yield Path(full).relative_to(root).as_posix(), Path(full)


def _script_count(names) -> int:
    return sum(1 for name in names if PurePosixPath(name).suffix.lower() in _SCRIPT_SUFFIXES)


def _cli_problem(cli_name: str) -> str:
    """角色指定的 CLI 在本机找不到可执行文件就返回命令名（空串 = 没发现问题）；只查 PATH，不启动进程。"""
    from cli_adapters import get_adapter

    try:
        command = get_adapter(cli_name).command
    except ValueError:
        return cli_name
    return "" if shutil.which(command) else command


def _tree_hashes_dir(root: Path) -> dict[str, str]:
    return {rel: hashlib.sha256(path.read_bytes()).hexdigest() for rel, path in _walk_skill_files(root)}


def _tree_hashes_bundle(files: dict[str, tuple[bytes, int]]) -> dict[str, str]:
    return {rel: hashlib.sha256(blob).hexdigest() for rel, (blob, _mode) in files.items()}


class RolePoolBundle:
    """角色池包的导出 / 导入编排；本身不持有状态，所有读写经传入的 manager 完成。"""

    def __init__(self, pools, roles, skills, mcp_path: Optional[Path] = None):
        self._pools = pools
        self._roles = roles
        self._skills = skills
        self._mcp_path = Path(mcp_path) if mcp_path else None

    # ------------------------------------------------------------------ 导出

    def _collect(self, pool_id: str) -> dict:
        """收集池涉及的角色 / skill / MCP；引用缺失只记告警，不失败。"""
        pool = self._pools.get_pool(pool_id)  # KeyError → 路由 404
        types_by_id: dict[str, list[str]] = {}
        for item in self._roles.list_roles():
            if item.get("type") in _ROLE_TYPES:
                types_by_id.setdefault(item["id"], []).append(item["type"])

        warnings: list[dict] = []
        roles: list[dict] = []
        skills: dict[str, Path] = {}
        mcp_names: list[str] = []
        missing_skills: set[str] = set()
        for role_id in pool["roles"]:
            role_types = sorted(types_by_id.get(role_id, []))
            if not role_types:
                warnings.append({"kind": "missing_role", "ref": role_id, "role": ""})
            for role_type in role_types:
                raw = self._roles.read_role_raw(role_id, role_type)
                roles.append(raw)
                cfg = raw["effective_config"]
                defaults = self._roles.default_config(role_type)
                if cfg.get("skills_dir") and cfg.get("skills_dir") != defaults.get("skills_dir"):
                    warnings.append({"kind": "custom_skills_dir", "ref": str(cfg["skills_dir"]), "role": role_id})
                for name in _str_list(cfg.get("skills")):
                    if name in skills or name in missing_skills:
                        continue
                    found = self._skills.skill_dir(name) if _SKILL_ID.fullmatch(name) else None
                    if found is None:
                        missing_skills.add(name)
                        warnings.append({"kind": "missing_skill", "ref": name, "role": role_id})
                    else:
                        skills[name] = found
                for name in _str_list(cfg.get("mcp")):
                    if name not in mcp_names:
                        mcp_names.append(name)

        specs = mcp_manager.read_servers(mcp_names, self._mcp_path) if mcp_names else {}
        for name in mcp_names:
            if name not in specs:
                warnings.append({"kind": "missing_mcp", "ref": name, "role": ""})
        return {"pool": pool, "roles": roles, "skills": skills, "mcp": specs, "warnings": warnings}

    def preview_export(self, pool_id: str) -> dict:
        """导出前给界面看的清单；``sensitive_mcp`` 只有服务名与键名。"""
        collected = self._collect(pool_id)
        return {
            "pool": {"id": collected["pool"]["id"], "name": collected["pool"]["name"]},
            "roles": [{"id": r["id"], "type": r["type"]} for r in collected["roles"]],
            "skills": list(collected["skills"]),
            "personal_skills": [
                name for name in collected["skills"]
                if self._skills.skill_dir(name, primary_only=True) is None
            ],
            "mcp": list(collected["mcp"]),
            "sensitive_mcp": [
                {"name": name, "keys": keys}
                for name, spec in collected["mcp"].items()
                if (keys := _sensitive_keys(spec))
            ],
            "warnings": collected["warnings"],
        }

    def build_export(self, pool_id: str) -> tuple[str, bytes]:
        """返回 (池名, zip 字节)。"""
        collected = self._collect(pool_id)
        pool = collected["pool"]
        manifest = {
            "format": FORMAT,
            "version": VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "defaults_sha256": {t: self._roles.default_fingerprint(t) for t in _ROLE_TYPES},
            "pool": {"name": pool["name"], "roles": list(pool["roles"])},
            "roles": [{"id": r["id"], "type": r["type"]} for r in collected["roles"]],
            "skills": list(collected["skills"]),
            "mcp": list(collected["mcp"]),
            "warnings": collected["warnings"],
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for role in collected["roles"]:
                archive.writestr(f"roles/{role['type'].capitalize()}/{role['id']}.yaml", role["content"])
            for skill_id, root in collected["skills"].items():
                for rel, path in _walk_skill_files(root):
                    archive.write(path, f"skills/{skill_id}/{rel}")
            if collected["mcp"]:
                archive.writestr(
                    "mcp_servers.json",
                    json.dumps({"servers": collected["mcp"]}, ensure_ascii=False, indent=2),
                )
        data = buffer.getvalue()
        if len(data) > MAX_BUNDLE_BYTES:
            raise ValueError(f"导出包 {len(data)} 字节，超过导入上限 {MAX_BUNDLE_BYTES}，接收方将无法导入")
        return pool["name"], data

    # ------------------------------------------------------------------ 导入：解析与校验

    def _parse(self, data: bytes) -> dict:
        """校验整个包（任何写入之前）；有问题就一次性抛出全部错误。"""
        if len(data) > MAX_BUNDLE_BYTES:
            raise BundleTooLarge([f"包体超过上限 {MAX_BUNDLE_BYTES} 字节"])
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise BundleError(["不是有效的 zip 文件"]) from None

        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise BundleError([f"条目数 {len(infos)} 超过上限 {MAX_ENTRIES}"])
        if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
            raise BundleError([f"解压后总量超过上限 {MAX_UNCOMPRESSED_BYTES} 字节"])

        errors: list[str] = []
        files: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            if info.is_dir():
                continue
            if not _safe_entry_name(info.filename):
                errors.append(f"非法条目路径: {info.filename!r}")
            elif info.filename in files:
                errors.append(f"重复条目: {info.filename}")
            else:
                files[info.filename] = info
        if errors:
            raise BundleError(errors)

        budget = [MAX_UNCOMPRESSED_BYTES]

        def read(info: zipfile.ZipInfo, limit: int) -> bytes:
            # file_size 来自包头，可以被伪造；按实际读到的字节数再卡一次。
            with archive.open(info) as handle:
                blob = handle.read(min(limit, budget[0]) + 1)
            if len(blob) > limit or len(blob) > budget[0]:
                raise BundleError([f"条目超过大小上限: {info.filename}"])
            budget[0] -= len(blob)
            return blob

        manifest_info = files.get("manifest.json")
        if manifest_info is None:
            raise BundleError(["缺少 manifest.json"])
        try:
            manifest = json.loads(read(manifest_info, 1024 * 1024).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BundleError(["manifest.json 不是合法 JSON"]) from None
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise BundleError(["不是角色池包（manifest.format 不匹配）"])
        version = manifest.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version > VERSION:
            raise BundleError([f"包格式版本 {version!r} 高于本机支持的 {VERSION}，请升级后再导入"])

        roles: list[dict] = []
        skills: dict[str, dict[str, tuple[bytes, int]]] = {}
        skill_bytes: dict[str, int] = {}
        mcp_doc: Any = None
        for name, info in files.items():
            parts = name.split("/")
            if name == "manifest.json":
                continue
            if name == "mcp_servers.json":
                try:
                    mcp_doc = json.loads(read(info, MAX_ROLE_BYTES * 5).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    errors.append("mcp_servers.json 不是合法 JSON")
            elif (
                len(parts) == 3 and parts[0] == "roles"
                and parts[1] in ("Manager", "Worker") and parts[2].endswith(".yaml")
            ):
                self._parse_role(parts[1].lower(), parts[2][:-5], read(info, MAX_ROLE_BYTES), roles, errors)
            elif len(parts) >= 3 and parts[0] == "skills" and _SKILL_ID.fullmatch(parts[1]):
                blob = read(info, MAX_SKILL_BYTES)
                skill_bytes[parts[1]] = skill_bytes.get(parts[1], 0) + len(blob)
                if skill_bytes[parts[1]] > MAX_SKILL_BYTES:
                    errors.append(f"skill '{parts[1]}' 超过大小上限 {MAX_SKILL_BYTES} 字节")
                    continue
                skills.setdefault(parts[1], {})["/".join(parts[2:])] = (blob, (info.external_attr >> 16) & 0o777)
            else:
                errors.append(f"包内出现无法识别的条目: {name}")
        for skill_id, skill_files in skills.items():
            if "SKILL.md" not in skill_files:
                errors.append(f"skill '{skill_id}' 缺少 SKILL.md")

        mcp = self._parse_mcp(mcp_doc, errors)
        pool = self._parse_pool(manifest, roles, errors)
        if errors:
            raise BundleError(errors)

        sender_defaults = manifest.get("defaults_sha256")
        return {
            "roles": sorted(roles, key=lambda r: (r["type"], r["id"])),
            "skills": skills,
            "mcp": mcp,
            "pool": pool,
            "defaults_sha256": sender_defaults if isinstance(sender_defaults, dict) else {},
            "source_warnings": self._clean_warnings(manifest.get("warnings")),
        }

    def _parse_role(self, role_type: str, role_id: str, blob: bytes, roles: list[dict], errors: list[str]) -> None:
        label = f"角色 {role_type}/{role_id}"
        if not _ROLE_ID.fullmatch(role_id) or role_id == "default":
            errors.append(f"{label}: 非法的角色 id")
            return
        try:
            content = blob.decode("utf-8")
            config = yaml.safe_load(content)
            # 写入用原文（保留注释），但字段必须先过白名单，否则原文能写进任意字段。
            self._roles.validate_config({} if config is None else config, role_type)
        except (UnicodeDecodeError, yaml.YAMLError, ValueError) as error:
            errors.append(f"{label}: {error}")
            return
        roles.append({"id": role_id, "type": role_type, "content": content, "config": config or {}})

    @staticmethod
    def _parse_mcp(doc: Any, errors: list[str]) -> dict[str, dict]:
        if doc is None:
            return {}
        servers = doc.get("servers") if isinstance(doc, dict) else None
        if not isinstance(servers, dict):
            errors.append("mcp_servers.json 缺少 servers 对象")
            return {}
        result: dict[str, dict] = {}
        for name, spec in servers.items():
            if not isinstance(name, str) or not _MCP_NAME.fullmatch(name):
                errors.append(f"MCP '{name}': 非法名字")
                continue
            try:
                result[name] = mcp_manager.validate_server(name, spec)
            except ValueError as error:
                errors.append(f"MCP '{name}': {error}")
        return result

    def _parse_pool(self, manifest: dict, roles: list[dict], errors: list[str]) -> dict:
        raw = manifest.get("pool")
        if not isinstance(raw, dict):
            errors.append("manifest 缺少 pool")
            return {"name": "", "roles": []}
        try:
            name = self._pools.validate_name(raw.get("name"))
        except ValueError as error:
            errors.append(f"池名: {error}")
            name = ""
        role_ids = raw.get("roles")
        if not isinstance(role_ids, list) or not role_ids:
            errors.append("池的角色列表不能为空")
            return {"name": name, "roles": []}
        known = {r["id"] for r in roles} | {item["id"] for item in self._roles.list_roles()}
        clean: list[str] = []
        for role_id in role_ids:
            if not isinstance(role_id, str) or not _ROLE_ID.fullmatch(role_id):
                errors.append(f"池里有非法的角色 id: {role_id!r}")
            elif role_id not in known:
                errors.append(f"池引用的角色 '{role_id}' 既不在包内也不在本机")
            elif role_id not in clean:
                clean.append(role_id)
        return {"name": name, "roles": clean}

    @staticmethod
    def _clean_warnings(raw: Any) -> list[dict]:
        """发送方写的告警是不可信输入：只留三个字符串字段并截断。"""
        if not isinstance(raw, list):
            return []
        cleaned = []
        for item in raw[:100]:
            if isinstance(item, dict) and isinstance(item.get("kind"), str):
                cleaned.append({
                    "kind": item["kind"][:40],
                    "ref": str(item.get("ref", ""))[:200],
                    "role": str(item.get("role", ""))[:100],
                })
        return cleaned

    # ------------------------------------------------------------------ 导入：计划

    def _local_mcp(self, names) -> dict[str, dict]:
        names = list(names)
        if not names:
            return {}
        try:
            return mcp_manager.read_servers(names, self._mcp_path)
        except mcp_manager.McpConfigError:
            return {}

    def _plan(self, parsed: dict) -> dict:
        """逐项对比本机：new（本机没有）/ same（内容一致，不算冲突）/ conflict（同名但内容不同）。"""
        local_pools = self._pools.list_pools()
        roles = []
        for role in parsed["roles"]:
            try:
                local = self._roles.read_role_raw(role["id"], role["type"])["content"]
            except KeyError:
                status = "new"
            else:
                status = "same" if _norm_text(local) == _norm_text(role["content"]) else "conflict"
            item = {"id": role["id"], "type": role["type"], "status": status, "key": f"role:{role['type']}/{role['id']}"}
            if status == "conflict":
                item["used_by"] = [p["name"] for p in local_pools if role["id"] in p["roles"]]
            roles.append(item)

        skills = []
        for skill_id, files in parsed["skills"].items():
            local_dir = self._skills.skill_dir(skill_id, primary_only=True)
            if local_dir is None:
                status = "new"
            else:
                same = _tree_hashes_dir(local_dir) == _tree_hashes_bundle(files)
                status = "same" if same else "conflict"
            item = {
                "id": skill_id, "status": status, "key": f"skill:{skill_id}",
                "file_count": len(files), "script_count": _script_count(files),
            }
            if status == "conflict":
                item["used_by"] = [r["id"] for r in self._roles.roles_referencing_skill(skill_id)]
            skills.append(item)

        local_mcp = self._local_mcp(parsed["mcp"])
        mcp = []
        for name, entry in parsed["mcp"].items():
            if name not in local_mcp:
                status = "new"
            else:
                try:
                    same = _canon(mcp_manager.validate_server(name, local_mcp[name])) == _canon(entry)
                except ValueError:
                    same = False
                status = "same" if same else "conflict"
            used_by = []
            if status == "conflict":
                used_by = [
                    r["id"] for r in mcp_manager.roles_referencing(
                        name, self._roles.iter_role_configs(include_defaults=True)
                    )
                ]
            mcp.append({
                "name": name,
                "status": status,
                "key": f"mcp:{name}",
                "used_by": used_by,
                "transport": mcp_manager.transport_of(entry),
                "command": str(entry.get("command") or ""),
                "args": [str(a) for a in entry.get("args") or []],
                "url": str(entry.get("url") or ""),
                "sensitive_keys": _sensitive_keys(entry),
            })

        pool = {
            "name": parsed["pool"]["name"], "roles": parsed["pool"]["roles"],
            "status": "new", "local_id": None, "key": "pool",
        }
        for local_pool in local_pools:
            if local_pool["name"] == pool["name"]:
                pool["local_id"] = local_pool["id"]
                pool["status"] = "same" if local_pool["roles"] == pool["roles"] else "conflict"
                if pool["status"] == "conflict":
                    pool["local_roles"] = list(local_pool["roles"])
                break

        warnings = []
        for role_type in sorted({r["type"] for r in parsed["roles"]}):
            sender = parsed["defaults_sha256"].get(role_type)
            local_fp = self._roles.default_fingerprint(role_type)
            if isinstance(sender, str) and sender and local_fp and sender != local_fp:
                warnings.append({"kind": "defaults_differ", "ref": role_type, "role": ""})
        warnings.extend(parsed["source_warnings"])

        statuses = [i["status"] for i in roles + skills + mcp] + [pool["status"]]
        return {
            "pool": pool,
            "roles": roles,
            "skills": skills,
            "mcp": mcp,
            "has_conflict": "conflict" in statuses,
            "warnings": warnings,
        }

    def _readiness(
        self,
        entries: list[dict],
        has_primary_skill: Callable[[str], bool],
        mcp_specs: dict[str, dict],
    ) -> list[dict]:
        """只读、不联网的可用性自检：skill 在不在、MCP 登记了没有、命令在本机能不能找到。"""
        report = []
        for entry in entries:
            cfg = entry["effective_config"]
            defaults = self._roles.default_config(entry["type"])
            skills_dir = cfg.get("skills_dir")
            issues: list[dict] = []
            for name in _str_list(cfg.get("skills")):
                if not _SKILL_ID.fullmatch(name):
                    present = False
                elif not skills_dir or skills_dir == defaults.get("skills_dir"):
                    present = has_primary_skill(name)
                else:
                    present = (resolve_repo_relative(str(skills_dir)) / name / "SKILL.md").is_file()
                if not present:
                    issues.append({"kind": "skill_missing", "ref": name, "detail": ""})
            for name in _str_list(cfg.get("mcp")):
                if name not in mcp_specs:
                    issues.append({"kind": "mcp_unregistered", "ref": name, "detail": ""})
                    continue
                for problem in mcp_manager.launch_problems(mcp_specs[name]):
                    issues.append({"kind": problem["kind"], "ref": name, "detail": problem["detail"]})
            cli = str(cfg.get("cli_name") or "").strip()
            if cli:
                missing = _cli_problem(cli)
                if missing:
                    issues.append({"kind": "cli_missing", "ref": cli, "detail": missing})
            report.append({"id": entry["id"], "type": entry["type"], "ready": not issues, "issues": issues})
        return report

    @staticmethod
    def _action(item: dict, on_conflict: str, decisions: dict[str, str]) -> str:
        """new → created，same → unchanged；冲突项先看逐项决定，没有再用全局策略。"""
        status = item["status"]
        if status == "new":
            return "created"
        if status == "same":
            return "unchanged"
        return "overwritten" if decisions.get(item["key"], on_conflict) == "overwrite" else "skipped"

    @staticmethod
    def _clean_decisions(decisions: Optional[dict]) -> dict[str, str]:
        if not decisions:
            return {}
        if not isinstance(decisions, dict):
            raise ValueError("decisions must be an object")
        for key, value in decisions.items():
            if value not in ("skip", "overwrite"):
                raise ValueError(f"decision for {key!r} must be 'skip' or 'overwrite'")
        return dict(decisions)

    def _mark_policies(self, plan: dict, on_conflict: str, decisions: dict[str, str]) -> None:
        """给冲突项标上最终生效的 skip / overwrite，界面据此显示每行的开关。"""
        for group in ("roles", "skills", "mcp"):
            for item in plan[group]:
                if item["status"] == "conflict":
                    item["policy"] = decisions.get(item["key"], on_conflict)
        if plan["pool"]["status"] == "conflict":
            plan["pool"]["policy"] = decisions.get("pool", on_conflict)

    def _predict_readiness(self, parsed: dict, plan: dict, on_conflict: str, decisions: dict[str, str]) -> list[dict]:
        """按「导入后」的状态预测可用性：冲突项取决于 skip / overwrite 的选择。"""
        entries = []
        for role, item in zip(parsed["roles"], plan["roles"]):
            if self._action(item, on_conflict, decisions) in ("created", "overwritten"):
                effective = {**self._roles.default_config(role["type"]), **role["config"]}
                entries.append({"id": role["id"], "type": role["type"], "effective_config": effective})
            else:
                entries.append(self._roles.read_role_raw(role["id"], role["type"]))

        needed = {name for e in entries for name in _str_list(e["effective_config"].get("mcp"))}
        specs = self._local_mcp(needed)
        for item in plan["mcp"]:
            if item["name"] in needed and self._action(item, on_conflict, decisions) in ("created", "overwritten", "unchanged"):
                specs[item["name"]] = parsed["mcp"][item["name"]]

        bundled = set(parsed["skills"])
        return self._readiness(
            entries,
            lambda name: name in bundled or self._skills.skill_dir(name, primary_only=True) is not None,
            specs,
        )

    # ------------------------------------------------------------------ 导入：入口与写入

    def import_bundle(
        self,
        data: bytes,
        on_conflict: str = "skip",
        dry_run: bool = False,
        decisions: Optional[dict[str, str]] = None,
    ) -> dict:
        """导入角色池包。

        ``dry_run=True`` 只返回计划（含各项状态、将写入的 MCP 命令、可用性预测），不写任何东西。
        ``on_conflict`` 只影响「同名但内容不同」的项：skip 保留本机版本，overwrite 用包内版本。
        ``decisions`` 按计划里的 ``key`` 给单个冲突项单独指定 skip / overwrite，优先于 ``on_conflict``；
        找不到对应项的 key 忽略（界面上的决定可能比包新）。
        """
        if on_conflict not in ("skip", "overwrite"):
            raise ValueError("on_conflict must be 'skip' or 'overwrite'")
        chosen = self._clean_decisions(decisions)
        parsed = self._parse(data)
        if dry_run:
            plan = self._plan(parsed)
            self._mark_policies(plan, on_conflict, chosen)
            plan["readiness"] = self._predict_readiness(parsed, plan, on_conflict, chosen)
            plan.update(dry_run=True, on_conflict=on_conflict)
            return plan
        with _APPLY_LOCK:
            return self._apply(parsed, self._plan(parsed), on_conflict, chosen)

    def _apply(self, parsed: dict, plan: dict, on_conflict: str, decisions: dict[str, str]) -> dict:
        undo: list[Callable[[], None]] = []
        cleanup: list[Callable[[], None]] = []
        try:
            for item in plan["mcp"]:
                action = self._action(item, on_conflict, decisions)
                item["action"] = action
                if action in ("created", "overwritten"):
                    self._write_mcp(item["name"], parsed["mcp"][item["name"]], undo)
            for item in plan["skills"]:
                action = self._action(item, on_conflict, decisions)
                item["action"] = action
                if action in ("created", "overwritten"):
                    self._write_skill(item["id"], parsed["skills"][item["id"]], action == "overwritten", undo, cleanup)
            for role, item in zip(parsed["roles"], plan["roles"]):
                action = self._action(item, on_conflict, decisions)
                item["action"] = action
                if action in ("created", "overwritten"):
                    self._write_role(role, action == "overwritten", undo)
            self._write_pool(plan["pool"], self._action(plan["pool"], on_conflict, decisions), undo)
        except Exception:
            logger.exception("Role pool import failed, rolling back %d step(s)", len(undo))
            for step in reversed(undo):
                try:
                    step()
                except Exception:
                    logger.exception("Rollback step failed")
            raise
        finally:
            for step in cleanup:
                try:
                    step()
                except Exception:
                    logger.exception("Import cleanup step failed")

        self._mark_policies(plan, on_conflict, decisions)
        entries = [self._roles.read_role_raw(r["id"], r["type"]) for r in parsed["roles"]]
        needed = {name for e in entries for name in _str_list(e["effective_config"].get("mcp"))}
        readiness = self._readiness(
            entries,
            lambda name: self._skills.skill_dir(name, primary_only=True) is not None,
            self._local_mcp(needed),
        )
        actions = [i["action"] for i in plan["roles"] + plan["skills"] + plan["mcp"]] + [plan["pool"]["action"]]
        return {
            "dry_run": False,
            "on_conflict": on_conflict,
            "pool": plan["pool"],
            "roles": plan["roles"],
            "skills": plan["skills"],
            "mcp": plan["mcp"],
            "counts": {k: actions.count(k) for k in ("created", "overwritten", "skipped", "unchanged")},
            "readiness": readiness,
            "warnings": plan["warnings"],
        }

    def _write_mcp(self, name: str, entry: dict, undo: list) -> None:
        previous = self._local_mcp([name]).get(name)
        mcp_manager.save_server(name, entry, self._mcp_path)
        undo.append(lambda: mcp_manager.restore_server(name, previous, self._mcp_path))

    def _write_role(self, role: dict, overwrite: bool, undo: list) -> None:
        role_id, role_type = role["id"], role["type"]
        if overwrite:
            previous = self._roles.read_role_raw(role_id, role_type)["content"]
            self._roles.update_role(role_id, content=role["content"], role_type=role_type)
            undo.append(lambda: self._roles.update_role(role_id, content=previous, role_type=role_type))
        else:
            self._roles.add_role(role_id, content=role["content"], role_type=role_type)
            undo.append(lambda: self._roles.delete_role(role_id, role_type))

    def _write_pool(self, pool: dict, action: str, undo: list) -> None:
        pool["action"] = action
        if action == "created":
            created = self._pools.create_pool(pool["name"], pool["roles"])
            undo.append(lambda: self._pools.delete_pool(created["id"]))
        elif action == "overwritten":
            previous = self._pools.get_pool(pool["local_id"])["roles"]
            self._pools.update_pool(pool["local_id"], roles=pool["roles"])
            undo.append(lambda: self._pools.update_pool(pool["local_id"], roles=previous))

    def _write_skill(
        self,
        skill_id: str,
        files: dict[str, tuple[bytes, int]],
        overwrite: bool,
        undo: list,
        cleanup: list,
    ) -> None:
        """先完整写进临时目录再换位；覆盖时旧目录改名留到整体成功后才删。"""
        root = self._skills.skills_dir
        root.mkdir(parents=True, exist_ok=True)
        target = root / skill_id
        staging = Path(tempfile.mkdtemp(prefix=f".aicu-import-{skill_id}-", dir=root.parent))
        cleanup.append(lambda: shutil.rmtree(staging, ignore_errors=True))
        base = staging.resolve()
        for rel, (blob, mode) in files.items():
            dest = (base / rel).resolve()
            if base not in dest.parents:
                raise ValueError(f"Invalid entry path in archive: {rel}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)
            if mode:
                os.chmod(dest, (mode & 0o777) | 0o600)

        if overwrite and target.exists():
            backup = root.parent / f".aicu-backup-{skill_id}-{uuid.uuid4().hex[:8]}"
            os.replace(target, backup)
            cleanup.append(lambda: shutil.rmtree(backup, ignore_errors=True))

            def restore() -> None:
                shutil.rmtree(target, ignore_errors=True)
                os.replace(backup, target)

            undo.append(restore)
            os.replace(staging, target)
        else:
            os.replace(staging, target)
            undo.append(lambda: shutil.rmtree(target, ignore_errors=True))
