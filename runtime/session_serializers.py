"""runtime/session_serializers.py — 节点 / 事件 / 日志的 API 展示形状。

从 ``session_server`` 拆出：这些函数只把 Runtime 的内部对象翻译成前端读的 dict，
不碰路由、不持有 app 状态。路由与 SSE 载荷必须共用同一份派生逻辑（尤其是
``_serialize_log`` 的稳定 id），所以集中在这里比散在路由闭包里安全。
"""

import json
from typing import Any

from runtime.models import LogLevel


def _node_label(runtime: Any, node_id: str) -> str:
    """节点展示标签（role/exec_type + 短 id），与 _node_detail 的 label 同源。"""
    node = runtime.get_node(node_id)
    if node is None:
        return f"missing {node_id[:8]}"
    return f"{node.role or node.exec_type} {node.id[:8]}"


def _coerce_event_content(content: Any) -> str:
    """事件 content 规范化为字符串（Manager review 类事件 content 为 dict，前端直接渲染会白屏）。"""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(content)


def _serialize_agent_event(runtime: Any, msg: Any) -> dict[str, Any]:
    """Agent 事件 → 前端可读 dict（producer/目标解析为节点标签）。"""
    targets = [
        {"node_id": target_id, "label": _node_label(runtime, target_id)}
        for target_id in (msg.routed_to or [])
    ]
    objective = getattr(msg, "objective", "") or ""
    if not objective:
        # 兼容旧 EventMessage（content 字段）
        objective = getattr(msg, "content", "") or ""
    return {
        "event_id": msg.event_id,
        "producer": msg.producer,
        "producer_label": _node_label(runtime, msg.producer),
        "objective": _coerce_event_content(objective),
        "intent": getattr(msg, "intent", "notification"),
        "affected_roles": list(getattr(msg, "affected_roles", None) or []),
        "scope": getattr(msg, "scope", "related"),
        "action": getattr(msg, "action", ""),
        "created_at": msg.created_at,
        "status": msg.status,
        "routed_to": list(msg.routed_to or []),
        "targets": targets,
    }


def _node_detail(runtime: Any, node_id: str) -> dict[str, Any]:
    node = runtime.get_node(node_id)
    data = json.loads(node.to_json())
    data["config"] = {
        key: value for key, value in (node.config or {}).items()
        if not key.startswith("_")
    }
    data["workspace_path"] = (node.config or {}).get("_workspace_relpath")
    data["label"] = f"{node.role or node.exec_type} {node.id[:8]}"
    data["children"] = [item.id for item in runtime.graph.children(node_id)]
    data["dependencies"] = [item.id for item in runtime.graph.dependencies(node_id)]
    data["dependents"] = [item.id for item in runtime.graph.dependents(node_id)]
    return data


def _serialize_log(node_id: str, entry: Any) -> dict[str, Any]:
    return {
        "id": id(entry) & 0x7FFFFFFF,
        "node_id": node_id,
        "ts": str(entry.ts),
        "timestamp": str(entry.ts),
        "level": entry.level.value if isinstance(entry.level, LogLevel) else str(entry.level),
        "message": entry.message,
        "cc_id": getattr(entry, "cc_id", None),
        "phase": getattr(entry, "phase", None),
        "event": getattr(entry, "event", None),
        "task": getattr(entry, "task", None),
    }
