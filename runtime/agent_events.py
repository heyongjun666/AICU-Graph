"""
runtime/agent_events.py — Agent 事件总线（Node → Router → Node）

共享工作区解决「共享实际产物」；Event Bus 解决「告诉别人有新产物/状态发生了」。
**DAG 决定节点能不能开始；Event 描述执行过程中发生了什么**。两者并存、互不干扰。

存储模型（四对象职责明确）：
  {node_ws}/events/*.json   发布入口（CC 写，Runtime 摄取后删）→ Producer outbox
  {workspace}/events.json   事件日志（历史/去重/重放源，事实来源；摄取新事件后立即落盘）
  {node_ws}/event_inbox/    待处理队列（持久，下次运行注入 prompt）→ **唯一待消费队列**
  {node_ws}/events_inbox/   实时提醒（运行中 CC 轮询，只读信号）→ RUNNING 实时目录

V3.1 P0#4：每节点队列路径统一契约 = ``event_queue_dirs(node)``（outbox/inbox/live 三目录），
worker/manager prompt 构建器与投递侧共用，物理布局不变（EventQueue 语义收敛，不加新存储类）。

方案三（独立 AICU Router）设计约束：
  - Event 不参与调度：路由/投递绝不触发状态转换、绝不创建节点，只写 inbox 目录。
  - 路由 = AICU Router：所有事件经 ``router.route(msg)`` 由 LLM 结合当前组织图决策目标
    Node 列表，**不区分 Manager/Worker**（目标可为任意 Node，跨 Manager 直接通信）。
    无订阅广播——Router 是唯一路由形态。
  - **单次消费语义**：inbox（``event_inbox/``）是每个事件的**唯一待消费队列**；
    live（``events_inbox/``）只是「运行中 Node 消费 inbox 的实时信号」，不构成第二份
    待消费副本。两条消费路径（RUNNING 节点 CC 有界轮询 / Scheduler 桥扫非运行态）
    都 delete-on-consume：任一路径先消费即删 inbox 文件，另一条路径因文件已不存在
    而天然不再处理，避免「live 消费 + inbox 消费」二次处理。
  - 线程安全：日志/路由/投递全在单把 _lock 内；锁顺序固定 agent → graph，反向禁止。
  - 消费简化：CC 读 event_inbox → 处理 → 删除；历史留 events.json。无 ACK/回执。
  - **事件状态与路由记录（重启不重跑 LLM）**：
      ``routed_to`` = Router 一次决策的产物（event_id → 目标 node_id 列表），属框架元数据
      而非事件内容；``status`` = 投递生命周期 ``routed → delivered → consumed``。
      - ``routed``：Router 已决策（含空目标）；若 crash 在投递前，重启 load() 时按
        ``routed_to`` 记录**确定性重投**（幂等写 inbox，不重跑 LLM）；
      - ``delivered``：所有目标 inbox 文件已落盘（空目标视为 delivered 终态）；
      - ``consumed``：全部目标 inbox 文件已删除（delete-on-consume 即消费）。该状态由
        save() 前的一次 reconcile 推断（delivered 且 inbox 全空 → consumed），消费方无需
        显式回调。
"""

import glob
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional

from runtime.models import EventMessage, NodeStatus

logger = logging.getLogger(__name__)

# RUNNING 状态额外投递实时 events_inbox 通知（其余状态仅持久 event_inbox）
_LIVE_DELIVERY_STATUSES = (NodeStatus.RUNNING,)

# objective 简短性硬约束（方案三：objective 就是「几句话」的通知，如
# 「本环节产出已写入共享产物文件，需要后续环节节点读取后继续推进」，不是报告）。
# 产出即验证——超出则拒收并投递反馈让生产者重写（不是截断，是打回重写）。
MAX_OBJECTIVE_SENTENCES = 3
MAX_OBJECTIVE_CHARS = 80
# 句末标点（与 worker prompt「每句以句号或分号结束」一致）+ 换行
_SENTENCE_SPLIT_RE = re.compile(r"[。；！？!?;\n]+")


def _count_objective_sentences(text: str) -> int:
    """按句末标点/换行拆分 objective，统计非空句子数。"""
    return len([p for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()])


def _objective_budget_reason(objective: str) -> str | None:
    """objective 超出「≤3 句 / ≤80 字」简短约束时返回原因，否则 None。"""
    chars = len(objective)
    sentences = _count_objective_sentences(objective)
    if chars > MAX_OBJECTIVE_CHARS and sentences > MAX_OBJECTIVE_SENTENCES:
        return (
            f"objective 过长（{chars} 字 / {sentences} 句），必须 ≤{MAX_OBJECTIVE_SENTENCES} 句"
            f"且 ≤{MAX_OBJECTIVE_CHARS} 字"
        )
    if chars > MAX_OBJECTIVE_CHARS:
        return f"objective 过长（{chars} 字），必须 ≤{MAX_OBJECTIVE_CHARS} 字"
    if sentences > MAX_OBJECTIVE_SENTENCES:
        return (
            f"objective 超过 {MAX_OBJECTIVE_SENTENCES} 句话（{sentences} 句），"
            f"请压缩为简短摘要"
        )
    return None


# 摄取端噪声过滤（V3.1 P2：配合 Router 语义拒绝，拦截「随意事件」）：
#   - 同 producer 短窗口内近似重复的 objective → 吞掉（不路由/不投递/不唤醒）；
#   - 同 producer 窗口内事件数超上限 → 吞掉（事件风暴保护）。
# 判定全部确定性、零 LLM 成本，在 Router 之前拦截明显噪声。
_DEDUP_WINDOW_SECONDS = 120.0
_MAX_EVENTS_PER_PRODUCER_WINDOW = 5
# 目标规范化的空白/标点（近似重复判定用）
_NORM_WS_RE = re.compile(r"[\s　，。；、,.!?！？:：;\"'“”‘’（）()\[\]【】\-—_/\\]+")


def _normalize_objective(text: str) -> str:
    """规范化 objective 指纹：去空白/标点、小写（近似重复判定用）。"""
    return _NORM_WS_RE.sub("", text or "").lower()


def _objectives_similar(a: str, b: str) -> bool:
    """两条**已规范化** objective 是否近似重复（确定性、保守）。

    完全相等 → True；``difflib.SequenceMatcher`` ratio ≥0.8 → True（同 producer
    「换个说法重发」：语义相同、措辞略变）。阈值从 0.9 降到 0.8 以拦截跨轮次
    重发（实测逃过的重发 ratio 多落在 0.81~0.84），同时保留明显余量——合法的
    递进更新因新内容占比显著下降而落在阈值之下。刻意**不含**"单向包含"规则：
    后一条包含前一条全文并追加新内容不应被误判为重复；事件风暴由独立限流
    （_MAX_EVENTS_PER_PRODUCER_WINDOW）兜底。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.8


def _iso_now() -> str:
    """当前时间 ISO 格式（秒级精度）。"""
    return datetime.now().isoformat(timespec="seconds")


def _gen_event_id() -> str:
    """生成全局唯一事件 ID。"""
    return f"evt_{uuid.uuid4().hex[:12]}"


def _node_workspace(node: Any) -> str:
    """从节点 config 解析节点私有 workspace（与 runtime.get_node_workspace 同源）。"""
    config = node.config or {}
    root = os.path.abspath(config.get("_workspace_root", os.getcwd()))
    relpath = config.get("_workspace_relpath", "")
    return os.path.realpath(os.path.join(root, relpath))


def event_queue_dirs(node: Any) -> dict[str, str]:
    """节点私有事件队列目录（V3.1 P0#4 EventQueue 统一契约）。

    每个 Node 都是 Producer + Consumer，各有自己的 event queue；物理布局
    （agent_events 模块顶部注释）与文档「publish → Event Bus → event_queue →
    下次执行时消费」一一对应：

      - ``outbox``:  Producer 发布入口（CC 写，Runtime 摄取后删）
      - ``inbox``:   持久 Consumer queue（下次运行注入 prompt）
      - ``live``:    RUNNING 节点实时投递目录（运行中 CC 有界轮询）

    worker/manager prompt 构建器与投递侧共用此契约，保证路径单一事实来源。
    """
    ws = _node_workspace(node)
    return {
        "outbox": os.path.join(ws, "events"),       # Producer 发布入口
        "inbox":  os.path.join(ws, "event_inbox"),   # 持久 Consumer queue
        "live":   os.path.join(ws, "events_inbox"),  # RUNNING 实时目录
    }


def list_inbox(node: Any) -> list[dict]:
    """只读列出节点 event_inbox 中的待处理事件（解析后的 dict）。

    不删除文件——消费由调用方负责（Scheduler 桥转换后删除 / CC 处理后删除）。
    目录不存在或文件损坏时静默跳过，与投递侧容错一致。
    """
    inbox_dir = event_queue_dirs(node)["inbox"]
    if not os.path.isdir(inbox_dir):
        return []
    out: list[dict] = []
    for path in sorted(glob.glob(os.path.join(inbox_dir, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


class AgentEventManager:
    """Agent 事件管理器：摄取、路由、投递、日志持久化与重放。

    单把 ``_lock`` 保护所有事件日志/路由/投递操作；graph 访问只读，
    且只通过 graph 自身的锁保护方法（如 update_node）变更，保证锁顺序
    agent → graph 无反向，避免死锁。

    方案三：路由完全委托给注入的 ``AICURouter``（Event + 组织图 → 目标 Nodes），
    Manager 只负责摄取/投递/日志，不做任何路由判断。
    """

    def __init__(self, workspace_root: str = ".", router: Any = None):
        self._workspace_root = os.path.abspath(workspace_root)
        # event_id → EventMessage（dict 保持插入序 = 全局顺序）
        self._events: dict[str, EventMessage] = {}
        self._lock = threading.Lock()
        self._graph: Any = None
        self._router: Any = router
        self._route_attempts: dict[str, int] = {}
        # 摄取端噪声过滤状态：producer -> [(monotonic_ts, 规范化 objective 指纹)]
        self._producer_recent: dict[str, list[tuple[float, str]]] = {}
        self._deduped_count = 0
        # 路由端历史去重计数（与已产出历史事件判重后不投递的事件数）
        self._route_deduped_count = 0

    def recover_pending(self) -> None:
        """Recover pending delivery after the owning Scheduler opens dispatch."""
        with self._lock:
            if not self._events:
                return
            self._redeliver_routed_locked()
            self._reconcile_statuses_locked()
        self.save()

    # ------------------------------------------------------------------
    # Graph 引用注入（延迟，避免循环导入）
    # ------------------------------------------------------------------

    def attach_graph(self, graph: Any) -> None:
        """注入 Graph 引用（路由/重放依赖节点列表与订阅配置）。"""
        self._graph = graph
        if self._router is not None:
            self._router.attach_graph(graph)

    # ------------------------------------------------------------------
    # 事件日志持久化（events.json，原子写盘）
    # ------------------------------------------------------------------

    @property
    def events_file(self) -> str:
        """全局事件日志路径。"""
        return os.path.join(self._workspace_root, "events.json")

    @property
    def router_traces_file(self) -> str:
        """Append-only structured route audit log for offline training."""
        return os.path.join(self._workspace_root, "router_traces.jsonl")

    def _append_router_trace(self, trace: dict[str, Any]) -> None:
        """Persist one bounded route trace without affecting event delivery."""
        try:
            os.makedirs(self._workspace_root, exist_ok=True)
            with open(self.router_traces_file, "a", encoding="utf-8") as file:
                file.write(json.dumps(trace, ensure_ascii=False, default=str) + "\n")
                file.flush()
        except OSError as exc:
            logger.error("Failed to append router trace: %s", exc)

    def load(self, *, recover_delivery: bool = True) -> None:
        """Load the session event log without requiring delivery recovery.

        ``recover_delivery`` remains enabled by default for standalone callers.
        Runtime startup disables it so loading persisted session state cannot
        invoke the Router before that session's Scheduler opens dispatch.
        """
        path = self.events_file
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load agent events %s: %s", path, exc)
            return
        if not isinstance(data, list):
            logger.error("Agent events log %s is not a list; skipping", path)
            return
        with self._lock:
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    msg = EventMessage.from_dict(item)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Skipping malformed event log entry: %s", exc)
                    continue
                if msg.event_id and msg.event_id not in self._events:
                    self._events[msg.event_id] = msg
            if recover_delivery:
                self._redeliver_routed_locked()
                self._reconcile_statuses_locked()
        # Standalone callers retain the historical load-and-reconcile behavior.
        if recover_delivery:
            self.save()

    def save(self) -> None:
        """原子写 events.json（tmp + fsync + rename，镜像 Persistence）。

        写前先 reconcile 事件状态（routed→delivered / delivered→consumed 由 inbox
        实际文件状态推断），保证日志与投递事实一致。

        调用时机：publish_events / scan_node 摄取到新事件后立即调用，加上 shutdown
        最终保存——每次产出的 event 都即时落盘（不再只等 shutdown）。save 失败仅
        log 不抛（幂等，下次摄取/关闭会重写补齐）。
        """
        with self._lock:
            self._reconcile_statuses_locked()
            items = [m.to_dict() for m in self._events.values()]
        tmp = self.events_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.events_file), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp, self.events_file)
        except OSError as exc:
            logger.error("Failed to save agent events: %s", exc)

    # ------------------------------------------------------------------
    # 发布（程序化入口：Runtime API / 测试）
    # ------------------------------------------------------------------

    def publish_events(self, producer: str, events: list[dict]) -> list[str]:
        """直接发布事件（补 event_id/created_at，按 event_id 去重，摄取并路由）。

        摄取新事件后立即落盘 events.json（每次产出即持久化，非仅 shutdown）。

        格式校验：content 非空字符串为合法；非字符串自动规范化（json.dumps）；
        空 content 判定非法，拒收并投递反馈事件给生产者（触发重新生成）。

        Args:
            producer: 生产者节点 ID。
            events: 原始事件 dict 列表。

        Returns:
            本次新增事件的 event_id 列表。
        """
        ok_raws, bad_entries = self._prepare_event_raws(producer, events)
        for entry in bad_entries:
            self._reject_event(
                producer, entry["raw"], entry["reason"], source_path=None,
            )
        with self._lock:
            ok_raws, dropped = self._filter_noise_locked(producer, ok_raws)
            for raw, reason in dropped:
                self._log_dropped_event(producer, raw, reason)
            ingested = self._ingest_locked(producer, ok_raws)
            if ingested:
                self._route_locked(ingested)
        if ingested:
            self.save()
        return [m.event_id for m in ingested]

    # ------------------------------------------------------------------
    # 摄取发布入口（events/*.json）
    # ------------------------------------------------------------------

    def scan_node(self, node_id: str) -> int:
        """摄取节点发布入口 ``{node_ws}/events/*.json``。

        按 event_id 去重后加入全局日志并路由；摄取后删除源文件（幂等）。
        容忍文件不存在（并发删除/未创建）。

        Args:
            node_id: 目标节点 ID。

        Returns:
            本次新摄取的事件数。
        """
        if self._graph is None:
            return 0
        node = self._graph.get_node(node_id)
        if node is None:
            return 0
        pub_dir = event_queue_dirs(node)["outbox"]
        matches = glob.glob(os.path.join(pub_dir, "*.json"))
        if not matches:
            return 0
        ok_raws: list[dict] = []
        for path in matches:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Failed to parse publish file %s: %s", path, exc)
                continue
            if isinstance(data, dict):
                items = [data]
            elif isinstance(data, list):
                items = [item for item in data if isinstance(item, dict)]
            else:
                items = []
            ok_items, bad_entries = self._prepare_event_raws(node_id, items)
            ok_raws.extend(ok_items)
            if bad_entries:
                # 格式非法：拒收，改名 .invalid 留存现场，投递反馈事件让生产者重新生成
                for entry in bad_entries:
                    self._reject_event(
                        node_id, entry["raw"], entry["reason"], source_path=path,
                    )
                try:
                    os.rename(path, path + ".invalid")
                except OSError as exc:
                    logger.warning(
                        "Failed to mark invalid publish file %s: %s", path, exc,
                    )
            else:
                # 合法：摄取后删除源文件（无论是否已去重）
                try:
                    os.remove(path)
                except OSError as exc:
                    logger.warning("Failed to remove publish file %s: %s", path, exc)
        if not ok_raws:
            return 0
        with self._lock:
            ok_raws, dropped = self._filter_noise_locked(node_id, ok_raws)
            for raw, reason in dropped:
                self._log_dropped_event(node_id, raw, reason)
            ingested = self._ingest_locked(node_id, ok_raws)
            if ingested:
                self._route_locked(ingested)
        if ingested:
            self.save()
        return len(ingested)

    def scan_all(self) -> int:
        """扫描所有节点的事件发布入口（调度线程 tick 调用）。

        Returns:
            本次新摄取的事件总数。
        """
        if self._graph is None:
            return 0
        total = 0
        for node_id in list(self._graph.nodes.keys()):
            try:
                total += self.scan_node(node_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("scan_node(%s) failed: %s", node_id, exc)
        return total

    # ------------------------------------------------------------------
    # 摄取端噪声过滤（V3.1 P2：配合 Router 语义拒绝）
    #   - 同 producer 短窗口内近似重复的 objective → 吞掉（不路由/不投递/不唤醒）
    #   - 同 producer 窗口内事件数超上限 → 吞掉（事件风暴保护）
    # 判定全部确定性、零 LLM 成本，在 Router 之前拦截明显噪声。
    # ------------------------------------------------------------------

    def _filter_noise_locked(
        self, producer: str, raws: list[dict],
    ) -> tuple[list[dict], list[tuple[dict, str]]]:
        """摄取端噪声过滤（锁内调用，维护 per-producer 最近事件指纹）。

        Returns:
            ``(kept_raws, dropped)``；dropped 每项 ``(raw, reason)``，reason ∈
            {duplicate_objective, rate_limited}。
        """
        if not raws:
            return raws, []
        now = time.monotonic()
        recent = [
            (ts, fp) for ts, fp in self._producer_recent.get(producer, [])
            if now - ts <= _DEDUP_WINDOW_SECONDS
        ]
        kept: list[dict] = []
        dropped: list[tuple[dict, str]] = []
        for raw in raws:
            objective = raw.get("objective") or raw.get("content") or ""
            fp = _normalize_objective(objective)
            if not fp:
                kept.append(raw)  # 空 objective 已由格式层拒收，这里不重复处理
                continue
            if len(recent) >= _MAX_EVENTS_PER_PRODUCER_WINDOW:
                dropped.append((raw, "rate_limited"))
                continue
            if any(_objectives_similar(fp, old_fp) for _, old_fp in recent):
                dropped.append((raw, "duplicate_objective"))
                continue
            recent.append((now, fp))
            kept.append(raw)
        self._producer_recent[producer] = recent
        return kept, dropped

    def _log_dropped_event(self, producer: str, raw: dict, reason: str) -> None:
        """记录被噪声过滤吞掉的事件（log + 落 router_trace 供审计/训练）。"""
        self._deduped_count += 1
        ev_id = raw.get("event_id") or "<generated>"
        logger.info(
            "Suppressed event '%s' from '%s' (%s)", ev_id, producer, reason,
        )
        self._append_router_trace({
            "trace_version": 1,
            "event_id": ev_id,
            "event": {
                "producer": producer,
                "objective": raw.get("objective") or raw.get("content", ""),
            },
            "route_attempt": 0,
            "route_status": "suppressed",
            "router": {"decision": "noise_filter", "reason": reason},
            "delivery": {"targets": {}},
        })

    # ------------------------------------------------------------------
    # 事件格式校验（产出即验证：content 必须是非空字符串）
    # ------------------------------------------------------------------

    def _prepare_event_raws(
        self, producer: str, raws: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """校验/规范化事件 raw dicts（objective 必须是非空字符串，且就几句话 ≤3 句 / ≤80 字）。

        - objective 为非空字符串且满足简短约束 → 合法（原样放行）。
        - objective 为空 / 缺失 / 超过 3 句 / 超过 80 字 → 非法（拒收并反馈重写）。
        - 向后兼容：无 objective 但有 content → 以 content 判定合法性（老 producer
          仍写 content 的过渡期可正常摄入；真正的迁入由 ``EventMessage.from_dict``
          的 ``objective or content`` 完成）。
        - objective 非字符串但有内容（dict/list/数字等）→ 自动规范化（json.dumps）
          后放行，作为兜底修复（正常路径由提示词保证 objective 为字符串，此分支仅防御）。

        Returns:
            ``(ok_raws, bad_entries)``；bad_entries 每项为 ``{"raw", "reason"}``。
        """
        ok_raws: list[dict] = []
        bad_entries: list[dict] = []
        for raw in raws:
            objective = raw.get("objective")
            # 向后兼容：旧格式只有 content（把 content 当作 objective 判合法性）
            if objective is None and "content" in raw:
                objective = raw.get("content")
            if isinstance(objective, str):
                if objective.strip():
                    reason = _objective_budget_reason(objective.strip())
                    if reason:
                        bad_entries.append({"raw": raw, "reason": reason})
                    else:
                        ok_raws.append(raw)
                else:
                    bad_entries.append({"raw": raw, "reason": "objective 为空字符串"})
                continue
            if objective is None:
                bad_entries.append({"raw": raw, "reason": "objective 字段缺失"})
                continue
            # 非字符串但有内容：规范化（json.dumps）作为兜底修复
            normalized = dict(raw)
            try:
                normalized["objective"] = json.dumps(
                    objective, ensure_ascii=False, default=str,
                )
            except (TypeError, ValueError):
                normalized["objective"] = str(objective)
            logger.warning(
                "Event from producer '%s' has non-string objective (%s); "
                "normalized to JSON string",
                producer, type(objective).__name__,
            )
            ok_raws.append(normalized)
        return ok_raws, bad_entries

    def _reject_event(
        self, producer: str, raw: dict, reason: str, source_path: str | None,
    ) -> None:
        """格式非法事件处理：不摄入；投递反馈事件给生产者 inbox 触发重新生成。

        反馈事件投递进生产者 inbox 后，由 Scheduler 事件→重激活桥在下一 tick
        激活生产者（若其处于可激活状态），生产者看到反馈后用非空字符串重新发布。

        Args:
            producer: 生产者节点 ID。
            raw: 原始（非法）事件 dict（未摄入，仅用于取 event_id 留痕）。
            reason: 非法原因。
            source_path: 源发布文件路径（scan 入口）；程序化入口为 None。
        """
        ev_id = raw.get("event_id") or (
            os.path.basename(source_path) if source_path else "unknown"
        )
        logger.error(
            "Rejected invalid event '%s' from '%s': %s",
            ev_id, producer, reason,
        )
        if self._graph is None:
            return
        node = self._graph.get_node(producer)
        if node is None:
            logger.warning(
                "Cannot deliver format feedback: producer '%s' not in graph",
                producer,
            )
            return
        feedback_text = (
            f"EVENT_FORMAT_INVALID: 你发布的事件 {ev_id} 格式非法（{reason}），"
            f"已被框架拒收、未投递。请重新发布一条符合约束的新事件"
            f"（在事件发布目录写一个新的 JSON 文件：objective 必须是非空字符串，"
            f"且 ≤{MAX_OBJECTIVE_SENTENCES} 句、≤{MAX_OBJECTIVE_CHARS} 字）。"
        )
        feedback = EventMessage(
            event_id=f"evt_fmt_invalid_{str(ev_id)[:20]}",
            producer=producer,
            objective=feedback_text,
            created_at=_iso_now(),
            status=EventMessage.EVENT_STATUS_DELIVERED,
        )
        self._deliver(node, feedback)

    # ------------------------------------------------------------------
    # 路由与投递
    # ------------------------------------------------------------------

    def _ingest_locked(
        self, producer: str, events: list[dict]
    ) -> list[EventMessage]:
        """在锁内将原始事件 dict 规范化为 EventMessage 并加入日志（按 event_id 去重）。

        方案三：不再要求 type（事件极简为 event_id/producer/content）。
        """
        ingested: list[EventMessage] = []
        for raw in events:
            msg = EventMessage.from_dict(raw)
            if not msg.producer:
                msg.producer = producer
            if not msg.event_id:
                msg.event_id = _gen_event_id()
            if not msg.created_at:
                msg.created_at = _iso_now()
            if msg.event_id in self._events:
                continue  # 全局去重
            self._events[msg.event_id] = msg
            ingested.append(msg)
        return ingested

    def _route_locked(self, messages: list[EventMessage]) -> None:
        """把新摄取的事件路由投递到目标节点 inbox（只在锁内调用）。

        方案三：所有事件经 ``self._router.route(msg)``（LLM：Event + 组织图 →
        目标 Node 列表）决策，对每个目标 ``_deliver`` 投递。Router 是唯一路由形态：
        - 无订阅广播；
        - Router 目标可以是任意 Node（Worker/Manager），Manager 不区分拦截；
        - 目标解析由 Router 完成，此处只按返回的 node_id 投递。

        路由决策产物落回事件本身：
        - ``msg.routed_to`` = 本次目标 node_id 列表（记录在案，重启按它确定性重投，
          不重跑 LLM）；
        - ``msg.status``：空目标（无人接收）→ delivered 终态；有目标 → 全部 inbox
          落盘后 delivered，投递失败保持 routed（下次 load() 重投）。
        """
        if self._graph is None:
            return
        if self._router is None:
            raise RuntimeError("AgentEventManager requires a configured AICURouter")
        for msg in messages:
            # 确定性短路：图中除生产者外没有其他节点 → 无人可接收，直接落
            # delivered 终态，**不调用 LLM Router**（避免为明显无人接收的事件
            # 付出一次路由调用）。生产者不在图中时保守放行，交给 Router 判断。
            if len(self._graph.nodes) <= 1 and msg.producer in self._graph.nodes:
                msg.routed_to = []
                msg.status = EventMessage.EVENT_STATUS_DELIVERED
                self._append_router_trace({
                    "trace_version": 1,
                    "event_id": msg.event_id,
                    "event": msg.to_dict(),
                    "route_attempt": 0,
                    "route_status": "delivered",
                    "router": {"decision": "no_other_nodes", "latency_ms": 0.0},
                    "delivery": {"targets": {}},
                })
                continue
            attempt = self._route_attempts.get(msg.event_id, 0) + 1
            self._route_attempts[msg.event_id] = attempt
            # 路由历史去重（P2）：与已产出的**同 producer** 历史事件比对，判定重复
            # 则不调用 LLM Router、不投递，直接落 delivered 终态（完成状态，保留
            # 审计 trace）。与摄取端 _filter_noise_locked 互补：前者挡 120s 短窗
            # 事件风暴，此处挡跨窗口的长期重复（如节点重跑后一字不差的重发）。
            dup_of = self._find_duplicate_locked(msg)
            if dup_of:
                msg.routed_to = []
                msg.status = EventMessage.EVENT_STATUS_DELIVERED
                self._route_deduped_count += 1
                self._append_router_trace({
                    "trace_version": 1,
                    "event_id": msg.event_id,
                    "event": msg.to_dict(),
                    "route_attempt": attempt,
                    "route_status": "delivered",
                    "router": {
                        "decision": "duplicate_suppressed",
                        "duplicate_of": dup_of,
                        "latency_ms": 0.0,
                    },
                    "delivery": {"targets": {}},
                })
                continue
            started = time.monotonic()
            try:
                targets = self._router.route(msg)
            except Exception as exc:  # noqa: BLE001
                elapsed_ms = round((time.monotonic() - started) * 1000, 2)
                msg.status = EventMessage.EVENT_STATUS_ROUTE_FAILED
                router_trace = getattr(self._router, "decision_trace", lambda: {})()
                if not isinstance(router_trace, dict):
                    router_trace = {}
                trace = {
                    "trace_version": 1,
                    "event_id": msg.event_id,
                    "event": msg.to_dict(),
                    "route_attempt": attempt,
                    "route_status": "failed",
                    "router": {
                        **router_trace,
                        "latency_ms": elapsed_ms,
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    },
                    "delivery": {"targets": {}},
                }
                self._append_router_trace(trace)
                logger.error(
                    "Router failed for event '%s': %s; marked for retry",
                    msg.event_id, exc,
                )
                continue
            elapsed_ms = round((time.monotonic() - started) * 1000, 2)
            msg.routed_to = [str(t) for t in targets]
            router_trace = getattr(self._router, "decision_trace", lambda: {})()
            if not isinstance(router_trace, dict):
                router_trace = {}
            router_trace["parsed_targets"] = list(msg.routed_to)
            if not msg.routed_to:
                # 无人接收：无投递动作，delivered 即终态
                msg.status = EventMessage.EVENT_STATUS_DELIVERED
                self._append_router_trace({
                    "trace_version": 1,
                    "event_id": msg.event_id,
                    "event": msg.to_dict(),
                    "route_attempt": attempt,
                    "route_status": "delivered",
                    "router": {
                        **router_trace,
                        "latency_ms": elapsed_ms,
                    },
                    "delivery": {"targets": {}},
                })
                continue
            msg.status = EventMessage.EVENT_STATUS_ROUTED
            delivery_targets: dict[str, dict[str, Any]] = {}
            for target_id in msg.routed_to:
                target_node = self._graph.get_node(target_id)
                if target_node is None:
                    delivery_targets[target_id] = {
                        "status": "target_missing",
                        "attempt": attempt,
                    }
                    logger.warning(
                        "Event '%s' router target '%s' not found in graph; skipped",
                        msg.event_id, target_id,
                    )
                    continue
                self._deliver(target_node, msg)
                delivered = self._target_inbox_present(msg, target_id)
                delivery_targets[target_id] = {
                    "status": "delivered" if delivered else "delivery_failed",
                    "attempt": attempt,
                }
            if self._all_targets_inbox_present(msg):
                msg.status = EventMessage.EVENT_STATUS_DELIVERED
            self._append_router_trace({
                "trace_version": 1,
                "event_id": msg.event_id,
                "event": msg.to_dict(),
                "route_attempt": attempt,
                "route_status": "delivered" if msg.status == EventMessage.EVENT_STATUS_DELIVERED else "routed",
                "router": {
                    **router_trace,
                    "latency_ms": elapsed_ms,
                },
                "delivery": {"targets": delivery_targets},
            })
            # 投递失败（inbox 未全落盘）保持 routed → 下次 load() 幂等重投

    def _find_duplicate_locked(self, msg: EventMessage) -> str | None:
        """与已产出的**同 producer** 历史事件比对，返回重复对象的 event_id。

        确定性判定：规范化 objective 指纹 + ``_objectives_similar``（≥0.8），与
        摄取端同阈值，零 LLM 成本。只比对同 producer：不同节点的相似目标属于
        各自独立的协作请求，不应压制（避免误杀跨节点真实协作）。历史以
        ``self._events`` 为准（含 load() 从 events.json 恢复的全量历史事件）。
        """
        if not msg.objective:
            return None
        probe = _normalize_objective(msg.objective)
        for other in self._events.values():
            if other.event_id == msg.event_id or other.producer != msg.producer:
                continue
            if _objectives_similar(probe, _normalize_objective(other.objective or "")):
                return other.event_id
        return None

    def _deliver(self, node: Any, msg: EventMessage) -> None:
        """按节点状态投递（方案三·单次消费语义）。

        - inbox（``event_inbox/``）**恒写** = 唯一待消费队列（持久副本只此一份）；
        - live（``events_inbox/``）仅当目标处于 RUNNING 态写 = 运行中 Node 消费
          inbox 的实时**信号**（非第二份待消费副本）。

        单次消费保证：两条消费路径（RUNNING 节点 CC 有界轮询 / Scheduler 桥扫
        非运行态）都以 inbox 文件为准并 delete-on-consume——任一路径先消费即删
        inbox 文件，另一条路径因文件已不存在而天然不再处理。live 残留是只读信号，
        不产生第二次待消费。

        幂等：inbox 文件已存在则跳过（重启按记录重投时不重复写）。
        """
        dirs = event_queue_dirs(node)
        inbox_dir = dirs["inbox"]
        try:
            os.makedirs(inbox_dir, exist_ok=True)
            inbox_path = os.path.join(inbox_dir, f"{msg.event_id}.json")
            if not os.path.exists(inbox_path):
                self._write_json(inbox_path, msg.to_dict())
        except OSError as exc:
            logger.error("Failed to deliver event %s to %s inbox: %s", msg.event_id, node.id, exc)
        if node.status in _LIVE_DELIVERY_STATUSES:
            live_dir = dirs["live"]
            try:
                os.makedirs(live_dir, exist_ok=True)
                self._write_json(
                    os.path.join(live_dir, f"{msg.event_id}.json"), msg.to_dict()
                )
            except OSError as exc:
                logger.error("Failed to deliver live event %s to %s: %s", msg.event_id, node.id, exc)

    # ------------------------------------------------------------------
    # 状态机：投递恢复（重启不重跑 LLM）与状态 reconcile
    # ------------------------------------------------------------------

    def _target_inbox_present(self, msg: EventMessage, target_id: str) -> bool:
        """目标节点 inbox 中是否存在该事件的待消费文件。"""
        if self._graph is None:
            return False
        node = self._graph.get_node(target_id)
        if node is None:
            return False
        inbox_dir = event_queue_dirs(node)["inbox"]
        return os.path.isfile(os.path.join(inbox_dir, f"{msg.event_id}.json"))

    def _all_targets_inbox_present(self, msg: EventMessage) -> bool:
        """事件的所有目标 inbox 文件都已落盘（无目标时视为 True）。"""
        if not msg.routed_to:
            return True
        return all(self._target_inbox_present(msg, t) for t in msg.routed_to)

    def _redeliver_routed_locked(self) -> None:
        """重启后按记录确定性重投未投递事件（**不重跑 LLM**）。

        仅处理 status=routed 且 routed_to 非空的事件：对每个目标 ``_deliver``
        （幂等，文件已存在则跳过）。全部落盘后置 delivered；有目标节点缺失 /
        投递失败则保持 routed（下次 load() 再试）。graph 未附加时跳过。
        """
        if self._graph is None:
            return
        for msg in self._events.values():
            if msg.status not in (
                EventMessage.EVENT_STATUS_ROUTED,
                EventMessage.EVENT_STATUS_ROUTE_FAILED,
            ):
                continue
            if msg.status == EventMessage.EVENT_STATUS_ROUTE_FAILED:
                # Router failures have no stable target list; retry the decision
                # with the current graph instead of treating them as delivered.
                try:
                    targets = self._router.route(msg)
                    msg.routed_to = [str(t) for t in targets]
                    msg.status = EventMessage.EVENT_STATUS_ROUTED
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Retry routing event '%s' failed: %s", msg.event_id, exc,
                    )
                    continue
            if not msg.routed_to:
                msg.status = EventMessage.EVENT_STATUS_DELIVERED
                continue
            for target_id in list(msg.routed_to):
                target_node = self._graph.get_node(target_id)
                if target_node is None:
                    logger.warning(
                        "Redeliver event '%s' target '%s' missing in graph",
                        msg.event_id, target_id,
                    )
                    continue
                self._deliver(target_node, msg)
            if self._all_targets_inbox_present(msg):
                msg.status = EventMessage.EVENT_STATUS_DELIVERED

    def _reconcile_statuses_locked(self) -> None:
        """据 inbox 实际文件状态校正各事件状态（save/load 时调用）。

        - routed_to 为空 → delivered（无人接收，终态；不依赖 graph）；
        - routed 且**全部**目标 inbox 落盘 → delivered（投递完成；部分未落盘保持
          routed，留待下次 load() 重投缺失目标）；
        - delivered 且 inbox 文件已全部消失（delete-on-consume）→ consumed。

        保守原则：graph 未附加时不做基于文件的推断（无法验证文件状态）。
        """
        for msg in self._events.values():
            if not msg.routed_to:
                if msg.status == EventMessage.EVENT_STATUS_ROUTED:
                    msg.status = EventMessage.EVENT_STATUS_DELIVERED
                continue
            if self._graph is None:
                continue
            if msg.status in (
                EventMessage.EVENT_STATUS_ROUTED,
                EventMessage.EVENT_STATUS_ROUTE_FAILED,
            ):
                if self._all_targets_inbox_present(msg):
                    msg.status = EventMessage.EVENT_STATUS_DELIVERED
            elif msg.status == EventMessage.EVENT_STATUS_DELIVERED:
                present_any = any(
                    self._target_inbox_present(msg, t) for t in msg.routed_to
                )
                if not present_any:
                    msg.status = EventMessage.EVENT_STATUS_CONSUMED

    @staticmethod
    def _write_json(path: str, data: dict) -> None:
        """原子写 inbox 文件（tmp + rename），避免 CC 读到半成品。"""
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # 只读访问（日志查询 / 测试断言）
    # ------------------------------------------------------------------

    def get_event(self, event_id: str) -> Optional[EventMessage]:
        """按 event_id 取事件。"""
        with self._lock:
            return self._events.get(event_id)

    def list_events(self) -> list[EventMessage]:
        """按全局顺序返回全部事件。"""
        with self._lock:
            return list(self._events.values())

    def event_count(self) -> int:
        """全局事件日志条数。"""
        with self._lock:
            return len(self._events)
