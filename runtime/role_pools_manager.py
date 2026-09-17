"""
runtime/role_pools_manager.py — 候选角色池管理

一个「候选角色池」是若干个角色 id 的命名集合，供会话绑定：绑定后该会话的
Manager 在组装提示词时只看到池内角色的摘要（见 roles_manager.build_available_roles_text）。

与 RolesManager / SkillsManager 同构：一个实体一个管理器、一份独立 JSON 文件。
池只保存 **角色 id 快照**，不复制角色定义；角色本身仍由 roles/ 下的 yaml 提供。

存储结构（runtime_sessions/role_pools.json）：
  {
    "version": 1,
    "pools": [
      {"id": "a1b2c3d4", "name": "博客开发", "roles": ["requirements", "code", "test"]}
    ]
  }

注意：池只是「提示词候选范围」，不是校验边界 —— PlanValidator 仍以 roles/ 全量角色
为允许集合（见 runtime/plan_validator.py）。因此池内角色被删除时，Manager 只是看不到它，
不会导致校验失败。
"""

import json
import logging
import os
import threading
import uuid
from typing import Any, Optional

from runtime.roles_manager import RolesManager

logger = logging.getLogger(__name__)

# 池名最大长度
_NAME_MAX_LEN = 60

# 存储文件格式版本（便于未来迁移；读到更高版本按当前格式尽力解析）
_STORE_VERSION = 1


class RolePoolsManager:
    """候选角色池管理器。

    职责:
        - list_pools:  返回全部池（按 name 排序）
        - get_pool:    按 id 取单个池
        - create_pool / update_pool / delete_pool: 增删改
        - 保存时校验 roles 中的每个 id 确实是 roles/ 下已存在的角色

    Raises:
        ValueError: 名称为空/超长、roles 非列表或含未知角色 id、池名重复。
        KeyError:   池 id 不存在。
    """

    def __init__(self, store_path: str, roles_manager: RolesManager):
        """初始化 RolePoolsManager。

        Args:
            store_path: 池存储文件路径（如 runtime_sessions/role_pools.json）。
            roles_manager: 用于校验 role id 是否存在的 RolesManager 实例。
        """
        self._store_path = store_path
        self._roles_manager = roles_manager
        self._lock = threading.RLock()
        self._pools: list[dict] = self._load()

    # ---------------------------------------------------------------- 查询

    def list_pools(self) -> list[dict]:
        """返回全部池，按 name 排序。"""
        with self._lock:
            return [
                self._copy(pool)
                for pool in sorted(self._pools, key=lambda p: p["name"])
            ]

    def get_pool(self, pool_id: str) -> dict:
        """按 id 取单个池。

        Raises:
            KeyError: 池不存在。
        """
        with self._lock:
            return self._copy(self._find(pool_id))

    # ---------------------------------------------------------------- 变更

    def create_pool(self, name: str, roles: list[str]) -> dict:
        """新建池。

        Args:
            name:  池名（strip 后非空、≤ 60 字符、不能与已有池重名）。
            roles: 角色 id 列表（非空、去重保序、每个 id 必须已存在）。

        Raises:
            ValueError: 校验失败。
        """
        clean_name = self._validate_name(name)
        clean_roles = self._normalize_roles(roles)
        with self._lock:
            if any(pool["name"] == clean_name for pool in self._pools):
                raise ValueError(f"Role pool '{clean_name}' already exists")
            pool = {
                "id": self._new_pool_id(),
                "name": clean_name,
                "roles": clean_roles,
            }
            self._pools.append(pool)
            self._save()
            logger.info("Created role pool: %s (%d roles)", clean_name, len(clean_roles))
            return self._copy(pool)

    def update_pool(
        self,
        pool_id: str,
        name: Optional[str] = None,
        roles: Optional[list[str]] = None,
    ) -> dict:
        """更新池。name / roles 为 None 表示保持原值；id 不可修改。

        Raises:
            KeyError:   池不存在。
            ValueError: 校验失败或新名称与其它池重名。
        """
        clean_name = self._validate_name(name) if name is not None else None
        clean_roles = self._normalize_roles(roles) if roles is not None else None
        with self._lock:
            pool = self._find(pool_id)
            if clean_name is not None and any(
                other["name"] == clean_name and other["id"] != pool_id
                for other in self._pools
            ):
                raise ValueError(f"Role pool '{clean_name}' already exists")
            if clean_name is not None:
                pool["name"] = clean_name
            if clean_roles is not None:
                pool["roles"] = clean_roles
            self._save()
            logger.info("Updated role pool: %s", pool_id)
            return self._copy(pool)

    def delete_pool(self, pool_id: str) -> None:
        """删除池。

        会话绑定的是角色 id 快照而非池 id，因此删除池不影响已绑定的会话。

        Raises:
            KeyError: 池不存在。
        """
        with self._lock:
            pool = self._find(pool_id)
            self._pools.remove(pool)
            self._save()
            logger.info("Deleted role pool: %s (%s)", pool_id, pool["name"])

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _copy(pool: dict) -> dict:
        """深拷贝一条池记录：内部 roles 列表不能泄漏给调用方，
        否则外部 append 会直接改到内存状态并在下次 _save 时落盘。"""
        return {"id": pool["id"], "name": pool["name"], "roles": list(pool["roles"])}

    def _find(self, pool_id: str) -> dict:
        """按 id 取池的可变引用（调用方需自行持有 self._lock）。

        Raises:
            KeyError: 池不存在。
        """
        for pool in self._pools:
            if pool["id"] == pool_id:
                return pool
        raise KeyError(f"Role pool '{pool_id}' not found")

    def _new_pool_id(self) -> str:
        """生成不与现有池冲突的短 id。"""
        existing = {pool["id"] for pool in self._pools}
        while True:
            candidate = uuid.uuid4().hex[:8]
            if candidate not in existing:
                return candidate

    @staticmethod
    def _validate_name(name: Any) -> str:
        """校验并归一化池名。"""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Role pool name cannot be empty")
        clean = name.strip()
        if len(clean) > _NAME_MAX_LEN:
            raise ValueError(
                f"Role pool name too long ({len(clean)} > {_NAME_MAX_LEN})"
            )
        return clean

    def _normalize_roles(self, roles: Any) -> list[str]:
        """校验并归一化角色 id 列表：非空、去重保序、每个 id 必须已存在。

        `default` 不在 RolesManager.list_roles() 的返回里（它是继承基类而非可选角色），
        因此无需特判即被拒。
        """
        if not isinstance(roles, list):
            raise ValueError("Role pool roles must be a list")
        known = {role["id"] for role in self._roles_manager.list_roles()}
        normalized: list[str] = []
        unknown: list[str] = []
        for role_id in roles:
            if not isinstance(role_id, str):
                raise ValueError("Role pool roles must be a list of strings")
            if role_id not in known:
                unknown.append(role_id)
                continue
            if role_id not in normalized:
                normalized.append(role_id)
        if unknown:
            raise ValueError(f"Unknown role ids: {sorted(set(unknown))}")
        if not normalized:
            raise ValueError("Role pool must contain at least one role")
        return normalized

    def _load(self) -> list[dict]:
        """读取存储文件；文件缺失/损坏/结构不符时回退为空列表。"""
        if not os.path.exists(self._store_path):
            return []
        try:
            with open(self._store_path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(
                "Failed to read role pools from %s: %s", self._store_path, error
            )
            return []
        if not isinstance(data, dict):
            return []
        raw_pools = data.get("pools")
        if not isinstance(raw_pools, list):
            return []
        pools: list[dict] = []
        seen_ids: set[str] = set()
        for raw in raw_pools:
            pool = self._parse_pool(raw, seen_ids)
            if pool is not None:
                pools.append(pool)
                seen_ids.add(pool["id"])
        return pools

    @staticmethod
    def _parse_pool(raw: Any, seen_ids: set[str]) -> Optional[dict]:
        """把存储文件里的一条记录解析成池；结构不合法则丢弃该条（不影响其余池）。"""
        if not isinstance(raw, dict):
            return None
        pool_id = raw.get("id")
        name = raw.get("name")
        roles = raw.get("roles")
        if not isinstance(pool_id, str) or not pool_id or pool_id in seen_ids:
            return None
        if not isinstance(name, str) or not name.strip():
            return None
        if not isinstance(roles, list):
            return None
        clean_roles = [r for r in roles if isinstance(r, str)]
        if not clean_roles:
            return None
        return {"id": pool_id, "name": name.strip(), "roles": clean_roles}

    def _save(self) -> None:
        """原子写入存储文件（tmp + fsync + os.replace）。"""
        payload = {"version": _STORE_VERSION, "pools": self._pools}
        os.makedirs(os.path.dirname(self._store_path) or ".", exist_ok=True)
        tmp_path = self._store_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, self._store_path)
