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
    """微批合并（流序列化侧）：200ms 内同类型新事件被合并为单帧。

    缓冲中每条事件保持独立（不丢/不乱序/不重复）；合并仅发生在
    merge_batch 把「客户端尚未消费的新事件」序列化为 SSE 帧之前。
    """

    def test_buffer_keeps_events_distinct(self):
        """缓冲不因微批合并改写已入缓冲事件（每事件独立 seq，无在途合并）。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("node_added", 0, {"idx": 1}))
        # 同 type 立即发布第二事件 —— 不应原地改写第一条
        bus.publish(_make_event("node_added", 0, {"idx": 2}))

        events = manager.get_events_since(0)
        assert len(events) == 2
        assert events[0].sequence_id == 1
        assert events[1].sequence_id == 2
        assert events[0].payload == {"idx": 1}
        assert events[1].payload == {"idx": 2}

        manager.stop()

    def test_same_type_within_window_merged(self):
        """流序列化侧：200ms 内同类型新事件合并为单帧（payload 变数组）。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("node_added", 0, {"idx": 1}))
        bus.publish(_make_event("node_added", 0, {"idx": 2}))

        merged = manager.merge_batch(manager.get_events_since(0))
        assert len(merged) == 1
        assert isinstance(merged[0].payload, list)
        assert len(merged[0].payload) == 2
        assert merged[0].payload[0]["idx"] == 1
        assert merged[0].payload[1]["idx"] == 2

        manager.stop()

    def test_different_types_not_merged(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("node_added", 0, {"idx": 1}))
        bus.publish(_make_event("node_updated", 0, {"idx": 2}))

        merged = manager.merge_batch(manager.get_events_since(0))
        assert len(merged) == 2
        assert merged[0].event_type == "node_added"
        assert merged[1].event_type == "node_updated"

        manager.stop()

    def test_same_type_after_delay_not_merged(self):
        """同类型但间隔超过 200ms 不合并。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("node_added", 0, {"idx": 1}))
        time.sleep(0.3)
        bus.publish(_make_event("node_added", 0, {"idx": 2}))

        merged = manager.merge_batch(manager.get_events_since(0))
        assert len(merged) == 2
        assert not isinstance(merged[0].payload, list)

        manager.stop()

    def test_merge_preserves_last_sequence_id(self):
        """合并帧 id = 批次末条 sequence_id（客户端续拉水位 = 组末 seq）。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("node_added", 0, {"idx": 1}))
        bus.publish(_make_event("node_added", 0, {"idx": 2}))

        merged = manager.merge_batch(manager.get_events_since(0))
        assert len(merged) == 1
        assert merged[0].sequence_id == 2

        manager.stop()


# ============================================================================
# TestNoRedeliveryAfterMerge
# ============================================================================


class TestNoRedeliveryAfterMerge:
    """微批合并不得把客户端已消费的事件再次投递（审查缺陷回归）。

    复现场景：客户端已收到 E1（seq=5, node_status_changed nA）后，200ms 内
    同 type E2（seq=6, node_status_changed nB）发布。若合并发生在缓冲摄入侧
    并覆写末条 seq/payload，客户端轮询将把 nA 随合并帧再次投递。正确行为：
    合并仅作用于「客户端尚未消费的新事件」，nA 不重复投递。
    """

    def test_consumed_event_not_redelivered(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        # 客户端已消费 E1（node_status_changed nA），last_seq = E1.seq
        bus.publish(_make_event("node_status_changed", 0, {"node_id": "nA", "status": "Running"}))
        e1 = manager.get_events_since(0)[-1]

        # 200ms 内同 type E2 → 流侧会尝试合并，但只作用于 E1 之后的新事件
        bus.publish(_make_event("node_status_changed", 0, {"node_id": "nB", "status": "Running"}))
        e2 = manager.get_events_since(e1.sequence_id)[-1]

        # 增量续拉只返回 E2（不含 nA），seq 单调
        assert e2.payload == {"node_id": "nB", "status": "Running"}
        assert e2.sequence_id > e1.sequence_id

        # 流序列化侧：仅 E2 一条待推送 → 不合并、不重复，水位 = E2.seq
        frames = manager.merge_batch(manager.get_events_since(e1.sequence_id))
        assert [f.sequence_id for f in frames] == [e2.sequence_id]
        assert frames[0].payload == {"node_id": "nB", "status": "Running"}

        manager.stop()

    def test_batch_merges_only_unconsumed(self):
        """客户端尚未消费的 200ms 内同 type 事件可合并为单帧（id=末条 seq）。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("node_status_changed", 0, {"node_id": "nA", "status": "Running"}))
        bus.publish(_make_event("node_status_changed", 0, {"node_id": "nB", "status": "Running"}))

        # 客户端从未消费 → 两条都是新事件 → 合并为单帧，payload = [nA, nB]
        raw = manager.get_events_since(0)
        frames = manager.merge_batch(raw)
        assert len(frames) == 1
        assert frames[0].sequence_id == raw[-1].sequence_id
        assert frames[0].payload == [
            {"node_id": "nA", "status": "Running"},
            {"node_id": "nB", "status": "Running"},
        ]

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
# TestWhitelistEnforcement
#   D6 白名单执行（module_division.md §4-D6）：
#     A 类 8 状态事件 + C 类 graph_updated → forward 客户端（入缓冲）
#     B 类 scheduler_tick → 仅内部统计，不进入转发缓冲区、不逐 tick 转发
#     未登记白名单的事件类型 → M6 拒推（不存不转）
# ============================================================================


class TestWhitelistEnforcement:
    """B 类 scheduler_tick 不进入转发缓冲区、不逐 tick 转发。"""

    def test_scheduler_tick_not_buffered(self):
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("scheduler_tick", 0, {"active_count": 2, "paused": False}))
        bus.publish(_make_event("node_added", 0, {"node_id": "n1"}))
        bus.publish(_make_event("scheduler_tick", 0, {"active_count": 3, "paused": False}))
        bus.publish(_make_event("node_status_changed", 0, {"node_id": "n1", "status": "Running"}))

        events = manager.get_events_since(0)
        types = [e.event_type for e in events]
        assert "scheduler_tick" not in types
        assert types == ["node_added", "node_status_changed"]

        # 缓冲区内也不应存在 scheduler_tick
        buffer_types = [e.event_type for e in list(manager._buffer)]
        assert "scheduler_tick" not in buffer_types

        manager.stop()

    def test_unknown_type_rejected(self):
        """未登记白名单的事件类型 → M6 拒推（不存不转）。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("custom_unregistered", 0, {"x": 1}))
        bus.publish(_make_event("node_added", 0, {"node_id": "n1"}))

        events = manager.get_events_since(0)
        assert [e.event_type for e in events] == ["node_added"]

        buffer_types = [e.event_type for e in list(manager._buffer)]
        assert "custom_unregistered" not in buffer_types

        manager.stop()

    def test_unknown_type_counted_in_stats(self):
        """拒推事件计入 get_stats() 的 rejected_event_types。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("custom_unregistered", 0, {"x": 1}))
        bus.publish(_make_event("custom_unregistered", 0, {"x": 2}))
        bus.publish(_make_event("node_added", 0, {"node_id": "n1"}))

        stats = manager.get_stats()
        assert stats["rejected_event_types"] == {"custom_unregistered": 2}

        manager.stop()

    def test_scheduler_tick_stats(self):
        """B 类 scheduler_tick 计入 get_stats()：tick 计数/调度延迟/协作水位。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        bus.publish(_make_event("scheduler_tick", 0, {"active_count": 1, "paused": False}))
        time.sleep(0.05)
        bus.publish(_make_event("scheduler_tick", 0, {"active_count": 3, "paused": False}))

        stats = manager.get_stats()
        scheduler = stats["scheduler_tick"]
        assert scheduler["tick_count"] == 2
        # 协作水位 = 最近一次 tick 的 active_count
        assert scheduler["active_count"] == 3
        # 调度延迟 = 相邻 tick 间隔（毫秒），两次 tick 间隔 ≥ 50ms
        assert scheduler["tick_delay_avg_ms"] >= 0
        assert scheduler["tick_delay_max_ms"] >= scheduler["tick_delay_avg_ms"]

        # scheduler_tick 不计入转发事件总数
        assert stats["total_events"] == 0
        assert "scheduler_tick" not in stats["event_type_distribution"]

        manager.stop()

    def test_scheduler_tick_stats_empty(self):
        """无 scheduler_tick 时，统计字段为零值且键齐全。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        stats = manager.get_stats()
        scheduler = stats["scheduler_tick"]
        assert scheduler["tick_count"] == 0
        assert scheduler["active_count"] == 0
        assert scheduler["tick_delay_avg_ms"] == 0
        assert scheduler["tick_delay_max_ms"] == 0
        assert stats["rejected_event_types"] == {}

        manager.stop()


# ============================================================================
# TestWhitelistIntegration
#   连续多节点状态变化场景：B 类 scheduler_tick 穿插期间，
#   A/C 类事件不丢、不乱序、不重复；/since 增量续拉可恢复。
# ============================================================================


class TestWhitelistIntegration:
    """scheduler_tick 穿插时，A/C 类事件流不丢不乱序不重复。"""

    def _publish_multi_node_flow(self, bus: EventBus) -> list[str]:
        """模拟多节点状态变化：节点 A 与节点 B 交替变化，穿插 scheduler_tick。"""
        expected = []
        bus.publish(_make_event("node_added", 0, {"node_id": "nA", "status": "Ready"}))
        expected.append("node_added")
        bus.publish(_make_event("scheduler_tick", 0, {"active_count": 1, "paused": False}))
        bus.publish(_make_event("node_status_changed", 0, {"node_id": "nA", "status": "Running"}))
        expected.append("node_status_changed")
        bus.publish(_make_event("node_added", 0, {"node_id": "nB", "status": "Ready"}))
        expected.append("node_added")
        bus.publish(_make_event("scheduler_tick", 0, {"active_count": 2, "paused": False}))
        bus.publish(_make_event("log_appended", 0, {"node_id": "nA", "message": "hello"}))
        expected.append("log_appended")
        bus.publish(_make_event("node_status_changed", 0, {"node_id": "nA", "status": "Completed"}))
        expected.append("node_status_changed")
        return expected

    def test_no_loss_no_dup_no_reorder_with_ticks(self):
        """scheduler_tick 穿插期间，A/C 事件不丢不乱序不重复（微批合并影响除外）。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        expected_types = self._publish_multi_node_flow(bus)

        events = manager.get_events_since(0)
        # 微批可能合并同 type 相邻事件，但不会影响 node_added 等离散变化的顺序；
        # 此处用类型序列断言顺序保持（对合并后的列表按首次出现展开）。
        received_types = []
        for e in events:
            n = len(e.payload) if isinstance(e.payload, list) else 1
            received_types.extend([e.event_type] * n)

        assert "scheduler_tick" not in received_types
        # 不丢：全部 A 类事件都到达
        assert received_types == expected_types
        # 不重复：逐条比对（与 expected 完全一致即无重复）
        # 序号单调：每条事件 sequence_id 严格递增
        seqs = [e.sequence_id for e in events]
        assert all(b > a for a, b in zip(seqs, seqs[1:]))

        manager.stop()

    def test_reconnect_since_incremental(self):
        """断线重连经 /since/{seq} 增量续拉，只返回新事件。"""
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        self._publish_multi_node_flow(bus)
        first_batch = manager.get_events_since(0)
        assert len(first_batch) >= 1
        last_seq = first_batch[-1].sequence_id

        # 断线期间新事件继续产生（含 scheduler_tick 穿插）
        bus.publish(_make_event("scheduler_tick", 0, {"active_count": 1, "paused": False}))
        bus.publish(_make_event("node_updated", 0, {"node_id": "nB", "task": "revised"}))
        bus.publish(_make_event("edge_added", 0, {"edge_id": "e1"}))

        # 增量续拉：只返回 last_seq 之后的事件，不重复
        second_batch = manager.get_events_since(last_seq)
        assert [e.event_type for e in second_batch] == ["node_updated", "edge_added"]
        assert all(e.sequence_id > last_seq for e in second_batch)

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

    def test_snapshot_sequence_id_from_base_seq(self, runtime):
        """快照事件携带捕获的 base_seq 作为 id（断线重连水位 = /since/base_seq）。

        回归：修复前快照恒 sequence_id=0，首连/重连后若连接即断，客户端
        Last-Event-ID=0 → /since/0 全量重放缓冲（含快照已覆盖事件）造成重复渲染。
        """
        bus = EventBus()
        manager = SSEManager(event_bus=bus)
        manager.start()

        # 已发布事件 → 全局 seq > 0
        bus.publish(_make_event("node_added", 0, {"node_id": "n1"}))
        bus.publish(_make_event("node_added", 0, {"node_id": "n2"}))
        base_seq = bus.get_last_sequence_id()
        assert base_seq >= 2

        # 缺省 base_seq=0（兼容直接调用）；显式传入捕获的 base_seq 时快照以其为 id
        assert manager.get_snapshot_event(runtime).sequence_id == 0
        snapshot = manager.get_snapshot_event(runtime, base_seq=base_seq)
        assert snapshot.sequence_id == base_seq

        manager.stop()
