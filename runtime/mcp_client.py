"""MCP server 的预检：连一次，确认它真的能起来并握手。

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

两种传输，同一个入口 ``probe_server`` 按 ``server.transport`` 分派：

  - **stdio** —— 起子进程，JSON-RPC over stdin/stdout。
  - **Streamable HTTP** —— POST 到单一端点，回复是 JSON 或一段 request-scoped 的 SSE 流。
    **不支持 SSE 传输**（那是被取代的旧远程传输，见 ``mcp_manager.transport_of``）。

两边做的握手**逐字相同**（同一份 ``initialize`` 参数、同一个 ``PROTOCOL_VERSION``）：
传输只改变消息怎么走，不改变握手里说什么。由此带来一条要说清楚的边界 ——
**预检通过不等于某家 CLI 会接受，预检失败也不等于它会拒绝**：这里连的是 server，
CLI 连的是它，两边的实现、配置字段、协议版本都可能不同。所以别拿「放宽预检」去修
CLI 侧的失败，那是两件事。
"""
from __future__ import annotations

import json
import os
import re
import select
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from runtime.mcp_manager import TRANSPORT_HTTP, McpServer

# 预检握手的默认超时（秒）。只覆盖「启动 + 握手」，不覆盖 server 自身的重活；
# stdio server 通常是本地小进程，超过这个数多半是卡住了而不是慢。
DEFAULT_PROBE_TIMEOUT = 20.0

PROTOCOL_VERSION = "2024-11-05"


def _initialize_payload() -> dict[str, Any]:
    """返回两种传输共用的 initialize 请求，避免握手参数各写一份。"""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "aicu", "version": "1.0"},
        },
    }


class McpProbeError(RuntimeError):
    """server 存在但连不上（起不来、握手超时、返回错误）。"""


@dataclass
class McpProbeReport:
    """一次预检的三态结果：已配置、已激活和可选失败。"""

    configured: list[McpServer]
    active: list[McpServer]
    tools: dict[str, list[str]]
    failures: dict[str, str]


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
    连完即收尾（stdio 杀掉子进程；HTTP 不留下任何东西 —— 这里只是为了探测，真正跑任务
    时由 CLI 自己去连 server）。

    唯一入口，调用方（``aicu.py`` / ``session_server.py`` 的「测试连接」按钮）不必知道
    传输 —— 两条路的返回与异常类型完全一致，切换传输不会改变调用方的任何一行代码。
    """
    if server.transport == TRANSPORT_HTTP:
        return _probe_http_server(server, timeout)
    return _probe_stdio_server(server, timeout)


def _probe_stdio_server(server: McpServer, timeout: float = DEFAULT_PROBE_TIMEOUT) -> list[str]:
    """本地 server 的预检：起子进程，走 stdin/stdout 上的 JSON-RPC。"""
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
        proc.stdin.write(json.dumps(_initialize_payload()) + "\n")
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


# =============================================================================
# 远程（Streamable HTTP）预检
#
# 只用标准库：为一个「连一下看看」的功能引依赖不划算，而这个协议在客户端这一侧
# 要的东西很少（POST 一个 JSON、读回一个 JSON 或一段 SSE）。请求体与握手内容与
# stdio 那条路逐字相同，差的只是消息怎么走。
# =============================================================================


def _probe_http_server(server: McpServer, timeout: float = DEFAULT_PROBE_TIMEOUT) -> list[str]:
    """远程 server 的预检：三次 POST 到同一个端点。

    顺序与 stdio 那条路一致（initialize → notifications/initialized → tools/list），
    只是每次都变成一次 HTTP 往返：

      1. ``initialize`` —— 拿到 ``Mcp-Session-Id``（**如果服务端给了**；它是可选的，
         spec 里服务端可以无状态、不给这个头）。
      2. ``notifications/initialized`` —— 通知，规范说服务端回 202 且无正文，所以这里
         **只看 HTTP 状态、不解析正文**：硬去解析会把一条正常的通知判成失败。
      3. ``tools/list`` —— 拿工具名。

    超时是**一个**总预算（``time.monotonic()`` 的 deadline），三次请求共享，每次用剩余
    时间当自己的 socket 超时。不做「每次各 20 秒」：三步各自超时的话最坏要等三倍，
    而用户看到的是一句「20 秒内没完成握手」—— 说好的 20 秒和实际等了 60 秒对不上。
    """
    deadline = time.monotonic() + timeout
    # 自定义 header 先铺，下面三个后覆盖。顺序是有意的：Accept 与 Content-Type 设错
    # 这个协议根本走不通（服务端可能因此回一个空流或直接 406），而 Authorization
    # 之类的用户 header 要原样留着。
    headers = {
        **server.headers,
        "Content-Type": "application/json",
        # 两个都要给：服务端可以按这次请求回普通 JSON，也可以回一段 SSE。
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }

    status, response_headers, body = _post(
        server, _initialize_payload(), headers,
        _budget(server, deadline, timeout, "initialize"),
        "initialize",
    )
    reply = _reply(server, status, response_headers, body, 1, "initialize")
    session_id = response_headers.get("Mcp-Session-Id")
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    status, _, _ = _post(
        server,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers,
        _budget(server, deadline, timeout, "initialize 之后的 initialized 通知"),
        "notifications/initialized",
    )
    if status >= 400:
        raise McpProbeError(_http_error_text(server, status, b""))

    status, response_headers, body = _post(
        server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers,
        _budget(server, deadline, timeout, "tools/list"),
        "tools/list",
    )
    reply = _reply(server, status, response_headers, body, 2, "tools/list")
    tools = (reply.get("result") or {}).get("tools") or []
    return [str(tool.get("name")) for tool in tools if isinstance(tool, dict)]


def _budget(server: McpServer, deadline: float, timeout: float, step: str) -> float:
    """距总 deadline 还剩几秒；已经用完就直接报「整体超时」。

    不返回一个 0 或负数让 ``urlopen`` 自己去报 —— 那会抛出一句与被探测的 server 毫无
    关系的 ``ValueError``，用户拿不到「是这一步慢」这个唯一有用的信息。
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise McpProbeError(
            f"MCP server '{server.name}' 在 {timeout:.0f} 秒内没有完成 {step}"
            f"（地址: {server.url}）"
        )
    return remaining


def _post(
    server: McpServer, payload: dict, headers: dict, timeout: float, step: str
) -> tuple[int, dict, bytes]:
    """POST 一条 JSON-RPC 消息，返回 ``(状态码, 响应头, 正文)``。

    **HTTP 错误码不当异常抛**：4xx/5xx 的正文里通常就是 server 的原话（「缺少凭据」
    「未知方法」），那是给用户最有用的线索。所以状态码照常返回，由调用方连同正文一起
    翻成一条可读的错。只把「连都没连上」那一层在这里翻译掉。

    ``Request(...)`` 的构造**也在 try 里**：畸形 URL（没有协议前缀之类）就是在那一步
    抛 ValueError 的，放在 try 外面会一路漏到调用方变成一个 500。

    请求头的**键名大小写不由我们决定**：urllib 在 ``do_open`` 里对每个头做 ``title()``
    （``MCP-Protocol-Version`` → ``Mcp-Protocol-Version``）。HTTP 头名本就不区分大小写，
    这是合法的，所以不去绕过它（要逐字节控制就得下沉到 http.client 自己拼请求，不值得）。
    """
    try:
        request = urllib.request.Request(
            server.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        try:
            body = error.read()
        except OSError:
            body = b""
        return error.code, dict(error.headers), body
    except urllib.error.URLError as error:
        # URLError 包住 DNS 失败、连接被拒、TLS 失败，**也包住 socket 超时**（urllib 把
        # 整层 OSError 都收进 URLError）。所以超时要从 reason 里认出来，否则用户读到的
        # 是一句「连接失败：timed out」—— 把「连不上」和「连上了但没回」说成同一件事，
        # 而这两件事该去查的东西完全不同（网络/地址 vs server 自己）。
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            raise McpProbeError(
                f"MCP server '{server.name}' 在 {step} 时请求超时（地址: {server.url}）"
            ) from None
        raise McpProbeError(
            f"MCP server '{server.name}' 在 {step} 时连接失败：{error.reason}"
            f"（地址: {server.url}）"
        ) from None
    except (TimeoutError, socket.timeout):
        raise McpProbeError(
            f"MCP server '{server.name}' 在 {step} 时请求超时（地址: {server.url}）"
        ) from None
    except ValueError as error:
        raise McpProbeError(
            f"MCP server '{server.name}' 的地址无法访问：{error}（地址: {server.url}）"
        ) from None


def _reply(
    server: McpServer, status: int, response_headers: dict, body: bytes,
    request_id: int, step: str,
) -> dict:
    """把一次 HTTP 响应翻成对应的那条 JSON-RPC 回复（或抛一条可读的错）。"""
    if status >= 400:
        raise McpProbeError(_http_error_text(server, status, body))
    message = _pick_message(_messages(server, status, response_headers, body, step), request_id)
    if message is None:
        raise McpProbeError(
            f"MCP server '{server.name}' 的 {step} 响应里没有针对本次请求的回复"
            f"（地址: {server.url}）"
        )
    if "error" in message:
        raise McpProbeError(
            f"MCP server '{server.name}' {step} 返回错误: {message['error']}"
        )
    return message


def _http_error_text(server: McpServer, status: int, body: bytes) -> str:
    """HTTP 错误码 + server 自己的原话。

    401/403 追加一句指向 headers：那是最常见的一类失败（少了 token、token 过期、
    header 名写错），而 server 的正文常常只有一句 ``Unauthorized``，用户不知道该去哪改。
    """
    hint = "（检查这条 server 的 headers 里的凭据是否有效）" if status in (401, 403) else ""
    return (
        f"MCP server '{server.name}' 返回 HTTP {status}{hint}："
        f"{_body_snippet(body)}（地址: {server.url}）"
    )


def _body_snippet(body: bytes, limit: int = 200) -> str:
    """响应正文的摘要，给用户留真线索（与 ``_stderr_hint`` 同一条纪律）。

    截断是必须的：有的 server 出错时回一整页 HTML，整段塞进错误信息会把真正有用的
    那一句淹掉。
    """
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return "（无正文）"
    return text[:limit] + ("…" if len(text) > limit else "")


def _messages(
    server: McpServer, status: int, response_headers: dict, body: bytes, step: str
) -> list[dict]:
    """从一次响应里取出全部 JSON-RPC 消息。

    按 ``Content-Type`` 分派，**认不出来时两种都试**：

      - ``text/event-stream`` —— 走 SSE 分帧。规范允许服务端对任何请求回一段流。
      - 其余（``application/json``、没有这个头、写错的类型）—— 先当纯 JSON 解，
        解不动再当 SSE 试。有的 server 回 SSE 却把类型写成 ``application/json``，
        严格按类型分派会把一个完全正常的 server 判成坏的。

    两种都读不出东西时才报错，且**必须带上 content type 与正文开头** —— 这是用户唯一
    能拿去搜的线索；只说一句「响应无法解析」等于让他从零开始猜。
    """
    content_type = str(response_headers.get("Content-Type") or "")
    text = body.decode("utf-8", errors="replace")

    if "text/event-stream" in content_type.lower():
        messages = _sse_messages(text)
        if messages:
            return messages
    else:
        stripped = text.strip()
        if stripped:
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return [parsed]
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
            messages = _sse_messages(text)
            if messages:
                return messages

    raise McpProbeError(
        f"MCP server '{server.name}' 的 {step} 响应无法解析"
        f"（HTTP {status}，Content-Type: {content_type or '未给出'}）：{_body_snippet(body)}"
        f"（地址: {server.url}）"
    )


def _sse_messages(text: str) -> list[dict]:
    """从一段 SSE 正文里取出所有能解析成对象的 JSON-RPC 消息。

    只按规范分帧：事件之间以空行分隔，事件内所有 ``data:`` 行用换行拼接，其余字段
    （``event:`` / ``id:`` / ``retry:`` / ``:`` 开头的注释）丢掉。解析不了的单个事件
    **跳过而不是报错** —— 流里出现保活注释或分片是常事，为一条无关的事件把整次预检
    判失败是错的。

    **不消费活流**：这里拿到的已经是响应体的全部（``urlopen`` 读完即关），预检只要
    一次回复，不会挂在 server 后续推的消息上。这也是为什么不需要处理分片重组 ——
    一次响应里带的都是完整事件。
    """
    messages: list[dict] = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for block in re.split(r"\n\n", normalized):
        data_lines = []
        for line in block.split("\n"):
            if not line.startswith("data:"):
                continue
            value = line[len("data:"):]
            # 规范：冒号后可以跟一个空格，跟了就去掉那一个（多余的属于内容）
            data_lines.append(value[1:] if value.startswith(" ") else value)
        if not data_lines:
            continue
        try:
            parsed = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            messages.append(parsed)
    return messages


def _pick_message(messages: list[dict], request_id: int) -> Optional[dict]:
    """挑出对 ``request_id`` 的那条回复；挑不出返回 None。

    先认 id 精确匹配（数字与字符串两种写法都认 —— 有的 server 把数字 id 回成 ``"1"``）。
    一条都对不上时退一步用「唯一的带 id 消息」，再退一步用「唯一的消息」：服务端回一条
    不带 id 的 JSON 是常见的不规范实现，而那**就是**本次的回复，为此报「没有针对本次
    请求的回复」会把一个能用的 server 说成坏的。真正含糊（多条都无法对应）时才放弃。
    """
    for message in messages:
        if message.get("id") in (request_id, str(request_id)):
            return message
    with_id = [message for message in messages if "id" in message]
    if len(with_id) == 1:
        return with_id[0]
    if len(messages) == 1:
        return messages[0]
    return None


def probe_servers_detailed(
    servers: list[McpServer], timeout: float = DEFAULT_PROBE_TIMEOUT
) -> McpProbeReport:
    """逐个预检并保留 optional failure，供 AICU 构造能力说明。

    ``active`` 是唯一可以下发给 CLI 的列表；``failures`` 只用于告知模型某条
    已配置但本次未连通的可选通道。required server 失败仍然立即抛出。
    """
    tools: dict[str, list[str]] = {}
    active: list[McpServer] = []
    failures: dict[str, str] = {}
    for server in servers:
        try:
            tools[server.name] = probe_server(server, timeout=timeout)
            active.append(server)
        except McpProbeError as error:
            if server.required:
                raise
            failures[server.name] = str(error)
            print(f"[AICU] 警告: {error}（该 server 标记为非必需，跳过）", file=sys.stderr)
    return McpProbeReport(
        configured=list(servers), active=active, tools=tools, failures=failures
    )


def probe_servers(
    servers: list[McpServer], timeout: float = DEFAULT_PROBE_TIMEOUT
) -> dict[str, list[str]]:
    """逐个预检，返回 active server 到工具名的映射。

    保留这个简洁返回契约给已有调用方；需要区分 optional failure 的调用方使用
    ``probe_servers_detailed``。
    """
    return probe_servers_detailed(servers, timeout=timeout).tools
