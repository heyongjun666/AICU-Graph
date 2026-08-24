"""runtime/delivery/gate.py — Artifact Gate（确定性产物校验门）。

Gate 回答：这个节点声称的产物真的存在、且属于当前 Session 的允许空间、
且满足最小 ArtifactContract？确定性、无 LLM。

check_order 固定：containment → exists → type_validation → contract_validation。
不可先 exists 再判断 root —— 否则越界路径会报「不存在」而非「不属于 Session」。

失败抛 ArtifactGateError（携带结构化 ErrorObject），由 Worker 捕获转 FAILED +
delivery_state="invalid"（硬门，不静默放行）。
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from runtime.models import ErrorObject

from .contracts import ArtifactContract, resolve_required_types

logger = logging.getLogger(__name__)

# 框架/非代码目录：产物扫描时整体排除（与 worker._ROUTING_SEARCH_EXCLUDE_DIRS 对齐，
# 另补 .codegraph/.artifacts —— 交付层内部目录）。
FRAMEWORK_DIR_NAMES = frozenset({
    "events", "event_inbox", "events_inbox",
    "cc_0", "cc_1", "cc_2", "deliverables",
    "__pycache__", "node_modules", ".tox", "venv", ".venv",
    ".codegraph", ".artifacts", ".git",
})

# VERDICT 判定块提取正则（复用 worker._scan_single_file 语义：只定位块位置，
# 状态由 json.loads 解析 = 结构化非正则推断）。
_VERDICT_RE = re.compile(r"VERDICT\s*(\{.*?\})\s*-->", re.DOTALL)

# 扫描深度/文件数上限（Gate 是最小存在性证明，不做全量清单；Assembly 负责整体）。
_MAX_SCAN_DEPTH = 4
_MAX_SCAN_FILES = 2000


@dataclass
class ArtifactRoots:
    """当前 Session 允许产物归属根。

    - shared_root: ``{session}/shared/``（会话级单一共享根）
    - node_root: ``{session}/nodes/{node_id}/``（节点私有执行空间）
    """

    shared_root: Optional[str] = None
    node_root: Optional[str] = None

    def allowed(self) -> list[str]:
        return [r for r in (self.node_root, self.shared_root) if r]


@dataclass
class GateResult:
    """Gate 输出（非 bool，便于 debug 与 UI 展示）。"""

    passed: bool
    artifact_path: str
    validated_files: list = field(default_factory=list)
    errors: list = field(default_factory=list)


class ArtifactGateError(Exception):
    """Gate 硬门失败：携带结构化 ErrorObject，由 Worker 转 FAILED + delivery_state=invalid。"""

    def __init__(self, error: ErrorObject, message: str):
        super().__init__(message)
        self.error = error
        self.message = message


def extract_verdict(path: str) -> Optional[dict]:
    """从文件提取 VERDICT 判定块（结构化 dict）；无块或解析失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(200_000)
    except (IOError, OSError):
        return None
    m = _VERDICT_RE.search(content)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


def _is_within(root: str, path: str) -> bool:
    if not root or not path:
        return False
    root = os.path.realpath(root)
    path = os.path.realpath(path)
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


def _contains_framework_dir(path: str) -> Optional[str]:
    """路径各段是否含框架目录；命中返回该段名，否则 None。"""
    if not path:
        return None
    for seg in os.path.realpath(path).split(os.sep):
        if seg in FRAMEWORK_DIR_NAMES:
            return seg
    return None


def _iter_files(path: str):
    """遍历产物文件（file → 自身；dir → 有界 os.walk，跳过框架目录）。

    深度/文件数上限防「output_path='' 指向共享根」时全树扫描。
    """
    if os.path.isfile(path):
        yield path
        return
    if not os.path.isdir(path):
        return
    count = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in FRAMEWORK_DIR_NAMES]
        depth = os.path.relpath(root, path)
        if depth == ".":
            cur = 0
        else:
            cur = depth.count(os.sep) + 1
        if cur > _MAX_SCAN_DEPTH:
            dirs[:] = []
            continue
        for name in sorted(files):
            yield os.path.join(root, name)
            count += 1
            if count >= _MAX_SCAN_FILES:
                return


def _is_empty(path: str) -> bool:
    if os.path.isfile(path):
        try:
            return os.path.getsize(path) == 0
        except OSError:
            return True
    if os.path.isdir(path):
        for _ in _iter_files(path):
            return False
        return True
    return True


def _matches_types(path: str, required: list[str]) -> bool:
    if not required:
        return True
    ext = os.path.splitext(path)[1].lower()
    return ext in required


def _find_verdict_file(path: str) -> Optional[str]:
    """在产物下找第一个含 VERDICT 块的文件。"""
    for f in _iter_files(path):
        if extract_verdict(f) is not None:
            return f
    return None


def check_gate(
    artifact_path: str,
    roots: ArtifactRoots,
    contract: ArtifactContract,
) -> GateResult:
    """确定性产物校验（纯函数，无 Node I/O 副作用）。

    check_order 固定：containment → exists → type_validation → contract_validation。
    """
    errors: list[str] = []
    path = artifact_path or ""

    # ① containment：必须属于允许空间（shared/** 或 nodes/{id}/**）
    allowed = roots.allowed()
    if not allowed or not any(_is_within(r, path) for r in allowed):
        errors.append(
            f"OUTSIDE_ALLOWED_ROOT: artifact path '{path}' is not within the session "
            "allowed roots (shared/** or nodes/{node_id}/**)"
        )
        return GateResult(False, path, [], errors)

    # ② exists
    if not os.path.exists(path):
        errors.append(f"MISSING_ARTIFACT: artifact path '{path}' does not exist")
        return GateResult(False, path, [], errors)

    # ③ type_validation：产物不得落在框架目录
    seg = _contains_framework_dir(path)
    if seg:
        errors.append(f"FRAMEWORK_DIR: artifact path contains framework dir '{seg}'")
        return GateResult(False, path, [], errors)

    # ④ contract_validation：非空 + 契约
    validators = set(contract.validators or ())
    validated_files: list[str] = []

    if "non_empty" in validators and _is_empty(path):
        errors.append(f"EMPTY_ARTIFACT: artifact '{path}' is empty")
        return GateResult(False, path, [], errors)

    if "source_file" in validators:
        required = resolve_required_types(contract)
        candidates = list(_iter_files(path))
        if required:
            matched = [f for f in candidates if _matches_types(f, required)]
        else:
            matched = [f for f in candidates if os.path.isfile(f)]
        if not matched:
            errors.append(
                f"TYPE_MISMATCH: no file matching required types {required} under '{path}'"
            )
            return GateResult(False, path, [], errors)
        validated_files.extend(matched)

    if "verdict_block" in validators:
        verdict_file = _find_verdict_file(path)
        if verdict_file is None:
            errors.append(
                f"VERDICT_MISSING: no file containing a VERDICT block under '{path}'"
            )
            return GateResult(False, path, [], errors)
        if verdict_file not in validated_files:
            validated_files.append(verdict_file)

    return GateResult(True, path, validated_files, errors)
