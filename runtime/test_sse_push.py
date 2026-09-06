"""M6 SSE 推送模块级测试 — 聚焦 server.py SSE 流行为（问题 17 M6 增量任务）。

SSEManager 白名单/统计的完整覆盖见 tests/test_sse_manager.py（cc_2 职责）。
本文件覆盖 server.py 的 SSE 线格式与流契约：
  ① GET /v1/graph/events（首连全量流）与 /since/{seq}（增量续拉）空 → 快照回退；
  ② SSE 线格式 event/data/id（W3C EventSource）+ sequence_id 单调；
  ③ 断线重连经 /since 增量可恢复：catchup 与实时流衔接处不丢事件；
  ④ B 类 scheduler_tick 不进入转发流（D6 白名单前置，供流测试回归）。
"""

import asyncio
import json
import time

from runtime.event_bus import EventBus, SSEEvent
from runtime.sse_manager import (
    SSE_FORWARD_TYPES,
    SSE_INTERNAL_TYPES,
    SSEManager,
    format_sse,
)
from runtime.server import _sse_generator, _sse_stream

# D6 A 类 8 个状态事件（M3 构造 · forward 客户端）
A_CLASS_TYPES = [
    "node_added",
    "node_updated",
    "node_removed",
    "edge_added",
    "edge_removed",
    "node_status_changed",
    "log_appended",
    "session_state_changed",
]


# ============================================================================
# Helpers
# ============================================================================


def _mk(
    event_type: str,
    payload: dict | None = None,
    *,
    ts_offset: float = 0.0,
    ts_base: float | None = None,
) -> SSEEvent:
    """构造 SSEEvent（timestamp 距 now 偏移 ts_offset 秒，可注入 base 保证确定性）。"""
    base = ts_base if ts_base is not None else time.time()
    return SSEEvent(
        event_type=event_type,
        sequence_id=0,  # EventBus.publish 会覆写
        timestamp=base + ts_offset,
        payload=payload if payload is not None else {},
    )


def _publish_spaced(bus: EventBus, events: list[SSEEvent]) -> None:
    """按 0.5s 时间戳间隔发布，避免同类型事件被 200ms 微批合并。"""
    base = time.time()
    for i, ev in enumerate(events):
        ev.timestamp = base + i * 0.5
        bus.publish(ev)


class FakeRequest:
    """SSE 流测试用伪 Request：控制 is_disconnected 触发断流。"""

    def __init__(self, disconnect_after: int = 1000):
        self._polls = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self._polls += 1
        return self._polls > self._disconnect_after


class FakeRuntime:
    """get_snapshot_event 测试用伪 Runtime（仅提供 get_graph_snapshot）。"""

    def __init__(self, nodes=None, edges=None):
        self._nodes = nodes or [{"id": "n1", "label": "coder n1"}]
        self._edges = edges or []

    def get_graph_snapshot(self) -> dict:
        return {
            "nodes": self._nodes,
            "edges": self._edges,
            "stats": {"node_count": len(self._nodes), "edge_count": len(self._edges)},
        }


async def _collect_body(response) -> str:
    parts: list[str] = []
    it = response.body_iterator if hasattr(response, "body_iterator") else response
    async for chunk in it:
        parts.append(chunk if isinstance(chunk, str) else chunk.decode())
    return "".join(parts)


def _first_events(text: str) -> list[dict]:
    """解析 SSE 文本，返回 [{event, data(json), id}, ...]。"""
    out = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        fields = {"event": None, "data": None, "id": None}
        for line in block.split("\n"):
            if line.startswith("event: "):
                fields["event"] = line[7:]
            elif line.startswith("data: "):
                fields["data"] = json.loads(line[6:])
            elif line.startswith("id: "):
                fields["id"] = int(line[4:])
            elif line.startswith(":"):
                continue  # keepalive 注释行
        if any(v is not None for v in fields.values()):
            out.append(fields)
    return out


# ============================================================================
# ② SSE 线格式
# ============================================================================


class TestSSELineFormat:
    def test_format_sse_w3c(self):
        event = SSEEvent(
            event_type="node_status_changed",
            sequence_id=42,
            timestamp=time.time(),
            payload={"node_id": "n1", "status": "Running"},
        )
        text = format_sse(event)
        assert text == (
            "event: node_status_changed\n"
            f"data: {json.dumps(event.payload, ensure_ascii=False, default=str)}\n"
            "id: 42\n\n"
        )

    def test_sequence_ids_monotonic_through_bus(self):
        """EventBus.publish 覆写 sequence_id，全局单调递增。"""
        bus = EventBus()
        mgr = SSEManager(event_bus=bus)
        mgr.start()
        _publish_spaced(bus, [_mk("node_added", {"i": i}) for i in range(5)])
        events = mgr.get_events_since(0)
        seqs = [e.sequence_id for e in events]
        assert seqs == list(range(1, 6))
        assert all(b > a for a, b in zip(seqs, seqs[1:]))


# ============================================================================
# ④ D6 白名单前置（供流测试回归；完整覆盖见 tests/test_sse_manager.py）
# ============================================================================


class TestD6WhitelistSanity:
    def test_constants_match_d6(self):
        assert set(A_CLASS_TYPES) | {"graph_updated"} == set(SSE_FORWARD_TYPES)
        assert SSE_INTERNAL_TYPES == {"scheduler_tick"}

    def test_scheduler_tick_not_forwarded(self):
        """B 类 scheduler_tick 不进入转发缓冲区（不逐 tick 转发）。"""
        bus = EventBus()
        mgr = SSEManager(event_bus=bus)
        mgr.start()
        bus.publish(_mk("scheduler_tick", {"active_count": 2, "paused": False}))
        bus.publish(_mk("node_added", {"node_id": "n1"}))
        events = mgr.get_events_since(0)
        assert [e.event_type for e in events] == ["node_added"]
        stats = mgr.get_stats()
        assert stats["scheduler_tick"]["tick_count"] == 1
        assert stats["scheduler_tick"]["active_count"] == 2


# ============================================================================
# ① SSE 流端点：首连全量流 / /since 增量续拉 / 断线重连不丢事件
# ============================================================================


class TestSseStreamNoLoss:
    """SSE 流：快照兜底 / catchup+实时流衔接不丢事件。"""

    def test_fresh_connection_snapshot_then_live(self):
        """GET /v1/graph/events：首连先发 graph_updated 快照，再续实时流。"""
        bus = EventBus()
        mgr = SSEManager(event_bus=bus)
        mgr.start()
        rt = FakeRuntime(nodes=[{"id": "n1", "label": "coder n1"}])
        req = FakeRequest(disconnect_after=1)
        resp = asyncio.run(_sse_stream(req, rt, mgr))
        body = asyncio.run(_collect_body(resp))
        blocks = _first_events(body)
        assert blocks[0]["event"] == "graph_updated"
        assert blocks[0]["data"]["summary"]["node_count"] == 1
        # 快照 id = 生成前捕获的 base_seq；本例无事件发布 → base_seq=0
        assert blocks[0]["id"] == 0

    def test_empty_catchup_falls_back_to_snapshot(self):
        """/since/{seq} 空 → graph_updated 快照兜底（server.py:494/:526 契约）。"""
        bus = EventBus()
        mgr = SSEManager(event_bus=bus)
        mgr.start()
        rt = FakeRuntime(nodes=[{"id": "n1", "label": "coder n1"}])
        req = FakeRequest(disconnect_after=1)
        # 客户端 since_id=100，但缓冲中无事件（从未发布）→ 空 → 快照兜底
        resp = asyncio.run(_sse_stream(req, rt, mgr, since_id=100))
        body = asyncio.run(_collect_body(resp))
        blocks = _first_events(body)
        assert blocks[0]["event"] == "graph_updated"

    def test_reconnect_catchup_then_live_no_loss(self):
        """断线重连：catchup 发增量 + 续拉实时流，衔接竞态窗口内新事件不丢失。"""
        bus = EventBus()
        mgr = SSEManager(event_bus=bus)
        mgr.start()
        # 已发布 5 个事件（交替类型避免合并）
        base = time.time()
        for i in range(5):
            etype = "node_added" if i % 2 == 0 else "node_updated"
            bus.publish(_mk(etype, {"idx": i}, ts_base=base, ts_offset=i * 0.5))
        buffered = mgr.get_events_since(0)
        since_id = buffered[2].sequence_id  # 客户端上次看到第 3 个事件

        rt = FakeRuntime()
        req = FakeRequest(disconnect_after=2)
        resp = asyncio.run(_sse_stream(req, rt, mgr, since_id=since_id))

        # 竞态：catchup 列表已计算（_sse_stream 返回时），随后发布新事件；
        # 实时流必须从 catchup 末位 seq 续起，把该事件推给客户端（不丢）。
        bus.publish(_mk("node_status_changed", {"node_id": "n1", "status": "Running"}))
        body = asyncio.run(_collect_body(resp))
        text = body

        blocks = _first_events(text)
        sent_events = [b["event"] for b in blocks]
        # since_id = idx2 的 seq → catchup 应为 idx3(node_updated)、idx4(node_added)
        assert sent_events[:2] == ["node_updated", "node_added"]
        # 竞态窗口内发布的新事件必须被推送
        assert "node_status_changed" in sent_events, (
            f"竞态事件丢失；已发送: {sent_events}"
        )


class TestSseGeneratorDirect:
    def test_generator_continues_from_last_seq(self):
        """_sse_generator 应从传入 last_seq 续起（而非重置到全局最新 seq）。"""
        bus = EventBus()
        mgr = SSEManager(event_bus=bus)
        mgr.start()
        base = time.time()
        for i in range(3):
            bus.publish(_mk("node_added", {"idx": i}, ts_base=base, ts_offset=i * 0.5))
        seen_seq = mgr.get_events_since(0)[1].sequence_id  # 客户端已看到第 2 个事件
        # 之后又发布 2 个事件
        bus.publish(_mk("node_added", {"idx": 3}, ts_base=base, ts_offset=2.0))
        bus.publish(_mk("node_added", {"idx": 4}, ts_base=base, ts_offset=2.5))

        req = FakeRequest(disconnect_after=2)
        body = asyncio.run(_collect_body(_sse_generator(req, mgr, last_seq=seen_seq)))
        text = body
        # idx 3 与 idx 4 的事件必须送达（时间戳相隔 0.5s，不合并）
        assert '"idx": 3' in text and '"idx": 4' in text

    def test_generator_defaults_to_global_seq(self):
        """未传 last_seq 时，_sse_generator 从当前全局 seq 起（兼容旧调用）。"""
        bus = EventBus()
        mgr = SSEManager(event_bus=bus)
        mgr.start()
        base = time.time()
        bus.publish(_mk("node_added", {"idx": 0}, ts_base=base, ts_offset=0.0))
        req = FakeRequest(disconnect_after=2)
        body = asyncio.run(_collect_body(_sse_generator(req, mgr)))
        # 全局 seq 已被事件 1 占据 → 不重放旧事件
        assert "node_added" not in body


# ============================================================================
# 审查缺陷回归：快照携带 base_seq / 微批合并不重复投递（不丢不乱序不重复）
# ============================================================================


class TestReviewRegression:
    def test_snapshot_id_equals_base_seq_for_reconnect(self):
        """快照 id = 生成前捕获的 base_seq：断线重连走 /since/{base_seq} 仅取增量。"""
        bus = EventBus()
        mgr = SSEManager(event_bus=bus)
        mgr.start()
        # 先发布事件，使全局 seq > 0
        _publish_spaced(bus, [_mk("node_added", {"i": i}) for i in range(3)])
        base_seq = bus.get_last_sequence_id()
        assert base_seq >= 3

        rt = FakeRuntime(nodes=[{"id": "n1", "label": "coder n1"}])
        req = FakeRequest(disconnect_after=1)
        resp = asyncio.run(_sse_stream(req, rt, mgr))
        body = asyncio.run(_collect_body(resp))
        blocks = _first_events(body)
        assert blocks[0]["event"] == "graph_updated"
        # 快照以 base_seq 为 id（而非恒 0）→ 客户端 Last-Event-ID=base_seq，
        # 断线重连 /since/base_seq 只取增量，不重复渲染快照已覆盖的事件
        assert blocks[0]["id"] == base_seq

    def test_realtime_no_dup_after_micro_batch(self):
        """实时流轮询落在同 type 事件发布间隙：已消费事件不被再次投递（不重复）。

        复现场景：客户端已收到 seq=5（nA 状态变化），200ms 内同 type seq=6（nB）
        发布。若微批合并发生在缓冲摄入侧并覆写末条 seq/payload，客户端下轮轮询
        会把 nA 随合并帧再次投递；流侧合并后只投递 nB 一次。
        """
        bus = EventBus()
        mgr = SSEManager(event_bus=bus)
        mgr.start()
        # 客户端已消费第一个 node_status_changed（nA）
        bus.publish(_mk("node_status_changed", {"node_id": "nA", "status": "Running"}))
        first = mgr.get_events_since(0)[-1]
        # 200ms 内同 type 新事件（nB）—— 流侧可尝试合并，但不得重投 nA
        bus.publish(_mk("node_status_changed", {"node_id": "nB", "status": "Running"}))

        req = FakeRequest(disconnect_after=2)
        body = asyncio.run(_collect_body(_sse_generator(req, mgr, last_seq=first.sequence_id)))
        blocks = _first_events(body)
        sent = [(b["event"], b["data"], b["id"]) for b in blocks if b["event"] is not None]
        # 只应有一条 node_status_changed，data = nB（nA 不重复投递）
        assert [ev for ev, _, _ in sent] == ["node_status_changed"]
        assert sent[0][1] == {"node_id": "nB", "status": "Running"}
        assert sent[0][2] > first.sequence_id
