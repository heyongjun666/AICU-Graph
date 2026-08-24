"""
runtime/event_bus.py — v2 事件总线模块

提供 SSE 风格的事件发布/订阅机制，支持通配符匹配和线程安全。
"""  # """  # noqa: D202

import threading
from dataclasses import dataclass
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

Subscriber = Callable[["SSEEvent"], None]
"""事件订阅者类型：接收一个 SSEEvent 参数，无返回值。"""


# ---------------------------------------------------------------------------
# SSEEvent
# ---------------------------------------------------------------------------

@dataclass
class SSEEvent:
    """SSE 风格事件。

    字段:
        event_type: 事件类型字符串（如 "node.status.changed"）。
        sequence_id: 全局单调递增序列号（由 EventBus.publish 覆写）。
        timestamp: 事件时间戳（time.time() 浮点数）。
        payload: 事件负载（任意 JSON 可序列化数据）。
    """
    event_type: str
    sequence_id: int
    timestamp: float
    payload: Any


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class EventBus:
    """事件总线，支持发布/订阅/取消订阅。

    特性:
        - 线程安全（所有公共方法持有 Lock）
        - 通配符匹配（* 和 prefix.*）
        - 全局单调递增 sequence_id（覆写 SSEEvent 中的值）
        - 订阅者异常隔离（单个订阅者抛异常不影响其它订阅者）
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sequence_counter = 0
        self._subscribers: dict[int, tuple[str, Subscriber]] = {}
        self._next_id = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def subscribe(self, pattern: str, handler: Subscriber) -> int:
        """注册事件订阅。

        Args:
            pattern: 事件类型匹配模式（支持 '*' 和 'prefix.*' 通配符）。
            handler: 事件处理函数。

        Returns:
            订阅者 ID（用于取消订阅）。
        """
        with self._lock:
            sid = self._next_id
            self._next_id += 1
            self._subscribers[sid] = (pattern, handler)
            return sid

    def unsubscribe(self, subscriber_id: int) -> None:
        """取消事件订阅。

        Args:
            subscriber_id: 订阅时返回的 ID。
        """
        with self._lock:
            self._subscribers.pop(subscriber_id, None)

    def get_last_sequence_id(self) -> int:
        """返回当前的全局序列号（最新已发布的 sequence_id）。

        Returns:
            当前的 sequence_counter 值。
        """
        with self._lock:
            return self._sequence_counter

    def publish(self, event: SSEEvent) -> None:
        """发布事件。

        将事件广播给所有匹配的订阅者。sequence_id 会被全局覆写。
        单个订阅者抛出的异常会被捕获并忽略，不影响其它订阅者。

        Args:
            event: 要发布的事件对象。
        """
        with self._lock:
            self._sequence_counter += 1
            event.sequence_id = self._sequence_counter

            # 在锁内快照匹配的订阅者，避免迭代时订阅者列表变更
            matched: list[Subscriber] = []
            for _sid, (pattern, handler) in self._subscribers.items():
                if _match(pattern, event.event_type):
                    matched.append(handler)

        # 在锁外逐个调用，避免恶意/异常 handler 长时间持有锁
        for handler in matched:
            try:
                handler(event)
            except Exception:
                # 订阅者异常隔离，不影响其它订阅者
                pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _match(pattern: str, event_type: str) -> bool:
    """判断事件类型是否匹配模式。

    支持的通配符:
        - '*' 匹配所有事件
        - 'prefix.*' 匹配 prefix. 开头的所有事件
        - 完整字符串精确匹配

    Args:
        pattern: 匹配模式。
        event_type: 事件类型字符串。

    Returns:
        是否匹配。
    """
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return event_type == prefix or event_type.startswith(prefix + ".")
    return pattern == event_type
