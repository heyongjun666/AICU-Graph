"""runtime/mcp_manager.py 单元测试：注册表读取、名字解析、McpServer 的中立表示。

全部离线，不启进程 —— 「真去连一个 server」那部分在 tests/test_mcp_client.py。
"""
import pytest

from runtime.mcp_manager import (
    McpConfigError,
    McpServer,
    REGISTRY_FILE,
    load_registry,
    resolve_servers,
)

ECHO_ARGS = ["tests/mcp_echo_server.py"]


def _echo_server(**overrides) -> McpServer:
    spec = dict(name="echo-probe", command="python3", args=ECHO_ARGS)
    spec.update(overrides)
    return McpServer(**spec)


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
