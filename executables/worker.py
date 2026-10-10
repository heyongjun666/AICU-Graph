"""
executables/worker.py — Worker 执行单元

Worker 是 Executable 的具体实现，使用真实 AICU 实例执行任务；测试替身仅通过依赖注入提供。

Worker.run() 在线程池中执行 AICU.ask()，避免阻塞主线程调度循环。
"""

import glob
import json
import logging
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Optional

from executables.base import Executable, ExecutableContext
from executables.handoff_prompts import render_checkpoint, render_notes_instructions
from runtime.agent_events import event_queue_dirs
from runtime.handoff import SESSION_FRESH, notes_path, read_notes
from runtime.models import NodeStatus, ErrorObject, LogEntry, LogLevel
from runtime.workspace_resolver import resolve_partition

logger = logging.getLogger(__name__)


def _normalize_worker_artifact(ctx: ExecutableContext, artifact_path: str) -> str:
    """Keep a worker's declared result inside its node workspace.

    所有节点自动挂载共享区（config 含 ``_shared_workspace``，会话级单一共享根），
    V3-2.1 起共享分区 = Worker 产物唯一事实源：本函数只做「事实认定」，**绝不
    copy/move 修复事实**（Runtime 只证明事实，不修复事实）：
      - 声明路径已在分区内（CC 直写 / AICU shared_target 直写）→ 幂等返回
      - 声明路径在分区外 → 产物权威 = 分区（Runtime 事实），返回分区，不复制
    空分区不静默救援：下游读到空目录即事实。
    """
    workspace_getter = getattr(ctx.runtime, "get_node_workspace", None)
    if not artifact_path or not callable(workspace_getter):
        return artifact_path
    workspace = os.path.realpath(workspace_getter(ctx.node.id))
    source = os.path.realpath(
        artifact_path
        if os.path.isabs(artifact_path)
        else os.path.join(workspace, artifact_path)
    )

    # ---- 共享工作区分支（V3-2.1：分区 = 唯一产物权威，不复制） ----
    config = ctx.node.config or {}
    shared_root = config.get("_shared_workspace")
    if shared_root:
        partition = resolve_partition(config)
        os.makedirs(partition, exist_ok=True)
        # 已在分区内（CC 直写 / AICU shared_target）→ 幂等返回
        if os.path.commonpath([partition, source]) == partition:
            return source
        # 分区外声明 → 产物权威 = 分区（Runtime 事实），不复制；
        return partition

    # ---- 非共享分支（Legacy：Node workspace 兜底；字节级保留） ----
    if os.path.commonpath([workspace, source]) == workspace:
        return source
    if not os.path.exists(source):
        return artifact_path

    output_dir = os.path.join(workspace, "output")
    os.makedirs(output_dir, exist_ok=True)
    destination = os.path.join(
        output_dir, os.path.basename(source.rstrip(os.sep)) or "result"
    )
    if os.path.isdir(source):
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(source, destination)
    return destination


def _resolve_dep_artifact_path(ctx: ExecutableContext, dep) -> str:
    """依赖节点产出路径：node.artifact_path 直传，缺失时 workspace 兜底。"""
    artifact = getattr(dep, "artifact_path", "") or ""
    did = getattr(dep, "id", "") or "unknown"
    if artifact:
        # artifact_path 存的是相对「依赖节点自己工作区」的路径，消费者的 cwd 不同，必须还原成绝对路径。
        if not os.path.isabs(artifact):
            try:
                return os.path.join(ctx.runtime.get_node_workspace(did), artifact)
            except Exception:
                return artifact
        return artifact
    return f"workspaces/{did}/output/"


def _validate_model(model_name: Optional[str]) -> Optional[str]:
    """模型名纯透传：不做任何代码级兜底（不要代码兜底）。

    未显式指定时返回 None，交由解析链继续：角色 yaml 显式 model → 系统设置
    （_session_model）→ 目标 CLI 自身默认模型。非空模型名一律信任原样透传——
    模型名是否有效由目标 CLI 自行判断（无效名会在 CLI 调用时暴露为错误，
    而非被静默回退到某个写死的默认值）。
    """
    return model_name or None


def _log_cc_lifecycle(
    runtime: Any,
    node_id: str,
    cc_id: str,
    event: str,
    *,
    phase: Optional[str] = None,
    task: Optional[str] = None,
    level: LogLevel = LogLevel.INFO,
) -> None:
    """Write one durable CC lifecycle fact when the Runtime supports it.

    Older RuntimeGateway test doubles only expose ``append_node_log`` and should
    continue to receive stream output without being forced to implement the
    newer lifecycle API. Lifecycle logging is intentionally best effort: an
    observability failure must never change the execution result.
    """
    log_lifecycle = getattr(runtime, "log_cc_lifecycle", None)
    if not callable(log_lifecycle):
        return
    try:
        log_lifecycle(
            node_id,
            cc_id,
            event,
            phase=phase,
            task=task,
            level=level,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "Failed to append CC lifecycle log node=%s cc=%s event=%s",
            node_id,
            cc_id,
            event,
            exc_info=True,
        )


def _aicu_cc_ids(aicu: Any) -> list[str]:
    """Return stable display IDs for the CCs owned by an AICU instance."""
    ccs = getattr(aicu, "ccs", None)
    if isinstance(ccs, (list, tuple)):
        return [f"CC-{index}" for index, _ in enumerate(ccs)]
    # An injected AICU-compatible implementation may not expose CC identity;
    # wait for its activity callback rather than inventing a CC ID.
    return []


def _handle_cc_stream_output(
    node_id: str,
    task: Optional[str],
    runtime: Any,
    stream: str,
    message: str,
) -> None:
    """处理 CC 流式输出回调（real 模式 AICU 的 output_callback）。

    目的：让前端 log 面板默认只展示粗粒度日志，不展示 CC 内部每次
    工具调用 / 每段 assistant 文本的流式细节（粒度太细）。

    分流规则：
    - stream == "activity"（CC 生命周期 started/completed/failed）：
      → 只更新 cc_activity（活动视图），不写 node.log；
    - stderr 事件：→ 写入 node.log（ERROR 级，错误必须可见）；
    - stdout 流式事件：→ 默认不写 node.log；仅当环境变量
      AICU_NODE_LOG_STREAMING=1 时全量写入，供排查 CC 内部细节。
    """
    update_activity = getattr(runtime, "update_cc_activity", None)
    stream_cc_id, separator, output_stream = stream.partition(":")
    is_activity_stream = stream == "activity" or (
        separator and output_stream == "activity"
    )
    if is_activity_stream:
        try:
            activity = json.loads(message)
        except (TypeError, ValueError):
            activity = {}
        cc_id = activity.get("cc_id") or (
            stream_cc_id if stream_cc_id.startswith("CC-") else None
        )
        activity_status = activity.get("status", "running")
        activity_phase = activity.get("phase")
        activity_task = activity.get("task") or task
        if callable(update_activity) and cc_id:
            update_activity(
                node_id,
                cc_id,
                activity_status,
                phase=activity_phase,
                task=task,
            )

        # AICU emits this callback at the semantic CC-attempt boundaries. Keep
        # the live activity update and durable timeline entry in one callback,
        # instead of duplicating lifecycle logs in every peer strategy.
        lifecycle_event = {
            "started": "cc_started",
            "finished": "cc_completed",
            "completed": "cc_completed",
            "failed": "cc_failed",
            "timeout": "cc_failed",
        }.get(str(activity.get("event") or "").lower())
        if lifecycle_event is None:
            lifecycle_event = {
                "running": "cc_started",
                "completed": "cc_completed",
                "failed": "cc_failed",
            }.get(str(activity_status).lower())
        if cc_id and lifecycle_event:
            _log_cc_lifecycle(
                runtime,
                node_id,
                str(cc_id),
                lifecycle_event,
                phase=activity_phase,
                task=activity_task,
                level=(LogLevel.ERROR if lifecycle_event == "cc_failed" else LogLevel.INFO),
            )
        return
    cc_id, _, output_stream = stream.partition(":")
    cc_id = cc_id if cc_id.startswith("CC-") else None
    level = LogLevel.ERROR if output_stream == "stderr" else LogLevel.INFO
    log_streaming = os.environ.get("AICU_NODE_LOG_STREAMING", "0") == "1"
    append_log = getattr(runtime, "append_node_log", None)
    if callable(append_log) and (log_streaming or output_stream != "stdout"):
        try:
            append_log(node_id, level, message, cc_id=cc_id, phase="streaming", task=task)
        except TypeError:
            append_log(node_id, level, message)
    if callable(update_activity) and cc_id:
        update_activity(
            node_id, cc_id, "running", phase="streaming", task=task, latest_output=message
        )


def _create_real_aicu(ctx: ExecutableContext) -> Any:
    """在 real 模式下按节点 role + workspace + exec_type 创建/复用 AICU 实例。

    每个节点拥有独立的 workspace 目录（workspaces/{node_id}）。
    首次执行时创建新 AICU 并缓存；路由激活重跑时复用缓存的 AICU，
    CC agent 通过 conversation_id + --resume 保持会话连续性，
    使 agent 记住之前的代码、决策和上下文。
    """
    from aicu import AICU

    node = ctx.node

    # 路由激活路径：复用缓存的 AICU → CC 自动 --resume
    with Worker._aicu_cache_lock:
        cached = Worker._aicu_cache.get(node.id)
        if cached is not None:
            # 清除 pause 时设置的 _preserve_conversation 标志，
            # 此后再发生错误时正常重置 conversation_id
            for cc in cached.ccs:
                cc._preserve_conversation.clear()
            return cached

    # 首次执行：优先使用 Runtime 提供的会话隔离、父子嵌套 Workspace。
    workspace_getter = getattr(ctx.runtime, "get_node_workspace", None)
    workspace_dir = (
        workspace_getter(node.id)
        if callable(workspace_getter)
        else os.path.join(os.getcwd(), "workspaces", node.id)
    )

    def append_output(stream: str, message: str) -> None:
        _handle_cc_stream_output(node.id, node.task, ctx.runtime, stream, message)

    # skills 空列表视为「未配置」，回退角色 yaml 的 skills —— 否则前端未选
    # skill 时传入的空数组会覆盖角色默认（config.skills=[] 会顶掉 role yaml）。
    selected_skills = node.config.get("skills") or None
    # skills_dir 未显式配置时保持 None，交由 AICU 回退到角色 yaml 的
    # skills_dir 默认（~/.claude/skills）。此前这里兜底到仓库根 skills/
    # 目录，与角色配置不一致且该目录往往不存在，导致带 skill 的节点失败。
    skills_dir = node.config.get("skills_dir")
    # MCP server 名字列表；同样以 None 表示「未配置」，由 AICU 回退到角色 yaml 的 mcp。
    # 解析与连通性预检都由 AICU 做（见 runtime/mcp_manager.py 与 runtime/mcp_client.py），
    # 这里只透传名字。
    selected_mcp = node.config.get("mcp")

    # ---- V3-2.1 shared_target：Worker 有共享工作区 → CC 直写分区 ----
    # 共享分区 = CC 产物唯一权威（不再产生 nodes/{id}/output 副本）。
    # 多 CC（pair/triple/full）同样直写同一分区：_copy_best_to_cc0 对同分区
    # 自动跳过复制，peer 编排只需产生方落分区、审查方在自己 CC 工作区出 review。
    # shared_root 与 shared_target 分开传：前者只用于工作区域边界声明（Manager 节点
    # 同样有 _shared_workspace，但 shared_target 仍为 None —— 它不直写产物分区）。
    shared_root = (node.config or {}).get("_shared_workspace")
    shared_target = None
    if node.exec_type == "worker" and shared_root:
        shared_target = resolve_partition(node.config)

    aicu = AICU(
        role_type=node.exec_type,  # "manager" → roles/Manager/, "worker" → roles/Worker/
        parent_workspace=workspace_dir,
        role=node.role or "default",
        num_ccs=node.config.get("num_ccs"),
        model=_validate_model(node.config.get("model")),
        # 系统设置（会话配置）以独立键注入，与节点显式 model 区分，
        # 保证解析优先级：节点显式 > 角色 yaml > 系统设置。
        session_model=node.config.get("_session_model"),
        cli_name=node.config.get("cli_name"),
        audit_rounds=node.config.get("audit_rounds"),
        output_callback=append_output,
        api_base_url=node.config.get("api_base_url"),
        api_key_env=node.config.get("api_key_env"),
        provider_id=node.config.get("provider_id"),
        skills=selected_skills,
        skills_dir=skills_dir,
        mcp=selected_mcp,
        skip_permissions=node.config.get("skip_permissions", True),
        shared_target=shared_target,
        # 只喂边界声明，不影响产物归属判定（那是 shared_target 的职责）。
        shared_root=shared_root,
        # 会话候选角色池：绑定后 Manager 只看到池内角色的摘要。
        # 会话级属性（Runtime），无需逐节点透传 —— 子节点构造 AICU 时同样读到这里。
        candidate_roles=getattr(ctx.runtime, "candidate_roles", None),
    )
    with Worker._aicu_cache_lock:
        Worker._aicu_cache[node.id] = aicu
    return aicu


def _backfill_failed(ctx: ExecutableContext, exc: Exception, *, stage: str) -> NodeStatus:
    """把失败写回节点：error + 一条 ERROR 日志。

    startup（建 AICU）与 execution（跑任务）两处失败共用这一份，因为它们对用户是同一件
    事 —— 节点红了、原因在日志里。``stage`` 只影响日志措辞，让人一眼看出失败发生在建
    AICU 之前还是任务执行当中；两者的 error 兜底形状也刻意保持一致。
    """
    error = getattr(exc, "error", None) or ErrorObject.create_worker_execution_error()
    ctx.runtime.update_node_status(
        ctx.node.id,
        NodeStatus.FAILED,
        error=error,
        log=[
            LogEntry(
                ts=datetime.now(),
                level=LogLevel.ERROR,
                message=f"Worker {stage} failed: {exc}",
            )
        ],
    )
    return NodeStatus.FAILED


# 失败重试：把上一次的报错原文交给模型，让它自己判断怎么调整（例如得知模型不支持
# 图片输入后就不再看图），而不是在框架里为某一种报错写死规则。
_API_REJECTION_RE = re.compile(r"API Error:\s*400", re.IGNORECASE)
_FAILURE_PREFIX = "Worker execution failed:"
_COMPLETED_PREFIX = "Worker execution completed"
_MAX_ERROR_CHARS = 800


def _is_retryable_api_rejection(exc: BaseException) -> bool:
    """请求被 API 以 400 拒绝（内容层面的错误，换个做法可能就能过）；超时/鉴权/5xx 不在此列。"""
    return bool(_API_REJECTION_RE.search(str(exc)))


def _extract_error_text(message: str) -> str:
    """从失败日志里截取报错核心：有 API Error 就从它开始，否则取尾部，限长。"""
    message = (message or "").strip()
    if message.startswith(_FAILURE_PREFIX):
        message = message[len(_FAILURE_PREFIX):].strip()
    idx = message.find("API Error:")
    if idx >= 0:
        return message[idx:idx + _MAX_ERROR_CHARS]
    return message[-_MAX_ERROR_CHARS:]


def _last_failure_text(node: Any) -> str:
    """节点最近一次执行若以失败告终，返回其报错文本；之后已成功或没有失败记录则返回空串。"""
    for entry in reversed(getattr(node, "log", None) or []):
        message = getattr(entry, "message", "") or ""
        if message.startswith(_COMPLETED_PREFIX):
            return ""
        if message.startswith(_FAILURE_PREFIX):
            return _extract_error_text(message)
    return ""


def _render_previous_failure(error_text: str) -> str:
    return (
        "## ⚠️ 上一次执行失败\n\n"
        "上一次执行报错如下：\n\n"
        f"```\n{error_text}\n```\n\n"
        "请先判断报错原因，并调整方案避免再次报错"
        "（例如报错表明模型不支持图片输入，就不要再读取或查看任何图片、不做需要看图的验证，"
        "只做不依赖视觉的验证，并如实写明哪些内容未做视觉验证）。\n"
        "上一次可能已在交付目录和工作目录留下部分产出：先检查已有文件，在其基础上继续，不必从头重做。"
    )


def _fail_startup(ctx: ExecutableContext, exc: Exception) -> NodeStatus:
    """AICU 构造期失败 → 节点 FAILED + 可读日志。

    ``_create_real_aicu`` 是在 Worker.run() 的 try 之外调用的：异常若直接上抛，会被
    scheduler 的 wrapper 收成一行服务端 stdout 日志，节点上只留下「Failed、日志为空、
    error 为空」。而这一类失败（MCP 预检连不上、skills_dir 不存在、角色 yaml 非法）
    恰恰都要用户去改环境或配置才能修，看不见原因就等于修不了。

    与执行期唯一的差别是这里多兜一层异常：回填本身若再抛出，就退回「日志为空」，
    等于白做。
    """
    try:
        return _backfill_failed(ctx, exc, stage="startup")
    except Exception:  # noqa: BLE001
        logger.exception("Failed to backfill startup failure for node '%s'", ctx.node.id)
        return NodeStatus.FAILED


# ========== 生产侧通用验收原则（领域无关，Worker 专属注入） ==========
# 背景：多个 CC 反复质检 + Manager review 仍可能放行有缺陷的产出（自证式审查闭环：
# 生产与评审共享同一套"看起来对"的信念，异常被各自解释掉）。
# 仅注入 Worker 首轮/续接 prompt（`_build_worker_context` / `_build_resume_task_body`），
# Manager 走自己的 expand/review prompt，不受此原则影响（见 aicu.py 顶部注释）。
# 文案刻意使用通用措辞，不绑定任何具体领域。
_PRODUCER_ACCEPTANCE_PRINCIPLES = (
    "## 验收原则（产出必读）\n"
    "1. **对着真实结果验收**：能运行、能演示、能观察的产出，必须实际跑过、看到真实表现后再判定完成；"
    "不能只凭「代码看起来对」「应该没问题」就交付。\n"
    "2. **异常信号先当产物缺陷追**：运行、自检或测试中的任何异常（报错、结果不符、数据异常），"
    "先当作产物本身的缺陷查清并修复再继续；不要解释成「测试写错了」「环境问题」「无关紧要」而放过。\n"
    "3. **区分「结论」与「现状读数」**：只有被下游消费的结论/判据/契约变了才算缺陷；"
    "文档里对「当前状态」的读数/快照过期但结论没变，**不视为缺陷**，不为此重跑、也不发事件。\n"
    "4. **检查要省轮次**：每次工具调用都会带上完整对话上下文，轮次越多越贵。"
    "把几个小检查合并成**一条命令**（`cmd1 && cmd2`，或写一个脚本一次跑完）；"
    "彼此独立的读取/检查在同一轮里**并行调用**多个工具，不要串行来回。"
)


class Worker(Executable):
    """Worker 执行单元 — 在子进程中运行 AICU.ask() 并回填结果。

    生命周期只有 run()：节点级停止走杀 CC 进程（aicu.kill_processes），
    不由本类提供 stop/pause/resume/cleanup_resources。
    """

    def __init__(
        self,
        context: ExecutableContext,
        aicu_instance: Optional[Any] = None,
    ):
        """
        Args:
            context:       执行上下文（含 Node、RuntimeGateway、配置）。
            aicu_instance: 可选的 AICU 实例；未提供时创建或复用真实实例。
        """
        super().__init__(context)
        self._aicu = aicu_instance

    # 节点级 AICU 缓存：node_id → AICU 实例
    # 路由激活时复用，使 CC agent 通过 --resume 保持会话连续性
    _aicu_cache: dict[str, Any] = {}
    _aicu_cache_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    # 需要区分 Runtime 环境与被测系统的角色
    _SUT_AWARE_ROLES = frozenset({"acceptance", "test", "grill"})

    @staticmethod
    def _build_worker_context(ctx: ExecutableContext) -> str:
        """构建 Worker 执行上下文前缀。

        帮助 Worker 的 CC agent 理解：
          1. 项目的真正目标（从祖先链提取）
          2. 显式声明的依赖节点产出（depends_on）
          3. （验收/测试角色）Runtime 环境 vs 被测系统的边界
          4. 共享工作区（Session Shared Workspace，所有节点自动挂载）
          5. Agent 事件发布/收件箱（当节点配置了 config.events 时）

        Worker 只能通过 depends_on 获取上游信息。
        如需访问其他节点的产出，Manager 必须在 ExecutionPlan 中显式声明 depends_on。

        Args:
            ctx: 执行上下文。

        Returns:
            上下文文本块（未找到信息时返回简短提示）。
        """
        parts: list[str] = []
        node = ctx.node
        runtime = ctx.runtime

        # ----- 项目目标（团队目标） -----
        # 爬父链取最顶层祖先的 task —— 那是整棵树的团队目标。
        #
        # **全文注入，不截断。** 这里曾经是 `root_task[:300]`：砍在半句上，而且没有任何
        # 标记说明被砍过 —— 节点会把一个残缺的目标当成目标本身，它无从察觉。目标该有多长
        # 由写这个任务的人决定，不是注入点能替他截的。再加上下面「## 你的任务」里的个人
        # 当前任务，两级目标连在一起给，节点才知道自己这一步在为什么服务。
        root_task = ""
        cursor_id = node.parent_id
        while cursor_id is not None:
            ancestor = runtime.get_node(cursor_id)
            if ancestor is None:
                break
            root_task = ancestor.task or ""
            cursor_id = ancestor.parent_id
        if root_task:
            # 不再用 `**...**` 包住任务正文：多段落文本套粗体会把 markdown 撑坏。
            parts.append(
                "## 项目目标（团队目标）\n\n"
                "你参与的项目的顶层目标为:\n\n"
                f"{root_task}\n\n"
                "请围绕此目标开展工作，所有产出须服务于该目标。\n"
                "以上是**团队目标**；下方「## 你的任务（个人当前目标）」"
                "是你在其中负责的当前任务。"
            )

        # ----- 依赖节点产出（显式 depends_on 的上游节点） -----
        # Worker 只能访问其 depends_on 声明的节点。
        # Manager 负责在 ExecutionPlan 中声明权威依赖关系。
        try:
            deps = runtime.get_dependencies(node.id)
        except Exception:
            deps = []
        completed_deps = [
            d for d in deps
            if hasattr(d, 'status')
            and getattr(d.status, 'value', str(d.status)) == 'Completed'
        ]
        if completed_deps:
            parts.append(Worker._build_dependency_reading_instructions(ctx, completed_deps))

        # ----- 共享工作区（Session Shared Workspace，所有节点自动挂载） -----
        shared_block = Worker._build_shared_workspace_instructions(node)
        if shared_block:
            parts.append(shared_block)

        # ----- Agent 事件总线（Node → Router → Node 动态协作信号） -----
        # 事件发布：config.events.publish 非空时；收件箱：event_inbox/ 已有待处理事件时。
        # 事件是唯一通信载体：content 只描述事件本身，由 AICU Router 统一决定给谁。
        # 事件不参与调度，只描述执行过程中发生了什么；CC 用 OS 工具直写/轮询。
        event_publish_block = Worker._build_event_publish_instructions(node)
        if event_publish_block:
            parts.append(event_publish_block)
        event_inbox_block = Worker._build_event_inbox_instructions(node)
        if event_inbox_block:
            parts.append(event_inbox_block)
        parts.append(render_notes_instructions(notes_path(node)))

        # ----- SUT 定位（只有验收/测试角色需要） -----
        role = (node.role or '').lower()
        if role in Worker._SUT_AWARE_ROLES:
            sut_name = root_task[:100] if root_task else "该项目"
            parts.append(
                "## ⚠️ 被测系统定位\n\n"
                f"你运行在 **cc_0 Runtime** agent 编排框架内部。\n"
                f"你要**测试和验收**的是「{sut_name}」，而不是 cc_0 Runtime 本身。\n\n"
                "**关键区分**：\n"
                "- cc_0 Runtime 的 API（例: `/v1/nodes`, `/v1/graph`）是编排基础设施，**不是**被测系统\n"
                "- 被测系统的代码在兄弟节点的 workspace 产出目录中（见上方「兄弟节点产出」）\n"
                "- **禁止** import 或测试 `runtime/`、`executables/` 下的 Runtime 内部模块\n"
                "- 只读、只测兄弟节点产出的业务代码\n"
                "\n"
                "在写验收脚本时：\n"
                "- 被测的 API 是业务模块暴露的接口（如博客的文章/标签 API），不是 Runtime 的管理接口\n"
                "- 业务代码可能以文件形式存在（未启动服务器），验收脚本需直接引用模块/函数测试\n"
            )

        # ----- 生产侧通用验收原则（对所有 Worker 首轮执行注入） -----
        # 领域无关，见模块顶部 _PRODUCER_ACCEPTANCE_PRINCIPLES 注释。
        parts.append(_PRODUCER_ACCEPTANCE_PRINCIPLES)

        if not parts:
            return ""
        return "\n\n".join(parts) + "\n\n---\n"

    @staticmethod
    def _build_shared_workspace_instructions(node: Any) -> str:
        """构建共享工作区指令块（节点配置了 ``_shared_workspace`` 时）。

        告知 CC agent：
          - 会话级共享区根目录与自身专属分区的绝对路径；
          - 写入规则（权限语义）：共享区完全共享——共享根下任意文件默认可读可写，
            可互相修改兄弟分区产物；不设锁机制，靠"改前读当前内容 + 原子替换 + 任务分工"
            等最佳实践避免并发覆盖；
          - 读取兄弟产物：用绝对路径通过 OS 工具读取，Glob 容忍缺失。

        无共享配置时返回空串。
        """
        config = node.config or {}
        shared_root = config.get("_shared_workspace")
        if not shared_root:
            return ""
        shared_root = os.path.realpath(shared_root)
        output_path = config.get("_output_path") or ""
        partition = resolve_partition(config)
        lines = [
            "## 📁 共享工作区（Session Shared Workspace）",
            "",
            f"- **协作区根目录**: `{shared_root}`（绝对路径，会话级单一共享区）",
        ]
        if output_path:
            lines.append(
                f"- **你专属的分区**: `{partition}`（你主要负责写该分区；共享区会话级全量可读）"
            )
        else:
            lines.append(
                f"- **你专属的分区**: `{partition}`（= 共享根，全局写权限，高权限模式，仅在确实需要时使用）"
            )
        lines += [
            "",
            "- **共享区文件完全共享**：共享根下任意文件默认可读可写，含兄弟分区，可互相修改兄弟的产出"
            "（如修订契约/接口文件）；但访问范围仍以上方「工作区边界」为准，**优先于**此处的「完全共享」。",
            "- **不设锁**，避免并发覆盖：改共享区任意文件前先 Read 当前内容；写文件用「临时文件 + os.replace」原子替换；"
            "以任务分工为主，同一文件尽量只由一个节点负责。",
            f"- 读兄弟产物：用绝对路径 `{shared_root}/...` 通过 Read/Glob 读取；产物可能尚未写入，先 Glob 容忍缺失，必要时稍后重试。",
            "- 你写入共享区的最终产物会被框架登记为 artifact；草稿放私有 cc_0 目录，无需写入共享区。",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Agent 事件总线（Worker → Runtime → Worker 动态协作信号）
    # 队列目录契约统一走 runtime.agent_events.event_queue_dirs（V3.1 P0#4）。
    # ------------------------------------------------------------------

    @staticmethod
    def _build_event_publish_instructions(node: Any) -> str:
        """构建事件发布指令块（方案三：发布**默认开启**，无条件注入）。

        方案三（独立 AICU Router）：事件为简短结构化字段（objective/intent/
        affected_roles/scope/action）——CC 只描述「你产生了什么 / 发现了什么」，
        由系统（AICU Router）统一决定交给哪些节点，**无需也不能指定目标**
        （这些字段都是参考信号，不含目标/路由指令）。

        发布对任何节点都可用：ingest（``scan_all``）扫描全部节点 outbox，Router 从任意
        producer 路由，与 ``config.events.publish`` 无关。因此无条件注入让每个 CC 都知道
        **它始终可以发布**——不需要在 plan 里声明 ``events.publish``（历史遗留门控已删除）。
        """
        events_dir = event_queue_dirs(node)["outbox"]
        lines = [
            "## 📣 事件发布（Event Publish）",
            "",
            "事件只用来表达依赖关系覆盖不到的协作：正常的上下游衔接不用发事件，依赖图会自动推进并唤醒下游。",
            "每条事件都会让接收节点被唤醒、重跑一轮，有成本：能不发就不发，能合并就合并。",
            "",
            "**发布自检（命中任一才发，都不命中就不发）**：",
            "1) 跨链影响：你的产出/发现会改变**不在你上下游依赖链上**的节点的工作，它不知道就会做错、白做或错过时机；",
            "2) 需要对方行动：其他节点需要为此提供信息、做决定、复核确认或调整任务（依赖链给不了这些）。",
            "判断尺度：对方收到后，会不会多出一个它现在没有、且只有收到这条事件才知道要做的动作？没有就不发。",
            "",
            "**时机与去重**：",
            "- 小发现攒到收尾时合并成一条；只有「对方不立刻知道就会做错/白做/卡住」的关键项才立刻单独发。",
            "- 同一件事不重复发；换个说法，或只是读数/快照变了而结论与请求没变，都不发。",
            "- 自己的问题自己修，不发事件：缺陷在你自己的产出里就直接改；问题在别的节点的产出上、你无法自己处理时才发。",
            "- 你因收到事件被唤醒：改完就收工，不要连环发「已修好/已更新」。例外：复核/测试类节点复测发现缺陷仍未修复或出现新缺陷，按「必须发布」处理。",
            "",
            "**一律不发布**：任务开始/完成、验证通过、产物已生成但只是下游依赖、普通进度汇报，以及「现状描述过时但结论没变」（不视为问题，也不重跑一轮去刷新）。",
            "",
            "**必须发布**：复核/验收/质量门/测试类节点发现**别的节点产出**的失败、缺陷、不通过项"
            "（含「测试套件缺失」「产物为空」这类没能跑到判定的异常），且该缺陷还没报告过（或报告后复测仍未修复）。"
            "不得只把缺陷写进留证文件就收工；一次复核的多个缺陷合并成一条，"
            "`intent` 用 `collaboration`（需协作修复）或 `escalation`（需上级协调），"
            "`objective` 写明「发现什么缺陷 + 影响谁 + 需要谁修复」。「验证通过」仍不发。",
            "",
            f"发布方式：在 `{events_dir}` 目录写入一个 JSON 文件（文件名唯一，如 `evt_xxx.json`），"
            "用「临时文件 + os.replace」原子写入；每发一条写一个新文件；发布后不要删除（框架摄取后自动清理）。格式：",
            "",
            "```json",
            "{",
            '  "objective": "就几句话：发生了什么 + 需要谁做什么（≤3 句、≤80 字）",',
            '  "intent": "collaboration | escalation",',
            '  "affected_roles": [{"role": "受影响的岗位", "duty": "该岗位职责"}],',
            '  "scope": "current | subtree | related | global",',
            '  "action": "接收节点要完成的动作（自由简短文本）"',
            "}",
            "```",
            "",
        ]
        lines += [
            "字段规则（**objective 不满足会被框架判定为格式非法而打回**）：",
            "- `objective` 必须是非空字符串，最多 3 句、总共 ≤80 字，只做「敲门通知」："
            "发生了什么 → 影响谁 → 期望对方注意什么。详细成果写进共享产物文件（如 `shared/` 分区下的报告），"
            "接收方按需读取；**禁止**在 objective 里写执行步骤、缺陷/验收清单、逐项罗列、日志或整篇报告。",
            "- `intent` 二选一：`collaboration`（需要协作）/ `escalation`（需要上级协调）。只是同步状态、不需要对方动作的信息不要发。",
            "- `affected_roles` 是受影响的**岗位**列表（参考信号，不是节点）；每项含 `role`（岗位名，**禁止**填节点名/模块名/节点 ID）与 `duty`，可为 `[]`。",
            "- `scope` 四选一：`current`（仅自身）/ `subtree`（本子树）/ `related`（相关节点）/ `global`（全局）。",
            "- `action` 是建议接收方做的动作（简短文本，可为 `\"\"`）。",
            "- **不要指定目标**：事件由系统统一路由，你无需也不能指定收件人。",
            "",
            "`objective` 正例（跨链协作）：`本环节发现一项变更会影响另一条任务链路的产出，请相关节点知悉并复核是否需要调整。`",
            "`objective` 正例（发现缺陷，必须发）：`本环节复跑质量门发现后端测试套件缺失（tests/ 为空），需上游产出方补齐后再回归。`",
            "`objective` 反例（整段报告，会被打回）：`已完成本阶段，共 N 项检查 M 项通过 K 项未通过，逐项说明第1项…第2项…第3项…`",
        ]
        return "\n".join(lines)

    @staticmethod
    def _build_event_inbox_instructions(node: Any) -> str:
        """构建事件收件箱指令块（``event_inbox/`` 非空时）。

        方案三：展示 inbox 事件（event_id/producer/objective/intent/scope/action），逐条消费后删除；
        运行中实时轮询 ``events_inbox/``（Glob 容忍缺失、有界重试、读后删两处副本，
        保证单次消费——inbox 是唯一待消费队列）。

        空守卫：inbox 无待处理文件 → 返回空串。
        """
        queue = event_queue_dirs(node)
        inbox_dir = queue["inbox"]
        live_dir = queue["live"]

        # 列出 event_inbox/ 中实际待处理的事件文件
        pending_files = []
        if os.path.isdir(inbox_dir):
            try:
                pending_files = sorted(
                    p for p in glob.glob(os.path.join(inbox_dir, "*.json"))
                )
            except Exception:  # noqa: BLE001
                pending_files = []
        has_pending = bool(pending_files)
        if not has_pending:
            return ""

        lines = [
            "## 📥 事件收件箱（Event Inbox）",
            "",
            "其他节点可能通过事件总线向你推送信号。事件**不阻塞你开始任务**：",
            "能先推进的工作先做，事件到达后按领域语义融合即可。",
            "",
            f"### 待处理事件（`{inbox_dir}`）",
            "以下事件已投递给你，处理步骤：",
            "",
        ]
        for pf in pending_files:
            ev = None
            try:
                with open(pf, "r", encoding="utf-8") as f:
                    ev = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(ev, dict):
                continue
            ev_id = ev.get("event_id", os.path.basename(pf))
            producer = ev.get("producer", "?")
            objective = ev.get("objective") or ev.get("content", "")
            intent = ev.get("intent", "")
            scope = ev.get("scope", "")
            action = ev.get("action", "")
            lines.append(f"- **`{ev_id}`** · producer=`{producer}`")
            if intent:
                lines.append(f"  intent: {intent}")
            if scope:
                lines.append(f"  scope: {scope}")
            if objective:
                lines.append(f"  objective: {objective}")
            if action:
                lines.append(f"  action: {action}")
        lines.append("")
        lines.append("对每条事件：**1)** 先判断这件事**是否属于你的职责范围**——"
                     "不属于就直接忽略（不要为此改产物、重跑或发事件）；属于才纳入。"
                     "**2)** 属于你的职责时，依据事件的领域含义推进你的任务。"
                     "**3)** 处理完成后删除 `event_inbox/{事件id}.json` 表示已消费。")
        lines.append("注意：`objective` 只是摘要/敲门通知，**完整内容在共享分区产物文件里**，"
                     "按需去读（先看 `_INDEX.md` 或主文档，不要期望 objective 装下全部细节）。")
        lines.append("")
        lines.append(f"### 运行中实时轮询（`{live_dir}`）")
        lines.append(f"- 当任务进行中需要等待某个协作信号时，可**有界轮询** `{live_dir}` 目录：")
        lines.append("  - 用 Glob 列出 `*.json`（目录可能暂时为空，容忍缺失）；")
        lines.append("  - 轮询要**有界**（如最多 10~20 次、每次间隔数秒），**不要**无限阻塞整个任务；")
        lines.append("  - 读到事件后：处理事件 → 删除 `event_inbox/{id}.json`（inbox 是唯一待消费队列，"
                     "删除即消费，避免二次处理）。")
        return "\n".join(lines)

    @staticmethod
    def _build_routing_context(node: Any) -> str:
        """构建路由激活上下文前缀。

        当节点被其他节点通过事件机制激活时，pending_context 包含
        上游节点的反馈/指令。将其注入到任务提示中。

        Args:
            node: 当前 Node 对象。

        Returns:
            路由上下文文本块；无 pending_context 时返回空字符串。
        """
        pending = getattr(node, 'pending_context', None)
        if not pending:
            return ""

        checkpoint = ""
        if pending.get('session_mode') == SESSION_FRESH:
            checkpoint = render_checkpoint(pending, read_notes(node))
            if pending.get('collaboration_type') == 'revise':
                return checkpoint

        activated_by = pending.get('activated_by', 'unknown')
        reason = pending.get('activation_reason', '')
        activated_at = pending.get('activated_at', '')

        lines = [
            "## 🔄 路由激活上下文",
            "",
            f"> 你被节点 `{activated_by[:8]}` 通过事件机制重新激活。",
        ]
        if reason:
            lines.append(f"> **原因**: {reason}")
        if activated_at:
            lines.append(f"> **时间**: {activated_at}")
        lines.append("")
        lines.append("### 上游反馈/指令")
        lines.append("")

        # 排除元信息字段，展示实际上下文
        meta_keys = {
            'activated_by', 'activation_reason', 'activated_at',
            'session_mode', 'original_task', 'prior_artifacts',
        }
        for key, value in pending.items():
            if key in meta_keys:
                continue
            if isinstance(value, list):
                lines.append(f"**{key}**：")
                for i, item in enumerate(value, 1):
                    if isinstance(item, dict):
                        lines.append(
                            f"  {i}. " + "；".join(f"{k}: {v}" for k, v in item.items())
                        )
                    else:
                        lines.append(f"  {i}. {item}")
            elif isinstance(value, dict):
                lines.append(f"**{key}**：")
                for k, v in value.items():
                    lines.append(f"  - {k}: {v}")
            else:
                lines.append(f"**{key}**: {value}")
            lines.append("")

        if pending.get('collaboration_type') != 'repair':
            lines.append("> 先判断这些反馈是否属于你的职责、是否真的需要改动；与你无关就不要动。")
        lines.append(
            "> 需要改的话：**只改受影响的部分，不要重跑全套验证**——只复核被改动的部分。"
        )
        lines.append("")
        feedback = "\n".join(lines)
        return f"{checkpoint}\n{feedback}" if checkpoint else feedback

    @staticmethod
    def _compose_task_body(task_parts: list[str], task: str) -> str:
        """拼首轮任务体：上下文前缀 + 分隔线 + 「## 你的任务」+ 本节点 task 全文。

        两级目标在这里合流 —— 前缀里是**团队目标**（`_build_worker_context` 的
        「## 项目目标（团队目标）」块），末尾是**个人当前目标**（本节点自己的 task）。
        两处都是全文，不截断，标题上也都点明了自己是哪一级。

        抽成函数只为可测：它是纯字符串拼接，执行路径的行为与原先内联的那一行完全一致。
        """
        if not task_parts:
            return task
        return "\n\n".join(task_parts) + "\n\n---\n\n## 你的任务（个人当前目标）\n\n" + task

    @staticmethod
    def _build_resume_task_body(
        node: Any,
        task: str,
        routing_prefix: str,
        event_inbox_block: str,
        event_publish_block: str,
    ) -> str:
        """构建续接轮任务体：激活反馈 + 事件收件箱 + 事件发布契约。

        续接模式（CC 已通过 --resume 保留完整对话历史）下不需要重建项目
        目标/依赖等上下文，只注入本次的增量信息。events.publish 非空时
        **重申**事件发布契约，保证被激活重跑的 worker 记得发事件。

        Args:
            node: 当前 Node。
            task: 原始任务文本。
            routing_prefix: 激活反馈前缀（_build_routing_context 输出）。
            event_inbox_block: 事件收件箱指令块（可能为空串）。
            event_publish_block: 事件发布指令块（可能为空串）。

        Returns:
            续接任务体文本。
        """
        # 已初始化的 AICU 会话只接收本轮增量，不重复注入原始 Main Task。
        # 没有事件/修复上下文时，使用 Continue 的最小指令继续已有主任务。
        incremental_parts = [routing_prefix] if routing_prefix else [
            "## 继续执行\n\n开始进行主任务"
        ]
        resume_parts = [
            p for p in incremental_parts + [
                event_inbox_block, event_publish_block,
                render_notes_instructions(notes_path(node)),
                _PRODUCER_ACCEPTANCE_PRINCIPLES,
            ]
            if p
        ]

        return (
            "## 🔄 会话续接 — 基于已有产出进行修订\n\n"
            "你之前已经完成了一轮工作，现在收到了新的反馈或指令。"
            "请基于你的完整对话历史（通过 --resume 保留）和以下增量信息，"
            "对已有产出进行修订和改进，**不要**从头开始。\n\n"
            + "\n\n".join(resume_parts)
        )

    @staticmethod
    def _build_dependency_reading_instructions(ctx: ExecutableContext, deps: list) -> str:
        """上游依赖的按需读取指令：先建索引，再读与本任务相关的部分，并留下可核对的痕迹。

        不再要求通读每个依赖的全部文件（依赖一多上下文就被上游细节占满），也不再
        按依赖个数切换两套规则。取而代之的是同一套流程，并用三道防线避免「按需读取」
        退化成「漏读关键信息」：

        1. 入口与契约类文件（README/INDEX、接口、规格、schema、模型）命中即必读全文；
        2. 先从本节点任务列出「需要从上游获得什么」，再逐项找依据 —— 以需求驱动读取，
           而不是凭文件名猜哪个相关；找不到再 grep 整个依赖目录，不允许直接跳过；
        3. 产出里写「上游依赖核对」小节，逐项给出来源、未读文件及理由，让遗漏可见、可复核。

        所有依赖都列出，不截断个数：漏掉一个依赖等于漏掉它的全部约束。
        """
        lines = [
            "## 🔴 上游依赖：按需读取（先建索引，再读取任务需要的部分，不得遗漏）",
            "",
            "> 你的工作必须严格基于上游节点的产出，**禁止编造或猜测**上游的设计决策、接口契约、",
            "> 数据模型或约束。不必通读上游全部文件，但「你的任务需要的信息」必须有上游原文作依据。",
            "",
            "### 依赖清单（产出路径若是文件，其所在目录即该依赖的产出目录）",
            "",
        ]
        for index, d in enumerate(deps, 1):
            role = getattr(d, 'role', '?') or '?'
            summary = (getattr(d, 'task_summary', '') or '').strip()
            task = ' '.join((getattr(d, 'task', '') or '').split())
            if len(task) > 300:
                task = task[:300] + "…"
            lines.append(f"**依赖 {index} — [{role}]**")
            if summary:
                lines.append(f"- 职责摘要: {summary}")
            if task:
                lines.append(f"- 任务: {task}")
            lines.append(f"- 📂 产出路径: `{_resolve_dep_artifact_path(ctx, d)}`")
            lines.append("")

        lines.extend([
            "### 读取流程（每个依赖都要执行）",
            "",
            "1. **建索引**：对每个依赖的产出目录做一次目录列表（Glob 或 `ls -l`，可看到文件大小），"
            "只建立「有哪些文件、多大」的认识，此时不读内容。",
            "2. **入口与契约必读全文**（存在即读）：`README*` / `SUMMARY*`、"
            "该依赖的主产出文档（如 `cc_output.md`、`best_solution_summary.txt`）、"
            "`*interface*` / `*api*` / `*contract*` / `*spec*` / `*schema*` / `*types*` / `models.*`、"
            "`*.proto` / OpenAPI / 路由与配置定义。\n"
            "   `_INDEX.md` / `INDEX*` 是导航索引，**只读它的清单部分**（从开头读到文件清单结束为止），"
            "用清单判断哪些文件与任务相关；清单之后的版本沿革、自检等叙述不必读（对下游有影响的决策与限制以主文档为准）。",
            "3. **以需求驱动读取**：通读你自己的任务后，先列出「我需要从上游获得什么」"
            "（接口签名、数据结构与字段名、错误码、配置项、命名与目录约定、验收标准、依赖版本……），"
            "再逐项在上游找原文依据：先看入口/契约文档；没找到就对依赖目录 "
            "`grep -rn \"<关键词>\"`，再读命中文件的相关部分。",
            "4. **读取粒度**：命中的文件在 800 行以内读全文；超过 800 行先 Read 前 100 行，"
            "再用 `grep -n \"^def \\|^class \\|^export \\|^interface \"` 提取签名"
            "（Markdown 等文档用 `grep -n \"^#\"` 提取标题），只读与任务相关的行范围。"
            "不确定是否相关时，读开头约 50 行再判断，不要仅凭文件名跳过；测试文件、静态资源、无关的内部实现可以不读。",
            "5. **找不到依据时不得编造**：按最保守的假设推进，并在「上游依赖核对」里写明"
            "「上游未定义」或「上游互相矛盾」及你的处理方式。",
            "",
            "### 上游依赖核对（必做）",
            "",
            "开工前自查：任务里提到的每个上游对象、接口、字段，是否都已找到上游原文依据。"
            "完成后在主产出文档末尾增加「上游依赖核对」小节"
            "（主产出是 JSON 等机器解析格式时，写入工作区根目录的 `upstream_notes.md`），对每个依赖写明：",
            "- 已读文件（标注全文还是片段）；",
            "- 采用的关键事实，一条一行，并标注来源（文件 + 行号或章节）；",
            "- 与任务可能相关却**没有读**的文件及理由（明显无关的不必列）；",
            "- 上游未定义 / 互相矛盾之处。",
            "",
            "> ⛔ 完成各依赖的「建索引」和「入口与契约必读」之前，**不得**开始执行本节点的任务。",
        ])
        return "\n".join(lines)

    def _full_task_body(self, ctx: ExecutableContext, routing_prefix: str) -> str:
        """首次执行形态的完整任务体（新建会话重试时用，不依赖旧 CC 会话历史）。"""
        context_prefix = self._build_worker_context(ctx)
        task_parts = [p for p in [routing_prefix, context_prefix] if p]
        return Worker._compose_task_body(task_parts, ctx.node.task)

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        """在线程池中执行 AICU.ask() 并回填结果。

        成功 → COMPLETED + artifact_path + 路由决策（如有）
        失败 → FAILED + ErrorObject + 路由决策（如有）
        """
        # 判断是否为会话续接（必须在 _create_real_aicu 之前检查，
        # 因为 _create_real_aicu 会把首次创建的 AICU 写入缓存）。
        # 测试 fake 也可通过 aicu_instance 注入，但不参与节点级真实会话缓存。
        # 新会话策略的唯一落点：pending_context 标了 fresh（Revise / 事件重激活）
        # 就丢弃缓存的 CC 会话，不再 --resume；上一轮状态由检查点交代。
        pending_at_start = getattr(ctx.node, 'pending_context', None) or {}
        # 节点上一次执行失败：旧 CC 会话的历史可能已被报错内容污染（如图片），--resume 会继续
        # 失败，所以新建会话重跑，并把报错原文交给模型自行调整。
        previous_failure = _last_failure_text(ctx.node)
        with Worker._aicu_cache_lock:
            if pending_at_start.get('session_mode') == SESSION_FRESH or previous_failure:
                Worker._aicu_cache.pop(ctx.node.id, None)
            is_continuation = Worker._aicu_cache.get(ctx.node.id) is not None

        # 捕获 Revise 复用标记（必须在使用前捕获：下方 _build_routing_context
        # 读取 pending_context 后会被清除，先捕获才能判断「resume 失败 → 新建会话」兜底）
        revise_resume = (
            is_continuation
            and (getattr(ctx.node, 'pending_context', None) or {})
            .get('collaboration_type') == 'revise'
        )

        # 真实 AICU；测试替身只能通过 aicu_instance 显式注入。
        aicu = self._aicu
        if aicu is None:
            try:
                aicu = _create_real_aicu(ctx)
            except Exception as exc:  # noqa: BLE001
                # 构造期失败（MCP 预检、skills_dir、角色 yaml…）在此就地回填，
                # 否则异常会绕过下方所有 FAILED 回填直接穿到 scheduler（见 _fail_startup）。
                return _fail_startup(ctx, exc)

        # Update status to RUNNING
        ctx.runtime.update_node_status(ctx.node.id, NodeStatus.RUNNING)

        # 构建路由反馈上下文（必须在使用前构建：_build_routing_context 读取
        # node.pending_context，而下方会立即清除它（标记已消费）。若先清除再构建，
        # 上游「验收/测试发现缺陷 → 路由反馈」的内容将丢失，被激活节点收不到反馈。
        routing_prefix = self._build_routing_context(ctx.node)

        # 清除 pending_context（已消费）
        if getattr(ctx.node, 'pending_context', None):
            ctx.runtime.update_node_status(ctx.node.id, NodeStatus.RUNNING, pending_context=None)

        # Execute AICU.ask() in thread pool to avoid blocking scheduler
        executor = ThreadPoolExecutor(max_workers=1)
        if is_continuation:
            # 续接模式：CC 已通过 --resume 保留完整对话历史，
            # 只需注入本次的路由反馈 + task，无需重建项目目标/依赖等上下文
            # 续接时注入事件收件箱/发布段：让 resume 的 CC 看到运行期间投递的未处理事件，
            # 并记得在反馈/协作时继续发事件（原 routing.json 契约由事件发布取代）。
            event_inbox_block = Worker._build_event_inbox_instructions(ctx.node)
            event_publish_block = Worker._build_event_publish_instructions(ctx.node)
            task_body = Worker._build_resume_task_body(
                ctx.node, ctx.node.task,
                routing_prefix, event_inbox_block, event_publish_block,
            )
        else:
            # 首次执行：构建完整上下文
            task_body = self._full_task_body(ctx, routing_prefix)
        if previous_failure:
            task_body = f"{_render_previous_failure(previous_failure)}\n\n---\n\n{task_body}"

        # The AICU owns individual CC start/finish/failure events. Worker owns
        # the outer task handoff so the timeline also explains what entered the
        # CC pool, including injected AICU-compatible implementations.
        cc_ids = _aicu_cc_ids(aicu)
        for cc_id in cc_ids:
            _log_cc_lifecycle(
                ctx.runtime,
                ctx.node.id,
                cc_id,
                "cc_received",
                phase="worker",
                task=ctx.node.task,
            )
        future = executor.submit(aicu.ask, task_body)

        # resume 失败兜底（仅 Revise 复用路径，窄范围）：CC --resume 续接的 AICU
        # 首次 ask 抛异常时（会话失效/resume 失败），pop 缓存 + 无 --resume 重建会话
        # 重试一次；普通激活续接路径不触发，异常原样传播（下方 FAILED 回填）。
        try:
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                api_rejected = not previous_failure and _is_retryable_api_rejection(exc)
                if not revise_resume and not api_rejected:
                    raise
                if api_rejected:
                    # 请求被 API 以 400 拒绝：新建会话重试一次，并把报错原文交给模型自行调整。
                    logger.warning(
                        "Node '%s' request rejected by API: %s. "
                        "Retrying once in a fresh session with the error attached.",
                        ctx.node.id, exc,
                    )
                    error_text = _extract_error_text(str(exc))
                    ctx.runtime.update_node_status(
                        ctx.node.id,
                        NodeStatus.RUNNING,
                        log=[LogEntry(
                            ts=datetime.now(),
                            level=LogLevel.WARN,
                            message=f"执行被 API 拒绝，已带上报错信息新建会话重试一次: {error_text}",
                        )],
                    )
                    task_body = (
                        f"{_render_previous_failure(error_text)}\n\n---\n\n"
                        f"{self._full_task_body(ctx, routing_prefix)}"
                    )
                else:
                    logger.warning(
                        "Revise-resume ask failed for node '%s' (first attempt): %s. "
                        "Rebuilding session without --resume and retrying once.",
                        ctx.node.id, exc,
                    )
                with Worker._aicu_cache_lock:
                    Worker._aicu_cache.pop(ctx.node.id, None)
                aicu = (self._aicu if api_rejected else None) or _create_real_aicu(ctx)
                result = aicu.ask(task_body)

            for cc_id in cc_ids:
                _log_cc_lifecycle(
                    ctx.runtime,
                    ctx.node.id,
                    cc_id,
                    "cc_handoff",
                    phase="handoff",
                    task=ctx.node.task,
                )

            # 守卫：若执行期间被手动 pause，跳过状态回填，保留 PAUSED 状态
            if getattr(ctx.node, 'status', None) == NodeStatus.PAUSED:
                return NodeStatus.PAUSED

            # Extract artifact path from result dict and keep it in the node workspace.
            artifact_path = _normalize_worker_artifact(
                ctx, result.get("final_output_path", "")
            )

            # 兜底摄取节点事件发布入口（防 scheduler 已停、无 tick 扫描导致事件滞留）。
            # 事件文件被摄取后由 Runtime 删除；此处仅触发摄取，不改变任何调度。
            # 反馈/协作闭环改由「带 target 事件」触发（V3.1 P2 事件→重激活桥），
            # 不再读取 routing.json。
            try:
                ctx.runtime.flush_node_events(ctx.node.id)
            except Exception:  # noqa: BLE001
                pass

            # Build the worker-level terminal entry. CC-level completion is
            # emitted by the activity callback and is intentionally separate.
            log_entries = [
                LogEntry(
                    ts=datetime.now(),
                    level=LogLevel.INFO,
                    message=f"Worker execution completed, artifact: {artifact_path}",
                ),
            ]

            # Backfill COMPLETED status with artifact_path and logs
            update_kwargs = {
                "artifact_path": artifact_path,
                "log": log_entries,
            }
            ctx.runtime.update_node_status(
                ctx.node.id,
                NodeStatus.COMPLETED,
                **update_kwargs,
            )

            return NodeStatus.COMPLETED

        except Exception as exc:
            # 守卫：若执行期间被手动 pause，跳过 FAILED 回填，保留 PAUSED 状态
            if getattr(ctx.node, 'status', None) == NodeStatus.PAUSED:
                return NodeStatus.PAUSED

            # 失败路径兜底摄取事件（Agent 可能在失败前已发布部分事件）。
            try:
                ctx.runtime.flush_node_events(ctx.node.id)
            except Exception:  # noqa: BLE001
                pass

            # A real CC failure, when available, is emitted once by the activity callback.
            return _backfill_failed(ctx, exc, stage="execution")

        finally:
            executor.shutdown(wait=False)
            # 缓存清理：释放不会再被激活的节点的 AICU 实例
            # 条件0: 节点被手动 pause 时，保留缓存供 resume 续接
            # 条件1: 首轮执行且无待处理路由（不会被反馈激活）
            node = ctx.node
            with Worker._aicu_cache_lock:
                if node.id not in Worker._aicu_cache:
                    pass  # 已清理
                elif getattr(node, 'status', None) == NodeStatus.PAUSED:
                    pass  # 保留 AICU 缓存，供 resume 时 --resume 续接
                elif (getattr(node, 'activation_count', 0) == 0
                      and not getattr(node, 'pending_routing', None)):
                    # pending_routing 已不再回填（routing.json 机制移除），恒真；保留以不变更语义
                    Worker._aicu_cache.pop(node.id, None)
