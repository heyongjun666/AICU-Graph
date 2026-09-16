#!/usr/bin/env python3
"""AICU 自带的 MCP 探针 server（stdio JSON-RPC）。

用途是验证 MCP 链路本身是否打通：注册表里的 ``echo-probe`` 指向它，角色 yaml 挂上
之后，如果 worker 能调到 ``probe_echo`` 并把回显写进产物，说明「角色声明 → AICU 解析
→ 预检 → 传给 CLI → agent 真的用上」这条链路是通的。与 tests/mock_providers.py 同类，
都是为了让外部依赖在测试里可控。

只实现握手与调用所需的最小方法集：initialize / tools/list / tools/call。
"""
import json
import sys

PROTOCOL_VERSION = "2024-11-05"

TOOL = {
    "name": "probe_echo",
    "description": (
        "回显传入的 text。AICU 用它验证 MCP 链路：调用方把一串标记文本传进来，"
        "把返回的内容写进产物即可证明工具真的被执行过。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要回显的文本"}
        },
        "required": ["text"],
    },
}


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle(msg: dict) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")
    if msg_id is None:
        return  # 通知，无需回复
    if method == "initialize":
        _send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": (
                    (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
                ),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "aicu-echo-probe", "version": "0.0.1"},
            },
        })
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [TOOL]}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        arguments = params.get("arguments") or {}
        text = arguments.get("text", "")
        _send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"content": [{"type": "text", "text": f"probe_echo: {text}"}]},
        })
    else:
        _send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        })


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        _handle(msg)


if __name__ == "__main__":
    main()
