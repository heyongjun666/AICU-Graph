"""
runtime/sse_manager.py — SSE 事件流管理模块

订阅 EventBus，按 D6 白名单过滤事件，维护环形缓冲区，提供事件查询、
快照生成、格式化和统计能力。A/C 类 forward 客户端、B 类 scheduler_tick
仅内部统计、未登记白名单的类型拒推。

微批合并（<200ms 同 type）在流序列化侧进行（merge_batch）：缓冲中每条
事件保持独立（sequence_id 全局单调），保证同一事件对同一客户端恰好投递
一次（不丢/不乱序/不重复）。
"""

import json
import threading
import time
from collections import deque
from typing import Any, Optional

from runtime.event_bus import EventBus, SSEEvent


# ---------------------------------------------------------------------------
# D6 SSE 事件白名单（module_division.md §4-D6）
#   A = M3 构造 · 状态事件（forward 客户端，8 个）
#   B = M3 构造 · 内部观测（不 forward 客户端）
#   C = M6 构造 · 视图/快照（forward 客户端）
# ---------------------------------------------------------------------------

# 转发白名单：A 类 8 状态事件 + C 类 graph_updated 快照（forward 客户端）
SSE_FORWARD_TYPES: frozenset[str] = frozenset({
    "node_added",
    "node_updated",
    "node_removed",
    "edge_added",
    "edge_removed",
    "node_status_changed",
    "log_appended",
    "session_state_changed",
    "graph_updated",
})

# 内部观测事件：B 类 scheduler_tick（仅统计，不 forward 客户端）
SSE_INTERNAL_TYPES: frozenset[str] = frozenset({
    "scheduler_tick",
})

# 全量白名单：未登记的事件类型 → M6 拒推
SSE_WHITELIST: frozenset[str] = SSE_FORWARD_TYPES | SSE_INTERNAL_TYPES

def format_sse(event: SSEEvent) -> str:
    """将 SSEEvent 格式化为 W3C EventSource 格式。

    Args:
        event: SSEEvent 对象。

    Returns:
        形如 "event: {type}\ndata: {json}\nid: {seq}\n\n" 的字符串。
    """
    lines = [
        f"event: {event.event_type}",
        f"data: {json.dumps(event.payload, ensure_ascii=False, default=str)}",
        f"id: {event.sequence_id}",
    ]
    return "\n".join(lines) + "\n\n"


class SSEManager:
    """SSE 事件流管理模块。

    订阅 EventBus，维护环形缓冲区，提供事件查询、快照生成、格式化和统计能力。

    Args:
        event_bus: EventBus 实例。
        max_events: 环形缓冲区最大容量（默认 10000）。
        ttl: 事件在缓冲区中的最大存活时间（秒，默认 1800）。
    """

    def __init__(
        self,
        event_bus: EventBus,
        max_events: int = 10000,
        ttl: int = 1800,
    ):
        self._event_bus = event_bus
        self._ttl = ttl

        self._buffer: deque[SSEEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()
        self._subscriber_id: Optional[int] = None

        # 标记缓冲区是否曾满过（发生过丢弃）
        self._has_overflowed = False

        # B 类 scheduler_tick 内部统计（tick 计数 / 调度延迟 / 协作水位）
        self._tick_count = 0
        self._tick_intervals: deque[float] = deque(maxlen=100)
        self._last_tick_ts: Optional[float] = None
        self._tick_active_count = 0

        # 拒推统计：未登记白名单的事件类型 → 计数
        self._rejected_by_type: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """订阅 EventBus，开始消费事件。"""
        if self._subscriber_id is not None:
            return
        self._subscriber_id = self._event_bus.subscribe("*", self._on_event)

    def stop(self) -> None:
        """取消订阅，停止消费。"""
        if self._subscriber_id is not None:
            self._event_bus.unsubscribe(self._subscriber_id)
            self._subscriber_id = None

    # ------------------------------------------------------------------
    # EventBus callback
    # ------------------------------------------------------------------

    def _on_event(self, event: SSEEvent) -> None:
        """EventBus 回调：按 D6 白名单过滤后处理。

        - A/C 类（forward 白名单）→ 存入环形缓冲区，forward 客户端；
          缓冲保持每条事件独立（sequence_id 全局单调），微批合并在流序列化
          侧（merge_batch）进行，避免在途合并改写已入缓冲事件导致重复投递；
        - B 类 scheduler_tick → 仅计入内部统计（不进入缓冲区、不逐 tick 转发）；
        - 未登记白名单的事件类型 → M6 拒推（不存不转，计入 rejected 统计）。
        """
        event_type = event.event_type

        # 未登记白名单 → M6 拒推（不存不转，计入 rejected 统计）
        if event_type not in SSE_WHITELIST:
            with self._lock:
                self._rejected_by_type[event_type] = (
                    self._rejected_by_type.get(event_type, 0) + 1
                )
            return

        # B 类内部观测事件：仅统计，不转发
        if event_type in SSE_INTERNAL_TYPES:
            self._record_tick(event)
            return

        # A/C 类：存入环形缓冲区
        with self._lock:
            now = time.time()

            # TTL 裁剪：移除超过 ttl 的旧事件
            while self._buffer and (now - self._buffer[0].timestamp) > self._ttl:
                self._buffer.popleft()

            # 检查是否即将溢出（deque 满时，append 会静默丢弃最左端元素）
            if len(self._buffer) == self._buffer.maxlen:
                self._has_overflowed = True

            self._buffer.append(event)

    def _record_tick(self, event: SSEEvent) -> None:
        """记录 B 类 scheduler_tick 内部统计。

        调度延迟 = 相邻两次 tick 的时间间隔（毫秒）；
        协作水位 = 最近一次 tick 的 active_count（并行执行数）。
        """
        with self._lock:
            now = time.time()
            if self._last_tick_ts is not None:
                self._tick_intervals.append((now - self._last_tick_ts) * 1000.0)
            self._last_tick_ts = now
            self._tick_count += 1
            active = (event.payload or {}).get("active_count")
            if isinstance(active, (int, float)):
                self._tick_active_count = int(active)

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    def get_events_since(self, sequence_id: int) -> list[SSEEvent]:
        """获取从 sequence_id 之后的所有事件。

        用于 Last-Event-ID 重连。如果请求的 sequence_id 早于缓冲区最早记录，
        说明事件已丢失，返回空列表以触发调用方回退到快照。

        Args:
            sequence_id: 起始序列号（不包含，返回 > sequence_id 的事件）。

        Returns:
            sequence_id 之后的事件列表。空列表表示需要快照兜底。
        """
        with self._lock:
            if not self._buffer:
                return []

            first_seq = self._buffer[0].sequence_id

            # sequence_id == 0 表示"从头开始"——只有当缓冲区曾溢出过才返回空
            if sequence_id == 0:
                if self._has_overflowed:
                    return []
                return list(self._buffer)

            # 请求的 sequence_id 已被缓冲区丢弃
            if sequence_id < first_seq:
                return []

            return [e for e in self._buffer if e.sequence_id > sequence_id]

    def merge_batch(self, events: list[SSEEvent]) -> list[SSEEvent]:
        """将待推送事件按微批合并为最终待序列化事件列表（流序列化侧）。

        缓冲中的每条事件保持独立（sequence_id 全局单调，不丢/不乱序/不重复）；
        微批合并仅发生在「把 get_events_since 返回的新事件序列化为 SSE 帧」之前：
        对其中相邻同 type 且时间间隔 < 200ms 的一组事件，合并为一条 SSEEvent——
        sequence_id 取该组末条的 sequence_id（客户端续拉水位 = 组末 seq，已消费
        事件不会被再次投递），payload 为该组各条 payload 组成的数组（未合并的
        单条事件原样返回，payload 保持单条对象形态）。

        调用方（server 流）应传入「同一客户端尚未消费的新事件」（均 > 其 last_seq），
        保证合并只发生在客户端全部可见的事件上，不破坏不重复保证。

        Args:
            events: 待推送事件（sequence_id 单调递增）。

        Returns:
            合并后的事件列表（长度 ≤ len(events)）。
        """
        if not events:
            return []

        batches: list[list[SSEEvent]] = []
        current: list[SSEEvent] = [events[0]]
        for event in events[1:]:
            prev = current[-1]
            gap = event.timestamp - prev.timestamp
            if event.event_type == prev.event_type and 0 <= gap < 0.2:
                current.append(event)
            else:
                batches.append(current)
                current = [event]
        batches.append(current)

        return [self._collapse_batch(batch) for batch in batches]

    @staticmethod
    def _collapse_batch(batch: list[SSEEvent]) -> SSEEvent:
        """把单个微批折叠为一条 SSEEvent（单条原样返回，多条 payload 变数组）。"""
        if len(batch) == 1:
            return batch[0]
        last = batch[-1]
        return SSEEvent(
            event_type=last.event_type,
            sequence_id=last.sequence_id,
            timestamp=last.timestamp,
            payload=[e.payload for e in batch],
        )

    def get_snapshot_event(self, runtime: Any, base_seq: int = 0) -> SSEEvent:
        """生成 graph_updated 快照事件（当前 Graph 完整状态）。

        遍历 Runtime 的 Graph，生成 nodes 列表、edges 列表和 stats。

        Args:
            runtime: Runtime 实例（需有 graph 属性）。
            base_seq: 生成快照前捕获的全局 sequence_id（默认 0）。快照以
                base_seq 作为自身 id：断线重连时客户端 Last-Event-ID = base_seq，
                走 /since/{base_seq} 仅取增量，避免全量重放缓冲造成重复渲染。

        Returns:
            graph_updated 类型 SSEEvent。
        """
        snapshot_builder = getattr(runtime, "get_graph_snapshot", None)
        if callable(snapshot_builder):
            snapshot = snapshot_builder()
            nodes = snapshot.get("nodes", [])
            edges = snapshot.get("edges", [])
            stats = snapshot.get("stats", {})
        else:
            # Keep compatibility with lightweight Runtime doubles and older
            # integrations while production Runtime supplies the full schema.
            graph = runtime.graph
            nodes = [
                {
                    "id": node.id,
                    "label": f"{node.role or node.exec_type} {node.id[:8]}",
                    "exec_type": node.exec_type,
                    "parent_id": node.parent_id,
                    "role": node.role,
                    "task": node.task,
                    "status": node.status.value
                    if hasattr(node.status, "value")
                    else str(node.status),
                    "manager_phase": node.manager_phase,
                    "config": {
                        key: value
                        for key, value in (node.config or {}).items()
                        if not key.startswith("_")
                    },
                }
                for node in graph.all_nodes()
            ]
            edges = [
                {
                    "id": edge.id,
                    "source": edge.from_node,
                    "target": edge.to_node,
                    "type": edge.type.value
                    if hasattr(edge.type, "value")
                    else str(edge.type),
                    "from_node": edge.from_node,
                    "to_node": edge.to_node,
                }
                for edge in graph.all_edges()
            ]
            stats = graph.get_stats()

        event = SSEEvent(
            event_type="graph_updated",
            sequence_id=base_seq,
            timestamp=time.time(),
            payload={
                "ts": time.time(),
                "summary": {
                    "node_count": len(nodes),
                    "edge_count": len(edges),
                },
                "nodes": nodes,
                "edges": edges,
                "stats": stats,
            },
        )
        return event

    def get_stats(self) -> dict:
        """返回缓冲区与内部观测统计信息。

        Returns:
            dict: total_events, earliest_sequence_id, latest_sequence_id,
                  event_type_distribution, has_overflowed, rejected_event_types,
                  scheduler_tick（B 类内部观测：tick_count 计数 / tick_delay_avg_ms、
                  tick_delay_max_ms 调度延迟 / active_count 协作水位）。
        """
        with self._lock:
            scheduler = self._scheduler_stats_locked()

            if not self._buffer:
                return {
                    "total_events": 0,
                    "earliest_sequence_id": 0,
                    "latest_sequence_id": 0,
                    "event_type_distribution": {},
                    "has_overflowed": self._has_overflowed,
                    "rejected_event_types": dict(self._rejected_by_type),
                    "scheduler_tick": scheduler,
                }

            dist: dict[str, int] = {}
            for e in self._buffer:
                event_type = e.event_type
                dist[event_type] = dist.get(event_type, 0) + 1

            return {
                "total_events": len(self._buffer),
                "earliest_sequence_id": self._buffer[0].sequence_id,
                "latest_sequence_id": self._buffer[-1].sequence_id,
                "event_type_distribution": dist,
                "has_overflowed": self._has_overflowed,
                "rejected_event_types": dict(self._rejected_by_type),
                "scheduler_tick": scheduler,
            }

    def _scheduler_stats_locked(self) -> dict:
        """计算 B 类 scheduler_tick 内部统计（调用方须持有 self._lock）。

        tick 计数 = 收到 scheduler_tick 的总次数；
        调度延迟 = 相邻两次 tick 的时间间隔（毫秒，avg/max）；
        协作水位 = 最近一次 tick 的 active_count（并行执行数）。
        """
        avg_ms = 0.0
        max_ms = 0.0
        if self._tick_intervals:
            avg_ms = sum(self._tick_intervals) / len(self._tick_intervals)
            max_ms = max(self._tick_intervals)
        return {
            "tick_count": self._tick_count,
            "tick_delay_avg_ms": round(avg_ms, 1),
            "tick_delay_max_ms": round(max_ms, 1),
            "active_count": self._tick_active_count,
        }
