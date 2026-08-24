"""runtime.context — Runtime Context Infrastructure（第一版：仅 CodeGraph Provider）。

原则：
- CodeGraph 只通过本模块进 Runtime；Manager/Worker 不直接依赖 codegraph。
- 确定性、无 LLM；不做语义判断，不做"第二个 Planner"。
- 全程 fail-silent：任何异常返回 ""，不阻塞执行。
- 整个增强层由 ``AICU_CODEGRAPH=1`` 门控，默认关字节级不变。
- Provider 返回 Raw（可能 >4KB），Resolver 做预算裁剪（≤4KB 注入预算）。
"""

import logging
import os
import time

from .codegraph import query_group_inventory, query_symbol
from .metrics import record
from .resolver import DEFAULT_BUDGET, assemble, extract_candidates
from .state import GraphState, GroupGraphState

logger = logging.getLogger(__name__)

# 注入段公共文案
_HEADER = "## 🔍 代码图谱上下文 (CodeGraph)"
_DISCLAIMER = (
    "此段由 AICU Runtime 基于代码图谱生成，用于精准定位代码位置；"
    "**仍需用 Read 读取真实文件确认内容**——图谱描述的是结构，不能替代阅读源码语义。"
)

# 项目侧查询模板（角色 → codegraph 查询类型）
_ROLE_PROJECT_QUERY = {
    "test": "callers",
    "acceptance": "callers",
    "grill": "callers",
    "code": "callees",
    "dev_member": "callees",
    "dev_lead": "callees",
    "architecture": "node",
    "incremental_plan": "node",
}
# 不做项目侧查询的角色（文档类）
_SKIP_PROJECT_ROLES = {"requirements", "document", "default"}


def _enabled() -> bool:
    return os.environ.get("AICU_CODEGRAPH") == "1"


def _is_real(ctx) -> bool:
    """stub/mock 一律 no-op（stub E2E 字节级不变）。"""
    mode = (ctx.config or {}).get("mode") or os.environ.get("EXEC_MODE", "stub")
    mode = str(mode).lower()
    return mode not in ("stub", "mock")


def notify_artifact_updated(shared_workspace: str) -> None:
    """上下文失效边界：Worker 只知"有产出写入共享组"，不知 CodeGraph。

    内部 dispatch 到 GroupGraphState.mark_stale（当前仅 CodeGraph；
    未来 Artifact/RAG/Symbol Index 由此扩展，但不实现）。flag 关时 no-op。
    """
    if not _enabled():
        return
    try:
        GroupGraphState.mark_stale(shared_workspace)
    except Exception:  # noqa: BLE001
        logger.warning("mark_stale failed for %s", shared_workspace, exc_info=True)


def build_codegraph_context(node, ctx) -> str:
    """Worker/Manager 开工前的代码图谱上下文（Push）。禁用/失败返回 ""。"""
    if not _enabled() or not _is_real(ctx):
        return ""
    t0 = time.time()
    try:
        blocks, group_queries, graph_builds, est_baseline_tokens = _build_dependency_blocks(node, ctx)
        role = (getattr(node, "role", "") or "").lower()
        project_queries = 0
        if role not in _SKIP_PROJECT_ROLES:
            proj, project_queries = _build_project_block(
                getattr(node, "task", "") or "", role
            )
            if proj:
                blocks.append(proj)
        if not blocks:
            return ""
        block = assemble(_HEADER, _DISCLAIMER, blocks, DEFAULT_BUDGET)
        record(
            node, role,
            injected_bytes=len(block),
            resolver_ms=int((time.time() - t0) * 1000),
            group_queries=group_queries,
            project_queries=project_queries,
            graph_builds=graph_builds,
            estimated_baseline_dependency_tokens=est_baseline_tokens,
        )
        return block
    except Exception:  # noqa: BLE001
        logger.warning("codegraph context build failed", exc_info=True)
        return ""


def build_review_context(ctx, children_info) -> str:
    """Manager Review：子节点共享组代码影响面摘要。禁用/失败返回 ""。"""
    if not _enabled() or not _is_real(ctx):
        return ""
    t0 = time.time()
    try:
        blocks, seen = [], set()
        group_queries, graph_builds = 0, 0
        est_baseline_tokens = _estimate_review_baseline_tokens(children_info)
        for child in children_info or []:
            shared = (child or {}).get("shared_workspace")
            if not shared or shared in seen:
                continue
            seen.add(shared)
            if GroupGraphState.get(shared) != GraphState.READY:
                graph_builds += 1
            if not GroupGraphState.ensure_ready(shared):
                continue
            try:
                raw = query_group_inventory(shared)
                group_queries += 1
            except Exception:  # noqa: BLE001
                continue
            if raw.strip():
                blocks.append(
                    f"### 共享组代码产出 — {os.path.basename(shared)}\n\n{raw}"
                )
        if not blocks:
            return ""
        block = assemble(_HEADER, _DISCLAIMER, blocks, DEFAULT_BUDGET)
        record(
            getattr(ctx, "node", None),
            getattr(getattr(ctx, "node", None), "role", None),
            injected_bytes=len(block),
            resolver_ms=int((time.time() - t0) * 1000),
            group_queries=group_queries,
            graph_builds=graph_builds,
            estimated_baseline_dependency_tokens=est_baseline_tokens,
        )
        return block
    except Exception:  # noqa: BLE001
        logger.warning("codegraph review context build failed", exc_info=True)
        return ""


def _build_dependency_blocks(node, ctx):
    """对已完成且写共享组的依赖，生成组图产出清单（协作主场景）。

    返回 (blocks, group_queries, graph_builds, est_baseline_tokens)。
    """
    try:
        deps = ctx.runtime.get_dependencies(node.id)
    except Exception:  # noqa: BLE001
        return [], 0, 0, 0
    blocks, seen = [], set()
    group_queries, graph_builds = 0, 0
    for dep in deps:
        shared = (dep.config or {}).get("_shared_workspace")
        if not shared or shared in seen:
            continue
        seen.add(shared)
        if GroupGraphState.get(shared) != GraphState.READY:
            graph_builds += 1
        if not GroupGraphState.ensure_ready(shared):
            continue
        try:
            raw = query_group_inventory(shared)
            group_queries += 1
        except Exception:  # noqa: BLE001
            continue
        if raw.strip():
            blocks.append(f"### 共享组代码产出 — {os.path.basename(shared)}\n\n{raw}")
    # 仅估算被 CodeGraph 组图清单替换的共享组依赖（基线对应"通读每个文件"样板）
    shared_deps = [
        d for d in deps if (d.config or {}).get("_shared_workspace")
    ]
    return blocks, group_queries, graph_builds, _estimate_dependency_baseline_tokens(shared_deps)


def _estimate_dependency_baseline_tokens(deps) -> int:
    """粗略估算基线（未开 codegraph）时依赖文本注入的 token，仅作对比参考。

    依据 worker._build_worker_context「普通节点」依赖块结构（role + task[:120] +
    artifact_path + 固定样板），不依赖任何 LLM。基线本有分层读取/按需 Read，
    非全文注入，故不宣称"实际节省 N token"。
    """
    header = 180  # "## 🔴 强制前置步骤..." 头部固定样板
    per_dep = 200  # "📂产出路径 / 🔍操作 / ⛔未完成..." 固定样板
    total_chars = header
    for d in deps:
        role = (getattr(d, "role", "") or "")[:20]
        task = (getattr(d, "task", "") or "")[:120]
        artifact = (getattr(d, "artifact_path", "") or "") or getattr(d, "id", "")
        total_chars += per_dep + len(role) + len(task) + len(artifact)
    return total_chars // 4


def _estimate_review_baseline_tokens(children_info) -> int:
    """Manager Review 基线估算：基线会注入每个子节点 role+task+path+artifact_content。"""
    total_chars = 100  # "## Children to Review" 头部
    for child in children_info or []:
        role = (child.get("role") or "")[:20]
        task = (child.get("task") or "")[:120]
        path = (child.get("artifact_path") or "") or ""
        content = (child.get("artifact_content") or "")[:2000]
        total_chars += 150 + len(role) + len(task) + len(path) + len(content)
    return total_chars // 4


def _build_project_block(task: str, role: str):
    """按角色模板对项目图查询（确定性提取，无 LLM）。返回 (text, queries)。"""
    files, symbols = extract_candidates(task)
    query_kind = _ROLE_PROJECT_QUERY.get(role, "node")
    candidates = symbols[:3] or files[:1]
    lines, queries = [], 0
    for cand in candidates:
        try:
            out = query_symbol(cand, query_kind)
            queries += 1
        except Exception:  # noqa: BLE001
            continue
        if out.strip():
            lines.append(f"- {cand} ({query_kind}):\n{out.strip()[:600]}")
    return ("\n\n".join(lines) if lines else ""), queries


__all__ = [
    "build_codegraph_context",
    "build_review_context",
    "notify_artifact_updated",
    "GroupGraphState",
]
