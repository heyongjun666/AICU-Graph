"""runtime/mcp_client.py 单元测试：真走一遍握手。

stdio 那条路对着 tests/mcp_echo_server.py 起真进程，HTTP 那条路对着
``http.server.ThreadingHTTPServer`` 起真端点 —— 两边都**刻意不 mock 传输**：握手是
时序协议，mock 掉就等于只测了「我们以为的协议」，而这正是最容易写错的地方。

HTTP 用端口 0（由系统分配）而不是写死端口：写死会与开发机上已有的服务撞，而且
本仓库有一条硬约束 —— 8080 上跑着常驻的 AICU 服务，任何测试都不许碰它。
注册表读取与名字解析在 tests/test_mcp_manager.py。
"""
import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pathlib import Path

from runtime.mcp_client import (
    PROTOCOL_VERSION,
    McpProbeError,
    McpProbeReport,
    _initialize_payload,
    probe_server,
    probe_servers,
    probe_servers_detailed,
)
from runtime.mcp_manager import TRANSPORT_HTTP, McpServer

# 绝对路径：相对写法 "tests/mcp_echo_server.py" 是相对 **cwd** 解析的，
# 从别的目录跑 pytest 就会变成「找不到文件」，看上去像 MCP 预检坏了。
ECHO_ARGS = [str(Path(__file__).resolve().parent / "mcp_echo_server.py")]

REMOTE_URL = "https://mcp.example.com/mcp"
TOOLS = [{"name": "probe_echo", "description": "回显"}, {"name": "other"}]


def _echo_server(**overrides) -> McpServer:
    spec = dict(name="echo-probe", command="python3", args=ECHO_ARGS)
    spec.update(overrides)
    return McpServer(**spec)


def _remote_server(url: str, **overrides) -> McpServer:
    spec = dict(name="remote-probe", command="", url=url, transport=TRANSPORT_HTTP)
    spec.update(overrides)
    return McpServer(**spec)


class TestProbe:
    def test_initialize_payload_is_recreated_with_the_shared_handshake(self):
        first = _initialize_payload()
        second = _initialize_payload()

        assert first == second
        assert first is not second
        assert first["params"] is not second["params"]
        assert first["params"]["protocolVersion"] == PROTOCOL_VERSION
        assert first["params"]["clientInfo"] == {"name": "aicu", "version": "1.0"}

    def test_probe_echo_server(self):
        assert probe_server(_echo_server(), timeout=20) == ["probe_echo"]

    def test_probe_servers_returns_tools_per_server(self):
        assert probe_servers([_echo_server()], timeout=20) == {
            "echo-probe": ["probe_echo"]
        }

    def test_missing_command(self):
        with pytest.raises(McpProbeError) as excinfo:
            probe_server(McpServer(name="gone", command="/nonexistent/aicu-gone"), timeout=5)
        assert "启动失败" in str(excinfo.value)

    def test_exits_immediately_reports_returncode_and_stderr(self):
        """秒退与「卡住」必须分开报：前者几乎总是命令/依赖写错，stderr 里有真话。"""
        server = McpServer(
            name="dies",
            command="python3",
            args=["-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"],
        )
        with pytest.raises(McpProbeError) as excinfo:
            probe_server(server, timeout=10)
        message = str(excinfo.value)
        assert "立刻退出" in message
        assert "返回码 3" in message
        assert "boom" in message

    def test_eof_hint_waits_for_the_process_to_actually_exit(self):
        """先关 stdout、后退出：父进程读到的 EOF 早于子进程被回收。

        这个窗口是真实的（解释器收尾时先关 fd 再 _exit），机器空闲时几乎撞不上，
        跑全量测试时必现。撞上时的表现不是报错而是**线索丢失**：那句本来该带上返回码
        和 stderr 的「立刻退出」退化成一个光秃秃的感叹号，正好把用户唯一能自查的东西
        拿掉了。所以这条测试构造出这个窗口，逼 _stderr_hint 等一等。
        """
        server = McpServer(
            name="late",
            command="python3",
            args=["-c", (
                "import os, sys, time;"
                "os.close(1);"
                "print('boom', file=sys.stderr); sys.stderr.flush();"
                "time.sleep(0.5);"
                "sys.exit(7)"
            )],
        )
        with pytest.raises(McpProbeError) as excinfo:
            probe_server(server, timeout=10)
        message = str(excinfo.value)
        assert "立刻退出" in message
        assert "返回码 7" in message
        assert "boom" in message

    def test_hangs_before_handshake(self):
        server = McpServer(
            name="hangs", command="python3", args=["-c", "import time; time.sleep(30)"]
        )
        with pytest.raises(McpProbeError) as excinfo:
            probe_server(server, timeout=1.5)
        assert "没有完成 initialize 握手" in str(excinfo.value)

    def test_required_false_only_warns(self, capsys):
        """标记为非必需的 server 连不上不阻断启动（工具本来就是可选的那些场景）。"""
        servers = [McpServer(name="opt", command="/nonexistent/aicu-opt", required=False)]
        assert probe_servers(servers, timeout=5) == {}
        assert "opt" in capsys.readouterr().err

    def test_required_true_blocks(self):
        with pytest.raises(McpProbeError):
            probe_servers([McpServer(name="must", command="/nonexistent/aicu-must")], timeout=5)

    def test_detailed_report_keeps_configured_active_tools_and_optional_failures(self):
        active = _echo_server(name="active")
        optional = McpServer(
            name="optional", command="/nonexistent/aicu-optional", required=False
        )
        report = probe_servers_detailed([active, optional], timeout=5)

        assert isinstance(report, McpProbeReport)
        assert [server.name for server in report.configured] == ["active", "optional"]
        assert [server.name for server in report.active] == ["active"]
        assert report.tools == {"active": ["probe_echo"]}
        assert "optional" in report.failures
        assert "optional" not in report.tools

    def test_empty_tools_is_active_in_detailed_report(self, monkeypatch):
        empty = McpServer(name="empty", command="python3")
        monkeypatch.setattr(
            "runtime.mcp_client.probe_server", lambda server, timeout: []
        )
        report = probe_servers_detailed([empty], timeout=1)

        assert report.configured == [empty]
        assert report.active == [empty]
        assert report.tools == {"empty": []}
        assert report.failures == {}

    def test_probe_kills_the_child(self):
        """预检只握手不干活，连完必须收掉子进程，否则每次建节点都漏一个。"""
        import time

        before = _echo_proc_count()
        probe_server(_echo_server(), timeout=20)
        time.sleep(0.5)
        assert _echo_proc_count() == before


def _echo_proc_count() -> int:
    import subprocess

    out = subprocess.run(
        ["pgrep", "-f", "mcp_echo_server.py"], capture_output=True, text=True
    )
    return len([line for line in out.stdout.splitlines() if line.strip()])


# =============================================================================
# 远程（Streamable HTTP）预检
#
# 每个用例起一个真端点。``respond`` 拿到 (handler, 本次请求) 自己决定怎么回 ——
# 要测的正是「服务端以各种方式回」时我们怎么读，所以这个决定权必须在用例手里。
# =============================================================================


def _write(handler, status, content_type, body: bytes, extra_headers=None) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    for name, value in (extra_headers or {}).items():
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if body:
        handler.wfile.write(body)


@contextlib.contextmanager
def _http_endpoint(respond):
    """起一个只在本机、端口由系统分配的假 MCP 端点。

    产出 ``(url, requests)``，``requests`` 里按顺序记着每次 POST 的路径、请求头与
    解析后的正文 —— 「我们到底发出去了什么」与「我们怎么读回来的」同样重要，
    自定义 header、会话 id、握手顺序全靠它断言。
    """
    requests: list = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = None
            requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            respond(self, requests[-1])

        def log_message(self, *args):  # 别把请求日志打到测试输出里
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/mcp", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _header(headers: dict, name: str) -> str:
    """按名字取请求头，**大小写不敏感**。

    urllib 发请求时会把每个头名过一遍 ``title()``（``MCP-Protocol-Version`` 变成
    ``Mcp-Protocol-Version``）。HTTP 头名本就不区分大小写，所以断言不该钉住大小写 ——
    钉了只会让测试在这个无关维度上红，而真正要守的是「这个头发出去了、值对不对」。
    """
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    raise AssertionError(f"请求里没有 {name} 头，实际有：{sorted(headers)}")


def _mcp_responder(style: str = "json", session_id=None, tools=None, error=None):
    """一个行为正常的 MCP 端点，可调成 SSE 回复 / 发会话 id / 对某个方法回错误。"""

    def respond(handler, request):
        method = (request["body"] or {}).get("method")
        if method == "notifications/initialized":
            # 规范：通知回 202 且无正文。响应体写点东西反而能验证「我们不解析它」。
            _write(handler, 202, "application/json", b"")
            return
        if error and method == error[0]:
            payload = {"jsonrpc": "2.0", "id": request["body"].get("id"), "error": error[1]}
        elif method == "initialize":
            payload = {
                "jsonrpc": "2.0",
                "id": request["body"].get("id"),
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "1"},
                },
            }
        elif method == "tools/list":
            payload = {
                "jsonrpc": "2.0",
                "id": request["body"].get("id"),
                "result": {"tools": TOOLS if tools is None else tools},
            }
        else:
            payload = {
                "jsonrpc": "2.0",
                "id": request["body"].get("id"),
                "error": {"code": -32601, "message": f"unknown method {method}"},
            }

        extra = {"Mcp-Session-Id": session_id} if session_id and method == "initialize" else None
        if style == "sse":
            # 一段真实的 SSE：保活注释 + 一条解析不了的无关事件，都该被跳过
            text = (
                ": keep-alive\n\n"
                "event: message\n"
                "data: {not json at all\n\n"
                f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            )
            _write(handler, 200, "text/event-stream; charset=utf-8", text.encode("utf-8"), extra)
        else:
            _write(
                handler, 200, "application/json",
                json.dumps(payload, ensure_ascii=False).encode("utf-8"), extra,
            )

    return respond


class TestHttpProbe:
    def test_probe_remote_json_endpoint(self):
        with _http_endpoint(_mcp_responder()) as (url, _):
            assert probe_server(_remote_server(url), timeout=10) == ["probe_echo", "other"]

    def test_probe_remote_sse_endpoint(self):
        """服务端可以对任何请求回一段 SSE —— 两条路都要能读。"""
        with _http_endpoint(_mcp_responder(style="sse")) as (url, _):
            assert probe_server(_remote_server(url), timeout=10) == ["probe_echo", "other"]

    def test_sse_response_under_a_json_content_type_still_reads(self, ):
        """有的 server 回 SSE 却把类型写成 ``application/json``。

        严格按 Content-Type 分派会把一个完全正常的 server 判成坏的，所以解不动时要回退
        再试一次。
        """
        def respond(handler, request):
            if (request["body"] or {}).get("method") == "notifications/initialized":
                _write(handler, 202, "application/json", b"")
                return
            result = {"tools": TOOLS} if request["body"]["method"] == "tools/list" else {}
            payload = {"jsonrpc": "2.0", "id": request["body"].get("id"), "result": result}
            text = f"data: {json.dumps(payload)}\n\n"
            _write(handler, 200, "application/json", text.encode("utf-8"))

        with _http_endpoint(respond) as (url, _):
            assert probe_server(_remote_server(url), timeout=10) == ["probe_echo", "other"]

    def test_handshake_is_the_same_three_messages_in_order(self):
        """顺序与内容必须与 stdio 那条路一致：传输只改变消息怎么走，不改变说什么。"""
        with _http_endpoint(_mcp_responder()) as (url, requests):
            probe_server(_remote_server(url), timeout=10)
        assert [r["body"].get("method") for r in requests] == [
            "initialize", "notifications/initialized", "tools/list",
        ]
        init = requests[0]["body"]
        assert init["params"]["protocolVersion"] == PROTOCOL_VERSION
        assert init["params"]["clientInfo"]["name"] == "aicu"
        assert init["id"] == 1 and requests[2]["body"]["id"] == 2
        # 通知按规范**没有 id**：带上它就成了请求，服务端会等一个没人读的回复。
        assert "id" not in requests[1]["body"]

    def test_all_three_requests_carry_the_protocol_headers(self):
        """``Accept`` 必须同时列两种：服务端据此决定回 JSON 还是回 SSE。

        只写 ``application/json`` 会让一部分 server 直接 406，而那种失败看起来与我们
        毫无关系。
        """
        with _http_endpoint(_mcp_responder()) as (url, requests):
            probe_server(_remote_server(url), timeout=10)
        for request in requests:
            assert _header(request["headers"], "Content-Type") == "application/json"
            assert "application/json" in _header(request["headers"], "Accept")
            assert "text/event-stream" in _header(request["headers"], "Accept")
            assert _header(request["headers"], "MCP-Protocol-Version") == PROTOCOL_VERSION
    def test_custom_headers_are_sent_but_cannot_break_the_protocol_ones(self):
        """用户 header 原样带上，但我们自己那三个**不可被覆盖**。

        ``Accept: text/plain`` 之类的写法会让协议根本走不通，而用户设它的时候多半
        并不知道这个头是干什么的 —— 静默照发等于把一个可诊断的失败变成玄学。
        """
        servers = _remote_server(
            "http://127.0.0.1:1/mcp",  # 占位，真正用下面那个
            headers={"Authorization": "Bearer secret", "Accept": "text/plain"},
        )
        with _http_endpoint(_mcp_responder()) as (url, requests):
            servers.url = url
            probe_server(servers, timeout=10)
        assert _header(requests[0]["headers"], "Authorization") == "Bearer secret"
        assert _header(requests[0]["headers"], "Accept") != "text/plain"

    def test_hyphenated_header_names_survive_urlibs_normalization(self):
        """``X-Api-Key`` 这类名字发出去还得是这个样子。

        urllib 会把每个头名过一遍 ``title()``（见 ``_header``）。对带连字符的常见写法
        它是幂等的，所以**这一条过得了不代表我们逐字节控制了大小写** —— 真正的保证是
        HTTP 头名不区分大小写。测试守的是「值对、名字没被改坏成别的头」。
        """
        with _http_endpoint(_mcp_responder()) as (url, requests):
            probe_server(
                _remote_server(url, headers={"X-Api-Key": "k", "X-Goog-Api-Key": "g2"}),
                timeout=10,
            )
        assert _header(requests[0]["headers"], "X-Api-Key") == "k"
        assert _header(requests[0]["headers"], "X-Goog-Api-Key") == "g2"

    def test_session_id_from_initialize_is_echoed_back(self):
        """``Mcp-Session-Id`` 是可选的：给了就必须在后续请求里带上，否则服务端不认。"""
        with _http_endpoint(_mcp_responder(session_id="sess-42")) as (url, requests):
            probe_server(_remote_server(url), timeout=10)
        assert not [h for h in requests[0]["headers"] if h.lower() == "mcp-session-id"]
        assert _header(requests[1]["headers"], "Mcp-Session-Id") == "sess-42"
        assert _header(requests[2]["headers"], "Mcp-Session-Id") == "sess-42"

    def test_a_stateless_server_without_a_session_id_works(self):
        """规范允许服务端无状态、不给这个头 —— 不能因此报错。"""
        with _http_endpoint(_mcp_responder()) as (url, requests):
            assert probe_server(_remote_server(url), timeout=10) == ["probe_echo", "other"]
        assert not [
            h for r in requests for h in r["headers"] if h.lower() == "mcp-session-id"
        ]

    def test_no_tools_is_an_empty_list_not_an_error(self):
        """连上了但一个工具都没有是**合法**的：那是 server 的事，不是连不上。"""
        with _http_endpoint(_mcp_responder(tools=[])) as (url, _):
            assert probe_server(_remote_server(url), timeout=10) == []

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_failures_point_at_the_headers(self, status):
        """401/403 的正文常常只有一句 Unauthorized，用户不知道该去哪改 —— 补一句。"""
        def respond(handler, _request):
            _write(handler, status, "text/plain", b"Unauthorized")

        with _http_endpoint(respond) as (url, _):
            with pytest.raises(McpProbeError) as excinfo:
                probe_server(_remote_server(url), timeout=10)
        message = str(excinfo.value)
        assert str(status) in message
        assert "Unauthorized" in message
        assert "headers" in message

    def test_server_error_carries_status_and_body(self):
        def respond(handler, _request):
            _write(handler, 500, "text/plain", "内部错误：数据库连不上".encode("utf-8"))

        with _http_endpoint(respond) as (url, _):
            with pytest.raises(McpProbeError) as excinfo:
                probe_server(_remote_server(url), timeout=10)
        assert "500" in str(excinfo.value)
        assert "数据库连不上" in str(excinfo.value)

    def test_jsonrpc_error_object_is_reported_verbatim(self):
        """server 用 JSON-RPC 错误回复时，它的原话要透出来（那是唯一的线索）。"""
        with _http_endpoint(_mcp_responder(error=("initialize", {"code": -32000, "message": "协议版本不支持"}))) as (url, _):
            with pytest.raises(McpProbeError) as excinfo:
                probe_server(_remote_server(url), timeout=10)
        assert "协议版本不支持" in str(excinfo.value)

    def test_unparseable_response_reports_content_type_and_body(self):
        """读不出来时必须带上类型与正文开头 —— 那是用户唯一能拿去搜的东西。"""
        def respond(handler, _request):
            _write(handler, 200, "text/html", b"<html><body>Not an MCP server</body></html>")

        with _http_endpoint(respond) as (url, _):
            with pytest.raises(McpProbeError) as excinfo:
                probe_server(_remote_server(url), timeout=10)
        message = str(excinfo.value)
        assert "无法解析" in message
        assert "text/html" in message
        assert "Not an MCP server" in message

    def test_connection_refused_is_reported_as_such(self):
        """连不上与应答错了要分开报：这两件事用户要去查的东西完全不同。"""
        server = _remote_server("http://127.0.0.1:1/mcp", name="refused")
        with pytest.raises(McpProbeError) as excinfo:
            probe_server(server, timeout=5)
        assert "连接失败" in str(excinfo.value)
        assert "refused" in str(excinfo.value)

    def test_hanging_endpoint_times_out(self):
        """挂住不回的端点必须在预算内失败，而不是一直等下去。"""
        def respond(handler, _request):
            time.sleep(10)
            _write(handler, 200, "application/json", b"{}")

        with _http_endpoint(respond) as (url, _):
            started = time.monotonic()
            with pytest.raises(McpProbeError) as excinfo:
                probe_server(_remote_server(url), timeout=1.5)
            assert time.monotonic() - started < 8
        assert "超时" in str(excinfo.value)

    def test_bad_url_does_not_leak_a_valueerror(self):
        """畸形地址在注册表那关已经挡过，这里兜底 —— 漏出去会变成调用方的 500。"""
        server = _remote_server("http://127.0.0.1:1/mcp", name="bad")
        server.url = "not-a-url"
        with pytest.raises(McpProbeError):
            probe_server(server, timeout=5)

    def test_required_false_remote_only_warns(self, capsys):
        """远程条目同样适用「非必需就只告警」—— 这个语义与传输无关。"""
        servers = [_remote_server("http://127.0.0.1:1/mcp", name="opt", required=False)]
        assert probe_servers(servers, timeout=5) == {}
        assert "opt" in capsys.readouterr().err

    def test_transport_decides_which_way_we_go(self):
        """远程条目绝不能去起子进程 —— command 在远程条目上是空串，真去起就是个谜之错误。"""
        with _http_endpoint(_mcp_responder()) as (url, _):
            server = _remote_server(url, command="/nonexistent/should-never-run")
            assert probe_server(server, timeout=10) == ["probe_echo", "other"]
