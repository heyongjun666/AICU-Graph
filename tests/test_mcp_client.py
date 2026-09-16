"""runtime/mcp_client.py 单元测试。

分三类：
  1. 注册表读取与名字解析（纯离线，不启进程）
  2. McpServer 的中立表示（下发配置该长什么样由 adapter 决定，见 test_cli_adapters.py）
  3. 预检：真的去启 tests/mcp_echo_server.py 走一遍握手，以及三种失败形态

第 3 类刻意不 mock 子进程 —— 握手是 stdio 上的时序协议，mock 掉就等于只测了
「我们以为的协议」，而这正是最容易写错的地方。
"""
import pytest

from runtime.mcp_client import (
    McpConfigError,
    McpProbeError,
    McpServer,
    REGISTRY_FILE,
    load_registry,
    probe_server,
    probe_servers,
    resolve_servers,
)

ECHO_ARGS = ["tests/mcp_echo_server.py"]


def _echo_server(**overrides) -> McpServer:
    spec = dict(name="echo-probe", command="python3", args=ECHO_ARGS)
    spec.update(overrides)
    return McpServer(**spec)


# ============================================================================
# 1. 注册表与名字解析
# ============================================================================


class TestRegistry:
    def test_repo_registry_loads(self):
        registry = load_registry()
        assert "echo-probe" in registry
        assert registry["echo-probe"].command == "python3"

    def test_relative_args_resolved_against_repo_root(self):
        """角色 yaml 里写相对路径、clone 下来即可用，不要求用户改成绝对路径。"""
        server = load_registry()["echo-probe"]
        assert server.args[0].startswith("/")
        assert server.args[0].endswith("tests/mcp_echo_server.py")

    def test_bare_command_left_to_path(self):
        """不含路径分隔符的值原样保留，交由 PATH 查找。"""
        server = load_registry()["echo-probe"]
        assert server.command == "python3"

    def test_missing_registry_file(self, tmp_path):
        with pytest.raises(McpConfigError):
            load_registry(tmp_path / "nope.json")

    def test_broken_json(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{ not json", encoding="utf-8")
        with pytest.raises(McpConfigError):
            load_registry(path)

    def test_missing_servers_object(self, tmp_path):
        path = tmp_path / "flat.json"
        path.write_text('{"echo-probe": {}}', encoding="utf-8")
        with pytest.raises(McpConfigError):
            load_registry(path)

    def test_entry_without_command(self, tmp_path):
        path = tmp_path / "nocmd.json"
        path.write_text('{"servers": {"x": {"args": []}}}', encoding="utf-8")
        with pytest.raises(McpConfigError):
            load_registry(path)

    def test_resolve_keeps_declared_order(self):
        registry = {
            "a": McpServer(name="a", command="x"),
            "b": McpServer(name="b", command="y"),
        }
        assert [s.name for s in resolve_servers(["b", "a"], registry)] == ["b", "a"]

    def test_resolve_empty(self):
        assert resolve_servers(None) == []
        assert resolve_servers([]) == []

    def test_resolve_unknown_name_lists_available(self):
        """报错必须带上「有哪些可用」，否则用户只能去翻注册表。"""
        with pytest.raises(McpConfigError) as excinfo:
            resolve_servers(["typo-probe"])
        message = str(excinfo.value)
        assert "typo-probe" in message
        assert "echo-probe" in message
        assert str(REGISTRY_FILE) in message


# ============================================================================
# 2. McpServer 的中立表示（各 CLI 的序列化在 tests/test_cli_adapters.py）
# ============================================================================


class TestMcpServerShape:
    def test_command_and_args_stay_separate(self):
        """command 与 args 必须分开存，不能提前拼成一条命令行。

        三家 CLI 要的形状互不相同：claude 系要 ``{"command": "python3", "args": [...]}``，
        opencode 要 ``command: ["python3", ...]``，codex 要一对对 ``-c`` 键值。一旦有人在
        这里先把它们拼成 ``"python3 x.py"``，opencode 那边就得再拆回来（还要正确引号，
        路径带空格即错）。这条守住「中立表示」这个前提。
        """
        server = _echo_server()
        assert server.command == "python3"
        assert server.args == ECHO_ARGS

    def test_env_defaults_to_empty_not_none(self):
        """env 恒为 dict：三个 adapter 都要直接遍历它，None 会逼出三处 `or {}`。"""
        assert _echo_server().env == {}


# ============================================================================
# 3. 预检（真启子进程）
# ============================================================================


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
