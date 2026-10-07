"""
runtime/router.py — AICU Router：独立事件路由决策器

方案三（独立 AICU Router）唯一真实实现。路由语义收敛为唯一形态：

    Event + 当前 Organization Graph → LLM Router → Targets

职责边界（用户定稿，必须遵守）：
  - Router 决定「一个 Event 该交给哪些 Node」，输入 = Event + 当前组织图上下文；
    输出 = 目标 node_id 列表（空 = 无人接收）。
  - **Event.content 只描述事件本身，不得携带任何 Router 指令 / 目标声明**。
    本模块绝不解析 content 中的 target/to/route_to 之类字段。
  - **Router 内部不发展第二套"确定性路由规则"**：无 Subscription Router /
    Collaboration Router / Rule Router / "if content contains ..." 特判。
    组织图遍历即唯一上下文来源，不建立 Capability Graph / Candidate Resolver /
    Routing Index 等第二套索引体系。
  - Router 目标可以是任意 Node（Worker→Worker / Worker→Manager / Manager→Manager）。

Router 使用真实 AICU；测试可通过 ``aicu_instance`` 显式注入实现 ``ask()`` 的 fake。
"""

import json
import logging
import os
import threading
from typing import Any, Optional

from runtime.models import EventMessage, NodeStatus

logger = logging.getLogger(__name__)


class _RouteParseError(Exception):
    """Router 输出无法解析为合法 ``{"targets": [...]}`` 结构。

    与「解析出空 targets（合法 ignore）」严格区分：空 targets 是 LLM 正常判断无人需要
    处理；本异常是根本没产出可解析结构（LLM 退化 / 非 JSON / 复述）。只有后者才应
    重置会话重试，而不是静默空路由后一路退化。
    """


class AICURouter:
    """决定一个 Event 该交给哪些 Node。

    LLM 决策。组织图快照是 Router 的唯一外部输入。producer 上下文以 node_id
    为准，step_ref 仅经兼容层解析。
    """

    def __init__(
        self,
        aicu_instance: Optional[Any] = None,
        workspace_root: Optional[str] = None,
        config: Optional[dict[str, Any]] = None,
    ):
        self._aicu = aicu_instance
        self._workspace_root = os.path.abspath(workspace_root or os.getcwd())
        self._config = dict(config or {})
        # A Router owns one single-CC AICU. Reset and ask must be atomic so
        # concurrent callers cannot reset each other's fresh invocation.
        self._route_lock = threading.RLock()
        self._graph: Any = None
        # 快照缓存：graph_version 未变化时复用（图没变不扫全量节点）。
        self._snapshot_version: Optional[int] = None
        self._cached_snapshot: Optional[list[dict]] = None
        # Router 生命周期：与组织结构版本绑定。结构变更 → 重置会话；
        # 运行状态变化（running/completed/failed）不切换。
        self._structure_epoch: Optional[int] = None
        # Structured decision trace consumed by AgentEventManager. The trace is
        # intentionally separate from EventMessage so the event wire contract
        # remains stable while route-training data can evolve independently.
        self._last_trace: dict[str, Any] = {}
        self._last_raw_output: Any = None
        self._last_parse_error: Optional[str] = None
        # 最近一次决策的 reason（ignore 时记录"为什么无人需要处理"，落 trace 审计）
        self._last_reason: Optional[str] = None

    # ------------------------------------------------------------------
    # Graph 引用注入（延迟，避免循环导入）
    # ------------------------------------------------------------------

    def attach_graph(self, graph: Any) -> None:
        """注入 Graph 引用（路由读取组织图快照）。重挂时清空缓存与 epoch。"""
        self._graph = graph
        self._snapshot_version = None
        self._cached_snapshot = None
        self._structure_epoch = None

    # ------------------------------------------------------------------
    # 路由入口
    # ------------------------------------------------------------------

    def route(self, event: EventMessage) -> list[str]:
        """决定 Event 该交给哪些 Node，返回目标 node_id 列表（空 = 无人接收）。

        每次真正调用 Router AICU 前都会清空 Router conversation；AICU 实例、workspace
        和 CLI 配置仍由 Router 生命周期复用。解析失败时继续只重试一次。不解析
        Event.content 中的任何“目标声明”——content 只作为事件本身的描述传给 LLM/桩。
        """
        with self._route_lock:
            self._last_trace = {}
            self._last_raw_output = None
            self._last_parse_error = None
            self._last_reason = None
            if self._graph is None:
                logger.warning("Router not attached to a graph; event '%s' not routed",
                               event.event_id)
                self._last_parse_error = "router_not_attached_to_graph"
                return []
            producer_id = self._resolve_producer_node(event.producer) or event.producer
            self._maybe_reset_on_structure_change()
            snapshot = self._graph_snapshot(producer_id)
            producer_snapshot = next(
                (dict(item) for item in snapshot if item.get("id") == producer_id),
                None,
            )
            self._last_trace = {
                "producer_id": producer_id,
                "producer_snapshot": producer_snapshot,
                "candidate_nodes": [dict(item) for item in snapshot],
                "graph_version": getattr(self._graph, "graph_version", None),
                "structure_epoch": self._structure_epoch,
            }
            prompt = self._build_prompt(event, producer_id, snapshot)
            # Route boundaries are isolated, but the AICU/workspace is reused.
            ai = self._ai()
            self._reset_conversation()
            try:
                result = ai.ask(prompt)
            except Exception as exc:
                self._last_parse_error = f"router_call_failed: {type(exc).__name__}: {exc}"
                raise
            self._last_raw_output = result
            try:
                targets = self._parse_targets(result)
            except _RouteParseError:
                # 解析失败（LLM 退化 / 非 JSON / 复述）→ 重置会话后重试一次，而非静默空路由。
                # 空 targets（合法 ignore）不在此路径：_parse_targets 正常返回空列表。
                self._last_parse_error = "parse_failed_retrying"
                self._last_reason = None
                self._reset_conversation()
                try:
                    result = ai.ask(prompt)
                except Exception as exc:
                    self._last_parse_error = (
                        f"router_call_failed_after_retry: {type(exc).__name__}: {exc}"
                    )
                    raise
                self._last_raw_output = result
                try:
                    targets = self._parse_targets(result)
                except _RouteParseError as exc:
                    # 重试仍失败 → fail-closed（空 targets），不再无限重试。
                    self._last_parse_error = f"parse_failed_after_retry: {exc}"
                    targets = []
            except Exception as exc:
                self._last_parse_error = f"router_parse_failed: {type(exc).__name__}: {exc}"
                raise
            self._last_trace["parsed_targets"] = list(targets)
            if self._last_reason:
                self._last_trace["decision_reason"] = self._last_reason
            elif not targets:
                self._last_trace["decision_reason"] = "ignore (empty targets)"
            return targets

    def decision_trace(self) -> dict[str, Any]:
        """Return a bounded, JSON-safe snapshot of the latest route decision."""
        trace = dict(self._last_trace)
        raw = self._last_raw_output
        if isinstance(raw, str):
            trace["raw_output"] = raw[:4000]
        elif raw is None or isinstance(raw, (int, float, bool)):
            trace["raw_output"] = raw
        else:
            try:
                trace["raw_output"] = json.dumps(
                    raw, ensure_ascii=False, default=str,
                )[:4000]
            except (TypeError, ValueError):
                trace["raw_output"] = str(raw)[:4000]
        if self._last_parse_error:
            trace["parse_error"] = self._last_parse_error[:1000]
        return trace

    # ------------------------------------------------------------------
    # 生命周期：Router 持有一个实例，route 边界隔离 conversation
    # ------------------------------------------------------------------

    def _maybe_reset_on_structure_change(self) -> None:
        """记录最新组织结构版本；conversation 由每次 route 边界统一重置。

        结构版本仍用于 trace 和缓存语义，但不再单独触发 reset，避免一次 route
        因为“结构变化 + route 边界”重复清理会话。运行状态变化也不影响该记录。
        """
        if self._graph is None:
            return
        self._structure_epoch = getattr(self._graph, "structure_version", 0)

    def _reset_conversation(self) -> None:
        """重置 AICU 会话（只清 conversation_id，不起新进程/不重建实例）。

        AICU 尚未创建时首次路由即全新会话，no-op。
        """
        if self._aicu is None:
            return
        for cc in getattr(self._aicu, "ccs", []):
            cc.reset_conversation()

    # ------------------------------------------------------------------
    # AI 调用
    # ------------------------------------------------------------------

    def _ai(self) -> Any:
        """返回注入或创建的真实 AICU 实例。"""
        if self._aicu is None:
            self._aicu = self._create_real_aicu()
        return self._aicu

    def _create_real_aicu(self) -> Any:
        """real 模式：solo AICU（num_ccs=1），工作区 ``{workspace_root}/router_cc/``。

        路由角色收敛到 roles/system_role/router.yaml（独立于 Manager/Worker）。
        num_ccs=1 已硬删自审：路由决策由单次生成直接返回，不再进入自审轮；
        角色配置里的「路由专用审查清单/审查位/审计位」因此不会在 solo 下渲染。
        """
        from aicu import AICU

        workspace_dir = os.path.join(self._workspace_root, "router_cc")
        return AICU(
            role_type="system_role",
            parent_workspace=workspace_dir,
            role="router",
            num_ccs=1,
            model=self._config.get("model"),
            cli_name=self._config.get("cli_name"),
            api_base_url=self._config.get("api_base_url"),
            api_key_env=self._config.get("api_key_env"),
            provider_id=self._config.get("provider_id"),
            skip_permissions=True,
        )

    # ------------------------------------------------------------------
    # producer 兼容层：正式语义 = node_id，step_ref 仅解析用
    # ------------------------------------------------------------------

    def _resolve_producer_node(self, producer: str) -> Optional[str]:
        """把 producer（node_id 或 step_ref）解析为 **node_id**（正式语义）。

        兼容层：CC 写事件文件时可能把 producer 写成可读的 step_ref，按 id 查不到
        时回退按 step_ref 匹配。返回 None 表示图中不存在（以原串兜底）。
        """
        if self._graph is None or not producer:
            return None
        node = self._graph.get_node(producer)
        if node is not None:
            return node.id
        for n in self._graph.all_nodes():
            if (n.step_ref or "") == producer:
                return n.id
        return None

    def _resolve_node_id(self, ref: str) -> Optional[str]:
        """把目标引用（node_id / step_ref / 截断 id 前缀）解析为 node_id；None = 图中不存在。

        prompt 里节点以 ``id[:8]…`` 截断形式展示，LLM 可能回传这种截断 id；
        此处做前缀容错，避免合法目标因截断而解析失败（导致空路由）。
        """
        if self._graph is None or not ref:
            return None
        ref = (ref or "").strip()
        node = self._graph.get_node(ref)
        if node is not None:
            return node.id
        for n in self._graph.all_nodes():
            if (n.step_ref or "") == ref:
                return n.id
        # 截断 id 前缀容错：`id[:8]…` / `id[:8]...`
        prefix = ref.rstrip("…").rstrip(".").strip()
        if len(prefix) >= 6:
            for n in self._graph.all_nodes():
                if n.id.startswith(prefix):
                    return n.id
        return None

    # ------------------------------------------------------------------
    # 组织图快照（Router 唯一外部输入）
    # ------------------------------------------------------------------

    def _org_path(self, node: Any) -> str:
        """parent_id 链回溯组织路径 → "Root → A → B"。"""
        if self._graph is None:
            return node.step_ref or node.id
        parts = []
        cur_id = node.id
        seen = set()
        while cur_id and cur_id not in seen:
            seen.add(cur_id)
            n = self._graph.get_node(cur_id)
            if n is None:
                break
            parts.append(n.step_ref or n.id[:8])
            cur_id = n.parent_id
        return " → ".join(reversed(parts)) or node.id

    def _graph_snapshot(self, producer_id: str) -> list[dict]:
        """当前组织图节点清单（只含路由决策所需的 Node 基本上下文，非全量状态）。

        graph_version 未变化时复用缓存快照（图没变不扫全量节点）；
        graph_version 变化（含状态变化）时重建以保持 status 新鲜。
        task 用截断（~140 字）。
        """
        if self._graph is None:
            return []
        version = self._graph.graph_version
        if self._snapshot_version == version and self._cached_snapshot is not None:
            return self._cached_snapshot
        out: list[dict] = []
        for node in self._graph.all_nodes():
            status = (
                node.status.value if hasattr(node.status, "value") else str(node.status)
            )
            # 废弃/已停节点（superseded → Stopped，或用户手动停止）不可再路由：
            # 它不参与后续评估，事件投进去也不会被 Scheduler 消费（非可重激活态），纯浪费一次路由与投递。
            if status == NodeStatus.STOPPED.value:
                continue
            out.append({
                "id": node.id,
                "step_ref": node.step_ref or "",
                "exec_type": node.exec_type,
                "role": node.role,
                "status": status,
                "org_path": self._org_path(node),
                "task": self._truncate(node.task or ""),
                # 职责索引：优先 task_summary（manager expand 时产出），缺失回退 task 截断
                "task_summary": getattr(node, "task_summary", None)
                or self._truncate(node.task or ""),
            })
        self._snapshot_version = version
        self._cached_snapshot = out
        return out

    @staticmethod
    def _truncate(text: str, limit: int = 140) -> str:
        """截断文本到指定长度并加省略号。"""
        s = (text or "").strip().replace("\n", " ")
        if len(s) <= limit:
            return s
        return s[:limit].rstrip() + "…"

    # ------------------------------------------------------------------
    # prompt 构建与目标解析
    # ------------------------------------------------------------------

    def _build_prompt(
        self, event: EventMessage, producer_id: str, snapshot: list[dict]
    ) -> str:
        """构建路由 prompt：Event + Producer 上下文 + 组织图清单 + 输出要求。"""
        producer_ctx = "（图中未找到）"
        if self._graph is not None:
            pnode = self._graph.get_node(producer_id)
            if pnode is not None:
                status = (
                    pnode.status.value
                    if hasattr(pnode.status, "value") else str(pnode.status)
                )
                producer_ctx = (
                    f"id=`{pnode.id}` · step_ref=`{pnode.step_ref or '-'}` · "
                    f"exec_type=`{pnode.exec_type}` · role=`{pnode.role}` · "
                    f"status=`{status}` · org_path=`{self._org_path(pnode)}` · "
                    f"task=`{self._truncate(pnode.task or '')}`"
                )

        intent = getattr(event, "intent", "") or "notification"
        scope = getattr(event, "scope", "") or "related"
        action = getattr(event, "action", "") or ""
        objective = getattr(event, "objective", "") or ""
        affected_roles = getattr(event, "affected_roles", None) or []

        lines = [
            "## Route Event",
            "",
            "你需要决定这条事件是否值得交给任何节点处理，以及交给谁。",
            "基于「事件信息 + 生产者信息 + 当前组织图」做统一判断。",
            "事件字段（objective/intent/affected_roles/scope/action）都是生产者的参考信号，",
            "不含路由指令；目标由你从组织图中独立选择。",
            "",
            "### 先判断：ignore（无人需要处理）vs deliver（值得投递）",
            "",
            "**应返回空数组（ignore）的情况**：",
            "- 例行进度 / 状态同步（如「进度到 60%」「正在做」「任务收尾」）；",
            "- 纯「完成 / 验证通过」声明，且不要求任何节点采取行动；",
            "- 与生产者近期已发布的事件重复（同一信息已通知过）；",
            "- 没有 actionable 需求（没有任何节点需要因此改变工作内容或做决策）。",
            "",
            "**应投递（deliver）的情况**：",
            "- objective 明确要求某类节点采取行动 / 决策 / 协作 / 升级；",
            "- 信息对目标节点是新的，且会改变其后续工作内容；",
            "- 目标节点的职责范畴（role / task_summary 所描述的「这个岗位负责什么」）能覆盖事件"
            "所需的事项 —— 把事件投给职责范畴不匹配的节点等于空唤醒，它无权也不该处理这件事。",
            "",
            "**判断职责范畴是否匹配，是你的工作**：逐条读事件，再看组织图里每个节点的 role 与 "
            "task_summary，问「这个节点的职责能承接这件事吗」。匹配不上就不要投给它（宁缺毋滥）；"
            "所有节点都匹配不上就返回空数组。",
            "",
            "### 事件",
            f"- event_id: `{event.event_id}`",
            f"- producer (node_id): `{producer_id}`",
            f"  - {producer_ctx}",
            f"- intent（事件性质）: `{intent}`",
            f"- scope（建议搜索范围）: `{scope}`",
            "",
            "### 事件描述（objective）",
            objective,
            "",
        ]
        if action:
            lines += [
                "### 建议动作（action）",
                action,
                "",
            ]
        if affected_roles:
            lines += [
                "### 受影响角色（affected_roles，参考信号）",
            ]
            for ar in affected_roles:
                if isinstance(ar, dict):
                    role = ar.get("role", "")
                    duty = ar.get("duty", "")
                    lines.append(f"- role=`{role}` · duty=`{duty}`")
                else:
                    lines.append(f"- {ar}")
            lines.append("")
        lines += [
            "### 当前组织图（节点清单）",
        ]
        if not snapshot:
            lines.append("（无节点）")
        for n in snapshot:
            lines.append(
                f"- `{n['step_ref'] or n['id']}` · id=`{n['id'][:8]}…` · "
                f"exec_type=`{n['exec_type']}` · role=`{n['role']}` · "
                f"status=`{n['status']}` · org=`{n['org_path']}` · "
                f"task_summary=`{n['task_summary']}`"
            )
        lines += [
            "",
            "### 输出要求",
            "只返回 JSON：",
            '{"targets": ["<step_ref 或 node_id>", "..."], "reason": "<一句话理由>"}',
            "",
            "- targets 为组织图中任一节点（Manager 或 Worker），空数组 = 无人接收（含 ignore）。",
            "- reason 用一句话说明为什么投递这些目标 / 或为什么无人需要处理。",
            "- 目标可以是 step_ref 或 node_id。",
            "- scope 是生产者的建议搜索范围（current=仅生产者自身 / subtree=生产者子树 /",
            "  related=相关节点 / global=全局），优先在范围内选；仅当确有必须响应者在",
            "  范围外时才越界。",
            "- 只基于事件本身与组织图判断，不要臆造不存在的节点。",
        ]
        return "\n".join(lines)

    def _parse_targets(self, result: Any) -> list[str]:
        """解析 AI 输出 ``{"targets": [...]}`` → 图中 node_id 列表（step_ref 解析为 id）。

        容忍 AI 返回 dict / JSON 字符串 / 含围栏的文本；解析失败抛 ``_RouteParseError``
        （与「解析出空 targets」严格区分，后者正常返回空列表）。

        real 模式下 ``AICU.ask()`` 返回**元数据 dict**（``content`` / ``content_parsed`` /
        ``final_output_path``，与 manager._extract_review_content 同源问题），顶层没有
        ``targets`` 键。因此先做一次归一化：顶层有 ``targets`` 直接用；
        否则按 content_parsed → content → final_output_path 优先级提取真实决策载荷。
        """
        # ---- real AICU 元数据 dict 归一化（顶层无 targets 时） ----
        if isinstance(result, dict) and "targets" not in result:
            extracted = self._extract_targets_payload(result)
            if extracted is None:
                raise _RouteParseError("real AICU 元数据 dict 无可提取的决策载荷")
            result = extracted
        raw = result
        if isinstance(raw, str):
            text = raw.strip()
            # 去掉可能的 ```json ... ``` 围栏
            if text.startswith("```"):
                text = text.strip("`")
                if text.lower().startswith("json"):
                    text = text[4:].lstrip("\n").strip()
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                # 尝试提取第一个 {...}
                start = text.find("{")
                end = text.rfind("}")
                if start >= 0 and end > start:
                    try:
                        parsed = json.loads(text[start : end + 1])
                    except json.JSONDecodeError:
                        raise _RouteParseError("JSON 字符串解码失败")
                else:
                    raise _RouteParseError("输出不含 JSON 对象")
        elif isinstance(raw, dict):
            parsed = raw
        else:
            raise _RouteParseError(f"输出类型非 str/dict: {type(raw).__name__}")
        if not isinstance(parsed, dict):
            raise _RouteParseError(f"解析结果非 dict: {type(parsed).__name__}")
        reason = parsed.get("reason")
        if isinstance(reason, str) and reason.strip():
            self._last_reason = reason.strip()
        targets = parsed.get("targets")
        if not isinstance(targets, list):
            raise _RouteParseError("targets 字段缺失或非 list")
        resolved: list[str] = []
        for ref in targets:
            if not isinstance(ref, str):
                continue
            node_id = self._resolve_node_id(ref)
            if node_id is not None and node_id not in resolved:
                resolved.append(node_id)
        return resolved

    @staticmethod
    def _extract_targets_payload(result: dict) -> Any:
        """从 real AICU 元数据 dict 提取决策载荷（None = 无可用载荷）。

        优先级与 manager._extract_review_content 对齐：
          1. content_parsed（已是 dict）→ 直接用
          2. content（dict 或 JSON 字符串）→ 用
          3. final_output_path（文件可读）→ 读文本
        """
        cp = result.get("content_parsed")
        if isinstance(cp, dict):
            return cp
        content = result.get("content")
        if isinstance(content, dict):
            return content
        if isinstance(content, str) and content.strip():
            return content
        output_path = result.get("final_output_path")
        if isinstance(output_path, str) and os.path.isfile(output_path):
            try:
                with open(output_path, "r", encoding="utf-8") as f:
                    return f.read()
            except OSError:
                return None
        return None
