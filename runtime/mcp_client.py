"""MCP server 的 stdio 预检：连一次，确认它真的能起来并握手。

与 ``runtime/mcp_manager.py`` 的分工：那边管注册表（server 定义从哪来、名字怎么解析），
本模块只管拿一个已经解析好的 ``McpServer`` 去连一下。分开是因为两者的失败面不同 ——
那边的错是配置写坏，这边的错是环境问题（命令不存在、缺依赖、握手超时）。

**为什么必须预检**：MCP server 起不来时，CLI 通常只打一条 warning 就照常启动 —— agent
发现工具不在会绕路，或者报一个与 MCP 毫无关系的错。于是节点不会失败，日志里也没有
「MCP 连不上」这句话，用户只能去翻 transcript 猜。所以 AICU 在建 CC 之前自己连一次
（``initialize`` + ``tools/list``），把「连不上」变成一条明确的启动错误。

这与 skill 的 fail-fast 校验是同一套路（见 ``aicu.py`` 里 skills_dir 的那段校验）。
两者失败路径也一致：AICU 构造抛异常 → Worker 捕获 → 节点 FAILED + 可读日志 → 用户修好
环境 → Continue 原地重跑（``runtime/state_machine.py`` 的 FAILED → WAITING）。

传输方式目前只支持 stdio（子进程 + JSON-RPC over stdin/stdout）。HTTP/SSE 型 MCP
server 的预检是另一次连接尝试，待有实际需求再加。
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from typing import Any

from runtime.mcp_manager import McpServer

# 预检握手的默认超时（秒）。只覆盖「启动 + 握手」，不覆盖 server 自身的重活；
# stdio server 通常是本地小进程，超过这个数多半是卡住了而不是慢。
DEFAULT_PROBE_TIMEOUT = 20.0

PROTOCOL_VERSION = "2024-11-05"


class McpProbeError(RuntimeError):
    """server 存在但连不上（起不来、握手超时、返回错误）。"""


class _Eof:
    """哨兵：stdout 已关闭（server 提前退出）。

    必须与「超时」分开 —— 两者的成因和用户该做的事完全不同：提前退出几乎总是命令写错、
    缺依赖、参数不合法（stderr 里通常就有真话），而超时是 server 卡住了。
    混为一谈会把一个一秒内就失败的问题描述成「20 秒内没完成握手」。
    """


_EOF = _Eof()


def _read_message(proc: subprocess.Popen, deadline_seconds: float) -> Any:
    """读一条 JSON-RPC 消息，跳过 server 写到 stdout 的非 JSON 噪声行。

    超时返回 None，stdout 关闭返回 ``_EOF``。只认能解析成 dict 的行 ——
    有的 server 启动时会先打 banner。
    """
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    remaining = deadline_seconds
    while remaining > 0:
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            return None
        line = proc.stdout.readline()
        if not line:
            return _EOF  # EOF：server 提前退出
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict):
            return msg
        remaining -= 0.5
    return None


def probe_server(server: McpServer, timeout: float = DEFAULT_PROBE_TIMEOUT) -> list[str]:
    """连一次 server，返回它暴露的工具名列表；失败抛 McpProbeError（信息可直接给用户看）。

    做的是最小握手：initialize → notifications/initialized → tools/list。
    连完即杀掉子进程（这里只是为了探测，真正跑任务时由 CLI 自己拉起 server）。
    """
    env = {**os.environ, **server.env}
    try:
        proc = subprocess.Popen(
            [server.command, *server.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except FileNotFoundError:
        raise McpProbeError(
            f"MCP server '{server.name}' 启动失败：找不到命令 '{server.command}'"
        ) from None
    except OSError as error:
        raise McpProbeError(
            f"MCP server '{server.name}' 启动失败：{error}"
        ) from None

    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "aicu", "version": "1.0"},
            },
        }) + "\n")
        proc.stdin.flush()

        response = _read_message(proc, timeout)
        if response is _EOF:
            raise McpProbeError(
                f"MCP server '{server.name}' 启动后立刻退出，"
                f"没能完成 initialize 握手（命令: {server.command} {' '.join(server.args)}）"
                f"{_stderr_hint(proc, await_exit=2.0)}"
            )
        if response is None:
            raise McpProbeError(
                f"MCP server '{server.name}' 在 {timeout:.0f} 秒内没有完成 initialize 握手"
                f"（命令: {server.command} {' '.join(server.args)}）{_stderr_hint(proc)}"
            )
        if "error" in response:
            raise McpProbeError(
                f"MCP server '{server.name}' initialize 返回错误: {response['error']}"
            )

        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }) + "\n")
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        }) + "\n")
        proc.stdin.flush()

        response = _read_message(proc, timeout)
        if response is _EOF:
            raise McpProbeError(
                f"MCP server '{server.name}' 握手成功后退出，tools/list 未及响应"
                f"{_stderr_hint(proc, await_exit=2.0)}"
            )
        if response is None:
            raise McpProbeError(
                f"MCP server '{server.name}' 握手成功但 tools/list 无响应"
                f"{_stderr_hint(proc)}"
            )
        if "error" in response:
            raise McpProbeError(
                f"MCP server '{server.name}' tools/list 返回错误: {response['error']}"
            )
        tools = (response.get("result") or {}).get("tools") or []
        return [str(tool.get("name")) for tool in tools if isinstance(tool, dict)]
    finally:
        _shutdown(proc)


def _stderr_hint(proc: subprocess.Popen, limit: int = 200, await_exit: float = 0.0) -> str:
    """进程已退出时附上 stderr 尾部，便于定位（如缺依赖、参数写错）。

    ``await_exit`` 是给「stdout 已 EOF」这条路径用的有界等待。EOF 与「进程已回收」
    之间有一个真实的窗口：子进程在解释器收尾时先关 fd、再 ``_exit()``，父进程完全
    可能在这两步之间读到 EOF。此时 ``poll()`` 还是 None，于是这条本该带上返回码和
    stderr 的错会退化成一句光秃秃的「立刻退出」—— 恰恰把最有用的线索丢了。
    机器空闲时窗口极短看不出来，跑全量测试时必现。等的是已经确定要退出的进程，
    所以不会拖住正常路径。
    """
    if proc.poll() is None and await_exit > 0:
        try:
            proc.wait(timeout=await_exit)
        except subprocess.TimeoutExpired:
            pass
    if proc.poll() is None:
        return ""
    try:
        assert proc.stderr is not None
        stderr = proc.stderr.read()
    except (OSError, ValueError):
        return ""
    if not stderr:
        return f"（进程已退出，返回码 {proc.returncode}）"
    tail = stderr.strip()[-limit:]
    return f"（进程已退出，返回码 {proc.returncode}；stderr: {tail}）"


def _shutdown(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def probe_servers(
    servers: list[McpServer], timeout: float = DEFAULT_PROBE_TIMEOUT
) -> dict[str, list[str]]:
    """逐个预检。

    ``required=False`` 的 server 连不上时只打一条告警并跳过，不阻断节点启动 ——
    「这个工具本来是可选、不用它也能干活」的场景不该硬失败。
    """
    results: dict[str, list[str]] = {}
    for server in servers:
        try:
            results[server.name] = probe_server(server, timeout=timeout)
        except McpProbeError as error:
            if server.required:
                raise
            print(f"[AICU] 警告: {error}（该 server 标记为非必需，跳过）", file=sys.stderr)
    return results
