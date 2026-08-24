"""Tests for SSEManager — SSE 事件流管理模块."""

import json
import time
import threading
from typing import List

from runtime.event_bus import EventBus, SSEEvent
from runtime.sse_manager import SSEManager, format_sse


# ============================================================================
# Helpers
# ============================================================================


def _make_event(event_type: str, seq: int, payload: dict = None) -> SSEEvent:
    return SSEEvent(
        event_type=event_type,
        sequence_id=seq,
        timestamp=time.time(),
        payload=payload or {},
    )


# ============================================================================
# TestFormatSSE
# ============================================================================


class TestFormatSSE:
    """format_sse() 输出符合 W3C EventSource 格式"""

    def test_format_sse_basic(self):
        event = SSEEvent(
            event_type="node_added",
            sequence_id=42,
            timestamp=time.time(),
            payload={"node_id": "n1", "status": "Ready"},
        )
        output = format_sse(event)
        lines = output.strip().split("\n")
        assert lines[0] == "event: node_added"
        assert lines[1].startswith("data: ")
        assert lines[2] == "id: 42"
        assert output.endswith("\n\n")

    def test_format_sse_data_is_json(self):
        event = SSEEvent(
            event_type="node_updated",
            sequence_id=7,
            timestamp=time.time(),
            payload={"node_id": "n1", "status": "Running"},
        )
        output = format_sse(event)
        # Extract data line
        for line in output.strip().split("\n"):
            if line.startswith("data: "):
                data_str = line[6:]
                data = json.loads(data_str)
                assert data["node_id"] == "n1"
                assert data["status"] == "Running"
                return
        assert False, "data line not found"

    def test_format_sse_double_newline_terminator(self):
        event = _make_event("test", 1)
        output = format_sse(event)
        assert output.endswith("\n\n")


# ============================================================================
# TestSubscribeAndCollect
# ============================================================================


class TestSubscribeAndCollect:
    """SSEManager 订阅后能收到事件"""

    def test_sse_manager_collects_events(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("node_added", 0, {"node_id": "n1"}))
        time.sleep(0.3)
        bus.publish(_make_event("node_added", 0, {"node_id": "n2"}))

        # Allow event to be processed
        events = manager.get_events_since(0)
        assert len(events) == 2

        manager.stop()

    def test_sse_manager_no_events_when_stopped(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()
        manager.stop()

        bus.publish(_make_event("node_added", 0, {"node_id": "n1"}))
        events = manager.get_events_since(0)
        assert len(events) == 0

    def test_subscribe_wildcard(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("node_added", 0, {}))
        bus.publish(_make_event("edge_added", 0, {}))
        bus.publish(_make_event("log_appended", 0, {}))

        events = manager.get_events_since(0)
        assert len(events) == 3


# ============================================================================
# TestRingBufferCapacity
# ============================================================================


class TestRingBufferCapacity:
    """环形缓冲区不超过 max_events"""

    def test_ring_buffer_respects_maxlen(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus, max_events=5)
        manager.start()

        # Alternate event types to avoid micro-batch merging
        for i in range(20):
            etype = "node_added" if i % 2 == 0 else "node_updated"
            bus.publish(_make_event(etype, 0, {"idx": i}))

        events = manager.get_events_since(0)
        assert len(events) <= 5
        manager.stop()

    def test_ring_buffer_overflow_detected(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus, max_events=3)
        manager.start()

        # Alternate event types to avoid micro-batch merging
        for i in range(10):
            etype = "node_added" if i % 2 == 0 else "node_updated"
            bus.publish(_make_event(etype, 0, {"idx": i}))

        assert manager._has_overflowed is True
        manager.stop()

    def test_ring_buffer_retains_recent_events(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus, max_events=5)
        manager.start()

        # Alternate event types to avoid micro-batch merging
        for i in range(10):
            etype = "node_added" if i % 2 == 0 else "node_updated"
            bus.publish(_make_event(etype, 0, {"idx": i}))

        events = manager.get_events_since(0)
        # After overflow, get_events_since(0) returns [] (snapshot fallback signal).
        # Verify buffer contents directly instead.
        stats = manager.get_stats()
        assert stats["total_events"] == 5
        assert stats["earliest_sequence_id"] > 0
        # Check buffer has events with idx 5-9 (alternating types prevents merging)
        buffer_payloads = [e.payload["idx"] for e in list(manager._buffer)]
        assert buffer_payloads == [5, 6, 7, 8, 9]
        manager.stop()


# ============================================================================
# TestGetEventsSince
# ============================================================================


class TestGetEventsSince:
    """get_events_since 增量事件获取"""

    def test_get_events_since_returns_new_events(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus, max_events=100)
        manager.start()

        bus.publish(_make_event("node_added", 0, {"idx": 1}))
        time.sleep(0.3)
        bus.publish(_make_event("node_added", 0, {"idx": 2}))

        events = manager.get_events_since(0)
        assert len(events) == 2

        time.sleep(0.3)
        bus.publish(_make_event("node_added", 0, {"idx": 3}))
        events = manager.get_events_since(events[-1].sequence_id)
        assert len(events) == 1
        assert events[0].payload["idx"] == 3

        manager.stop()

    def test_get_events_since_with_gap_returns_empty(self):
        """当请求的 sequence_id 已被丢弃时返回空列表（快照兜底信号）。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus, max_events=3)
        manager.start()

        # Alternate event types to avoid micro-batch merging
        for i in range(10):
            etype = "node_added" if i % 2 == 0 else "node_updated"
            bus.publish(_make_event(etype, 0, {"idx": i}))

        # Try to get events from seq 0 — buffer has overflowed so should return empty
        events = manager.get_events_since(0)
        assert len(events) == 0

        manager.stop()

    def test_get_events_since_no_events(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        events = manager.get_events_since(0)
        assert len(events) == 0
        manager.stop()


# ============================================================================
# TestMicroBatchMerge
# ============================================================================


class TestMicroBatchMerge:
    """微批合并：200ms 内同类型事件被合并"""

    def test_same_type_within_window_merged(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("node_added", 0, {"idx": 1}))
        # Immediately publish same type — should merge
        bus.publish(_make_event("node_added", 0, {"idx": 2}))

        events = manager.get_events_since(0)
        assert len(events) == 1
        # The merged event should have a list payload
        assert isinstance(events[0].payload, list)
        assert len(events[0].payload) == 2
        assert events[0].payload[0]["idx"] == 1
        assert events[0].payload[1]["idx"] == 2

        manager.stop()

    def test_different_types_not_merged(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("node_added", 0, {"idx": 1}))
        bus.publish(_make_event("node_updated", 0, {"idx": 2}))

        events = manager.get_events_since(0)
        assert len(events) == 2
        assert events[0].event_type == "node_added"
        assert events[1].event_type == "node_updated"

        manager.stop()

    def test_same_type_after_delay_not_merged(self):
        """同类型但间隔超过 200ms 不合并。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("node_added", 0, {"idx": 1}))
        time.sleep(0.3)
        bus.publish(_make_event("node_added", 0, {"idx": 2}))

        events = manager.get_events_since(0)
        assert len(events) == 2
        assert not isinstance(events[0].payload, list)

        manager.stop()

    def test_merge_preserves_sequence_id(self):
        """合并后保留最新的 sequence_id。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        # Publish two events — they will get seq 1 and 2
        bus.publish(_make_event("node_added", 0, {"idx": 1}))
        bus.publish(_make_event("node_added", 0, {"idx": 2}))

        events = manager.get_events_since(0)
        assert len(events) == 1
        # The merged event should have sequence_id = last.id = 2
        assert events[0].sequence_id == 2

        manager.stop()


# ============================================================================
# TestStats
# ============================================================================


class TestStats:
    """SSEManager.get_stats() 返回正确的统计信息"""

    def test_stats_empty(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        stats = manager.get_stats()
        assert stats["total_events"] == 0
        assert stats["has_overflowed"] is False

        manager.stop()

    def test_stats_with_events(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus, max_events=100)
        manager.start()

        bus.publish(_make_event("node_added", 0, {}))
        time.sleep(0.3)
        bus.publish(_make_event("node_added", 0, {}))
        time.sleep(0.3)
        bus.publish(_make_event("node_updated", 0, {}))

        stats = manager.get_stats()
        assert stats["total_events"] == 3
        assert stats["has_overflowed"] is False
        assert stats["event_type_distribution"]["node_added"] == 2
        assert stats["event_type_distribution"]["node_updated"] == 1

        manager.stop()

    def test_stats_overflowed(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus, max_events=3)
        manager.start()

        # Alternate event types to avoid micro-batch merging
        for i in range(10):
            etype = "node_added" if i % 2 == 0 else "node_updated"
            bus.publish(_make_event(etype, 0, {}))

        stats = manager.get_stats()
        assert stats["has_overflowed"] is True
        assert stats["total_events"] == 3

        manager.stop()


# ============================================================================
# TestSnapshotEvent
# ============================================================================


class TestSnapshotEvent:
    """get_snapshot_event 生成快照事件"""

    def test_snapshot(self, runtime):
        """Runtime fixture 提供 graph，验证快照事件字段。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus)

        snapshot = manager.get_snapshot_event(runtime)
        assert snapshot.event_type == "graph_updated"
        assert "nodes" in snapshot.payload
        assert "edges" in snapshot.payload
        assert "stats" in snapshot.payload
        assert "summary" in snapshot.payload
