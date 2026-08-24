"""runtime/delivery/registry.py — Artifact Registry（产物注册表，非权威 Snapshot）。

定位（V3-2 支柱 B）：
- Registry 只是 COMPLETED 时刻的快照 / 索引，**不是 Source of Truth**。
  Shared Workspace 文件系统才是最终事实（Assembly 以 filesystem 重新扫描为最终判定）。
- **Registry 写失败 ≠ Worker 失败**：Gate PASS 后 Registry FAIL 不影响 COMPLETED，
  delivery_state 不被污染。
- 最小接口（评审约束）：ArtifactRegistryBackend Protocol 只声明 ``write/read/resolve``，
  不加 list/delete/update/transaction/watch。
- Symbolic Reference 边界：``@{step_ref}`` / ``@role:name`` 只负责解析身份（resolve to
  path），不保证内容正确——LLM 拿到路径后仍需 Read/Glob 确认；解析失败降级 = 现状直传。
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Optional, Protocol

from ..workspace_resolver import _partition_of
from .contracts import ArtifactContract, resolve_required_types
from .gate import _iter_files, extract_verdict

logger = logging.getLogger(__name__)

# 注册表目录名（位于 {shared_root}/.artifacts/，与 Gate FRAMEWORK_DIR_NAMES 对齐）
REGISTRY_DIRNAME = ".artifacts"


@dataclass
class ArtifactEntry:
    """Registry 条目（COMPLETED 时刻的产物快照，Metadata 非内容）。

    Args:
        step_ref: 节点步骤引用（Registry 主键，repair/resume 以同名覆盖）。
        node_id: 产出节点 ID。
        role: 产出角色。
        artifact_type: 角色产物契约的 artifact_type。
        artifact_path: 产物绝对路径（Gate 通过后的归一化路径）。
        partition: 归属共享分区（{shared_root}/{output_path}）。
        files: 产物下文件清单 [{relpath, size}]（有界扫描，跳过框架目录）。
        key_files: 定义产物的主文件相对路径（按契约 required_types / VERDICT 块筛选）。
    """

    step_ref: str
    node_id: str
    role: str
    artifact_type: str
    artifact_path: str
    partition: str
    files: list = field(default_factory=list)
    key_files: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ArtifactEntry":
        return cls(
            step_ref=data.get("step_ref", "") or "",
            node_id=data.get("node_id", "") or "",
            role=data.get("role", "") or "",
            artifact_type=data.get("artifact_type", "") or "",
            artifact_path=data.get("artifact_path", "") or "",
            partition=data.get("partition", "") or "",
            files=list(data.get("files") or []),
            key_files=list(data.get("key_files") or []),
        )


def scan_artifact_files(path: str) -> list[dict]:
    """扫描产物文件（有界 os.walk，跳过框架目录）→ [{relpath, size}]。

    path 为文件时返回该文件自身；为空/不存在返回 []。
    """
    if not path:
        return []
    result: list[dict] = []
    for f in _iter_files(path):
        try:
            size = os.path.getsize(f)
        except OSError:
            size = 0
        if os.path.isdir(path):
            relpath = os.path.relpath(f, path)
        else:
            relpath = os.path.basename(f)
        result.append({"relpath": relpath, "size": size})
    return result


def key_files_for(
    artifact_path: str, files: list[dict], contract: ArtifactContract
) -> list[str]:
    """定义产物的主文件：verdict_block 契约取含 VERDICT 块的文件；
    否则扩展名命中 required_types（无类型规则时全部文件）。
    """
    if not files:
        return []
    if "verdict_block" in set(contract.validators or []):
        verdicts: list[str] = []
        for f in files:
            if os.path.isdir(artifact_path):
                abs_path = os.path.join(artifact_path, f["relpath"])
            else:
                abs_path = artifact_path
            if extract_verdict(abs_path) is not None:
                verdicts.append(f["relpath"])
        if verdicts:
            return verdicts
    required = resolve_required_types(contract)
    if required:
        return [
            f["relpath"] for f in files
            if os.path.splitext(f["relpath"])[1].lower() in required
        ]
    return [f["relpath"] for f in files]


def build_artifact_entry(
    node,
    artifact_path: str,
    contract: ArtifactContract,
    shared_root: str,
    output_path: str = "",
) -> ArtifactEntry:
    """构建 Registry 条目（纯函数，无 I/O 副作用）。

    Args:
        node: 产出节点（读取 step_ref / id / role）。
        artifact_path: Gate 通过后的产物绝对路径。
        contract: 角色产物契约。
        shared_root: 会话共享根（{session}/shared/）。
        output_path: 节点分区子路径（config._output_path）。
    """
    artifact_path = os.path.realpath(artifact_path) if artifact_path else ""
    partition = _partition_of(shared_root, output_path)
    files = scan_artifact_files(artifact_path)
    return ArtifactEntry(
        step_ref=getattr(node, "step_ref", "") or "",
        node_id=getattr(node, "id", "") or "",
        role=getattr(node, "role", "") or "",
        artifact_type=contract.artifact_type,
        artifact_path=artifact_path,
        partition=partition,
        files=files,
        key_files=key_files_for(artifact_path, files, contract),
    )


class ArtifactRegistryBackend(Protocol):
    """Registry 后端最小接口（评审约束：只 write/read/resolve，不加 list/delete/...）。"""

    def write(self, entry: ArtifactEntry) -> None:
        ...

    def read(self, step_ref: str) -> Optional[ArtifactEntry]:
        ...

    def resolve(self, reference: str) -> Optional[str]:
        ...


class FilesystemRegistry:
    """确定性 os.walk 快照 → 原子写 ``{shared_root}/.artifacts/{step_ref}.json``。

    原子写：临时文件 + os.replace，防并发/半写。写失败由调用方（Worker）捕获，
    仅 log，不改变 Worker 的 COMPLETED 判定。
    """

    def __init__(self, shared_root: str):
        self.shared_root = os.path.realpath(shared_root) if shared_root else ""
        self._dir = os.path.join(self.shared_root, REGISTRY_DIRNAME)

    # ---- Protocol: write/read/resolve ----

    def write(self, entry: ArtifactEntry) -> None:
        if not entry.step_ref:
            logger.warning("Registry write skipped: empty step_ref")
            return
        os.makedirs(self._dir, exist_ok=True)
        target = self._entry_path(entry.step_ref)
        tmp = os.path.join(
            self._dir, f".{entry.step_ref}.{os.getpid()}.tmp"
        )
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)

    def read(self, step_ref: str) -> Optional[ArtifactEntry]:
        if not step_ref:
            return None
        target = self._entry_path(step_ref)
        if not os.path.isfile(target):
            return None
        try:
            with open(target, "r", encoding="utf-8") as f:
                return ArtifactEntry.from_dict(json.load(f))
        except (OSError, ValueError):
            return None

    def resolve(self, reference: str) -> Optional[str]:
        """解析符号引用 → 确定性绝对路径。

        支持：
          - ``@{step_ref}``            → 该 step_ref 的 artifact_path
          - ``@{step_ref}/sub/path``   → artifact_path 下子路径
          - ``@role:name``             → 该角色最新产出（step_ref 字典序最大）的 artifact_path

        未知引用 / 未找到 → None（调用方降级为现状直传，不引入新错误面）。
        """
        ref = (reference or "").strip()
        if not ref.startswith("@"):
            return None
        body = ref[1:]
        if not body:
            return None
        if body.startswith("role:"):
            entry = self._find_by_role(body[len("role:"):])
            return entry.artifact_path if entry else None
        parts = body.split("/", 1)
        entry = self.read(parts[0])
        if entry is None:
            return None
        if len(parts) == 2 and parts[1]:
            return os.path.join(entry.artifact_path, parts[1])
        return entry.artifact_path

    # ---- 内部辅助（非 Protocol） ----

    def _entry_path(self, step_ref: str) -> str:
        return os.path.join(self._dir, f"{step_ref}.json")

    def _find_by_role(self, role: str) -> Optional[ArtifactEntry]:
        """按角色找最新产出（step_ref 字典序最大，确定性）。"""
        if not role or not os.path.isdir(self._dir):
            return None
        best: Optional[ArtifactEntry] = None
        for name in os.listdir(self._dir):
            if not name.endswith(".json"):
                continue
            entry = self.read(name[: -len(".json")])
            if entry is None or entry.role != role:
                continue
            if best is None or entry.step_ref > best.step_ref:
                best = entry
        return best

    # ---- 非 Protocol 查询扩展（Session Outputs 用；可替换后端时可有可无） ----

    def entries(self) -> list:
        """枚举全部 Registry 条目（非 Protocol 扩展；后端可替换时可有可无）。
        .artifacts/{step_ref}.json 逐个 read，坏文件跳过（与 read 容错一致）。"""
        if not os.path.isdir(self._dir):
            return []
        out = []
        for name in sorted(os.listdir(self._dir)):
            if not name.endswith(".json"):
                continue
            entry = self.read(name[: -len(".json")])
            if entry is not None:
                out.append(entry)
        return out

    def latest_per_partition(self) -> dict:
        """每分区代表性条目（realpath(partition) → entry；非 Protocol 扩展）。

        "latest" 指与 resolve/@role 相同的确定性选择规则：step_ref 字典序最大
        （与 _find_by_role 一致，该语义已由既有 resolve 契约明确规定），
        由 Registry 单一权威决定，UI/Runtime 不得另定规则。
        **不表示墙钟时间上的最近**——step_ref 不是时间序列。
        空分区（共享根本身）不产出。
        """
        out: dict = {}
        for e in self.entries():
            if not e.partition:
                continue
            key = os.path.realpath(e.partition)
            current = out.get(key)
            if current is None or e.step_ref > current.step_ref:
                out[key] = e
        return out
