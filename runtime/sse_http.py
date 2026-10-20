"""runtime/sse_http.py — SSE 传输层（W3C EventSource）。

与 ``runtime/session_server.py`` 的 REST 路由分开：这个模块只负责把
``SSEManager`` 的缓冲变成 HTTP 流，不关心会话/节点路由，也不持有业务状态。

历史：这段实现原先住在 ``runtime/server.py`` 的单会话 app 里；单会话 app
只剩测试在用，生产入口是 ``session_server.create_session_app``。把 SSE 抽出来
后，旧 app 可以整体退役，而重连/快照水位这些难改的逻辑仍只有一份。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import StreamingResponse

from runtime.sse_manager import SSEManager, format_sse

# 三个分支（catchup / 快照兜底 / 首连）共用同一组头，避免某条分支漏配后
# 被代理缓冲住、前端看起来「连上了但没数据」。
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _streaming_response(body) -> StreamingResponse:
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers=dict(_SSE_HEADERS),
    )


async def _sse_stream(
    request: Request,
    runtime: Any,
    sse_manager: SSEManager,
    since_id: Optional[int] = None,
):
    """生成 SSE 事件流（W3C EventSource 格式）。

    三个分支：
      1) since_id 且缓冲命中 → 先发增量（catchup），再续实时流；
      2) since_id 且缓冲为空 → graph_updated 快照兜底，再续实时流；
      3) 无 since_id（首连）→ graph_updated 快照，再续实时流。

    实时流始终从「已发送的最后一条 seq」续起（last_seq），而非全局最新 seq，
    避免 catchup/快照生成与实时流启动之间的竞态窗口内事件被静默跳过（不丢）。
    """
    # 确保 sse_manager 已启动
    sse_manager.start()

    # 如果指定了 since_id，尝试从缓冲区恢复
    if since_id is not None and since_id > 0:
        events = sse_manager.get_events_since(since_id)
        if events:
            # 增量续拉命中：先发缓冲事件（微批合并，不重复投递已消费事件），
            # 再从 catchup 末位 seq 续实时流
            events = sse_manager.merge_batch(events)
            last_seq = events[-1].sequence_id

            async def _catchup_stream():
                for event in events:
                    yield format_sse(event)
                async for event in _sse_generator(request, sse_manager, last_seq=last_seq):
                    yield event

            return _streaming_response(_catchup_stream())

        # 缓冲区丢失 → 返回快照兜底。先捕获全局 seq（快照已覆盖 ≤ 该 seq 的
        # 事件），再合成快照（携带 base_seq 作为 id，断线重连水位 = /since/base_seq），
        # 实时流从该 seq 续起，避免生成窗口内事件被跳过。
        base_seq = sse_manager._event_bus.get_last_sequence_id()
        snapshot = sse_manager.get_snapshot_event(runtime, base_seq)

        async def _reconnect_stream():
            yield format_sse(snapshot)
            async for event in _sse_generator(request, sse_manager, last_seq=base_seq):
                yield event

        return _streaming_response(_reconnect_stream())

    # 无 since_id：首连全量流。先捕获全局 seq（快照覆盖 ≤ 该 seq 的事件），
    # 再合成快照（携带 base_seq 作为 id），实时流从该 seq 续起。
    base_seq = sse_manager._event_bus.get_last_sequence_id()
    snapshot = sse_manager.get_snapshot_event(runtime, base_seq)

    async def _full_stream():
        yield format_sse(snapshot)
        async for event in _sse_generator(request, sse_manager, last_seq=base_seq):
            yield event

    return _streaming_response(_full_stream())


async def _sse_generator(
    request: Request,
    sse_manager: SSEManager,
    last_seq: Optional[int] = None,
):
    """异步生成器：轮询 SSEManager 缓冲区，产生 SSE 格式事件。

    Args:
        request: FastAPI Request（用于检测客户端断开）。
        sse_manager: SSEManager 实例。
        last_seq: 起始序列号（不包含）。默认取当前全局最新 seq（仅向后兼容；
                 流内实际应从「已发送的最后一条 seq」续起，避免断线重连丢事件）。
    """
    if last_seq is None:
        last_seq = sse_manager._event_bus.get_last_sequence_id()
    last_keepalive = time.monotonic()

    while True:
        # 检查客户端是否已断开
        if await request.is_disconnected():
            break

        # 获取新事件（微批合并后序列化；合并仅作用于「客户端尚未消费的新事件」，
        # 已消费事件不会被再次投递）
        events = sse_manager.merge_batch(sse_manager.get_events_since(last_seq))
        for event in events:
            yield format_sse(event)
            last_seq = event.sequence_id
            last_keepalive = time.monotonic()

        # 没有新事件则等待
        if not events:
            # 每 15 秒发送 keepalive
            now = time.monotonic()
            if now - last_keepalive >= 15:
                yield ": keepalive\n\n"
                last_keepalive = now
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
