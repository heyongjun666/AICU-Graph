"""runtime/mcp_manager.py 单元测试：注册表读取、名字解析、写盘校验、McpServer 的中立表示。

全部离线，不启进程 —— 「真去连一个 server」那部分在 tests/test_mcp_client.py。
"""
import json
from pathlib import Path

import pytest

from runtime.mcp_manager import (
    McpConfigError,
    McpServer,
    REGISTRY_FILE,
    REPO_ROOT,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
    delete_server,
    list_servers,
    load_registry,
    resolve_servers,
    roles_referencing,
    save_server,
    validate_server,
)

ECHO_ARGS = ["tests/mcp_echo_server.py"]
REMOTE_URL = "https://mcp.example.com/mcp"


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

    def test_scoped_npm_package_arg_is_left_alone(self, tmp_path):
        """npm 作用域包名带 `/`，但它不是路径。

        按仓库相对路径解析会把它改写成本机一个**不存在**的绝对路径，npx 拿到后只会报
        自己的错，用户很难反推回「包名被当路径了」。而 `npx -y @scope/pkg` 正是市面上
        绝大多数 MCP server 的标准写法。
        """
        path = _write_registry(tmp_path, {
            "everything": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-everything"]},
        })
        server = load_registry(path)["everything"]
        assert server.command == "npx"
        assert server.args == ["-y", "@modelcontextprotocol/server-everything"]
        assert not server.args[1].startswith("/")

    def test_url_arguments_are_left_alone(self, tmp_path):
        """含 `://` 的参数同理：它不可能是仓库里的一个文件。"""
        path = _write_registry(tmp_path, {
            "probe": {"command": "probe-cli", "args": ["--endpoint", "https://api.example.com/x"]},
        })
        assert load_registry(path)["probe"].args[1] == "https://api.example.com/x"

    def test_remote_entry_loads_with_its_transport(self, tmp_path):
        path = _write_registry(tmp_path, {"remote": {"url": REMOTE_URL}})
        server = load_registry(path)["remote"]
        assert server.transport == TRANSPORT_HTTP
        assert server.url == REMOTE_URL
        assert server.command == ""
        assert server.args == []
        assert server.env == {}

    def test_remote_url_and_headers_never_go_through_path_resolution(self, tmp_path):
        """headers 的值里出现 `/` 是常事（`Bearer a/b`），被当成相对路径就毁了。"""
        path = _write_registry(tmp_path, {
            "remote": {"url": REMOTE_URL, "headers": {"Authorization": "Bearer a/b"}},
        })
        server = load_registry(path)["remote"]
        assert server.url == REMOTE_URL
        assert server.headers == {"Authorization": "Bearer a/b"}

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

    def test_transport_defaults_to_stdio(self):
        """既有构造点都不传 transport（各测试、各处 McpServer(...)），必须仍是本地。"""
        assert _echo_server().transport == TRANSPORT_STDIO
        assert _echo_server().url == ""
        assert _echo_server().headers == {}


# =============================================================================
# 写路径：设置页的增删改查就落在这几个函数上
#
# 与读路径最大的不同是**文件是用户累积出来的**：里面会有仓库自己写的 _comment、会有
# 用户手写的相对路径、会有别的条目。写一条不能把其余部分碰坏 —— 所以下面盯的是
# 「写完以后文件还是不是原来那个样子」，而不只是「新条目进去了」。
# =============================================================================

COMMENT_LINES = ["AICU 的 MCP server 注册表。", "command/args 集中在这里。"]


def _write_registry(tmp_path, servers: dict, comment: list[str] | None = None) -> Path:
    """造一个临时注册表，形状与仓库根那份一致（含 _comment 数组）。"""
    path = tmp_path / "mcp_servers.json"
    payload: dict = {}
    if comment is not None:
        payload["_comment"] = comment
    payload["servers"] = servers
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _spec(**overrides) -> dict:
    spec = {"command": "python3", "args": ["tests/mcp_echo_server.py"]}
    spec.update(overrides)
    return spec


def _remote_spec(**overrides) -> dict:
    spec = {"url": REMOTE_URL}
    spec.update(overrides)
    return spec


class TestValidateServer:
    """名字与 env 键的白名单不是洁癖：它们会变成 CLI 的参数和配置键。"""

    @pytest.mark.parametrize("name", ["Echo", "1echo", "echo.probe", "echo]x", "echo probe", "", "ec/ho"])
    def test_rejects_names_that_break_some_cli(self, name):
        """名字会进 grok 的 TOML 表名、codex 的 -c 键路径、gemini 的 --allowed-...

        带 `.` 的名字会把 codex 的键路径劈成嵌套，带 `]` 的会写坏 TOML 表名 —— 这些
        在写盘时不拦，就要等某个 CLI 报一个与 MCP 毫无关系的错时才暴露。
        """
        with pytest.raises(McpConfigError):
            validate_server(name, _spec())

    @pytest.mark.parametrize("name", ["echo-probe", "echo_probe", "a1", "my-server-2"])
    def test_accepts_ordinary_names(self, name):
        assert validate_server(name, _spec())["command"] == "python3"

    @pytest.mark.parametrize("command", [None, "", "   ", 3, ["python3"]])
    def test_requires_a_usable_command(self, command):
        with pytest.raises(McpConfigError):
            validate_server("echo-probe", _spec(command=command))

    def test_rejects_non_list_args(self):
        with pytest.raises(McpConfigError):
            validate_server("echo-probe", _spec(args="tests/mcp_echo_server.py"))

    def test_rejects_non_object_env(self):
        with pytest.raises(McpConfigError):
            validate_server("echo-probe", _spec(env=["A=1"]))

    @pytest.mark.parametrize("key", ["a.b", "1A", "A-B", ""])
    def test_rejects_env_keys_that_break_codex(self, key):
        """codex 走 `-c mcp_servers.<name>.env.<KEY>=...`，KEY 里带 `.` 会被当成嵌套路径。"""
        with pytest.raises(McpConfigError):
            validate_server("echo-probe", _spec(env={key: "1"}))

    def test_required_defaults_to_true(self):
        """与 load_registry 的读侧默认值一致 —— 两侧不一致会造成「存进去读出来变了」。"""
        assert validate_server("echo-probe", _spec())["required"] is True
        assert validate_server("echo-probe", _spec(required=False))["required"] is False

    def test_what_was_validated_can_be_read_back(self, tmp_path):
        """写前校验的产物必须过得了读侧那一关。

        「界面上存得进去、运行时读不出来」是这类管理界面最典型的坏法，而且失败点在另一个
        进程、另一段时间。这里把 validate 的结果直接喂给 load_registry 走一遍。
        """
        path = _write_registry(tmp_path, {"echo-probe": validate_server("echo-probe", _spec(
            env={"MCP_TOKEN": "x"}, description="探针", required=False,
        ))})
        server = load_registry(path)["echo-probe"]
        assert server.env == {"MCP_TOKEN": "x"}
        assert server.description == "探针"
        assert server.required is False


class TestValidateRemoteServer:
    """远程条目：看 url/headers，与本地那组字段互斥。

    互斥的字段**报错而不是忽略** —— 一个存得进去、永远不会被下发的字段，用户会以为
    配上了（CLI 配置里根本没有放它的位置）。这与 `_resolve_path` 那个缺陷是同一类坏法。
    """

    def test_accepts_a_remote_entry(self):
        assert validate_server("remote", _remote_spec(headers={"Authorization": "Bearer x"})) == {
            "url": REMOTE_URL,
            "headers": {"Authorization": "Bearer x"},
            "required": True,
        }

    def test_omits_empty_headers(self):
        assert validate_server("remote", _remote_spec()) == {"url": REMOTE_URL, "required": True}

    def test_accepts_header_names_with_hyphens(self):
        """真实 header 名带连字符，所以这里刻意不套 env 那种正则。"""
        entry = validate_server("remote", _remote_spec(headers={"X-Api-Key": "k"}))
        assert entry["headers"] == {"X-Api-Key": "k"}

    def test_rejects_both_connection_targets_at_once(self):
        with pytest.raises(McpConfigError):
            validate_server("remote", _remote_spec(command="python3"))

    def test_rejects_no_connection_target_at_all(self):
        with pytest.raises(McpConfigError):
            validate_server("remote", {})

    @pytest.mark.parametrize("key,value", [("args", ["--x"]), ("env", {"A": "1"})])
    def test_rejects_stdio_only_fields_on_a_remote_entry(self, key, value):
        with pytest.raises(McpConfigError):
            validate_server("remote", _remote_spec(**{key: value}))

    def test_rejects_headers_without_a_url(self):
        with pytest.raises(McpConfigError):
            validate_server("echo-probe", _spec(headers={"X-Api-Key": "k"}))

    @pytest.mark.parametrize("url", ["ftp://h/x", "file:///etc/passwd", "ws://h/x", "mcp.example.com/mcp", "   ", 3])
    def test_rejects_urls_that_are_not_http(self, url):
        """只支持 Streamable HTTP。SSE 与其他协议在这里就挡掉，别等预检去猜。"""
        with pytest.raises(McpConfigError):
            validate_server("remote", _remote_spec(url=url))

    @pytest.mark.parametrize("headers", [["A=1"], "A=1", 3])
    def test_rejects_non_object_headers(self, headers):
        with pytest.raises(McpConfigError):
            validate_server("remote", _remote_spec(headers=headers))

    @pytest.mark.parametrize("key,value", [
        ("X-Api-Key\nX-Evil", "1"),
        ("X-Api-Key", "1\r\nX-Evil: 2"),
    ])
    def test_rejects_header_injection(self, key, value):
        """换行会把一个 header 劈成两个。挡在这里，而不是等 http.client 报一句与 MCP 无关的话。"""
        with pytest.raises(McpConfigError):
            validate_server("remote", _remote_spec(headers={key: value}))

    def test_remote_entry_round_trips_through_the_read_side(self, tmp_path):
        path = _write_registry(tmp_path, {
            "remote": validate_server("remote", _remote_spec(headers={"X-Api-Key": "k"}, required=False)),
        })
        server = load_registry(path)["remote"]
        assert server.transport == TRANSPORT_HTTP
        assert server.headers == {"X-Api-Key": "k"}
        assert server.required is False


class TestSaveServer:
    def test_saves_a_new_server(self, tmp_path):
        path = _write_registry(tmp_path, {}, comment=COMMENT_LINES)
        save_server("echo-probe", _spec(), path)
        assert load_registry(path)["echo-probe"].command == "python3"

    def test_relative_args_survive_the_round_trip(self, tmp_path):
        """写回去的必须是**原始写法**，不能是解析后的绝对路径。

        load_registry 会把 command/args 解析成绝对路径。若写路径图省事拿它的返回值重建，
        `tests/mcp_echo_server.py` 就会变成写死在本机的绝对路径 —— 功能照常，换台机器才炸，
        是那种不会有人立刻发现的坏法。所以写路径从原始 JSON 文档出发。
        """
        path = _write_registry(tmp_path, {}, comment=COMMENT_LINES)
        save_server("echo-probe", _spec(), path)

        text = path.read_text(encoding="utf-8")
        assert "tests/mcp_echo_server.py" in text
        assert str(REPO_ROOT) not in text, "相对路径被解析成了本机绝对路径"
        # 读侧仍然照常解析成绝对路径，功能不受影响
        assert load_registry(path)["echo-probe"].args[0].startswith(str(REPO_ROOT))

    def test_comment_block_is_preserved(self, tmp_path):
        """_comment 是写给改这个文件的人看的说明，程序不能因为自己写了一次就把它抹掉。"""
        path = _write_registry(tmp_path, {}, comment=COMMENT_LINES)
        save_server("echo-probe", _spec(), path)
        assert json.loads(path.read_text(encoding="utf-8"))["_comment"] == COMMENT_LINES

    def test_remote_entry_round_trip_writes_url_and_no_command(self, tmp_path):
        path = _write_registry(tmp_path, {}, comment=COMMENT_LINES)
        save_server("remote", _remote_spec(headers={"Authorization": "Bearer x"}), path)

        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["_comment"] == COMMENT_LINES
        assert saved["servers"]["remote"] == {
            "url": REMOTE_URL,
            "headers": {"Authorization": "Bearer x"},
            "required": True,
        }

    def test_replaces_an_existing_entry_without_touching_others(self, tmp_path):
        path = _write_registry(tmp_path, {"keep": {"command": "node"}}, comment=COMMENT_LINES)
        save_server("echo-probe", _spec(), path)
        save_server("echo-probe", _spec(command="/usr/bin/python3"), path)

        saved = json.loads(path.read_text(encoding="utf-8"))["servers"]
        assert saved["echo-probe"]["command"] == "/usr/bin/python3"
        assert saved["keep"] == {"command": "node"}

    def test_a_rejected_spec_leaves_the_file_untouched(self, tmp_path):
        """校验失败必须发生在写盘之前 —— 否则用户看到报错，文件却已经被改坏了。"""
        path = _write_registry(tmp_path, {"keep": {"command": "node"}}, comment=COMMENT_LINES)
        before = path.read_text(encoding="utf-8")
        with pytest.raises(McpConfigError):
            save_server("echo-probe", _spec(command=""), path)
        assert path.read_text(encoding="utf-8") == before

    def test_leaves_no_tmp_file_behind(self, tmp_path):
        """原子写用 `<path>.tmp` 中转；写完必须只剩正式文件。

        残留的 .tmp 会被下次 load_registry 当成一个不存在的注册表吗？不会 —— 但它是仓库根
        下一个没人认识的 JSON，会进 git status，用户会来问这是什么。
        """
        path = _write_registry(tmp_path, {}, comment=COMMENT_LINES)
        save_server("echo-probe", _spec(), path)
        assert not (tmp_path / "mcp_servers.json.tmp").exists()


class TestDeleteServer:
    def test_deletes_only_the_named_server(self, tmp_path):
        path = _write_registry(
            tmp_path, {"a": {"command": "node"}, "b": {"command": "node"}}, comment=COMMENT_LINES
        )
        delete_server("a", path)

        document = json.loads(path.read_text(encoding="utf-8"))
        assert list(document["servers"]) == ["b"]
        assert document["_comment"] == COMMENT_LINES

    def test_unknown_name_raises_key_error(self, tmp_path):
        """KeyError 而不是 McpConfigError：路由层据此翻 404（与角色/技能的删除一致）。"""
        path = _write_registry(tmp_path, {}, comment=COMMENT_LINES)
        with pytest.raises(KeyError):
            delete_server("nope", path)


class TestListServers:
    def test_returns_raw_unresolved_values(self, tmp_path):
        """界面要显示和编辑的是**用户写下的那份**，解析成绝对路径是运行时的事。"""
        path = _write_registry(tmp_path, {"echo-probe": _spec(description="探针", required=False)})
        item = list_servers(path)[0]
        assert item == {
            "name": "echo-probe",
            "command": "python3",
            "args": ["tests/mcp_echo_server.py"],
            "env": {},
            "description": "探针",
            "required": False,
        }

    def test_sorted_by_name(self, tmp_path):
        path = _write_registry(tmp_path, {"zeta": {"command": "a"}, "alpha": {"command": "b"}})
        assert [item["name"] for item in list_servers(path)] == ["alpha", "zeta"]

    def test_stdio_entry_carries_no_url_keys(self, tmp_path):
        """本地条目恒为六个键：界面那套「命令 + 参数 + 环境变量」的表单正是照这个形状写的，
        多两个恒为空的键只会让人以为要填。"""
        path = _write_registry(tmp_path, {"echo-probe": _spec()})
        item = list_servers(path)[0]
        assert "url" not in item and "headers" not in item

    def test_remote_entry_exposes_url_and_headers(self, tmp_path):
        path = _write_registry(tmp_path, {"remote": {
            "url": REMOTE_URL, "headers": {"X-Api-Key": "k"},
        }})
        item = list_servers(path)[0]
        assert item["url"] == REMOTE_URL
        assert item["headers"] == {"X-Api-Key": "k"}
        # 其余键仍在（值为空），界面拿到的条目形状是统一的
        assert item["command"] == "" and item["args"] == [] and item["env"] == {}

    def test_broken_entry_is_shown_rather_than_raised(self, tmp_path):
        """坏条目要能列出来 —— 这个界面正是用来修它的，读不出来就没法修。

        与 load_registry 刻意相反：那条是运行路径，坏条目必须当场炸。
        """
        path = _write_registry(tmp_path, {"broken": {"args": []}, "ok": {"command": "node"}})
        items = {item["name"]: item for item in list_servers(path)}
        assert items["broken"]["command"] == ""
        assert items["ok"]["command"] == "node"


class TestRolesReferencing:
    """反向查询：还有谁在用这个 server（删除确认框要列出来）。"""

    def test_finds_roles_listing_the_name(self):
        configs = [
            ("mcp_probe", "worker", {"mcp": ["echo-probe"]}),
            ("plain", "worker", {}),
        ]
        assert roles_referencing("echo-probe", configs) == [{"id": "mcp_probe", "type": "worker"}]

    def test_scalar_mcp_counts_as_one_name(self):
        """写成 `mcp: echo-probe` 的也认。

        运行时与界面都把标量视为一个 server 名字，而不是字符序列；界面要如实显示「有人在
        引用」，否则用户会以为这个 server 没人用。
        """
        configs = [("mcp_probe", "worker", {"mcp": "echo-probe"})]
        assert roles_referencing("echo-probe", configs) == [{"id": "mcp_probe", "type": "worker"}]

    def test_ignores_unrelated_and_malformed_configs(self):
        configs = [
            ("other", "worker", {"mcp": ["another"]}),
            ("none", "worker", {"mcp": []}),
            ("broken", "worker", None),
        ]
        assert roles_referencing("echo-probe", configs) == []

    def test_same_id_under_both_types_stays_distinguishable(self):
        """角色 id 在 Manager 与 Worker 下可以重名（两个目录各一份）。

        只回 id 的话，界面上会写着「被 dev 引用」而说不清是哪一个 —— 删除确认框里那行字
        恰恰是用户唯一的判断依据。
        """
        configs = [
            ("dev", "worker", {"mcp": ["echo-probe"]}),
            ("dev", "manager", {"mcp": ["echo-probe"]}),
        ]
        assert roles_referencing("echo-probe", configs) == [
            {"id": "dev", "type": "manager"},
            {"id": "dev", "type": "worker"},
        ]

    def test_sorted_and_deduplicated(self):
        """排序与 RolesManager.list_roles() 一致（先 type 后 id），重复声明只算一次。"""
        configs = [
            ("zeta", "worker", {"mcp": ["echo-probe"]}),
            ("alpha", "manager", {"mcp": ["echo-probe", "echo-probe"]}),
            ("alpha", "worker", {"mcp": ["echo-probe"]}),
        ]
        assert roles_referencing("echo-probe", configs) == [
            {"id": "alpha", "type": "manager"},
            {"id": "alpha", "type": "worker"},
            {"id": "zeta", "type": "worker"},
        ]
