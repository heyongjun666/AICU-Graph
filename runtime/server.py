"""
runtime/server.py — FastAPI HTTP REST API + SSE 端点

提供 20 个 REST API 端点（6 Graph 级 + 11 Node 级 + 1 全局日志 + 2 元数据 roles）
和 SSE 事件流端点。通过 create_app(runtime, sse_manager) 工厂函数创建，
便于测试时注入依赖。
"""

import logging
import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from runtime.models import (
    MAX_TASK_LENGTH,
    ExecutionPlan,
    LogLevel,
    NodeStatus,
    Step,
    non_whitelisted_config_keys,
    non_whitelisted_config_message,
    task_too_long_message,
)
from runtime.repo_paths import REPO_ROOT
from runtime.roles_manager import RolesManager
from runtime.sse_manager import SSEManager, format_sse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic 请求模型
# ---------------------------------------------------------------------------


class CreateManagerRequest(BaseModel):
    task: str
    role: str = "default"
    config: dict = {}


class SubmitPlanRequest(BaseModel):
    parent_node_id: str
    plan: dict  # ExecutionPlan JSON dict


class UpdateTaskRequest(BaseModel):
    task: str


class ExternalInputRequest(BaseModel):
    wait_token: str
    input_data: Any


class CreateRoleRequest(BaseModel):
    id: str
    content: str = ""
    type: str = "worker"  # "manager" 或 "worker"，决定写入 Manager/ 还是 Worker/ 子目录


# ---------------------------------------------------------------------------
# 校验函数
# ---------------------------------------------------------------------------


def validate_config_whitelist(config: dict) -> None:
    """校验 config 只包含白名单字段，否则抛 422。"""
    extra = non_whitelisted_config_keys(config)
    if extra:
        raise HTTPException(status_code=422, detail=non_whitelisted_config_message(extra))


def validate_task_length(task: str) -> None:
    """校验 task 不超最大长度，否则抛 422。"""
    if len(task) > MAX_TASK_LENGTH:
        raise HTTPException(status_code=422, detail=task_too_long_message(len(task)))


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(runtime: Any, sse_manager: SSEManager) -> FastAPI:
    """创建 FastAPI 应用实例。

    Args:
        runtime: Runtime 实例（需实现 Runtime 的公开 API 方法）。
        sse_manager: SSEManager 实例。

    Returns:
        配置好的 FastAPI 应用。
    """
    app = FastAPI(title="cc_0 Runtime API", version="0.4.0")

    # 将 runtime/sse_manager 注入 app.state，供 SSE 端点使用
    app.state.runtime = runtime
    app.state.sse_manager = sse_manager

    # 角色配置管理器（roles/ 目录相对于项目根）
    roles_dir = str(REPO_ROOT / "roles")
    roles_manager = RolesManager(roles_dir)

    # ====================================================================
    # Graph 级端点
    # ====================================================================

    @app.get("/v1/graph")
    def get_graph():
        """获取完整 Graph 快照。"""
        return runtime.get_graph_snapshot()

    @app.post("/v1/graph/manager", status_code=201)
    def create_manager(body: CreateManagerRequest):
        """创建根 Manager 节点。"""
        validate_config_whitelist(body.config)
        validate_task_length(body.task)
        try:
            node_id = runtime.submit_root_manager(
                task=body.task, role=body.role, config=body.config
            )
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"node_id": node_id, "status": "Ready"}

    @app.post("/v1/graph/plan", status_code=201)
    def submit_plan(body: SubmitPlanRequest):
        """提交 ExecutionPlan 并注入 Graph。"""
        plan_dict = body.plan
        steps_data = plan_dict.get("steps", [])
        if not steps_data:
            raise HTTPException(status_code=422, detail="Plan must contain at least one step")
        # 转换为 ExecutionPlan 对象
        steps = [
            Step(
                step_id=s["step_id"],
                exec_type=s["exec_type"],
                role=s["role"],
                task=s["task"],
                task_summary=s.get("task_summary", ""),
                config=s.get("config", {}),
                depends_on=s.get("depends_on", []),
            )
            for s in steps_data
        ]
        plan = ExecutionPlan(steps=steps)
        nodes = runtime.inject_plan(body.parent_node_id, plan)
        return {"nodes": [n.id for n in nodes]}

    @app.get("/v1/graph/stats")
    def get_graph_stats():
        """获取事件缓冲区统计信息。"""
        return sse_manager.get_stats()

    @app.post("/v1/graph/pause")
    def pause_graph():
        """全局暂停调度。"""
        runtime.global_pause()
        return {"status": "paused"}

    @app.post("/v1/graph/resume")
    def resume_graph():
        """全局恢复调度。"""
        runtime.global_resume()
        return {"status": "resumed"}

    @app.post("/v1/graph/stop")
    def stop_graph():
        """全局停止所有执行。"""
        runtime.global_stop()
        return {"status": "stopped"}

    @app.delete("/v1/graph")
    def clear_graph():
        """清除图（移除所有节点和边）。"""
        try:
            runtime.clear_graph()
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        return {"status": "cleared"}

    # ====================================================================
    # Node 级查询端点
    # ====================================================================

    @app.get("/v1/nodes/{node_id}")
    def get_node(node_id: str):
        """获取指定节点的详细信息。"""
        node = runtime.get_node(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
        return {
            "id": node.id,
            "label": f"{node.role or node.exec_type} {node.id[:8]}",
            "parent_id": node.parent_id,
            "exec_type": node.exec_type,
            "role": node.role,
            "task": node.task,
            "config": node.config,
            "status": node.status.value if isinstance(node.status, NodeStatus) else str(node.status),
            "manager_phase": node.manager_phase,
            "created_at": str(node.created_at) if hasattr(node, 'created_at') else None,
        }

    @app.get("/v1/nodes/{node_id}/children")
    def get_node_children(node_id: str):
        """获取指定节点的子节点列表。"""
        node = runtime.get_node(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
        children = runtime.get_children(node_id)
        return [
            {
                "id": c.id,
                "label": f"{c.role or c.exec_type} {c.id[:8]}",
                "parent_id": c.parent_id,
                "exec_type": c.exec_type,
                "role": c.role,
                "task": c.task,
                "status": c.status.value if isinstance(c.status, NodeStatus) else str(c.status),
            }
            for c in children
        ]

    @app.get("/v1/nodes/{node_id}/logs")
    def get_node_logs(node_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
        """获取指定节点的日志条目。"""
        try:
            logs = runtime.get_logs(node_id, offset=offset, limit=limit)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return [
            {
                "id": id(log) & 0x7FFFFFFF,
                "node_id": node_id,
                "ts": str(log.ts) if hasattr(log, 'ts') else str(log.ts),
                "timestamp": str(log.ts) if hasattr(log, 'ts') else str(log.ts),
                "level": log.level.value if isinstance(log.level, LogLevel) else str(log.level),
                "message": log.message,
            }
            for log in logs
        ]

    @app.get("/v1/nodes/{node_id}/artifact")
    def get_node_artifact(node_id: str, path: str = Query(..., min_length=1)):
        """获取指定节点的产物文件内容。"""
        node = runtime.get_node(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
        try:
            content = runtime.get_artifact(node_id, path)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=f"Artifact not found: {e}")
        return PlainTextResponse(content)

    # ====================================================================
    # Node 级控制端点（mutation）
    # ====================================================================

    @app.post("/v1/nodes/{node_id}/start")
    def start_node(node_id: str):
        """启动/恢复指定节点。"""
        node = runtime.get_node(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
        # 如果节点已经是 RUNNING，返回 200（幂等）
        if node.status == NodeStatus.RUNNING:
            return {"status": "already_running", "node_id": node_id}
        # 如果节点是 PAUSED，恢复
        if node.status == NodeStatus.PAUSED:
            try:
                runtime.resume_node(node_id)
            except ValueError as e:
                raise HTTPException(status_code=409, detail=str(e))
            return {"status": "resumed", "node_id": node_id}
        # READY → 交由 scheduler 调度（不做额外操作）
        if node.status == NodeStatus.READY:
            return {"status": "queued", "node_id": node_id}
        raise HTTPException(status_code=409, detail=f"Node '{node_id}' cannot be started (status={node.status.value})")

    @app.post("/v1/nodes/{node_id}/pause")
    def pause_node(node_id: str):
        """暂停指定节点。"""
        try:
            runtime.pause_node(node_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"status": "paused", "node_id": node_id}

    @app.post("/v1/nodes/{node_id}/stop")
    def stop_node(node_id: str):
        """停止指定节点。"""
        try:
            runtime.stop_node(node_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"status": "stopped", "node_id": node_id}

    @app.post("/v1/nodes/{node_id}/continue")
    def continue_node(node_id: str):
        """继续执行任意 FAILED/STOPPED 节点（Manager/Worker 通用）。

        保留已有日志和产物，仅重置错误和运行时间。
        Manager 有子节点时仅重新 review（不 re-expand），并额外授予
        MANUAL_CONTINUE_BUDGET 轮 review 预算。
        """
        try:
            node_id = runtime.continue_node(node_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"status": "running", "node_id": node_id}

    @app.put("/v1/nodes/{node_id}/task")
    def update_node_task(node_id: str, body: UpdateTaskRequest):
        """更新节点的 task 描述。"""
        validate_task_length(body.task)
        node = runtime.get_node(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
        runtime.update_node_task(node_id, body.task)
        return {"status": "updated", "node_id": node_id}

    @app.delete("/v1/nodes/{node_id}")
    def delete_node(node_id: str):
        """删除指定节点（仅叶子节点；Manager 有子节点 → 拒绝，删除保护）。"""
        try:
            runtime.delete_manual_node(node_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"status": "deleted", "node_id": node_id}

    @app.post("/v1/nodes/{node_id}/external-input")
    def external_input(node_id: str, body: ExternalInputRequest):
        """向等待外部输入的节点投递数据。"""
        _ = node_id  # node_id 用于路由匹配，实际通过 wait_token 查找
        try:
            runtime.deliver_external_input(body.wait_token, body.input_data)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"status": "delivered"}

    # ====================================================================
    # 全局日志端点
    # ====================================================================

    @app.get("/v1/logs")
    def get_global_logs(
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
        node_id: Optional[str] = Query(None),
        level: Optional[str] = Query(None),
        keyword: Optional[str] = Query(None),
    ):
        """获取全局日志（聚合所有节点的日志并按时间排序）。

        支持按 node_id / level / keyword 过滤，返回 {logs, total} 格式。
        """
        all_entries = []
        for node in runtime.graph.all_nodes():
            for log_entry in node.log:
                entry = {
                    "id": id(log_entry) & 0x7FFFFFFF,  # 稳定的内存 id 作为 LogEntry.id
                    "node_id": node.id,
                    "ts": str(log_entry.ts) if hasattr(log_entry, 'ts') else str(log_entry.ts),
                    "timestamp": str(log_entry.ts) if hasattr(log_entry, 'ts') else str(log_entry.ts),
                    "level": log_entry.level.value if isinstance(log_entry.level, LogLevel) else str(log_entry.level),
                    "message": log_entry.message,
                }
                # 过滤
                if node_id and entry["node_id"] != node_id:
                    continue
                if level and entry["level"] != level:
                    continue
                if keyword and keyword not in entry["message"]:
                    continue
                all_entries.append(entry)
        # 按时间戳排序
        all_entries.sort(key=lambda x: x["ts"])
        total = len(all_entries)
        return {
            "logs": all_entries[offset:offset + limit],
            "total": total,
        }

    # ====================================================================
    # 元数据端点 — 角色列表
    # ====================================================================

    @app.get("/v1/roles")
    def list_roles():
        """获取角色列表（扫描 roles/Manager/ 和 roles/Worker/ 子目录下的 yaml 文件）。"""
        return roles_manager.list_roles()

    @app.post("/v1/roles", status_code=201)
    def create_role(body: CreateRoleRequest):
        """新增角色（在 roles/Manager/ 或 roles/Worker/ 子目录下创建 yaml 文件）。"""
        try:
            result = roles_manager.add_role(body.id, body.content, role_type=body.type)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return result

    # ====================================================================
    # SSE 端点
    # ====================================================================

    @app.get("/v1/graph/events")
    async def stream_events(request: Request):
        """SSE 事件流端点（支持 Last-Event-ID 请求头）。"""
        since_id = None
        last_event_id = request.headers.get("last-event-id")
        if last_event_id is not None and last_event_id.isdigit():
            since_id = int(last_event_id)
        return await _sse_stream(request, runtime, sse_manager, since_id=since_id)

    @app.get("/v1/graph/events/since/{sequence_id}")
    async def stream_events_since(sequence_id: int, request: Request):
        """SSE 事件流端点（支持 Last-Event-ID 重连）。"""
        return await _sse_stream(request, runtime, sse_manager, since_id=sequence_id)

    return app


# ---------------------------------------------------------------------------
# run_server 入口函数
# ---------------------------------------------------------------------------


def run_server(
    runtime: Any,
    sse_manager: SSEManager,
    host: str = "0.0.0.0",
    port: int = 8080,
    log_level: str = "info",
) -> None:
    """运行 FastAPI HTTP 服务器。

    Args:
        runtime: Runtime 实例。
        sse_manager: SSEManager 实例。
        host: 监听地址，默认 0.0.0.0。
        port: 监听端口，默认 8080。
        log_level: 日志级别，默认 info。
    """
    import uvicorn

    app = create_app(runtime, sse_manager)
    uvicorn.run(app, host=host, port=port, log_level=log_level)


# ---------------------------------------------------------------------------
# SSE 流实现
# ---------------------------------------------------------------------------


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

            return StreamingResponse(
                _catchup_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # 缓冲区丢失 → 返回快照兜底。先捕获全局 seq（快照已覆盖 ≤ 该 seq 的
        # 事件），再合成快照（携带 base_seq 作为 id，断线重连水位 = /since/base_seq），
        # 实时流从该 seq 续起，避免生成窗口内事件被跳过。
        base_seq = sse_manager._event_bus.get_last_sequence_id()
        snapshot = sse_manager.get_snapshot_event(runtime, base_seq)

        async def _reconnect_stream():
            yield format_sse(snapshot)
            async for event in _sse_generator(request, sse_manager, last_seq=base_seq):
                yield event

        return StreamingResponse(
            _reconnect_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # 无 since_id：首连全量流。先捕获全局 seq（快照覆盖 ≤ 该 seq 的事件），
    # 再合成快照（携带 base_seq 作为 id），实时流从该 seq 续起。
    base_seq = sse_manager._event_bus.get_last_sequence_id()
    snapshot = sse_manager.get_snapshot_event(runtime, base_seq)

    async def _full_stream():
        yield format_sse(snapshot)
        async for event in _sse_generator(request, sse_manager, last_seq=base_seq):
            yield event

    return StreamingResponse(
        _full_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
    import asyncio

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
