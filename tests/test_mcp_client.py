"""runtime/mcp_client.py 单元测试：对着 tests/mcp_echo_server.py 真走一遍 stdio 握手。

刻意不 mock 子进程 —— 握手是 stdio 上的时序协议，mock 掉就等于只测了「我们以为的
协议」，而这正是最容易写错的地方。注册表读取与名字解析在 tests/test_mcp_manager.py。
"""
import pytest

from pathlib import Path

from runtime.mcp_client import McpProbeError, probe_server, probe_servers
from runtime.mcp_manager import McpServer

# 绝对路径：相对写法 "tests/mcp_echo_server.py" 是相对 **cwd** 解析的，
# 从别的目录跑 pytest 就会变成「找不到文件」，看上去像 MCP 预检坏了。
ECHO_ARGS = [str(Path(__file__).resolve().parent / "mcp_echo_server.py")]


def _echo_server(**overrides) -> McpServer:
    spec = dict(name="echo-probe", command="python3", args=ECHO_ARGS)
    spec.update(overrides)
    return McpServer(**spec)


class TestProbe:
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
