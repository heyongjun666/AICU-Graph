"""
runtime/sse_manager.py — SSE 事件流管理模块

订阅 EventBus，维护环形缓冲区，将内部事件转换为 7 种 SSE 事件类型，
支持微批合并、Last-Event-ID 重连和快照兜底。
"""

import json
import threading
import time
from collections import deque
from typing import Any, Optional

from runtime.event_bus import EventBus, SSEEvent

# SSE 事件类型集合
SSE_EVENT_TYPES = frozenset({
    "node_added",
    "node_updated",
    "node_removed",
    "edge_added",
    "edge_removed",
    "log_appended",
    "graph_updated",
    "session_state_changed",
})


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
        self._max_events = max_events
        self._ttl = ttl

        self._buffer: deque[SSEEvent] = deque(maxlen=max_events)
        self._lock = threading.Lock()
        self._subscriber_id: Optional[int] = None

        # 标记缓冲区是否曾满过（发生过丢弃）
        self._has_overflowed = False

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
        """EventBus 回调：接收事件并存入环形缓冲区（含微批合并）。"""
        with self._lock:
            now = time.time()

            # TTL 裁剪：移除超过 ttl 的旧事件
            while self._buffer and (now - self._buffer[0].timestamp) > self._ttl:
                self._buffer.popleft()

            # 微批合并：相邻事件类型相同且在 200ms 内，合并 payload
            if self._buffer:
                last = self._buffer[-1]
                time_diff = event.timestamp - last.timestamp
                if last.event_type == event.event_type and 0 <= time_diff < 0.2:
                    # 合并：将 payload 转为数组（如果尚未是数组）
                    if not isinstance(last.payload, list):
                        last.payload = [last.payload]
                    last.payload.append(event.payload)
                    last.sequence_id = event.sequence_id
                    last.timestamp = event.timestamp
                    return

            # 检查是否即将溢出（deque 满时，append 会静默丢弃最左端元素）
            if len(self._buffer) == self._buffer.maxlen:
                self._has_overflowed = True

            self._buffer.append(event)

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

    def get_snapshot_event(self, runtime: Any) -> SSEEvent:
        """生成 graph_updated 快照事件（当前 Graph 完整状态）。

        遍历 Runtime 的 Graph，生成 nodes 列表、edges 列表和 stats。

        Args:
            runtime: Runtime 实例（需有 graph 属性）。

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
            sequence_id=0,
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
        """返回缓冲区统计信息。

        Returns:
            dict: total_events, earliest_sequence_id, latest_sequence_id,
                  event_type_distribution, has_overflowed。
        """
        with self._lock:
            if not self._buffer:
                return {
                    "total_events": 0,
                    "earliest_sequence_id": 0,
                    "latest_sequence_id": 0,
                    "event_type_distribution": {},
                    "has_overflowed": self._has_overflowed,
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
            }
