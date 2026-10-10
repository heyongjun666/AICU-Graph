"""
executables/manager.py — Manager 执行单元（双模式：expand / review）

Manager 是 Executable 的具体实现，负责两段式生命周期：
- Expand 阶段：调用 AICU 生成 ExecutionPlan → submit_plan → 转 Waiting
- Review 阶段：读取子节点 Artifact → 调用 AICU 评审 → 迭代判断

依赖关系：executables/base.py, runtime/models.py
"""

import glob
import json
import logging
import os
import re
import traceback
from datetime import datetime
from typing import Any, Optional

from executables.base import Executable, ExecutableContext
from executables.worker import _aicu_cc_ids, _log_cc_lifecycle
from runtime.models import (
    ErrorObject,
    ExecutionPlan,
    LogEntry,
    LogLevel,
    Node,
    NodeStatus,
    Step,
)
from runtime.closed_loop import (
    build_closed_loop_report,
    build_deliverable_manifest,
    write_closed_loop_report,
    write_deliverable_manifest,
)
from runtime.workspace_resolver import partition_of

logger = logging.getLogger(__name__)


def _L(ctx: ExecutableContext, zh: str, en: str) -> str:
    """按节点语言（默认 zh）返回引导文案的中/英文版本。"""
    lang = (getattr(ctx, "config", None) or {}).get("language", "zh")
    return zh if lang == "zh" else en


def _extract_review_json(text: str) -> Optional[dict]:
    """从 review 文本中提取 JSON 对象（供 _parse_review_assessments 使用）。

    review CC 的产出可能包含多种形态：
    - 纯 JSON（推荐）
    - markdown ``` 代码围栏包裹的 JSON
    - 叙述性 markdown + 末尾嵌入的 JSON（review CC 常见输出）

    提取策略依次尝试：
    1. 围栏提取 → json.loads
    2. 整体 json.loads
    3. 反向扫描最后一个平衡大括号块 → json.loads
    （反向扫描而非正向，是因为叙述部分可能包含 { } 等花括号字符，
     而 JSON 结构通常位于文本末尾。）

    Args:
        text: review 输出文本。

    Returns:
        解析出的 JSON dict，或 None。
    """
    text = text.strip()

    # 1. markdown fence 包裹
    if "```" in text:
        lines = text.split("\n")
        json_lines = []
        in_fence = False
        for line in lines:
            if line.strip().startswith("```"):
                if in_fence:
                    break
                in_fence = True
                continue
            if in_fence:
                json_lines.append(line)
        if json_lines:
            try:
                data = json.loads("\n".join(json_lines).strip())
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass

    # 2. 整体解析
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # 3. 反向扫描最后一个平衡大括号块
    depth = 0
    close_idx = -1
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if ch == "}":
            if depth == 0:
                close_idx = i
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[i:close_idx + 1])
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError:
                    pass
                break
    return None


class Manager(Executable):
    """Manager 执行单元 — 两段式生命周期（expand → review）。

    生命周期:
        run() → _expand() → WAITING（等待子节点完成）
        → on_children_completed() → READY(review)
        → run() → _review() → COMPLETED / FAILED / 迭代
    """

    def __init__(
        self,
        context: Optional[ExecutableContext] = None,
        aicu_instance: Optional[Any] = None,
    ):
        """初始化 Manager。

        Args:
            context: ExecutableContext 实例。
            aicu_instance: 可选的 AICU 实例；未提供时按节点 role 创建真实实例。
        """
        super().__init__(context)
        self._aicu = aicu_instance

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        """主入口。根据 ctx.node.manager_phase 分派：
        - None / "expand" → _expand()
        - "review"        → _review()

        异常安全：
        try/except 包裹 _expand()/_review()，任何异常均被捕获：
        1. 构造 ErrorObject
        2. ctx.runtime.update_node_status(Failed, error=...)
        3. 异常不向上传播
        """
        try:
            phase = ctx.node.manager_phase
            if phase is None or phase == "expand":
                result = self._expand(ctx)
            elif phase == "review":
                result = self._review(ctx)
            else:
                raise ValueError(f"Unknown manager_phase: '{phase}'")
            # 方案三：Manager 也是事件 Producer（review 阶段可发布协作事件）。
            # 阶段完成后兜底摄取发布入口，否则 Manager 事件会滞留（scheduler tick 扫描
            # 仅在存在 RUNNING 节点时执行，且 Manager 无 worker 的 flush 兜底）。
            flush = getattr(ctx.runtime, "flush_node_events", None)
            if callable(flush):
                try:
                    flush(ctx.node.id)
                except Exception:  # noqa: BLE001
                    pass
            return result
        except Exception as exc:
            tb = traceback.format_exc()
            logger.error(
                "Manager.run() failed for node '%s': %s\n%s",
                ctx.node.id, exc, tb,
            )
            # 失败路径同样要兜底摄取事件：Manager 在 review 阶段可能已经发布了部分
            # 协作事件，若只在成功路径 flush，这些事件会滞留在发布目录里再也无人摄取
            # （scheduler tick 只在有 RUNNING 节点时扫描）。与 worker.py 失败路径对齐。
            flush = getattr(ctx.runtime, "flush_node_events", None)
            if callable(flush):
                try:
                    flush(ctx.node.id)
                except Exception:  # noqa: BLE001
                    pass
            error = ErrorObject.create_manager_execution_error()
            log_entries = [
                LogEntry(
                    ts=datetime.now(),
                    level=LogLevel.ERROR,
                    message=f"Manager execution failed: {exc}",
                ),
            ]
            ctx.runtime.update_node_status(
                ctx.node.id,
                NodeStatus.FAILED,
                error=error,
                log=log_entries,
            )
            return NodeStatus.FAILED

    # ------------------------------------------------------------------
    # Phase: Expand
    # ------------------------------------------------------------------

    def _expand(self, ctx: ExecutableContext) -> NodeStatus:
        """Expand 阶段：
        1. 构造 prompt
        2. _call_aiCU_with_retry() 调 AICU
        3. 解析 AICU 输出 → ExecutionPlan
        4. RuntimeGateway.submit_plan() → 翻译并注入 Graph
        5. 转 Waiting（等待子节点完成）
        """
        # 暴露 node id 给 _parse_plan 日志使用（_parse_plan 不持有 ctx）
        self._current_node_id = ctx.node.id

        # 1. 构造 prompt。已有子节点的重新规划（Revise / 修复 / 路由重激活）一律开新会话：
        #    历史规划对话不再带入，prompt 自带完整上下文（快照 + 候选子节点 + 增量说明），
        #    这是一次完整的首轮请求，AICU 会照常附带规划协议。
        fresh_session = bool(
            getattr(ctx.node, "pending_revise", False)
            or ctx.runtime.get_children(ctx.node.id)
        )
        prompt = self._build_expand_prompt(ctx)

        # 2. 调用 AICU。
        raw_output = self._call_aiCU_with_retry(ctx, prompt, fresh_session=fresh_session)

        # 3. 解析 ExecutionPlan
        plan = self._parse_plan(raw_output)

        # 4. 提交 Plan（Runtime 内部翻译为 Node+Edge 注入 Graph）
        #    有子节点的 Manager 重激活（修复/路由）一律走 Revise 选择性复用通道——
        #    让 Manager 在既有子节点之上增量续跑（reuse_step_ref 复用，必要时新建），
        #    绝不删除原有子节点整体重建（清理子节点是用户前端手动操作）。
        #    仅无子节点（首跑 / 无子节点重试）走全量通道（inject_plan 已拒绝有子节点注入）。
        has_children = bool(ctx.runtime.get_children(ctx.node.id))
        revision = bool(getattr(ctx.node, "pending_revise", False) or has_children)
        if revision and has_children and not plan.steps:
            # 空计划 = Manager 自己判定「本轮增量/事件不需要本子树采取行动」：
            # 事件/增量不属于本节点职责范畴，或既有子节点已覆盖且无需重新执行。
            # 「是否需要行动」是语义判断，只能由节点（LLM）作出 —— Runtime 不比对
            # 计划与现有树的文字差异来替它下结论（那会把「模型没规划出新东西」
            # 误当成「任务已完成」，静默吞掉真实增量）。
            # 此处只做机械衔接：不派发、不重激活任何子节点。
            # 前提是名下确有子树可"不行动"；无子节点的空计划仍按原路交回 Runtime，
            # 由 validate_plan 结构校验（"ExecutionPlan has no steps"）明确报错，
            # 不留「静默 WAITING 却永远等不到子节点」的口子。
            pending = getattr(ctx.node, "pending_context", None) or {}
            if pending.get("collaboration_type") == "repair":
                # 修复激活的契约：父级评审（另一个独立判断）已经判定存在缺陷，
                # 本轮必须产出修复动作。空计划在这里不是「要不要做」的判断问题，
                # 而是计划不成立 —— 明确失败，不留「静默转入 review 后空转迭代」
                # 的口子（那正是 max_iterations 被耗尽的假象来源）。
                raise ValueError(
                    "Repair activation received an empty ExecutionPlan: node "
                    f"'{ctx.node.id}' was reactivated to fix "
                    f"{len(pending.get('defects') or [])} reviewed defect(s) and must "
                    "plan at least one step"
                )
            children = ctx.runtime.get_children(ctx.node.id)
            terminal = {
                NodeStatus.COMPLETED,
                NodeStatus.FAILED,
                NodeStatus.STOPPED,
            }
            next_status = (
                NodeStatus.READY
                if children and all(child.status in terminal for child in children)
                else NodeStatus.WAITING
            )
            next_phase = "review" if next_status == NodeStatus.READY else "waiting"
            reason = (plan.no_action_reason or "").strip()
            logger.info(
                "Manager '%s' reported no action needed for this revision: "
                "next_phase=%s, reason=%s",
                ctx.node.id[:8], next_phase, reason or "(none)",
            )
            ctx.runtime.update_node_status(
                ctx.node.id,
                next_status,
                manager_phase=next_phase,
                log=[
                    LogEntry(
                        ts=datetime.now(),
                        level=LogLevel.INFO,
                        message=(
                            "Manager 判定本轮增量/事件无需本子树采取行动，未派发子节点任务"
                            f"（理由：{reason or '未给出'}）"
                        ),
                    ),
                ],
                pending_revise=False,
                pending_context=None,
            )
            return next_status
        if revision:
            submit = getattr(ctx.runtime, "submit_revision_plan", None)
            if submit is not None:
                submit(ctx.node.id, plan)
            else:
                ctx.runtime.submit_plan(ctx.node.id, plan)
        else:
            ctx.runtime.submit_plan(ctx.node.id, plan)

        # 5. 转 Waiting — 通过 update_node_status 直接更新
        # 注意：manager_phase 必须设为 "waiting"（与 ManagerPhase.WAITING 枚举值一致），
        # 否则 Scheduler._get_effective_running_count / waiting_managers 会无法识别此节点，
        # 错误地占用并发槽位。
        log_entries = [
            LogEntry(
                ts=datetime.now(),
                level=LogLevel.INFO,
                message="Manager expand phase completed, plan submitted",
            ),
        ]
        # 消费增量标记：pending_revise 是 Revise 独有的一次性标记，
        # 后续 review 不通过触发的 re-expand 走正常 repair 全量重建路径。
        ctx.runtime.update_node_status(
            ctx.node.id,
            NodeStatus.WAITING,
            manager_phase="waiting",
            log=log_entries,
            pending_revise=False,
            pending_context=None,
        )

        return NodeStatus.WAITING

    # ------------------------------------------------------------------
    # Phase: Review
    # ------------------------------------------------------------------

    def _review(self, ctx: ExecutableContext) -> NodeStatus:
        """Review 阶段 — 基于关键产物的目标一致性判断，精准修复不合格项。

        1. 收集子节点信息（step_ref, role, task, status, shared workspace partition）
        2. AICU 先识别关键产物，再定向检查关键内容并输出结构化评估
        3. 全部通过 → COMPLETED
        4. 部分失败 + 有剩余迭代 → 精准修复（仅重建失败子节点，保留合格子节点）
        5. 全部/部分失败 + 无剩余迭代 → FAILED
        """
        # 1. 获取子节点
        children = ctx.runtime.get_children(ctx.node.id)

        # 2. 收集子节点元数据；Review 只定位关键产物，不预加载或逐项读取全部 Artifact。
        children_info: list[dict] = []
        artifacts_for_file: dict[str, str] = {}
        for child in children:
            # 被替代节点（submit_fix_plan 因角色不匹配新建节点后标记 STOPPED）不再
            # 参与评审：它已退出活跃评估，评审只面对替代节点与其余存活子节点，
            # 避免 STOPPED 原节点反复被判失败 → 修复 → 再失败的评审死循环。
            if child.status == NodeStatus.STOPPED:
                continue
            step_ref = child.step_ref or child.id
            child_cfg = child.config or {}
            children_info.append({
                "step_ref": step_ref,
                "node_id": child.id,
                "exec_type": child.exec_type,
                "role": child.role,
                "task": child.task,
                "status": child.status,
                "shared_workspace": child_cfg.get("_shared_workspace"),
                "output_path": child_cfg.get("_output_path"),
            })

        # 3. 构造 prompt 并调用 AICU
        #    Review 是独立会话：不继承 Expand 的规划对话。规划协议已在 CLAUDE.md，
        #    这里只发 Review prompt + 输出格式（incremental），所以 prompt 必须自带
        #    评审所需的全部上下文（见 _build_review_prompt 的 Review Context 块）。
        prompt = self._build_review_prompt(ctx, children_info)
        raw_output = self._call_aiCU_with_retry(
            ctx, prompt, incremental=True, fresh_session=True
        )
        # 3.1 统一解析 review 内容为单一事实来源（real 模式下 ask() 返回元数据 dict，
        #     真实契约内容在 content/content_parsed 或 final_output_path 文件中）。
        #     同一份 content 喂给 _parse_review_assessments / _write_review_output，
        #     杜绝"各解析器各自重读、漏读即挂"的抽象泄漏。
        review_content = self._resolve_review_content(raw_output)

        # 4. 解析 AICU 结构化评估结果
        assessments, parse_status = self._parse_review_assessments(review_content, children_info)

        # 4.1 格式失败（JSON 契约未满足）不是质量信号：先给一次修复性重试，
        #     重试仍失败则按 AICU 元数据降级，避免"格式失败→重建→再格式失败"
        #     的无效循环（历史问题：Review AICU 输出成 markdown/产物回显导致
        #     Manager 反复重建至 max_iterations 而 FAILED）。
        if parse_status == "unparseable":
            repair_prompt = self._build_repair_review_prompt(prompt)
            try:
                repaired_raw = self._call_aiCU_with_retry(ctx, repair_prompt)
            except Exception as exc:
                logger.warning(
                    "Repair review retry failed for node '%s': %s",
                    ctx.node.id, exc,
                )
                repaired_raw = None
            if repaired_raw is not None:
                repaired_content = self._resolve_review_content(repaired_raw)
                assessments, parse_status = self._parse_review_assessments(
                    repaired_content, children_info
                )
                if parse_status == "parsed":
                    # 报告与实际采用的修复判定同源；降级时仍保留原始内容。
                    review_content = repaired_content
            if parse_status == "unparseable":
                assessments, parse_status = self._degrade_review(raw_output, children_info)

        failed_assessments = [a for a in assessments if not a.get("passed", False)]
        passed_count = len(assessments) - len(failed_assessments)

        iteration_count = ctx.node.iteration_count
        # 降级通过时在 Review 报告中显式记录决策，便于排查
        if parse_status == "degraded_pass" and isinstance(raw_output, dict):
            review_report = {
                **raw_output,
                "summary": (
                    "AICU review output unparseable; audit passed with no defects — "
                    "degraded to ALL-PASS (accepted with warning)"
                ),
            }
        else:
            review_report = review_content
        review_path = self._write_review_output(
            ctx, review_report, artifacts_for_file, iteration_count,
        )
        max_iterations = ctx.node.max_iterations

        # A2 闭环台账：记录本轮评审的通过/失败/修复指派（closed_loop.json 数据源）。
        # 业务产物在全部通过时落盘，此处仅累积运行时台账，不写 Runtime 外部状态。
        ledger_round = {
            "iteration": iteration_count,
            "timestamp": datetime.now().isoformat(),
            "review_path": review_path,
            "overall_passed": not bool(failed_assessments),
            "passed": [a["step_ref"] for a in assessments if a.get("passed", False)],
            "failed": [a["step_ref"] for a in failed_assessments],
            "fixes": [Manager._ledger_fix_entry(a) for a in failed_assessments],
        }
        ledger = list(getattr(ctx.node, "collaboration_ledger", []) or [])
        ledger.append(ledger_round)

        # 4.2 格式失败 + 审计存在已知缺陷 → 无法精准修复（格式失败会反复复现，
        #     重建无法自愈），快速失败并给出明确诊断，绝不进入重建循环。
        if parse_status == "unparseable_fail":
            log_entries = [
                LogEntry(
                    ts=datetime.now(),
                    level=LogLevel.ERROR,
                    message=(
                        "Review output unparseable and audit found defects; "
                        "cannot auto-repair — failing fast without rebuild"
                    ),
                ),
            ]
            ctx.runtime.update_node_status(
                ctx.node.id,
                NodeStatus.FAILED,
                error=ErrorObject.create_manager_execution_error(),
                artifact_path=review_path,
                log=log_entries,
            )
            return NodeStatus.FAILED

        # 5. 全部通过 → COMPLETED（A2：写只读闭环业务产物 closed_loop.json）
        if not failed_assessments:
            # 方案三：跨 Manager 协作不再经 review JSON 顶层 collaboration 字段解析
            # （已移除 _parse_collaboration）。Manager 发现需要兄弟节点配合时，直接由
            # CC 发布结构化事件（objective/intent/affected_roles/scope/action），
            # 由 AICU Router 决定交给谁，Scheduler 事件→重激活
            # 桥触发目标节点。

            log_entries = [
                LogEntry(
                    ts=datetime.now(),
                    level=LogLevel.INFO,
                    message=f"Manager review passed — all {passed_count} children approved",
                ),
            ]
            update_kwargs: dict[str, Any] = dict(
                manager_phase=None,
                artifact_path=review_path,
                collaboration_ledger=ledger,
                log=log_entries,
            )
            ctx.runtime.update_node_status(
                ctx.node.id,
                NodeStatus.COMPLETED,
                **update_kwargs,
            )
            # A2：全部通过时把协作闭环写成只读交付物（失败/中断轮次不落盘，
            # 保证产物代表"最终完整闭环"而非中间态）。
            try:
                # C2：读取 Scheduler 升级台账（不可收敛的协作路由），并入闭环报告，
                # 使交付审计可见"存在未闭合协作"（overall_passed 但不 converged）。
                escalations = []
                getter = getattr(ctx.runtime, "get_escalations", None)
                if callable(getter):
                    try:
                        escalations = list(getter() or [])
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Failed reading escalations for '%s': %s",
                            ctx.node.id, exc,
                        )
                report = build_closed_loop_report(
                    ctx.node, children, ledger, escalations=escalations,
                )
                workspace_getter = getattr(ctx.runtime, "get_node_workspace", None)
                if callable(workspace_getter):
                    ws = workspace_getter(ctx.node.id)
                    closed_loop_path = write_closed_loop_report(report, ws)
                    logger.info(
                        "Manager '%s' closed_loop artifact written: %s",
                        ctx.node.id[:8], closed_loop_path,
                    )
                    # C3：PASSED 时同时落盘交付物清单（最终产物完整性审计）。
                    manifest = build_deliverable_manifest(
                        ctx.node, children, escalations=escalations,
                    )
                    manifest_path = write_deliverable_manifest(manifest, ws)
                    logger.info(
                        "Manager '%s' deliverable_manifest written: %s "
                        "(files=%d)",
                        ctx.node.id[:8], manifest_path,
                        manifest.get("summary", {}).get("file_count", 0)
                        if "file_count" in manifest.get("summary", {})
                        else manifest["summary"]["complete"],
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to write closed_loop artifact for '%s': %s",
                    ctx.node.id, exc,
                )
            return NodeStatus.COMPLETED

        # 6. 有失败项 + 还有迭代次数 → 精准修复
        if iteration_count + 1 < max_iterations:
            new_iteration = iteration_count + 1
            failed_step_refs = {a["step_ref"] for a in failed_assessments}

            log_entries = [
                LogEntry(
                    ts=datetime.now(),
                    level=LogLevel.INFO,
                    message=(
                        f"Review: {passed_count} passed, {len(failed_assessments)} failed — "
                        f"targeted fix ({new_iteration}/{max_iterations})"
                    ),
                ),
            ]

            # 提交精准修复计划（仅替换失败子节点）
            try:
                ctx.runtime.submit_fix_plan(
                    ctx.node.id,
                    failed_assessments,
                    new_iteration,
                )
            except Exception as exc:
                logger.error(
                    "Failed to submit fix plan for manager '%s': %s",
                    ctx.node.id, exc,
                )
                ctx.runtime.update_node_status(
                    ctx.node.id,
                    NodeStatus.FAILED,
                    error=ErrorObject.create_plan_validation_error(),
                    artifact_path=review_path,
                    log=log_entries,
                )
                return NodeStatus.FAILED

            ctx.runtime.update_node_status(
                ctx.node.id,
                NodeStatus.WAITING,
                manager_phase="waiting",
                iteration_count=new_iteration,
                artifact_path=review_path,
                collaboration_ledger=ledger,
                log=log_entries,
            )

            logger.info(
                "Manager '%s' targeted fix: replacing %d failed children (%s), "
                "preserving %d passed children",
                ctx.node.id, len(failed_assessments),
                ", ".join(sorted(failed_step_refs)), passed_count,
            )
            return NodeStatus.WAITING

        # 7. 有失败项但无剩余迭代 → FAILED
        error = ErrorObject.create_max_iteration_exceeded()
        log_entries = [
            LogEntry(
                ts=datetime.now(),
                level=LogLevel.ERROR,
                message=(
                    f"Max iterations ({max_iterations}) exceeded, "
                    f"{len(failed_assessments)} children still failing: "
                    f"{', '.join(a['step_ref'] for a in failed_assessments)}"
                ),
            ),
        ]
        ctx.runtime.update_node_status(
            ctx.node.id,
            NodeStatus.FAILED,
            error=error,
            artifact_path=review_path,
            collaboration_ledger=ledger,
            log=log_entries,
        )
        return NodeStatus.FAILED

    @staticmethod
    def _ledger_fix_entry(a: dict) -> dict:
        """从单条失败评估中提取闭环修复条目（含 B1 契约扩展字段透传）。"""
        fix = a.get("fix") or {}
        entry = {
            "step_ref": a.get("step_ref", ""),
            "role": fix.get("role", ""),
            "task": fix.get("task", ""),
        }
        for key in ("file", "line", "desc", "expected_action", "check"):
            if fix.get(key):
                entry[key] = fix[key]
        return entry

    @staticmethod
    def _resolve_review_content(raw_output: Any) -> Any:
        """统一解析 review 输出的一等内容，消除"各解析器各自重读 final_output_path
        文件、漏读即挂"的抽象泄漏。

        real 模式下 aicu.ask() 返回元数据 dict（scores/final_output_path/...），真正
        review 契约内容（overall_passed / assessments）在
        content / content_parsed 字段或 final_output_path 指向的文件里。此处只解析
        一次，产出同一份内容供 _parse_review_assessments / _write_review_output
        消费（单一事实来源）。

        优先级：
        1. content_parsed（已是 dict）→ 直接返回
        2. content（str/dict）→ str 时尝试提取 review JSON，成功返回 dict，否则返回原 str
        3. final_output_path（文件可读）→ 读取文本 → 同样尝试提取 JSON
        4. 原样返回（stub / 单测直传 dict 兼容）

        Args:
            raw_output: aicu.ask() 的原始返回。

        Returns:
            dict（可解析出契约）或 str（仅文本）。
        """
        if not isinstance(raw_output, dict):
            return raw_output

        content = raw_output.get("content_parsed")
        if isinstance(content, dict):
            return content

        content = raw_output.get("content")
        if isinstance(content, dict):
            return content
        if isinstance(content, str) and content.strip():
            data = _extract_review_json(content)
            return data if data is not None else content

        output_path = raw_output.get("final_output_path")
        if (
            output_path
            and isinstance(output_path, str)
            and os.path.isfile(output_path)
        ):
            try:
                with open(output_path, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError as exc:
                logger.warning(
                    "_resolve_review_content: failed to read review output "
                    "file %s: %s", output_path, exc,
                )
            else:
                data = _extract_review_json(text)
                return data if data is not None else text

        return raw_output

    # ------------------------------------------------------------------
    # Callback: on_children_completed
    # ------------------------------------------------------------------

    @staticmethod
    def on_children_completed(node: Node, runtime: Any) -> None:
        """由 Runtime 在检测到所有子节点 Completed 时回调。

        1. 设置 manager_phase = "review"
        2. 设置 status = READY（Scheduler 将在下一 tick 调度）

        Args:
            node: Manager 节点对象。
            runtime: RuntimeGateway 实例。
        """
        runtime.update_node_status(
            node.id,
            NodeStatus.READY,
            manager_phase="review",
        )

    # ------------------------------------------------------------------
    # Internal: AICU 调用
    # ------------------------------------------------------------------

    def _call_aiCU_with_retry(
        self,
        ctx: ExecutableContext,
        prompt: str,
        retries: int = 2,
        incremental: bool = False,
        fresh_session: bool = False,
    ) -> Any:
        """统一 AICU 调用 + 重试逻辑。

        Args:
            ctx: 执行上下文。
            prompt: 发送给 AICU 的 prompt。
            retries: 重试次数（默认 2）。
            incremental: 只发送本轮 prompt + 输出格式（不再附带首轮规划协议）。
            fresh_session: 丢弃节点缓存的 AICU，以全新会话（无 --resume）发起本次调用；
                工作区与产物保留。仅作用于真实 AICU，注入的测试替身不受影响。

        Returns:
            AICU.ask() 的原始返回结果。

        Raises:
            重试耗尽时抛出最后一次异常。
        """
        # 生产路径始终使用真实 AICU；测试可通过 aicu_instance 显式注入 fake。
        from executables.worker import (
            Worker,
            _create_real_aicu,
            _extract_error_text,
            _is_retryable_api_rejection,
            _render_previous_failure,
        )

        aicu = self._aicu
        if aicu is None:
            if fresh_session:
                with Worker._aicu_cache_lock:
                    Worker._aicu_cache.pop(ctx.node.id, None)
            aicu = _create_real_aicu(ctx)

        cc_ids = _aicu_cc_ids(aicu)
        phase = ctx.node.manager_phase or "expand"
        for cc_id in cc_ids:
            _log_cc_lifecycle(
                ctx.runtime,
                ctx.node.id,
                cc_id,
                "cc_received",
                phase=phase,
                task=ctx.node.task,
            )

        last_exception: Optional[Exception] = None
        # API 以 400 拒绝（如模型不支持图片输入）时，把报错原文交给模型自行调整，
        # 并改用全新会话：原会话历史里可能已带着被拒的内容，续接只会再次 400。
        error_note = ""
        for attempt in range(retries + 1):
            send_prompt = f"{error_note}\n\n---\n\n{prompt}" if error_note else prompt
            try:
                if incremental:
                    ask_incremental = getattr(aicu, "ask_incremental", None)
                    result = (
                        ask_incremental(send_prompt)
                        if callable(ask_incremental)
                        else aicu.ask(send_prompt)
                    )
                else:
                    result = aicu.ask(send_prompt)
                for cc_id in cc_ids:
                    _log_cc_lifecycle(
                        ctx.runtime,
                        ctx.node.id,
                        cc_id,
                        "cc_handoff",
                        phase="handoff",
                        task=ctx.node.task,
                    )
                return result
            except Exception as exc:
                last_exception = exc
                logger.warning(
                    "AICU call attempt %d/%d failed for node '%s': %s",
                    attempt + 1, retries + 1, ctx.node.id, exc,
                )
                if not error_note and attempt < retries and _is_retryable_api_rejection(exc):
                    error_text = _extract_error_text(str(exc))
                    error_note = _render_previous_failure(error_text)
                    ctx.runtime.update_node_status(
                        ctx.node.id,
                        NodeStatus.RUNNING,
                        log=[LogEntry(
                            ts=datetime.now(),
                            level=LogLevel.WARN,
                            message=f"执行被 API 拒绝，已带上报错信息新建会话重试: {error_text}",
                        )],
                    )
                    if self._aicu is None:
                        with Worker._aicu_cache_lock:
                            Worker._aicu_cache.pop(ctx.node.id, None)
                        aicu = _create_real_aicu(ctx)
                continue

        raise last_exception  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Internal: Plan 解析
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_plan_json(text: str) -> Optional[dict]:
        """从文本中提取 plan JSON（尝试直接解析或从 execution_plan_json 字段提取）。"""
        import json as _json
        text = text.strip()
        # 直接解析 JSON
        try:
            data = _json.loads(text)
            if isinstance(data, dict):
                return data
        except _json.JSONDecodeError:
            pass
        # 尝试从 execution_plan_json 字段提取（文本可能包含该字段的 JSON 字符串值）
        try:
            data = _json.loads(text)
            if isinstance(data, str):
                return _json.loads(data)
        except _json.JSONDecodeError:
            pass
        # 尝试用平衡大括号提取
        start = text.find('{')
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                if depth == 0:
                    try:
                        return _json.loads(text[start:i+1])
                    except _json.JSONDecodeError:
                        break
        return None

    @staticmethod
    def _with_no_action_reason(plan: ExecutionPlan, payload: Any) -> ExecutionPlan:
        """空计划时带上 LLM 给出的理由（``reason`` / ``no_action_reason``）。

        「本轮不需要行动」是 Manager 自己的判断，理由只用于节点日志留痕（让用户
        看得见为什么没派活），不参与任何判定。
        """
        if plan.steps or not isinstance(payload, dict):
            return plan
        reason = payload.get("no_action_reason") or payload.get("reason")
        if isinstance(reason, str) and reason.strip():
            plan.no_action_reason = reason.strip()
        return plan

    @staticmethod
    def _execution_plan_from_payload(payload: Any) -> Optional[ExecutionPlan]:
        """从已解析的 AICU payload 提取执行计划。"""
        if not isinstance(payload, dict):
            return None

        if "steps" in payload and isinstance(payload["steps"], list):
            return Manager._with_no_action_reason(
                Manager._dict_to_execution_plan(payload["steps"]), payload
            )

        nested = payload.get("execution_plan_json")
        if isinstance(nested, dict):
            return Manager._execution_plan_from_payload(nested)
        if isinstance(nested, str) and nested.strip():
            nested_data = Manager._extract_plan_json(nested)
            if nested_data is not None:
                return Manager._execution_plan_from_payload(nested_data)
        return None

    def _parse_plan(self, raw_output: Any) -> ExecutionPlan:
        """从 AICU 输出解析 ExecutionPlan。

        支持多种格式：
        - dict 类型：从 "execution_plan_json" 字段读取 JSON 字符串
        - str 类型：直接作为 JSON 解析
        - dict 含 "final_output_path"：从该路径读取文件内容并提取 JSON

        Args:
            raw_output: AICU.ask() 的原始返回结果。

        Returns:
            ExecutionPlan 对象。

        Raises:
            ValueError: 无法解析时抛出。
        """
        import json as _json
        import os as _os

        # 通过实例属性记录当前节点 id（由 run() 设置），供静态分支日志引用
        node_id_hint = getattr(self, "_current_node_id", "?")

        if isinstance(raw_output, dict):
            # Real AICU returns aggregate metadata. Its plan is nested in the
            # parsed content, while legacy stubs may put it at the top level.
            content_parsed = raw_output.get("content_parsed")
            plan = self._execution_plan_from_payload(content_parsed)
            if plan is not None:
                return plan

            content = raw_output.get("content")
            if isinstance(content, dict):
                plan = self._execution_plan_from_payload(content)
            elif isinstance(content, str) and content.strip():
                plan_data = self._extract_plan_json(content)
                plan = self._execution_plan_from_payload(plan_data)
            else:
                plan = None
            if plan is not None:
                return plan

            plan = self._execution_plan_from_payload(raw_output)
            if plan is not None:
                return plan

            # 尝试从 final_output_path 读取文件
            output_path = raw_output.get("final_output_path")
            if output_path and _os.path.isfile(output_path):
                try:
                    with open(output_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    plan_data = self._extract_plan_json(content)
                    if not plan_data:
                        # 兜底1: CLI 偶尔会少输出一个闭合 }，导致 JSON 不合法，
                        # 逐个追加 } 试探最多补 3 个大括号 — 这是高频已知问题。
                        candidate = content
                        for _ in range(3):
                            try:
                                inner_data = _json.loads(candidate)
                                if isinstance(inner_data, dict):
                                    logger.info(
                                        "Recovered malformed plan for node '%s' "
                                        "(added trailing braces), original_len=%d",
                                        node_id_hint, len(content),
                                    )
                                    plan_data = inner_data
                                    break
                            except Exception:
                                candidate = candidate + "}"

                    plan = self._execution_plan_from_payload(plan_data)
                    if plan is not None:
                        return plan
                    # Fallback: 尝试从非 JSON 文本中解析 Markdown 表格格式
                    steps = self._parse_markdown_plan(content)
                    if steps:
                        logger.info(
                            "Parsed plan from markdown table for node '%s' (%d steps)",
                            node_id_hint, len(steps),
                        )
                        return ExecutionPlan(steps=steps)
                except Exception as parse_exc:
                    # 不要静默吞掉异常 — 留下排查线索再向上层报错
                    logger.warning(
                        "Failed to read/parse plan file '%s' for node '%s': %s",
                        output_path, node_id_hint, parse_exc,
                    )
            # 无法识别的格式 — 抛出异常由外层 try/except 处理
            raise ValueError(
                f"AICU output dict has no 'execution_plan_json' or 'steps' field: "
                f"{list(raw_output.keys())}"
            )

        if isinstance(raw_output, str):
            return ExecutionPlan.from_json(raw_output)

        raise ValueError(
            f"Cannot parse plan from output type: {type(raw_output).__name__}"
        )

    @staticmethod
    def _dict_to_execution_plan(steps_data: list[dict]) -> ExecutionPlan:
        """从 dict 列表构造 ExecutionPlan。"""
        steps = []
        for s in steps_data:
            steps.append(Step(
                step_id=s["step_id"],
                exec_type=s.get("exec_type", "worker"),
                role=s.get("role", "default"),
                task=s.get("task", ""),
                task_summary=s.get("task_summary", ""),
                config=s.get("config", {}),
                depends_on=s.get("depends_on", []),
                reuse_step_ref=s.get("reuse_step_ref"),
            ))
        return ExecutionPlan(steps=steps)

    # 列名别名映射（大小写不敏感）
    _STEP_ID_KEYS = ('id', 'step', 'name', 'step_id', 'step name', 'node', 'node_id')
    _EXEC_TYPE_KEYS = ('类型', 'type', 'exec_type', 'kind', '节点类型')
    _ROLE_KEYS = ('角色', 'role', 'assignee', 'responsible')
    _DEPENDS_KEYS = ('依赖', 'depends_on', 'depends', 'depends on', 'dependencies', 'prerequisite')
    _TASK_KEYS = ('task', 'description', 'covers', 'covers requirements', 'notes', 'details', 'responsibilities')

    @staticmethod
    def _normalize_header(h: str) -> str:
        """标准化表头名称（去空格/去星号/全小写）。"""
        return h.strip().strip('*').strip().lower()

    @staticmethod
    def _parse_markdown_plan(text: str) -> list[Step]:
        """从 Markdown 表格文本中解析 Step 列表（LLM 未按要求输出 JSON 时的兜底方案）。

        支持的格式:
        | Step | ID | 类型 | 角色 | 依赖 |
        |------|----|------|------|------|
        | 1 | **design_spec** | Manager | default | — |

        也支持无 ID 列的表（通过 Step 列或行号生成 step_id）。
        """
        steps = []
        lines = text.strip().split('\n')
        in_table = False
        headers = []
        for line in lines:
            line = line.strip()
            # 检测表格行
            if line.startswith('|') and line.endswith('|'):
                cells = [c.strip() for c in line.split('|')[1:-1]]
                if not in_table:
                    # 表头行
                    headers = cells
                    in_table = True
                    continue
                # 分隔行（|---|----|）
                if all(set(c.strip('- :')) == set() or all(ch in '- :' for ch in c) for c in cells):
                    continue
                # 数据行
                if headers:
                    row = dict(zip(headers, cells))
                    norm_row = {Manager._normalize_header(k): v for k, v in row.items()}

                    # step_id: 多别名查找，找不到用行号生成
                    step_id = ''
                    for key in Manager._STEP_ID_KEYS:
                        if key in norm_row:
                            step_id = norm_row[key].strip('*').strip('**').strip()
                            break
                    if not step_id:
                        step_id = f'step_{len(steps)}'

                    # exec_type
                    exec_type_raw = 'worker'
                    for key in Manager._EXEC_TYPE_KEYS:
                        if key in norm_row:
                            exec_type_raw = norm_row[key].lower()
                            break
                    exec_type = 'manager' if 'manager' in exec_type_raw else 'worker'

                    # role
                    role = 'default'
                    for key in Manager._ROLE_KEYS:
                        if key in norm_row:
                            role = norm_row[key].strip('*').strip('**').strip()
                            break

                    # depends_on
                    depends_raw = ''
                    for key in Manager._DEPENDS_KEYS:
                        if key in norm_row:
                            depends_raw = norm_row[key]
                            break
                    depends = [d.strip().strip('*').strip('**').strip() for d in depends_raw.split(',') if d.strip() and d.strip() != '—']

                    # task
                    task = step_id
                    for key in Manager._TASK_KEYS:
                        if key in norm_row:
                            task = norm_row[key].strip('*').strip('**').strip()
                            break

                    steps.append(Step(
                        step_id=step_id,
                        exec_type=exec_type,
                        role=role or 'default',
                        task=task,
                        task_summary=task,
                        config={},
                        depends_on=depends,
                    ))
            elif in_table and line == '':
                break  # 表格结束
        return steps

    # ------------------------------------------------------------------
    # Internal: Prompt 构造
    # ------------------------------------------------------------------

    def _build_expand_prompt(self, ctx: ExecutableContext) -> str:
        """构造 Expand 阶段 prompt — 含执行上下文快照 + 原始task。

        上下文块通过 _build_execution_context_snapshot() 生成；失败时仍保留事件、
        repair 和任务上下文，避免协作信号因 snapshot 不可用而丢失。
        Manager 可自由创建子 Manager，组织树深度由 Phase D 反问门自然控制。
        """
        base_task = ctx.node.task or ""
        try:
            ctx_block = self._build_execution_context_snapshot(ctx)
        except Exception as exc:
            logger.warning(
                "Failed building execution context for node '%s': %s",
                getattr(getattr(ctx, 'node', None), 'id', '?'), exc,
            )
            ctx_block = ""

        sep = "\n" + "-" * 60 + "\n"
        # 共享工作区 / 事件协作 / depends_on 正交等协议均由 roles/Manager/*.yaml 的
        # generate_instruction 注入（aicu 在每次 ask 时统一注入），此处不再重复维护。
        # B3：被路由重新激活且携带事件上下文时，注入事件上下文供决策。
        # 方案三：协作意图由 Event.objective 承载，Manager 收到事件后自行理解。
        collab_block = self._build_event_context_block(ctx)
        # 方案三：Manager 也是 Producer + Consumer，注入事件发布能力（跨 Manager 协作
        # 由 CC 发布结构化事件触发，不再经 review JSON）。
        # V3.1 P0#4：Manager 补事件收件箱块（事件收件箱/运行中实时轮询），
        # 复用 Worker._build_event_inbox_instructions 的 empty-guard（无订阅且 inbox 空 → 空串）。
        try:
            from executables.worker import Worker as _Worker
            event_publish_block = _Worker._build_event_publish_instructions(ctx.node)
            event_inbox_block = _Worker._build_event_inbox_instructions(ctx.node)
        except Exception:  # noqa: BLE001
            event_publish_block = ""
            event_inbox_block = ""
        parts = [ctx_block]
        if collab_block:
            parts.append(collab_block)
        # 2c：修复上下文 — pending_context 为 repair 类型（本 Manager 被父级 review 修复，
        # 或修复闭环重新激活）时，渲染缺陷明细，让 Manager 知道"哪里坏了、要改成什么"。
        repair_block = self._build_repair_context_block(ctx)
        if repair_block:
            parts.append(repair_block)
        if event_inbox_block:
            parts.append(event_inbox_block)
        if event_publish_block:
            parts.append(event_publish_block)
        # Revise（增量续跑）：pending_revise 为真，或有子节点的 Manager 重激活（修复/路由）→
        # 注入可复用候选节点块 + 增量任务说明，让 Manager 按「决策 0 · 增量续跑」优先复用
        # 既有子节点（reuse_step_ref），仅在无候选覆盖的新能力处新建 step。
        has_children = False
        try:
            has_children = bool(ctx.runtime.get_children(ctx.node.id))
        except Exception:  # noqa: BLE001
            has_children = False
        if getattr(ctx.node, "pending_revise", False) or has_children:
            revise_block = self._build_revise_candidates_block(ctx)
            if revise_block:
                parts.append(revise_block)
                pending = getattr(ctx.node, "pending_context", None) or {}
                original = pending.get("original_task")
                incremental = pending.get("incremental_task") or base_task
                revise_lines = [_L(ctx, "## 增量续跑任务", "## Incremental Continue Task"), ""]
                if original:
                    # 原任务同样给全文(这里曾是 original[:300]). 它与下面的增量任务是
                    # 一对 —— 要让 Manager 判断"哪些既有子节点能覆盖这个增量", 被砍掉
                    # 后半段的那份原任务是没法比的. 与 incremental 的写法保持一致.
                    revise_lines.append(
                        f"- {_L(ctx, '原任务', 'Original task')}: {original}"
                    )
                revise_lines.append(
                    f"- {_L(ctx, '增量任务', 'Incremental task')}: {incremental}"
                )
                revise_lines.append("")
                revise_lines.append(
                    _L(
                        ctx,
                        "必须按「决策 0 · 增量续跑」处理。第一步先判断：这次增量任务/事件，是否需要本子树"
                        "采取任何行动 —— 它可能落在你的职责范畴之外（与你和你的子节点负责的能力无关），"
                        "也可能既有子节点已经在做的事就是它要求的、无需任何人重新执行。**这是你自己的判断。**"
                        "若确实无需行动，就输出 {\"steps\": [], \"reason\": \"<为什么无需本子树的任何节点行动>\"}："
                        "空 steps 表示本轮不派活，框架不会重激活任何子节点，你随后进入复核。"
                        "**绝不要为了凑出一份非空计划，把既有子节点按原样重发一遍** —— 那等于让它们白跑一轮，"
                        "既浪费执行、又掩盖了真正的增量。\n"
                        "若确有行动要做：再逐项比较增量任务与全部既有子节点的 role、任务和任务摘要，"
                        "只要某个既有子节点能够承担该增量任务，就必须原地复用它，并在对应 step 中填写该节点的 "
                        "reuse_step_ref（使用候选中的 step_ref 或 node_id）；此时禁止填写 null，也禁止新建同职责 step。"
                        "只有既有子节点都无法覆盖的全新能力才允许新建 step；新建 step 的 task 必须描述当前增量任务中的该新能力，"
                        "不得照抄候选节点原 task。复用意味着保留原产物和工作区，该节点以新会话重新执行当前增量任务。",
                        "You MUST follow \"Decision 0 · Incremental Continue\". First decide whether this "
                        "increment/event requires ANY action from your subtree at all: it may fall outside your "
                        "remit (unrelated to what you and your children are responsible for), or your existing "
                        "children may already be doing exactly what it asks, so nobody needs to rerun. "
                        "**This judgement is yours.** If no action is needed, output "
                        "{\"steps\": [], \"reason\": \"<why no node in this subtree needs to act>\"} — empty "
                        "steps means nothing is dispatched this round and no child is reactivated; you then move "
                        "to review. Do NOT pad out a non-empty plan by re-sending existing children unchanged: "
                        "that makes them rerun for nothing and hides the real increment.\n"
                        "If action IS needed: compare the incremental task with every existing child by role, "
                        "task, and task summary. If any existing child "
                        "can perform the increment, you MUST reuse it in place and fill that step's reuse_step_ref "
                        "with the candidate step_ref or node_id; null is forbidden and creating a new step with the "
                        "same responsibility is forbidden. Create a new step only for a genuinely new capability "
                        "that no existing child can cover; the new step's task must describe that new capability from "
                        "the current incremental task and must not copy a candidate's original task. Reuse preserves "
                        "the existing artifacts and workspace while the node reruns the current incremental task in a fresh session.",
                    )
                )
                parts.append("\n".join(revise_lines))
        parts.append(
            # 标题从「(本节点原始 Task 描述)」改成「(个人当前目标)」: node.task 是
            # **可变**的「现在该执行什么」槽位, 经一次 Revise 后它就是增量任务, 而真正的
            # 原任务由上面的「## 增量续跑任务」块单独标为「原任务」给出。旧标题管后者叫
            # 「原始」, 会在同一条 prompt 里和那个「原任务」互相打脸。顺便与团队目标对称。
            f"{_L(ctx, '## 你的任务(个人当前目标)', '## Your Task (your current personal goal)')}\n\n{base_task}"
        )
        return sep.join(parts)

    def _build_revise_candidates_block(self, ctx: ExecutableContext) -> str:
        """Revise（增量续跑）：列出既有子节点作为可复用候选，供 Manager 决策 0 选择复用。

        每个候选只给出 step_ref / node_id / role / status / task_summary，
        不读取子节点产物或目录；Manager 仅凭任务摘要判断能力是否覆盖增量需求。
        节点没有摘要时才退回到截断后的 task。
        Manager 按角色匹配增量需求 → 填 reuse_step_ref，无候选覆盖的新能力才新建 step。
        未复用的已完成兄弟保持不动。
        """
        try:
            children = ctx.runtime.get_children(ctx.node.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "get_children failed building revise candidates for node '%s': %s",
                getattr(getattr(ctx, 'node', None), 'id', '?'), exc,
            )
            return ""
        if not children:
            return ""
        lines = [
            _L(ctx, "## 可复用候选节点 (Reuse Candidates)", "## Reuse Candidates"),
            "",
            _L(
                ctx,
                "上级要求在本节点既有子节点之上做增量续跑。以下是本节点现有的全部子节点（仅含任务摘要）：",
                "The parent requires incremental continuation on top of this node's existing "
                "child nodes. Below are all current child nodes (task summary only):",
            ),
            "",
        ]
        for child in children:
            step_ref = child.step_ref or child.id
            status = (
                child.status.value if hasattr(child.status, "value") else str(child.status)
            )
            lines.append(
                f"- step_ref: `{step_ref}` | node_id: `{child.id}` | "
                f"role: {child.role} | status: {status}"
            )
            summary = (child.task_summary or "").strip() or self._truncate(child.task or "", 100)
            if summary:
                lines.append(f"  task_summary: {summary}")
        lines.append("")
        lines.append(
            _L(
                ctx,
                "增量决策规则（必须执行）：先将每个计划 step 与候选节点逐项做能力覆盖匹配。"
                "候选角色能够完成该 step 的增量任务时，必须复用候选，并将 reuse_step_ref 设置为候选列表中原样的 "
                "step_ref 或 node_id；仅沿用相同 step_id 不算复用。对已有能力进行修改、完善、调整或再次执行，仍属于该能力的增量续跑，"
                "不能因此新建 step。只有没有任何候选能够完成的全新能力，才允许新建 step，此时 reuse_step_ref 才能为 null。"
                "输出 execution_plan 前必须自检：所有可由候选覆盖的 step "
                "都已填写有效 reuse_step_ref；未复用的已完成兄弟节点保持不动。",
                "Incremental decision rule (MANDATORY): match every planned step against the candidates by capability. "
                "When a candidate role can perform the step's incremental task, you MUST reuse that candidate and set "
                "reuse_step_ref to the exact step_ref or node_id shown in the candidate list; using the same step_id alone "
                "does not count as reuse. Modifying, refining, adjusting, or rerunning an existing capability is still "
                "an incremental continuation of that capability and must not create a new step. Create a new step only "
                "for a genuinely new capability that no candidate can "
                "perform; only then may reuse_step_ref be null. Before outputting execution_plan, self-check that every "
                "step covered by a candidate has a valid reuse_step_ref; completed siblings that are not reused stay unchanged.",
            )
        )
        return "\n".join(lines)

    @staticmethod
    def _collab_from_pending_context(ctx: ExecutableContext):
        """Return canonical event entries, with scalar pending_context compatibility."""
        pending = getattr(ctx.node, "pending_context", None) or {}
        batch = pending.get("event_batch")
        if isinstance(batch, list) and batch:
            events = [event for event in batch if isinstance(event, dict)]
            if events:
                return events

        content = pending.get("event_content")
        if not content:
            return None
        return [{
            "event_id": pending.get("event_id") or "",
            "producer": pending.get("event_producer") or "",
            "objective": content,
            "intent": pending.get("event_intent") or "",
            "scope": pending.get("event_scope") or "",
            "action": pending.get("event_action") or "",
            "affected_roles": pending.get("event_affected_roles") or [],
        }]

    @staticmethod
    def _collab_from_event_inbox(ctx: ExecutableContext):
        """Read all renderable inbox events for the running Manager fallback."""
        try:
            ws = ctx.runtime.get_node_workspace(ctx.node.id)
            inbox = os.path.join(ws, "event_inbox")
            files = sorted(glob.glob(os.path.join(inbox, "*.json")))
        except Exception:  # noqa: BLE001
            return None
        events = []
        for pf in files:
            try:
                with open(pf, "r", encoding="utf-8") as f:
                    ev = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(ev, dict):
                continue
            content = ev.get("objective") or ev.get("content")
            if not content:
                continue
            events.append(ev)
        return events or None

    @staticmethod
    def _build_event_context_block(ctx: ExecutableContext) -> str:
        """Render canonical event_batch entries, falling back to scalar event fields."""
        events = Manager._collab_from_pending_context(ctx)
        if events is None:
            events = Manager._collab_from_event_inbox(ctx)
        if not events:
            return ""
        lines = [_L(ctx, "## 📥 待处理事件上下文 (Event)", "## 📥 Pending Event Context")]
        lines.append(
            _L(
                ctx,
                "> 你收到了事件，请先判断每条是否落在你的职责范围内：无关的事件忽略"
                "（不据此调整计划），相关的才结合自身任务与职责理解其含义并纳入本次展开决策。",
                "> You received events. First judge whether each falls within your "
                "responsibilities: ignore irrelevant ones (do not adjust your plan for "
                "them); only interpret and factor in the relevant ones.",
            )
        )
        for index, event in enumerate(events, 1):
            if len(events) > 1:
                lines.append(f"### {_L(ctx, '事件', 'Event')} {index}")
            event_id = event.get("event_id") or ""
            producer = event.get("event_producer") or event.get("producer") or ""
            content = (
                event.get("event_content")
                or event.get("objective")
                or event.get("content")
                or ""
            )
            intent = event.get("event_intent") or event.get("intent") or ""
            scope = event.get("event_scope") or event.get("scope") or ""
            action = event.get("event_action") or event.get("action") or ""
            affected_roles = event.get("event_affected_roles") or event.get("affected_roles") or []
            if event_id:
                lines.append(f"- event_id: `{event_id}`")
            if producer:
                lines.append(f"- {_L(ctx, '事件来源 node', 'Event source node')}: `{producer}`")
            if intent:
                lines.append(f"- {_L(ctx, '事件性质', 'Event intent')}: {intent}")
            if scope:
                lines.append(f"- {_L(ctx, '影响范围', 'Event scope')}: {scope}")
            lines.append(f"- {_L(ctx, '事件描述', 'Event objective')}: {content}")
            if action:
                lines.append(f"- {_L(ctx, '建议动作', 'Event action')}: {action}")
            if affected_roles:
                lines.append(f"- {_L(ctx, '受影响角色', 'Affected roles')}: {affected_roles}")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _build_repair_context_block(ctx: ExecutableContext) -> str:
        """修复上下文：pending_context 为 repair 类型时，渲染缺陷明细块。

        数据来源：submit_fix_plan 续接写入的 pending_context
        （collaboration_type="repair" + defects[{step_ref, reason, fix_role, fix_task,
        file, line, desc, expected_action, check}]，见 runtime._build_fix_pending_context）。

        触发场景：本 Manager 被父级 review 判失败 → 原地续接重置 READY → 重新 expand。
        此时它带着"哪里坏了"的明细重新规划，配合 Revise 候选块，在既有子节点之上
        选择有能力修复的子节点复用，或新增节点补能力。空守卫保证非 repair 上下文
        字节级不变。
        """
        pending = getattr(ctx.node, "pending_context", None) or {}
        if pending.get("collaboration_type") != "repair":
            return ""
        defects = pending.get("defects")
        if not defects:
            return ""
        lines = [_L(ctx, "## 🔧 修复要求 (Repair)", "## 🔧 Repair Requirements")]
        reason = pending.get("activation_reason")
        if reason:
            lines.append(f"> {reason}")
            lines.append("")
        for idx, d in enumerate(defects, 1):
            lines.append(f"{idx}. step_ref: `{d.get('step_ref', '')}`")
            if d.get("reason"):
                lines.append(f"   - {_L(ctx, '原因', 'Reason')}: {d['reason']}")
            if d.get("file") or d.get("line"):
                loc = d.get("file") or ""
                if d.get("line"):
                    loc = f"{loc}:{d['line']}" if loc else str(d["line"])
                lines.append(f"   - {_L(ctx, '位置', 'Location')}: {loc}")
            if d.get("desc"):
                lines.append(f"   - {_L(ctx, '说明', 'Description')}: {d['desc']}")
            if d.get("fix_role"):
                lines.append(f"   - {_L(ctx, '修复角色', 'Fix role')}: {d['fix_role']}")
            if d.get("fix_task"):
                lines.append(f"   - {_L(ctx, '修复任务', 'Fix task')}: {d['fix_task']}")
            if d.get("expected_action"):
                lines.append(f"   - {_L(ctx, '预期动作', 'Expected action')}: {d['expected_action']}")
            if d.get("check"):
                lines.append(f"   - {_L(ctx, '验收', 'Acceptance')}: {d['check']}")
        lines.append("")
        lines.append(
            _L(
                ctx,
                "请据此在既有子节点之上做增量修复（Revise）：优先复用有能力修复的子节点，"
                "仅在新能力处新建节点，绝不删除原有节点整体重新规划。",
                "Perform incremental repair (Revise) on top of existing child nodes: prefer "
                "reusing child nodes capable of the fix; only create new nodes for new "
                "capabilities; never delete the original nodes and replan from scratch.",
            )
        )
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _truncate(text: str, limit: int = 140) -> str:
        """截断文本到指定长度并加省略号."""
        s = (text or "").strip().replace("\n", " ")
        if len(s) <= limit:
            return s
        return s[:limit].rstrip() + "…"

    @staticmethod
    def _status_emoji(status) -> str:
        """把 NodeStatus 映射成可视化emoji便于一眼看清状态."""
        from runtime.models import NodeStatus as NS
        mapping = {
            NS.RUNNING: "🔄",
            NS.WAITING: "⏸️",
            NS.COMPLETED: "✅",
            NS.FAILED: "❌",
        }
        return mapping.get(status, "?")

    def _build_execution_context_snapshot(self, ctx: ExecutableContext) -> str:
        """生成当前节点的祖先链 + 同父兄弟 + 已完成兄弟产物路径的格式化文本.

        Returns:
            str: 多行 markdown 文本; 若节点是root且无任何兄弟则返回空串.
        """
        runtime = ctx.runtime
        me = ctx.node

        lines = ["## 执行上下文快照(Context Snapshot)", "",
                 "> 本段由系统自动注入供你做决策参考. 你必须先阅读此部分再开始决策流程.",
                 "> 特别注意「已完成」状态的兄弟分支产物可被复用以避免重复劳动; "
                 "若兄弟声明了 shared_workspace，其协作区路径在快照中给出，可直接复用共享分区真实产物.", ""]

        # ---- Part0: 团队目标(根节点 task 全文) ----
        # 祖先链里每个节点都截到 130 字 —— 那是一条**索引**, 不是正文, 用在链上合适.
        # 但目标不能只给 130 字: Manager 唯一的职责就是判断「派哪些角色、切成哪几步」,
        # 它必须看全目标; 而且层级越深, 链上那段摘要离真正的目标越远(对嵌套 Manager 来说
        # 那已经是"某一步的 step 描述"了). 所以根节点的 task 单独成段给全文, 不截断.
        # 本节点自己就是 root 时 root_task 为空, 该段跳过 —— 那种情况下本节点的 task
        # 本身就是团队目标, 见 prompt 末尾的「## 你的任务」.
        root_task = ""
        cur = me
        seen_root = {me.id}
        while cur.parent_id and cur.parent_id not in seen_root:
            par = runtime.get_node(cur.parent_id)
            if par is None:
                break
            seen_root.add(par.id)
            root_task = par.task or ""
            cur = par
        if root_task:
            lines.append("### 〇、团队目标(根节点任务全文)")
            lines.append(root_task)
            lines.append("")

        # ---- Part1: 祖先链 (从root到本节点的直系上级)
        ancestors = []
        cur = me
        seen_ids = {me.id}
        while cur.parent_id and cur.parent_id not in seen_ids:
            par = runtime.get_node(cur.parent_id)
            if par is None:
                break
            seen_ids.add(par.id)
            ancestors.append(par)
            cur = par
        ancestors.reverse()   # 让最顶层在最前

        lines.append("### 一、祖先链(root → 本节点的直接上级)")
        if not ancestors:
            lines.append("(无 —— 本节点为根 Manager)")
        else:
            for i, anc in enumerate(ancestors):
                etype_short = anc.exec_type[:3] if hasattr(anc,'exec_type') else "?"
                role_s = getattr(anc, 'role', '-') or '-'
                step_ref = getattr(anc, 'step_ref', '-') or '(root)'
                stt = self._status_emoji(getattr(anc, 'status', None))
                tt = self._truncate(getattr(anc, 'task', ''), 130)
                indent = "  " * i
                marker = ""
                lines.append(f"{indent}[d{i}]{marker} {stt} [{etype_short.upper()}|role={role_s}|step={step_ref}]")
                lines.append(f"{indent}      task: {tt}")
            my_depth = len(ancestors)
            indent_me = "  " * my_depth
            my_role = getattr(me, 'role', '-') or '-'
            my_step = getattr(me, 'step_ref', '-') or '-'
            # 本节点自己的 task 也给全文(保留换行并逐行缩进, 维持树的形状).
            # 链上**别人**的任务当索引用, 截断无妨; 但"我这一步要干什么"是 Manager 做规划
            # 最需要的信息, 砍在 130 字上等于让它在信息不完整的情况下决策.
            # 标签与 Part0 的「团队目标」成对: 一个是团队级, 一个是个人级.
            my_task = (getattr(me, 'task', '') or '').strip()
            lines.append(f"{indent_me}← [你在这里 d{my_depth}] [MGR|role={my_role}|step={my_step}]")
            lines.append(f"{indent_me}      个人当前目标:")
            for task_line in (my_task.splitlines() or [""]):
                lines.append(f"{indent_me}      {task_line}")
        lines.append("")

        # ---- Part2: 同父兄弟节点列表 ----
        siblings = []
        if me.parent_id:
            all_kids = runtime.get_children(me.parent_id)
            siblings = [k for k in all_kids if k.id != me.id]

        lines.append(f"### 二、同父兄弟节点({len(siblings)}个)")
        MAX_SIBLINGS_SHOW = 12
        truncated_count = max(0, len(siblings) - MAX_SIBLINGS_SHOW)
        shown = siblings[:MAX_SIBLINGS_SHOW]

        if not shown:
            lines.append("(无 —— 本节点是其父的唯一子节点)")
        else:
            # 排序: completed优先 → running → waiting → failed
            priority = {
                NodeStatus.COMPLETED: 0,
                NodeStatus.RUNNING: 1,
                NodeStatus.WAITING: 2,
                NodeStatus.FAILED: 4,
            }
            shown.sort(key=lambda n: (priority.get(n.status,99), n.step_ref or ''))

            for k in shown:
                ks = self._status_emoji(k.status)
                ket = (getattr(k,'exec_type','?')[:3]).upper()
                kr = getattr(k,'role','-') or '-'
                ksr = getattr(k,'step_ref','-') or '-'
                kt = self._truncate(k.task,120)

                line = f"- {ks} [{ket}|role={kr}|step={ksr}] status={str(k.status).lower().split('.')[-1]}"
                line += f"\n     task摘要:{kt}"
                # 如果有artifact_path则展示产物位置
                ap = getattr(k, 'artifact_path', None)
                if k.status == NodeStatus.COMPLETED and ap:
                    line += f"\n     ★已完成★ artifact_path={ap}"
                    # 若兄弟声明了共享工作区，标注协作区路径供复用
                    k_shared = (k.config or {}).get("_shared_workspace")
                    if k_shared:
                        line += f"\n     📁 共享工作区: {k_shared}"
                    # 同时尝试推断workspace目录作为复用入口
                    import os.path as osp
                    workspaces_root_marker = '/workspaces/'
                    idx = ap.find(workspaces_root_marker)
                    ws_hint = ''
                    if idx >= 0:
                        end = ap.find('/', idx+len(workspaces_root_marker))
                        if end > idx:
                            node_uuid = ap[idx+len(workspaces_root_marker):end]
                            ws_full = ap[:end]
                            ws_hint = (
                                f"\n     可参考目录(workspaces/{node_uuid}/)"
                                f": 下含 cc_output.md / best_solution_summary.txt / "
                                f"{osp.basename(ws_full)} 等产出物"
                            )
                            line += ws_hint
                elif k.status in (NodeStatus.WAITING,):
                    line += "\n     ⛔未触底 该Mgr尚未展开自己的ExecutionPlan"
                elif k.status == NodeStatus.RUNNING and ket=='WRK':
                    line += "\n     🟢正在执行中..."
                lines.append(line)

            if truncated_count > 0:
                lines.append(f"... 还有 {truncated_count} 个兄弟因篇幅省略不列.")
        lines.append("")

        # ---- Part3: 可复用产物清单汇总 ----
        reusable = []
        for sib in siblings:
            if sib.status != NodeStatus.COMPLETED:
                continue
            ap = getattr(sib, 'artifact_path', None)
            entry = {
                'sibling_node_id': sib.id,
                'sibling_step': getattr(sib,'step_ref','-'),
                'sibling_role': getattr(sib,'role','-'),
                'sibling_exec_type': getattr(sib,'exec_type','?'),
                'artifact_path': ap,
                'task_summary': self._truncate(sib.task,100),
                'shared_workspace': (sib.config or {}).get('_shared_workspace'),
            }
            if not ap:
                continue
            # 进一步尝试定位 workspace 根目录用于 sibling_artifacts 引用
            m = re.search(r'/workspaces/[a-fA-F0-9-]+/', ap)
            entry['workspace_root'] = m.group(0) if m else None
            reusable.append(entry)

        lines.append(f"### 三、可直接复用的已完成兄弟产物清单({len(reusable)}项)")
        if not reusable:
            lines.append("(无可复用兄弟产物)")
            lines.append("")
            lines.append("> 说明: 这意味着你的Capability在已展开的同层范围内还没有先例工作. "
                         "若你认为某些主题可能与更深层的堂表亲重合,"
                         "可在Phase B阶段主动声明'我无法访问跨层级历史但承诺避免重复劳动'.")
        else:
            lines.append("")
            lines.append("#### 📦 可复用兄弟产物")
            lines.append("")
            lines.append("**重要**: 当你在Phase B识别出的某项 Capability 与下列任一产物的任务高度重合时,")
            lines.append("应当在本Plan里设置 config.sibling_artifacts=[\"<绝对路径>\"] 显式引用该外部输入,")
            lines.append("并在该 step 的 task 字段首句标注 \"refine based on prior artifact <sid>\" 表明这是迭代而非推倒重来;")
            lines.append("若新工作的范围与既有产物完全重叠应改为合并到已有分支而非新建独立 manager 子树.")
            lines.append("")
            for r in reusable:
                paths_block = []
                if r['workspace_root']:
                    paths_block.append(r['workspace_root'])
                if r['artifact_path'] and r['artifact_path'] != r['workspace_root']:
                    paths_block.append(r['artifact_path'])
                if r['shared_workspace']:
                    paths_block.append(r['shared_workspace'])
                lines.append(
                    f"- sid=`{r['sibling_step']}` exec_type={r['sibling_exec_type']} "
                    f"role={r['sibling_role']} node_id={r['sibling_node_id']}"
                )
                lines.append(f"    任务概述: {r['task_summary']}")
                lines.append("    建议引用路径:")
                for p in dict.fromkeys(paths_block):
                    lines.append(f"      • `{p}`")

        lines.append("")
        lines.append("---")
        lines.append("*以上即为本轮 expand 阶段的全部已知组织上下文.*")
        lines.append("*接下来请进入 Phase A→H 工作流基于这些事实+上方generate_instruction协议产出 ExecutionPlan JSON.*")

        return "\n".join(lines)

    @staticmethod
    def _build_review_goal_block(ctx: ExecutableContext) -> str:
        """Review 新会话的目标上下文：团队目标（根任务全文）+ 本节点当前目标。

        Review 不再继承 Expand 的会话，评审「子节点产出是否满足目标」所需的目标
        必须由 prompt 自己带上。两级目标均给全文，与 Expand 的快照口径一致。
        """
        runtime = ctx.runtime
        me = ctx.node
        root_task = ""
        cur = me
        seen = {me.id}
        while cur.parent_id and cur.parent_id not in seen:
            parent = runtime.get_node(cur.parent_id)
            if parent is None:
                break
            seen.add(parent.id)
            root_task = parent.task or ""
            cur = parent

        lines = [_L(ctx, "## Review Context（评审目标）", "## Review Context")]
        if root_task.strip():
            lines.extend([
                "",
                _L(ctx, "### 团队目标（根节点任务全文）", "### Team goal (root task, full text)"),
                root_task.strip(),
            ])
        if (me.task or "").strip():
            lines.extend([
                "",
                _L(
                    ctx,
                    "### 本节点当前目标（子节点产出须共同满足它）",
                    "### This node's current goal (the children's outputs must jointly satisfy it)",
                ),
                me.task.strip(),
            ])
        return "\n".join(lines) if len(lines) > 1 else ""

    @staticmethod
    def _build_review_prompt(
        ctx: ExecutableContext,
        children_info: list[dict],
    ) -> str:
        """构造 Review 阶段 prompt — 先识别关键产物，再输出结构化 JSON。

        每个 child_info 包含 step_ref、role、task、status 和 Shared Workspace 分区。
        AICU 只定向检查与目标直接相关的关键产物，不遍历全部子节点输出；结果用于
        精准修复而非全量重建。
        """
        node_ref = ctx.node.step_ref or ctx.node.id
        parts = [
            "## Review Task",
            _L(
                ctx,
                f"当前评审节点 step_ref: `{node_ref}`",
                f"Current review node step_ref: `{node_ref}`",
            ),
            _L(
                ctx,
                "这是一个全新的评审会话，之前的规划对话不会带入：请仅依据下方的目标、"
                "子节点任务与状态进行评审；不要重新展开主任务。",
                "This is a fresh review session; the earlier planning conversation is not "
                "carried over. Review using only the goals and the children's tasks and "
                "statuses below; do not re-expand the main task.",
            ),
            "",
        ]
        goal_block = Manager._build_review_goal_block(ctx)
        if goal_block:
            parts.extend([goal_block, ""])
        event_block = Manager._build_event_context_block(ctx)
        if event_block:
            parts.extend([event_block, ""])
        parts.extend([
            "## Children to Review",
            "",
        ])

        for i, child in enumerate(children_info):
            parts.append(
                f"### {i + 1}. {child['step_ref']} "
                f"(exec_type: {child.get('exec_type', 'worker')}, "
                f"role: {child.get('role', 'unknown')})"
            )
            parts.append(f"**Task**: {child.get('task', 'N/A')}")
            if child.get("status") is not None:
                parts.append(
                    f"**Status**: {getattr(child.get('status'), 'value', child.get('status'))}"
                )
            if child.get("shared_workspace"):
                partition = partition_of(
                    child["shared_workspace"], child.get("output_path") or ""
                )
                parts.append(f"**Shared Workspace Partition**: {partition}")
            parts.append("")
            parts.append(
                _L(
                    ctx,
                    "按下方「关键产物检查流程」，仅检查该分区的关键产物，不要遍历全部产出、源码或日志。",
                    "Follow the \"Key-deliverable inspection flow\" below: inspect only this partition's key "
                    "deliverables; do not traverse all outputs, source files, or logs.",
                )
            )
            parts.append("")

        # 方案三：Manager 在 review 阶段也可能发起跨 Manager 协作，协作意图由
        # Event.objective 承载，从事件发布目录写入。注入与 expand prompt 同源的事件发布
        # 指令，供下方「Optional — cross-node collaboration」引用（见上方「事件发布」）。
        # V3.1 P0#4：review 阶段同样补事件收件箱块（收到其他 Manager 事件时可见）。
        try:
            from executables.worker import Worker as _Worker
            event_publish_block = _Worker._build_event_publish_instructions(ctx.node)
            event_inbox_block = _Worker._build_event_inbox_instructions(ctx.node)
        except Exception:  # noqa: BLE001
            event_publish_block = ""
            event_inbox_block = ""
        if event_publish_block:
            parts.append(event_publish_block)
        if event_inbox_block:
            parts.append(event_inbox_block)

        parts.extend([
            "---",
            "",
            "## Instructions",
            "",
            _L(
                ctx,
                "**关键产物检查流程（对每个子节点依次执行）**:",
                "**Key-deliverable inspection flow (apply to each child)**:",
            ),
            _L(
                ctx,
                "1. 先根据子节点 task 与当前整体目标，判断其分区中哪些文件/目录是完成目标所必需的关键产物；",
                "1. Based on each child's task and the overall goal, first judge which files/directories in its "
                "partition are essential deliverables;",
            ),
            _L(
                ctx,
                "2. 查看该分区的目录/文件清单或已有产物索引，完成关键产物筛选；",
                "2. View the partition's directory/file listing or existing artifact index to confirm and complete "
                "the key-deliverable selection;",
            ),
            _L(
                ctx,
                "3. 只读取筛选出的关键产物的内容做目标一致性判断；关键产物本身是目录时，先查看其清单，再只打开与目标相关的文件；",
                "3. Read the content of only the selected key deliverables to judge goal alignment; if a key "
                "deliverable is itself a directory, view its listing first, then open only the goal-relevant files;",
            ),
            _L(
                ctx,
                "4. 不遍历全部产出、无关源码或完整日志；未读取的非关键文件缺失不判失败。",
                "4. Do not traverse all outputs, unrelated source files, or full logs; missing non-key files that "
                "you did not read must not cause failure.",
            ),
            _L(
                ctx,
                "若无法从目录清单确定关键产物，按“无法确认”处理，并在该子节点 assessment 的 reason 中说明，"
                "不要读取整个 Shared Workspace。",
                "If you cannot determine the key deliverables from the directory listing, treat them as "
                "\"unconfirmed\" and state so in that child's assessment reason; do not read the entire shared "
                "workspace.",
            ),
            _L(
                ctx,
                "**独立运行验收**：当关键产物可以运行/演示/观察（如可执行脚本、可启动程序、可渲染页面）时，"
                "实际运行或观察它的真实表现后再判定通过；不要只凭静态描述或“看起来对”就放行。"
                "若运行或检查中出现异常信号，必须先当作产物缺陷复核，再决定是否判过。",
                "**Independent runtime acceptance**: when a key deliverable is runnable/observable "
                "(e.g. an executable script, launchable program, or renderable page), run or observe its "
                "actual behavior before passing it; do not pass based only on static description or "
                "\"it looks fine\". If any anomaly appears during run or inspection, treat it as a "
                "potential product defect and re-verify before deciding to pass.",
            ),
            "",
            "Evaluate each child's output against its task description.",
            "For each child, determine whether the output meets the requirements.",
            "",
            "**Output ONLY a JSON object** with this exact structure (no markdown fences, no extra text):",
            "",
            "{",
            '  "overall_passed": true,',
            '  "assessments": [',
            "    {",
            '      "step_ref": "<exact step_ref from above>",',
            '      "passed": true,',
            '      "reason": "<brief explanation of why it passed or failed>"',
            "    },",
            "    {",
            '      "step_ref": "<exact step_ref from above>",',
            '      "passed": false,',
            '      "reason": "<what specifically is wrong>",',
            '      "fix": {',
            '        "role": "<role name for the fix worker>",',
            '        "task": "<concrete, actionable fix task description>"',
            "      }",
            "    }",
            "  ]",
            "}",
            "",
            _L(
                ctx,
                "**Optional — cross-node collaboration (方案三, 经事件总线)**:",
                "**Optional — cross-node collaboration (via event bus)**:",
            ),
            _L(
                ctx,
                "当本节点的整体任务需要某个兄弟节点的协作才能闭合（例如:",
                "When this node's overall task requires a sibling node's collaboration to close "
                "(for example:",
            ),
            _L(
                ctx,
                "你是需求 Manager，评审发现实现计划 Manager 的产出需要你的需求评审结论做输入；",
                "you are a requirements Manager and find that the implementation-plan Manager's "
                "output needs your requirements-review conclusion as input;",
            ),
            _L(
                ctx,
                "或你是实现计划 Manager，评审发现需求 Manager 的需求变更需要你重新排期），",
                "or you are the implementation-plan Manager and find that a requirements change "
                "needs you to reschedule),",
            ),
            _L(
                ctx,
                "且所有子节点本身均已通过评审时，**不要**在 JSON 中附加 collaboration 字段，",
                "and all child nodes have already passed review, do **not** attach a collaboration "
                "field in the JSON;",
            ),
            _L(
                ctx,
                "改为在事件发布目录（见上方的「事件发布」指令，`{node_ws}/events/`）写入一个 JSON 事件文件，",
                "instead write a JSON event file to the event-publish directory (see the "
                "\"event publish\" instructions above, `{node_ws}/events/`),",
            ),
            "格式：",
            '{"objective": "<简短描述你希望协作的意图（≤3 句）>",',
            '"intent": "collaboration",',
            '"affected_roles": [{"role": "<岗位名（不是节点名）>", "duty": "<该岗位需注意的事项>"}],',
            '"scope": "related",',
            '"action": "<期望对方做什么及其原因>"}',
            _L(
                ctx,
                "仅当你确实需要向兄弟节点发起协作时才发布事件。",
                "Only publish an event when you truly need to initiate collaboration with a "
                "sibling node.",
            ),
            "",
            "Rules:",
            '- "overall_passed" must be true ONLY if ALL children passed',
            '- For FAILED children, "fix.role" should be an appropriate role for the fix task',
            '- For FAILED children, "fix.task" must be a concrete, self-contained task description',
            '- Include EVERY child in the assessments array',
            '- "step_ref" must exactly match the child identifier shown above',
            '- Output raw JSON directly, no markdown code fences',
        ])

        return "\n".join(parts)

    def _write_review_output(
        self,
        ctx: ExecutableContext,
        content: Any,
        artifacts: dict[str, str],
        iteration_count: int,
    ) -> str:
        """将 AICU review 结果写入 review_v{iteration_count}.md。

        契约感知：优先从评审契约 dict（overall_passed / assessments）提取真实判定，
        避免 real 模式元数据 dict 导致空模板；元数据 dict（summary / audit_passed /
        defects）与纯文本场景回退原逻辑。

        Args:
            ctx: 执行上下文。
            content: _resolve_review_content 解析出的评审内容（dict 或 str）。
            artifacts: 子节点 Artifact 字典。
            iteration_count: 当前迭代次数。

        Returns:
            review 文件的绝对路径。
        """
        workspace_getter = getattr(ctx.runtime, "get_node_workspace", None)
        # 兜底用 `or` 而不是 dict.get 的默认值：`workspace_dir` **存在但为 None**
        # （yaml 里写了 `workspace_dir:` 或前端传 null）时，get 的默认值不生效，
        # 会返回 None，紧接着 os.path.join(None, filename) 直接 TypeError。
        # 兜底路径与 worker.py 的首次执行保持一致（{cwd}/workspaces/{node_id}）。
        workspace = workspace_getter(ctx.node.id) if callable(workspace_getter) else (
            ctx.config.get("workspace_dir")
            or os.path.join(os.getcwd(), "workspaces", ctx.node.id)
        )
        filename = f"review_v{iteration_count}.md"
        filepath = os.path.join(workspace, filename)

        # 契约感知解析 review 结果
        if isinstance(content, dict):
            if "assessments" in content or "overall_passed" in content:
                # 评审契约 dict
                summary = content.get("summary", "")
                audit_passed = bool(content.get("overall_passed", True))
                defects = content.get("defects", [])
                assessments = content.get("assessments", [])
                if not summary and isinstance(assessments, list) and assessments:
                    passed = sum(1 for a in assessments if a.get("passed"))
                    summary = f"{passed}/{len(assessments)} children passed"
                reviewed = [
                    a.get("step_ref") for a in assessments
                    if a.get("step_ref")
                ] or list(artifacts)
            else:
                # 元数据 dict 回退（summary / audit_passed / defects）
                summary = content.get("summary", "")
                audit_passed = content.get("audit_passed", content.get("review_passed", True))
                defects = content.get("defects", [])
                reviewed = list(artifacts)
        else:
            summary = str(content)[:2000] if content else ""
            audit_passed = True
            defects = []
            reviewed = list(artifacts)

        # 构造 markdown 内容
        lines = [
            f"# Review Report (iteration {iteration_count})",
            "",
            f"**Audit Passed**: {audit_passed}",
            "",
            f"**Summary**: {summary}",
            "",
        ]

        if defects:
            lines.append("**Defects**:")
            for d in defects:
                lines.append(f"- {d}")
            lines.append("")

        if reviewed:
            lines.append(f"**Artifacts Reviewed**: {len(reviewed)}")
            for step_ref in reviewed:
                lines.append(f"- {step_ref}")
            lines.append("")

        lines.append("---")
        lines.append(f"*Generated at {datetime.now().isoformat()}*")
        lines.append("")

        markdown_text = "\n".join(lines)

        # 写入文件
        os.makedirs(workspace, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(markdown_text)

        logger.info(
            "Review output written to %s for node '%s'",
            filepath, ctx.node.id,
        )
        return filepath

    @staticmethod
    def _parse_review_assessments(
        raw_output: Any,
        children_info: list[dict],
    ) -> tuple[list[dict], str]:
        """解析 AICU 的结构化评估输出。

        期望格式:
        {
          "overall_passed": false,
          "assessments": [
            {"step_ref": "s1", "passed": true, "reason": "..."},
            {"step_ref": "s2", "passed": false, "reason": "...",
             "fix": {"role": "default", "task": "fix X by doing Y"}}
          ]
        }

        Returns:
            (assessments, status)：
              - status="parsed"       已成功解析出逐子节点评估（可能含失败项）。
              - status="unparseable"  无法解析出 assessments（JSON 缺失/空/非法）。
                此时返回全量失败清单；调用方应触发修复性重试或按 AICU 元数据降级，
                避免"格式失败→重建→再格式失败"的无效循环（历史问题：Review AICU
                输出成 markdown/产物回显而非 JSON 契约，导致 Manager 反复重建至
                max_iterations 而 FAILED）。
        """
        # 提取 JSON（可能被 markdown fences 包裹 / 文末嵌入 / AICU result 元数据 dict）
        if isinstance(raw_output, dict):
            # AICU.ask() 在 real 模式返回的是 result 元数据 dict（scores/final_output_path
            # 等，不含 assessments）。若 dict 无 assessments 但带 final_output_path 且文件
            # 可读，先读取实际 review 产出内容再解析（与 _parse_plan 的 final_output_path
            # 分支同模式）——否则 assessments 恒为空，永远降级为全量失败。
            if not raw_output.get("assessments"):
                output_path = raw_output.get("final_output_path")
                if (
                    output_path
                    and isinstance(output_path, str)
                    and os.path.isfile(output_path)
                ):
                    try:
                        with open(output_path, "r", encoding="utf-8") as f:
                            raw_output = f.read()
                    except OSError as exc:
                        logger.warning(
                            "_parse_review_assessments: failed to read review output "
                            "file %s: %s", output_path, exc,
                        )
            if isinstance(raw_output, dict):
                data = raw_output
            else:
                data = _extract_review_json(str(raw_output))
        elif isinstance(raw_output, str):
            data = _extract_review_json(raw_output)
        else:
            data = None

        if data is None:
            logger.warning(
                "_parse_review_assessments: failed to parse AICU output as JSON, "
                "returning full-failure (status=unparseable)"
            )
            return (
                [
                    {
                        "step_ref": c["step_ref"],
                        "passed": False,
                        "reason": "AICU output unparseable",
                        "fix": {"role": c.get("role", "default"), "task": c.get("task", "")},
                    }
                    for c in children_info
                ],
                "unparseable",
            )

        assessments = data.get("assessments", [])

        # 无逐项 assessments，但输出 dict 携带明确整体结论（legacy review_passed /
        # passed / overall_passed）：按整体结论构造逐子节点评估。这是真实判定
        # （review 确实给出了结论），不应误入"格式失败"降级路径。
        if not assessments and isinstance(data, dict):
            verdict = data.get("review_passed", data.get("passed"))
            if verdict is None and "overall_passed" in data:
                verdict = bool(data.get("overall_passed"))
            if verdict is not None:
                defects = data.get("defects") or data.get("audit_defects") or []
                if verdict:
                    return (
                        [
                            {"step_ref": c["step_ref"], "passed": True,
                             "reason": "Overall review passed"}
                            for c in children_info
                        ],
                        "parsed",
                    )
                reason = "; ".join(str(d) for d in defects) if defects else "Overall review failed"
                return (
                    [
                        {
                            "step_ref": c["step_ref"],
                            "passed": False,
                            "reason": reason,
                            "fix": {"role": c.get("role", "default"), "task": c.get("task", "")},
                        }
                        for c in children_info
                    ],
                    "parsed",
                )

        # 若 assessments 为空或缺失且无整体结论，视为格式失败（非质量信号），
        # 交由调用方修复重试 / 按元数据降级，而不是直接触发全量重建。
        if not assessments:
            logger.warning(
                "_parse_review_assessments: empty assessments array, "
                "returning full-failure (status=unparseable)"
            )
            return (
                [
                    {
                        "step_ref": c["step_ref"],
                        "passed": False,
                        "reason": "No assessment provided",
                        "fix": {"role": c.get("role", "default"), "task": c.get("task", "")},
                    }
                    for c in children_info
                ],
                "unparseable",
            )

        # 补全缺失的 step_ref（AICU 可能遗漏某些子节点）
        assessed_refs = {a.get("step_ref", "") for a in assessments}
        for c in children_info:
            if c["step_ref"] not in assessed_refs:
                assessments.append({
                    "step_ref": c["step_ref"],
                    "passed": False,
                    "reason": "Not assessed by reviewer",
                    "fix": {"role": c.get("role", "default"), "task": c.get("task", "")},
                })

        # 为每个 assessment 确保 fix 字段存在
        for a in assessments:
            if not a.get("passed", False) and "fix" not in a:
                original = next(
                    (c for c in children_info if c["step_ref"] == a.get("step_ref")),
                    None,
                )
                a["fix"] = {
                    "role": original.get("role", "default") if original else "default",
                    "task": original.get("task", a.get("reason", "Fix issue")) if original else "Fix issue",
                }

        return assessments, "parsed"

    @staticmethod
    def _build_repair_review_prompt(original_prompt: str) -> str:
        """构造修复性 Review 重试 prompt — 要求 AICU 严格输出 JSON 契约。

        不重复嵌入 artifact 内容（上轮 prompt 已含），只强调输出格式，
        让模型基于已读上下文补交正确格式的评估。
        """
        return (
            "你的上一轮 Review 输出无法解析为要求的 JSON 契约（很可能是 markdown/叙述"
            "文本，或产物回显而没有输出评估 JSON）。\n"
            "请重新输出 Review 结论，且**只**输出一个严格的 JSON 对象，"
            "不要 markdown 代码块、不要任何额外文字：\n\n"
            "{\n"
            '  "overall_passed": true,\n'
            '  "assessments": [\n'
            '    { "step_ref": "<子节点step_ref>", "passed": true, "reason": "..." },\n'
            '    { "step_ref": "<子节点step_ref>", "passed": false, '
            '"reason": "...", "fix": { "role": "...", "task": "..." } }\n'
            "  ]\n"
            "}\n\n"
            "要求：\n"
            "- 对上方 Children to Review 中的**每一个**子节点给出 exactly 一条 assessment；\n"
            '- "step_ref" 必须与上方一致；\n'
            '- 失败的项必须带 "fix.role" 与 "fix.task"；\n'
            "- 直接输出原始 JSON，不加 markdown fences。"
        )

    @staticmethod
    def _degrade_review(
        raw_output: Any,
        children_info: list[dict],
    ) -> tuple[list[dict], str]:
        """Review 输出格式失败（JSON 契约未满足、无任何判定）后的降级决策。

        AICU 的审计字段只为**确实跑过审计**的策略（CC≥4）写入 —— 没有审计阶段
        就不会伪造 `audit_passed=True`。以此区分三种情况：
          - 显式 audit_passed=True 且无缺陷 → 无已知质量问题，降级为**全量通过**，
            让 Manager 完成（避免格式失败触发无意义重建循环）。
          - 显式 audit_passed=False 或有缺陷 → 存在已知问题但无法解析逐项评估，
            降级为**快速失败**（格式失败会反复复现，重建无法自愈，不再重建）。
          - 无任何 audit 信号（CC<4 的结果、legacy stub / 异常 dict）→ 保持旧行为：
            全量失败，status="parsed" 走正常迭代修复路径（submit_fix_plan）。

        Returns:
            (assessments, status)：status ∈ {"degraded_pass", "unparseable_fail",
            "parsed"}。
        """
        if isinstance(raw_output, dict) and (
            "audit_passed" in raw_output
            or "audit_defects" in raw_output
            or "defects" in raw_output
        ):
            audit_passed = raw_output.get("audit_passed", True)
            defects = (
                raw_output.get("audit_defects")
                or raw_output.get("defects")
                or []
            )
            if audit_passed and not defects:
                logger.warning(
                    "Review output unparseable but audit_passed=True with no defects; "
                    "degrading to ALL-PASS for %d children "
                    "(format failure, not quality signal)",
                    len(children_info),
                )
                return (
                    [
                        {
                            "step_ref": c["step_ref"],
                            "passed": True,
                            "reason": (
                                "AICU review output unparseable; audit passed with no "
                                "defects — accepted with warning"
                            ),
                        }
                        for c in children_info
                    ],
                    "degraded_pass",
                )

            logger.error(
                "Review output unparseable and audit reported defects (%d); "
                "cannot produce targeted fixes — failing fast without rebuild",
                len(defects),
            )
            return (
                [
                    {
                        "step_ref": c["step_ref"],
                        "passed": False,
                        "reason": "AICU output unparseable and audit found defects",
                        "fix": {"role": c.get("role", "default"), "task": c.get("task", "")},
                    }
                    for c in children_info
                ],
                "unparseable_fail",
            )

        # 无任何 audit/verdict 信号：保持旧行为（全量失败 → 迭代修复）。
        logger.warning(
            "Review output unparseable and carries no audit/verdict signal; "
            "falling back to full failure (iterative fix)"
        )
        return (
            [
                {
                    "step_ref": c["step_ref"],
                    "passed": False,
                    "reason": "AICU output unparseable (no verdict)",
                    "fix": {"role": c.get("role", "default"), "task": c.get("task", "")},
                }
                for c in children_info
            ],
            "parsed",
        )
