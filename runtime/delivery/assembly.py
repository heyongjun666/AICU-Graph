"""runtime/delivery/assembly.py — Assembly Gate（最终总装核对门）。

Assembly 回答：整个 Session 的最终交付是否真实、完整、可交付？
确定性机械校验，无 LLM。

判定语义（评审约束 1）：passed = blocking_issues == []。Observation 不参与 passed。
registry_drift 是 Observation（Registry Snapshot 过期是正常信号，兄弟节点改写
shared 文件后 Snapshot 不自动更新），不改变 Source of Truth、不触发 repair。

边界（评审约束 5）：只做 Filesystem facts + Registry facts + Node metadata +
Delivery contract。不做 API 正确性 / 代码能否运行 / 测试质量 / 架构合理性 /
需求满足度 —— 这些留给 Worker / Reviewer / Manager LLM。

只读边界（评审约束 2）：本模块只读 Node metadata（经 children dict 传入）+ 返回
AssemblyResult，绝不调用 update_node_status / 不 mutate Node 状态。状态写回仅经
Manager → Runtime。

manifest 内容 = scan(shared_root)（评审约束 3）：Assembly 以 filesystem 重新扫描
为最终事实，Registry 仅补 metadata（step_ref/role/artifact_type/key_files），
不定义 manifest 内容。delivery_manifest.json 只描述真实存在文件。
"""

import os
from datetime import datetime
from typing import Any, Optional

from runtime.shared_io import build_dir_index

from .gate import FRAMEWORK_DIR_NAMES, _is_empty, _iter_files
from .issues import AssemblyResult, DeliveryIssue, DeliveryObservation
from .registry import ArtifactRegistryBackend, scan_artifact_files


def scan_shared_root(shared_root: str) -> dict:
    """扫描共享根 → Session 交付清单（只描述真实存在文件，跳过框架目录）。

    符合评审约束 3：manifest 内容 = scan(shared_root)，Registry 缺失/失败时仍
    生成完整 manifest —— filesystem 是 Source of Truth。

    Returns:
        dict: {type, version, generated_at, shared_root, files: [{relpath, size}],
               directories: [顶层分区目录], summary: {file_count, byte_total}}
    """
    root = os.path.realpath(shared_root) if shared_root else ""
    files: list[dict] = []
    directories: list[str] = []
    if root and os.path.isdir(root):
        for f in _iter_files(root):
            try:
                size = os.path.getsize(f)
            except OSError:
                size = 0
            files.append({
                "relpath": os.path.relpath(f, root),
                "size": size,
            })
        for name in sorted(os.listdir(root)):
            if name in FRAMEWORK_DIR_NAMES:
                continue
            if os.path.isdir(os.path.join(root, name)):
                directories.append(name)
    return {
        "type": "delivery_manifest",
        "version": 1,
        "generated_at": datetime.now().isoformat(),
        "shared_root": root,
        "files": files,
        "directories": directories,
        "summary": {
            "file_count": len(files),
            "byte_total": sum(f["size"] for f in files),
            "directories": directories,
        },
    }


def _child_artifact_path(child: dict) -> str:
    return child.get("artifact_path") or ""


def _child_config(child: dict) -> dict:
    return child.get("config") or {}


def _registry_metadata(children: list[dict], registry) -> list[dict]:
    """Registry 仅补 metadata（step_ref/role/artifact_type/key_files），不定义 manifest 内容。"""
    if registry is None:
        return []
    meta: list[dict] = []
    for child in children:
        step_ref = child.get("step_ref") or ""
        if not step_ref:
            continue
        entry = registry.read(step_ref)
        if entry is None:
            continue
        meta.append({
            "step_ref": step_ref,
            "node_id": entry.node_id or child.get("node_id") or "",
            "role": entry.role or child.get("role") or "",
            "artifact_type": entry.artifact_type,
            "artifact_path": entry.artifact_path,
            "key_files": list(entry.key_files or []),
        })
    return meta


def verify_assembly(
    children: list[dict],
    shared_root: str,
    registry: Optional[ArtifactRegistryBackend] = None,
) -> AssemblyResult:
    """Session 级总装核对（确定性、无 LLM、无 Node I/O 副作用）。

    Args:
        children: 交付链子节点 dict 列表，字段：
            step_ref / node_id / role / artifact_path /
            config（可含 _output_path 与 depends_on: list[step_ref]）。
        shared_root: ``{session}/shared/``（Source of Truth）。
        registry: 可选 Registry Backend；仅用于 registry_drift Observation 核对，
            缺失/失败不影响 manifest（manifest 永远来自 scan(shared_root)）。

    Returns:
        AssemblyResult(passed, issues, observations, manifest)。
        passed = blocking_issues == []；registry_drift 不参与 passed。
    """
    issues: list[DeliveryIssue] = []
    observations: list[DeliveryObservation] = []
    children = list(children or [])

    # ---- blocking checks ----
    seen_partitions: dict[str, str] = {}   # output_path -> step_ref
    step_children: dict[str, dict] = {}    # step_ref -> child

    for child in children:
        step_ref = child.get("step_ref") or child.get("node_id") or ""
        node_id = child.get("node_id") or ""
        cfg = _child_config(child)
        ap = _child_artifact_path(child)
        output_path = cfg.get("_output_path") or ""
        step_children[step_ref] = child

        # ① missing_artifact：声明产物不存在（None / 空 / filesystem 无此对象）
        if not ap or not os.path.exists(ap):
            issues.append(DeliveryIssue(
                severity="blocking",
                issue_type="missing_artifact",
                node_id=node_id,
                step_ref=step_ref,
                expected=f"artifact for '{step_ref}' exists on filesystem",
                actual=ap or "<none>",
                reason="child completed but declared artifact is missing or does not exist",
            ))
            continue

        # ② empty_artifact：存在但无任何文件
        if _is_empty(ap):
            issues.append(DeliveryIssue(
                severity="blocking",
                issue_type="empty_artifact",
                node_id=node_id,
                step_ref=step_ref,
                expected="non-empty artifact",
                actual=ap,
                reason="artifact exists but is empty",
            ))

        # ③ output_collision：两个不同子节点写同一非空输出分区（共享根自身除外）
        if output_path:
            if output_path in seen_partitions:
                issues.append(DeliveryIssue(
                    severity="blocking",
                    issue_type="output_collision",
                    node_id=node_id,
                    step_ref=step_ref,
                    expected=f"unique output partition for '{step_ref}'",
                    actual=f"output_path '{output_path}' also written by '{seen_partitions[output_path]}'",
                    reason="two children write the same output partition",
                ))
            else:
                seen_partitions[output_path] = step_ref

    # ④ dependency_mismatch：child.config.depends_on 引用的产物缺失
    for child in children:
        step_ref = child.get("step_ref") or child.get("node_id") or ""
        depends = _child_config(child).get("depends_on") or []
        for dep in depends:
            dep_child = step_children.get(dep)
            dep_ap = _child_artifact_path(dep_child) if dep_child else ""
            if dep_child is None or not dep_ap or not os.path.exists(dep_ap):
                issues.append(DeliveryIssue(
                    severity="blocking",
                    issue_type="dependency_mismatch",
                    node_id=child.get("node_id") or "",
                    step_ref=step_ref,
                    expected=f"dependency '{dep}' artifact exists",
                    actual=dep_ap or "<missing dependency>",
                    reason=f"child depends on '{dep}' whose artifact is missing",
                ))

    # ---- observations：registry_drift（非阻塞）----
    if registry is not None:
        for child in children:
            step_ref = child.get("step_ref") or ""
            ap = _child_artifact_path(child)
            if not step_ref or not ap or not os.path.exists(ap):
                continue
            entry = registry.read(step_ref)
            if entry is None:
                continue
            snapshot = list(entry.files or [])
            current = scan_artifact_files(ap)
            if snapshot != current:
                observations.append(DeliveryObservation(
                    obs_type="registry_drift",
                    node_id=child.get("node_id") or "",
                    step_ref=step_ref,
                    detail=(
                        f"registry snapshot for '{step_ref}' stale: "
                        f"{len(snapshot)} files in snapshot vs {len(current)} on filesystem "
                        "(sibling rewrites of shared files are expected; filesystem is source of truth)"
                    ),
                ))

    # ---- manifest：永远来自 scan(shared_root)，Registry 仅补 metadata ----
    manifest = scan_shared_root(shared_root)
    meta = _registry_metadata(children, registry)
    if meta:
        manifest["deliverables"] = meta

    passed = not any(i.severity == "blocking" for i in issues)
    return AssemblyResult(
        passed=passed,
        issues=issues,
        observations=observations,
        manifest=manifest,
    )


def compute_session_delivery_state(
    root_status: Any,
    chain_states: Optional[list[str]] = None,
    has_blocking_issues: bool = False,
) -> str:
    """Session 级派生交付状态（确定性计算，非第三套状态机）。

    三态（评审⑥）：
      - "invalid"  ：存在 blocking Issue（Gate FAIL / Assembly BLOCK）
      - "pending"  ：root 未 COMPLETED，或交付链未全部 delivered
      - "delivered"：root COMPLETED 且交付链全部 delivered

    Args:
        root_status: 根节点执行状态（NodeStatus 或 str）。
        chain_states: 交付链各节点 delivery_state 列表。
        has_blocking_issues: Assembly 是否存在 blocking Issue。

    Returns:
        "invalid" / "pending" / "delivered" 之一。
    """
    status = getattr(root_status, "name", root_status)
    if has_blocking_issues:
        return "invalid"
    if str(status) != "COMPLETED":
        return "pending"
    states = list(chain_states or [])
    if states and all(s == "delivered" for s in states):
        return "delivered"
    return "pending"
