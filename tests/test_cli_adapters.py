"""CLI 适配层单元测试。"""

import hashlib
import json
import os
import types

import pytest

import cli_adapters
from cli_adapters import (
    AntigravityAdapter,
    ClaudeCodeAdapter,
    CodeBuddyAdapter,
    CodexAdapter,
    CursorAdapter,
    GeminiAdapter,
    GenericExecAdapter,
    KimiAdapter,
    OpenCodeAdapter,
    PiAdapter,
    QwenAdapter,
    detect_default,
    detect_installed,
    get_adapter,
)
from claude_code_agent import ClaudeCodeAgent
from runtime.credentials import CredentialStore, set_credential_store, vendor_of_model
from runtime.mcp_manager import (
    TRANSPORT_HTTP,
    McpConfigError,
    McpServer,
    load_registry,
    resolve_servers,
)


def _mcp_servers(*names, **kwargs) -> list:
    """按名字造一批 McpServer。

    下发到 adapter 的货币是 **server 对象**而不是名字字符串：三家 CLI 要的配置形态
    不同（CC 族 argv JSON / codex 的 -c 键值 / opencode 的环境变量 JSON），名字字符串
    在这些形状里放不下 command、args。测试跟着用真实类型，别用字符串凑 ——
    字符串会让 `server.name` 这种属性访问直接炸，而真实调用路径上它永远不是字符串。
    """
    return [
        McpServer(name=name, command="python3", args=["/tmp/echo.py"], **kwargs)
        for name in names
    ]


REMOTE_URL = "https://mcp.example.com/mcp"


def _remote_servers(*names, **kwargs) -> list:
    """按名字造一批**远程**（Streamable HTTP）McpServer。

    与 ``_mcp_servers`` 并列而不是给它加个参数：一份「本地」的服务器列表和一个
    「远程」的，在断言里读起来是两件事，而加参数会让每条断言都先在心里解一层
    transport 从哪来。``transport`` 照真实构造点那样显式给（运行时由
    ``mcp_manager.transport_of`` 从 url 派生，adapter 只读结果）。
    """
    kwargs.setdefault("headers", {"Authorization": "Bearer t"})
    return [
        McpServer(name=name, command="", url=REMOTE_URL, transport=TRANSPORT_HTTP, **kwargs)
        for name in names
    ]


class TestRegistry:
    def test_get_adapter_known(self):
        assert isinstance(get_adapter("claude"), ClaudeCodeAdapter)
        assert isinstance(get_adapter("codebuddy"), CodeBuddyAdapter)
        assert isinstance(get_adapter("codex"), CodexAdapter)
        assert isinstance(get_adapter("kimi"), KimiAdapter)
        assert isinstance(get_adapter("qwen"), QwenAdapter)
        assert isinstance(get_adapter("opencode"), OpenCodeAdapter)
        assert isinstance(get_adapter("pi"), PiAdapter)
        assert isinstance(get_adapter("gemini"), GeminiAdapter)
        assert isinstance(get_adapter("cursor"), CursorAdapter)
        assert isinstance(get_adapter("antigravity"), GenericExecAdapter)
        assert isinstance(get_adapter("grok"), GenericExecAdapter)

    def test_get_adapter_unknown_raises(self):
        with pytest.raises(ValueError):
            get_adapter("copilot")

    def test_priority_order_prefers_claude_and_excludes_copilot(self):
        ids = [a.id for a in cli_adapters._ADAPTERS]
        assert ids[0] == "claude"
        assert "copilot" not in ids


class TestDetect:
    def test_detect_default_respects_priority(self, monkeypatch):
        def fake_which(command):
            return "/usr/bin/x" if command in {"claude", "codex"} else None

        monkeypatch.setattr(cli_adapters.shutil, "which", fake_which)
        assert detect_default() == "claude"

    def test_detect_installed_filters(self, monkeypatch):
        def fake_which(command):
            return "/usr/bin/x" if command in {"claude", "codex"} else None

        monkeypatch.setattr(cli_adapters.shutil, "which", fake_which)
        # codex 的 is_installed 需额外甄别 --help；此处让其通过（OpenAI Codex 特征含 exec）
        monkeypatch.setattr(
            cli_adapters.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(stdout="exec", stderr=""),
        )
        assert detect_installed() == ["claude", "codex"]

    def test_detect_default_none_raises(self, monkeypatch):
        monkeypatch.setattr(cli_adapters.shutil, "which", lambda command: None)
        with pytest.raises(RuntimeError):
            detect_default()


class TestAdapterEnv:
    def test_agent_does_not_inject_api_key_for_unrouted_cli(self, tmp_path):
        """没纳入路由的 CLI（cursor）不把本机存的 key 注进环境。"""
        store = CredentialStore(str(tmp_path / "api_keys.json"))
        store.set("deepseek", "sk-secret")
        set_credential_store(store)
        try:
            agent = object.__new__(ClaudeCodeAgent)
            agent.adapter = get_adapter("cursor")
            agent.cli_name = "cursor"
            agent.model = "deepseek-v4-flash"
            agent.api_base_url = None
            agent.api_key_env = None
            agent.mcp_servers = []
            env = agent._build_process_env()
            assert env.get("DEEPSEEK_API_KEY") != "sk-secret"
        finally:
            set_credential_store(None)


def _allowed_tools(cmd: list) -> list:
    """取出 ``--allowedTools`` 放行的工具名，**兼容两种 argv 形态**。

    测的是语义（哪些工具被放行），不是某一种拼法：

      - claude：「空格连接成单个参数」—— claude 的文档写明认这种；
      - codebuddy：「一个名字一个 argv 项」—— 它不拆串，整串会被当成**一个**工具名，
        白名单于是整个失效（见 CodeBuddyAdapter.allowed_tools_flag）。

    形态本身另有专门用例钉住（TestCodeBuddyAllowlistShape），别让这里顺手把它放过去。
    """
    names: list = []
    i = cmd.index("--allowedTools") + 1
    while i < len(cmd) and not cmd[i].startswith("--"):
        names.extend(cmd[i].split())
        i += 1
    return names


class TestMcpAllowlist:
    """MCP 工具必须并进 --allowedTools 白名单。

    实测（claude 系）：--allowedTools 是**白名单**不是「先问再放行」，dontAsk
    下不在名单里的工具直接拒绝，且没有任何通配写法（`mcp__*` 实测无效）。所以声明了
    MCP 却不放行，表现是 permission_denials 里出现 mcp__<server>__<tool>、agent 拿不到
    结果后绕路或报一个与 MCP 无关的错 —— 正是预检要消除的那类失败。
    """

    @pytest.mark.parametrize("cli", ["claude", "codebuddy"])
    def test_mcp_server_added_to_allowlist(self, cli):
        cmd = get_adapter(cli).build_base_command(
            model=None,
            skip_permissions=True,
            enable_project_skills=False,
            mcp_servers=_mcp_servers("echo-probe"),
        )
        allowed = _allowed_tools(cmd)
        assert "mcp__echo-probe" in allowed
        assert set("Read Write Edit Bash".split()) <= set(allowed)

    def test_multiple_servers_all_added(self):
        cmd = get_adapter("claude").build_base_command(
            model=None,
            skip_permissions=True,
            enable_project_skills=False,
            mcp_servers=_mcp_servers("a", "b"),
        )
        allowed = _allowed_tools(cmd)
        assert "mcp__a" in allowed and "mcp__b" in allowed

    def test_no_mcp_keeps_allowlist_byte_identical(self):
        """没声明 MCP 时命令与改动前逐字相同 —— 这是既有节点的行为基线。"""
        cmd = get_adapter("claude").build_base_command(
            model=None, skip_permissions=False, enable_project_skills=False, mcp_servers=None
        )
        assert cmd == ["claude"]

        cmd = get_adapter("claude").build_base_command(
            model=None, skip_permissions=True, enable_project_skills=False, mcp_servers=[]
        )
        assert cmd[cmd.index("--allowedTools") + 1] == "Read Write Edit Bash"

    def test_allowlist_sourced_from_same_list_sent_to_cli(self):
        """白名单名与 --mcp-config 里的 mcpServers 必须同源。

        agent 只收一份 server 列表，白名单和下发配置各自从它上面取 —— 同一个来源才
        不可能出现「配了 A 却放行 B」。这里把两条路径的产物都拿出来对账。
        """
        servers = resolve_servers(["echo-probe"], load_registry())
        agent = object.__new__(ClaudeCodeAgent)
        agent.adapter = get_adapter("claude")
        agent.cli_name = "claude"
        agent.cli_model_arg = None
        agent.skip_permissions = True
        agent.enable_project_skills = False
        agent.mcp_servers = servers

        cmd = agent._build_base_command()
        allowed = cmd[cmd.index("--allowedTools") + 1].split()

        # mcp_flags 由 chat() 在 build_base_command 之后追加，两条路径吃的是同一份列表
        cmd.extend(agent.adapter.mcp_flags(agent.mcp_servers))
        config_arg = next(a for a in cmd if a.startswith("--mcp-config="))
        sent = json.loads(config_arg.split("=", 1)[1])["mcpServers"]
        assert set(sent) == {"echo-probe"}
        for name in sent:
            assert f"mcp__{name}" in allowed


class TestClaudeAdapter:
    def test_base_command_claude(self):
        adapter = get_adapter("claude")
        cmd = adapter.build_base_command(
            model="claude-sonnet-4-6", skip_permissions=True, enable_project_skills=True
        )
        assert cmd[0] == "claude"
        assert "--model" in cmd and "claude-sonnet-4-6" in cmd
        assert "--allowedTools" in cmd and "--permission-mode" in cmd
        assert "--setting-sources" in cmd

    def test_base_command_claude_no_username(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_USERNAME", "someone")
        adapter = get_adapter("claude")
        cmd = adapter.build_base_command(
            model=None, skip_permissions=False, enable_project_skills=False
        )
        assert "--username" not in cmd

    def test_print_flags_streaming_vs_json(self):
        adapter = get_adapter("claude")
        assert adapter.print_flags(streaming=True) == [
            "--print", "--verbose", "--output-format", "stream-json",
        ]
        assert adapter.print_flags(streaming=False) == [
            "--print", "--output-format", "json",
        ]

    def test_parse_result_single_json(self):
        adapter = get_adapter("claude")
        raw = json.dumps({"result": "ok", "session_id": "s1", "subtype": "success"})
        out = adapter.parse_result(raw)
        assert out["result"] == "ok"
        assert out["session_id"] == "s1"
        assert out["subtype"] == "success"
        assert out["errors"] == []

    def test_parse_result_error_subtype_coerced_to_list(self):
        adapter = get_adapter("claude")
        raw = json.dumps({"subtype": "error_during_execution", "errors": ["boom"]})
        out = adapter.parse_result(raw)
        assert out["subtype"] == "error_during_execution"
        assert out["errors"] == ["boom"]

    def test_cross_vendor_model_authenticates_with_auth_token(self):
        """claude CLI 走第三方 Anthropic 端点时只认 ANTHROPIC_AUTH_TOKEN。

        实测：改道到 deepseek 的 /anthropic 端点后，只注入 ANTHROPIC_API_KEY 时
        CLI 直接判成「Not logged in · Please run /login」并 rc=1（连请求都没发）；
        换成 ANTHROPIC_AUTH_TOKEN 才通。这里断言同族/跨族两条路径。
        """
        adapter = get_adapter("claude")
        # 同族：Anthropic 自己的模型，认证方式不变
        assert adapter.api_key_env_for_model("claude-sonnet-4-6") == "ANTHROPIC_API_KEY"
        assert adapter.api_key_env_for_model("auto") == "ANTHROPIC_API_KEY"
        # 跨厂商：改用 Bearer 令牌变量
        assert adapter.api_key_env_for_model("deepseek-v4-pro") == "ANTHROPIC_AUTH_TOKEN"
        assert adapter.api_key_env_for_model("glm-5.2") == "ANTHROPIC_AUTH_TOKEN"

    def test_build_env_drops_stale_anthropic_key_when_rerouted(self):
        """改道时清掉父环境残留的 ANTHROPIC_API_KEY，免得把 Anthropic 的 key 发到别家。"""
        adapter = get_adapter("claude")
        base_env = {"ANTHROPIC_API_KEY": "sk-ant-parent", "PATH": "/usr/bin"}
        rerouted = adapter.build_env(
            base_env, api_base_url="https://api.deepseek.com/anthropic",
            api_key_env="ANTHROPIC_AUTH_TOKEN",
        )
        assert rerouted["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
        assert "ANTHROPIC_API_KEY" not in rerouted
        # 同族（未改道）不动既有认证变量
        native = adapter.build_env(
            base_env, api_base_url=None, api_key_env="ANTHROPIC_API_KEY"
        )
        assert native["ANTHROPIC_API_KEY"] == "sk-ant-parent"

    def test_format_stream_event_still_passes_existing_assertion(self):
        event = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "tool_use", "name": "Read"},
                    {"type": "text", "text": "last"},
                ]
            },
        })
        assert ClaudeCodeAgent._format_stream_event(event) == "first\nTool: Read\nlast"


class TestCodexAdapter:
    def test_is_installed_rejects_unrelated_codex_binary(self, monkeypatch):
        """PATH 上叫 codex 的可能是别的工具（--help 里没有 exec 子命令），需甄别为非 OpenAI Codex。"""
        monkeypatch.setattr(cli_adapters.shutil, "which", lambda cmd: "/usr/bin/codex")
        monkeypatch.setattr(
            cli_adapters.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(
                stdout="usage: codex [-b FILE] [-p PROTO]\nsome unrelated log tool",
                stderr="",
            ),
        )
        assert get_adapter("codex").is_installed() is False

    def test_is_installed_accepts_openai_codex(self, monkeypatch):
        monkeypatch.setattr(cli_adapters.shutil, "which", lambda cmd: "/usr/bin/codex")
        monkeypatch.setattr(
            cli_adapters.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(
                stdout="Usage: codex [OPTIONS]\n  exec  Run codex in headless mode",
                stderr="",
            ),
        )
        assert get_adapter("codex").is_installed() is True

    def test_base_command_exec_and_prompt_stdin(self):
        adapter = get_adapter("codex")
        cmd = adapter.build_base_command(
            model="gpt-5-codex", skip_permissions=True, enable_project_skills=False
        )
        assert cmd[:2] == ["codex", "exec"]
        assert "--model" in cmd and "gpt-5-codex" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--full-auto" not in cmd
        args, via_stdin = adapter.prompt_args("hello")
        assert args == []
        assert via_stdin is True

    def test_resume_uses_subcommand(self):
        adapter = get_adapter("codex")
        assert adapter.resume_flag("c1") == ["resume", "c1"]

    def test_parse_result_json(self):
        adapter = get_adapter("codex")
        raw = json.dumps({"session_id": "c1", "output": "done"})
        out = adapter.parse_result(raw)
        assert out["result"] == "done"
        assert out["session_id"] == "c1"

    def test_parse_result_ndjson_takes_last_event(self):
        adapter = get_adapter("codex")
        raw = "\n".join([
            json.dumps({"type": "turn", "session_id": "c1"}),
            json.dumps({"type": "item", "output": "final answer"}),
        ])
        out = adapter.parse_result(raw)
        assert out["result"] == "final answer"
        assert out["session_id"] == "c1"

    def test_parse_result_reads_agent_message_item(self):
        """codex exec --json 现行输出把正文包在 item 信封里（实测形态）。

        老解析器只看顶层 result/output/text/message，认不出这种形态，于是
        chat() 兜底返回整段原始 NDJSON —— 用户看到的就是一大坨 JSON 而不是回复。
        """
        adapter = get_adapter("codex")
        raw = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t1"}),
            json.dumps({"type": "item.completed", "item": {
                "id": "item_0", "type": "reasoning", "text": "想一下",
            }}),
            json.dumps({"type": "item.completed", "item": {
                "id": "item_1", "type": "agent_message", "text": "可用",
            }}),
            json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}),
        ])
        out = adapter.parse_result(raw)
        assert out["result"] == "可用"
        assert out["session_id"] == "t1"

    def test_parse_result_ignores_non_message_items(self):
        """过程事件（命令输出）不得被当成回复采集。"""
        adapter = get_adapter("codex")
        raw = "\n".join([
            json.dumps({"type": "item.completed", "item": {
                "id": "item_0", "type": "command_execution",
                "command": "ls", "aggregated_output": "a.txt",
            }}),
            json.dumps({"type": "item.completed", "item": {
                "id": "item_1", "type": "error", "message": "模型元数据缺失",
            }}),
        ])
        out = adapter.parse_result(raw)
        # 没有 agent_message 事件时保持原有兜底：整段原文（调用方据此报错排查）
        assert out["result"] == raw

    def test_format_stream_event_reads_agent_message_item(self):
        adapter = get_adapter("codex")
        line = json.dumps({"type": "item.completed", "item": {
            "id": "item_1", "type": "agent_message", "text": "可用",
        }})
        assert adapter.format_stream_event(line) == "可用"
        command = json.dumps({"type": "item.completed", "item": {
            "id": "item_0", "type": "command_execution", "aggregated_output": "a.txt",
        }})
        assert adapter.format_stream_event(command) == ""


class TestCodexMcp:
    """codex 的 MCP 注入是 `-c mcp_servers.<name>.*`，且**必须排在 `exec` 之前**。

    codex 的 CLI 是 ``codex [全局 flag] exec [子命令 flag]``：``exec`` 是子命令，
    它后面的东西归子命令解析。AICU 通用口子 ``mcp_flags`` 是在 build_base_command
    之后追加的，用在这里会把 ``-c`` 甩到 ``exec`` 后面 —— 位置错，codex 要么报
    unknown option 要么静默忽略。所以 codex 走 build_base_command 自己插。
    """

    def test_mcp_flags_injected_before_exec(self):
        cmd = get_adapter("codex").build_base_command(
            model=None,
            skip_permissions=False,
            enable_project_skills=False,
            mcp_servers=_mcp_servers("echo-probe"),
        )
        assert cmd[0] == "codex"
        assert cmd.index("exec") > 1
        # 每一条 -c 都在 exec 之前
        assert all(i < cmd.index("exec") for i, a in enumerate(cmd) if a == "-c")

    def test_mcp_encoded_as_repeated_c_flags(self):
        """形态是重复的 `-c key=value`，值用 JSON 引号 —— 实测这样才被正确解析。"""
        cmd = get_adapter("codex").build_base_command(
            model=None,
            skip_permissions=False,
            enable_project_skills=False,
            mcp_servers=_mcp_servers("echo-probe", env={"K": "v"}),
        )
        pairs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-c"]
        assert 'mcp_servers.echo-probe.command="python3"' in pairs
        # args 是 JSON 数组：path 带空格时也只有这一种写法不会散架
        assert 'mcp_servers.echo-probe.args=["/tmp/echo.py"]' in pairs
        assert 'mcp_servers.echo-probe.env.K="v"' in pairs

    def test_no_mcp_keeps_command_byte_identical(self):
        """没声明 MCP 时命令与改动前逐字相同 —— codex 节点是既有行为基线。"""
        cmd = get_adapter("codex").build_base_command(
            model=None, skip_permissions=True, enable_project_skills=False, mcp_servers=None
        )
        assert cmd == ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]

    def test_args_omitted_when_empty(self):
        """args 为空就不发这条 -c：发一个空数组与不发等价，少一条少一处出错面。"""
        server = McpServer(name="x", command="python3", args=[])
        cmd = get_adapter("codex").build_base_command(
            model=None, skip_permissions=False, enable_project_skills=False, mcp_servers=[server]
        )
        pairs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-c"]
        assert pairs == ['mcp_servers.x.command="python3"']

    def test_mcp_flags_hook_stays_empty(self):
        """codex 不用通用口子，mcp_flags 必须返回空 —— 否则同一份配置会被注入两次。"""
        assert get_adapter("codex").mcp_flags(_mcp_servers("echo-probe")) == []


class TestQwenAdapter:
    def test_base_command_model_and_yolo(self):
        adapter = get_adapter("qwen")
        cmd = adapter.build_base_command(
            model="qwen3-coder-plus", skip_permissions=True, enable_project_skills=False
        )
        assert cmd[0] == "qwen"
        assert "--model" in cmd and "qwen3-coder-plus" in cmd
        assert "--yolo" in cmd
        assert "--print" not in cmd

    def test_print_flags_output_format(self):
        adapter = get_adapter("qwen")
        assert adapter.print_flags(streaming=True) == ["--output-format", "stream-json"]
        assert adapter.print_flags(streaming=False) == ["--output-format", "json"]

    def test_parse_result_stream_json(self):
        """stream-json 的 JSONL 要取出正文，不能把整段 JSON 当答案返回。

        实测形状（qwen --output-format stream-json，glm-5.2 与 deepseek-v4-pro 同形）：
        首行 system，中间 assistant（正文在 message 里），末行 result。正文不在
        行顶层的 role/content 上，所以早期只认 role=="assistant" 的兜底收不到东西，
        会把整个 JSONL 原样返回 —— 用户在流式界面里看到的就是一坨 JSON。
        """
        raw = "\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}),
            json.dumps({"type": "assistant", "message": {"role": "assistant",
                                                         "content": [{"type": "text", "text": "可用"}]}}),
            json.dumps({"type": "result", "subtype": "success", "result": "可用",
                        "session_id": "sess-1"}),
        ])
        parsed = get_adapter("qwen").parse_result(raw)
        assert parsed["result"] == "可用"
        assert parsed["session_id"] == "sess-1"

    def test_parse_result_single_object_still_openai_style(self):
        """单对象 JSON 仍走 OpenAI 风格那条，别被事件流解析抢走。"""
        raw = json.dumps({"role": "assistant", "content": "可用"})
        assert get_adapter("qwen").parse_result(raw)["result"] == "可用"

    def test_format_stream_event_reads_assistant_envelope(self):
        """流式显示要认出 assistant 信封，否则界面上一路没有任何输出。

        实测 qwen 的 stream-json：正文在 {"type":"assistant","message":{...}} 里，
        行顶层没有 role；thinking 块也在同样的信封里，必须留空不显示。
        """
        adapter = get_adapter("qwen")
        thinking = json.dumps({
            "type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "thinking", "thinking": "用户要两个字"}]},
        })
        text = json.dumps({
            "type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "可用"}]},
        })
        assert adapter.format_stream_event(thinking) == ""
        assert adapter.format_stream_event(text) == "可用"
        # 非正文事件（system / goal_state / result）一律不产出
        assert adapter.format_stream_event(
            json.dumps({"type": "stream_event", "event": {"type": "goal_state"}})) == ""

    def test_max_turns_uses_max_session_turns(self):
        adapter = get_adapter("qwen")
        assert adapter.max_turns_flag(7) == ["--max-session-turns", "7"]

    def test_prompt_args_uses_dash_p(self):
        adapter = get_adapter("qwen")
        args, via_stdin = adapter.prompt_args("hello")
        assert args == ["-p", "hello"]
        assert via_stdin is False

    def test_build_env_uses_bailian_api_key(self, monkeypatch):
        monkeypatch.setenv("BAILIAN_API_KEY", "secret")
        adapter = get_adapter("qwen")
        env = adapter.build_env({}, api_base_url=None, api_key_env="BAILIAN_API_KEY")
        assert env["BAILIAN_API_KEY"] == "secret"

    def test_parse_result_json(self):
        adapter = get_adapter("qwen")
        raw = json.dumps({"result": "done", "session_id": "q1"})
        out = adapter.parse_result(raw)
        assert out["result"] == "done"
        assert out["session_id"] == "q1"


class TestOpenCodeAdapter:
    def test_base_command_run_subcommand_and_model(self):
        adapter = get_adapter("opencode")
        cmd = adapter.build_base_command(
            model="anthropic/claude-sonnet-4-6", skip_permissions=True, enable_project_skills=False
        )
        assert cmd[:2] == ["opencode", "run"]
        assert "--model" in cmd and "anthropic/claude-sonnet-4-6" in cmd
        assert "--auto" in cmd

    def test_base_command_no_auto_when_not_skip_permissions(self):
        adapter = get_adapter("opencode")
        cmd = adapter.build_base_command(
            model=None, skip_permissions=False, enable_project_skills=False
        )
        assert "--auto" not in cmd

    def test_print_flags_format_json(self):
        adapter = get_adapter("opencode")
        assert adapter.print_flags(streaming=True) == ["--format", "json"]
        assert adapter.print_flags(streaming=False) == ["--format", "json"]

    def test_resume_uses_session_flag(self):
        adapter = get_adapter("opencode")
        assert adapter.resume_flag("s1") == ["--session", "s1"]

    def test_max_turns_not_supported(self):
        adapter = get_adapter("opencode")
        assert adapter.max_turns_flag(7) == []

    def test_prompt_args_positional(self):
        adapter = get_adapter("opencode")
        args, via_stdin = adapter.prompt_args("hello")
        assert args == ["hello"]
        assert via_stdin is False

    def test_api_key_env_for_model_by_vendor(self):
        adapter = get_adapter("opencode")
        assert adapter.api_key_env_for_model("claude-sonnet-4-6") == "ANTHROPIC_API_KEY"
        assert adapter.api_key_env_for_model("gpt-5.6") == "OPENAI_API_KEY"
        assert adapter.api_key_env_for_model("kimi-k3") == "MOONSHOT_API_KEY"
        assert adapter.api_key_env_for_model("grok-4.6") == "XAI_API_KEY"
        assert adapter.api_key_env_for_model("gemini-3") == "GOOGLE_GENERATIVE_AI_API_KEY"
        assert adapter.api_key_env_for_model("deepseek-v4-pro") == "DEEPSEEK_API_KEY"
        assert adapter.api_key_env_for_model("glm-5.2") == "ZHIPU_API_KEY"
        assert adapter.api_key_env_for_model("MiniMax-M3") == "MINIMAX_API_KEY"
        assert adapter.api_key_env_for_model("qwen3.8-max") == "DASHSCOPE_API_KEY"
        # 内部模型 / 未知模型 → None
        assert adapter.api_key_env_for_model("auto-内部") is None
        assert adapter.api_key_env_for_model("some-random-model") is None

    def test_parse_result_jsonl_collects_text_and_session(self):
        adapter = get_adapter("opencode")
        raw = "\n".join([
            json.dumps({"type": "message.updated", "sessionID": "opn_123"}),
            json.dumps({"type": "message.part.updated", "parts": [{"type": "text", "text": "first"}]}),
            json.dumps({"type": "message.part.updated", "parts": [{"type": "text", "text": "last"}]}),
        ])
        out = adapter.parse_result(raw)
        assert out["result"] == "first\nlast"
        assert out["session_id"] == "opn_123"

    def test_parse_result_plain_text_fallback(self):
        adapter = get_adapter("opencode")
        out = adapter.parse_result("plain answer")
        assert out["result"] == "plain answer"
        assert out["session_id"] is None


class TestOpenCodeMcp:
    """opencode 的 MCP 走环境变量 OPENCODE_CONFIG_CONTENT，与 provider 配置**同一个 JSON**。

    两个坑：
      1. 那个变量原来只在 ``api_base_url`` 非空时才设（角色没配自定义端点就没有它），
         MCP 会没地方放 —— 条件必须放宽成「有端点或有 server」。
      2. 同一个变量设两次是覆盖不是合并，所以 mcp 段必须与 provider 段拼进同一份 JSON。
    """

    def _config(self, env: dict) -> dict:
        return json.loads(env["OPENCODE_CONFIG_CONTENT"])

    def test_mcp_present_without_custom_endpoint(self):
        """没配自定义端点时，这个变量本来根本不存在 —— 这条守住条件放宽。"""
        env = get_adapter("opencode").build_env(
            {}, api_base_url=None, api_key_env=None, mcp_servers=_mcp_servers("echo-probe")
        )
        assert "OPENCODE_CONFIG_CONTENT" in env
        assert "echo-probe" in self._config(env)["mcp"]

    def test_command_is_an_array_not_string_plus_args(self):
        """opencode 要 ``command: [程序, ...参数]``，与 CC 族的 command + args 不是一回事。

        照抄 CC 族那份 serializer 的话，这里会写成一个字符串 + 一个多余的 args 键，
        opencode 直接起不来 server。
        """
        env = get_adapter("opencode").build_env(
            {},
            api_base_url=None,
            api_key_env=None,
            mcp_servers=_mcp_servers("echo-probe"),
        )
        entry = self._config(env)["mcp"]["echo-probe"]
        assert entry == {
            "type": "local",
            "command": ["python3", "/tmp/echo.py"],
            "enabled": True,
        }

    def test_environment_key_only_when_present(self):
        adapter = get_adapter("opencode")
        plain = adapter.build_env(
            {}, api_base_url=None, api_key_env=None, mcp_servers=_mcp_servers("a")
        )
        assert "environment" not in self._config(plain)["mcp"]["a"]
        with_env = adapter.build_env(
            {},
            api_base_url=None,
            api_key_env=None,
            mcp_servers=_mcp_servers("a", env={"K": "v"}),
        )
        assert self._config(with_env)["mcp"]["a"]["environment"] == {"K": "v"}

    def test_mcp_and_provider_share_one_json(self):
        """端点与 MCP 必须落在同一份 JSON 里 —— 设两次变量是覆盖，先设的那份会丢。"""
        env = get_adapter("opencode").build_env(
            {},
            api_base_url="https://x.example/v1",
            api_key_env="DEEPSEEK_API_KEY",
            mcp_servers=_mcp_servers("echo-probe"),
        )
        config = self._config(env)
        assert "echo-probe" in config["mcp"]
        assert config["provider"]  # provider 段没被 mcp 段挤掉

    def test_no_mcp_no_endpoint_leaves_env_untouched(self):
        """两者都没有时不该凭空造这个变量 —— 设了它会覆盖用户自己的 opencode 配置。"""
        env = get_adapter("opencode").build_env(
            {}, api_base_url=None, api_key_env=None, mcp_servers=None
        )
        assert env == {}


class TestQwenMcp:
    """qwen 的 MCP 注入点与前几家都不同：只能写 cwd 下的**项目级配置文件**。

    没有 argv 口子、没有 env 口子（``qwen --help`` 已核实），所以配置必须落盘，
    再由调用方在进程结束后删掉 —— 落盘与删除这两件事分别由 adapter 和
    claude_code_agent 负责，本类只测 adapter 那一半（另一半见 TestTransientMcpFiles）。
    """

    def _write(self, adapter, workspace, servers):
        """跑一次注入，返回 (返回的路径列表, 解析后的 JSON)。"""
        paths = adapter.mcp_workspace_files(workspace, servers)
        if not paths:
            return paths, None
        return paths, json.loads(paths[0].read_text(encoding="utf-8"))

    def test_settings_written_into_the_workspace_it_was_given(self, tmp_path):
        """必须落在**本次 CC 的 cwd**里 —— qwen 的项目级配置是相对 cwd 解析的。"""
        ws = tmp_path / "cc_0"
        paths, data = self._write(get_adapter("qwen"), ws, _mcp_servers("echo-probe"))
        assert paths == [ws / ".qwen" / "settings.json"]
        assert "echo-probe" in data["mcpServers"]

    def test_shape_matches_what_qwen_mcp_add_writes(self, tmp_path):
        """形状与本机 ``qwen mcp add -s project`` 写出来的文件逐字对齐（已实测比对）。

        自己编一套形状的风险是「看着对、qwen 不认」，而这种失败在无头模式下不会报错，
        只会表现为工具不存在。
        """
        _, data = self._write(get_adapter("qwen"), tmp_path, _mcp_servers("echo-probe"))
        assert data == {
            "mcpServers": {
                "echo-probe": {
                    "command": "python3",
                    "args": ["/tmp/echo.py"],
                    "trust": True,
                }
            }
        }

    def test_trust_is_set_because_headless_has_nobody_to_approve(self, tmp_path):
        """``trust`` 不是装饰：项目级 server 默认是 "Pending approval"（实测）。

        AICU 跑无头模式，没有人在旁边点批准，少了这个键等于下发一个永远批不下来的
        工具 —— 而且是静默的。这条单独拉出来测，是因为它最容易被当成可选字段删掉。
        """
        _, data = self._write(get_adapter("qwen"), tmp_path, _mcp_servers("a", "b"))
        assert all(entry["trust"] is True for entry in data["mcpServers"].values())

    def test_args_stay_a_separate_array(self, tmp_path):
        """qwen 要 ``command`` 字符串 + ``args`` 数组，与 opencode 的数组合一形式相反。

        两边共用一个序列化器就会有一边错，所以这条守着别把 qwen 也「顺手统一」掉。
        """
        _, data = self._write(get_adapter("qwen"), tmp_path, _mcp_servers("a"))
        entry = data["mcpServers"]["a"]
        assert entry["command"] == "python3"
        assert entry["args"] == ["/tmp/echo.py"]

    def test_env_passed_through_when_present(self, tmp_path):
        _, data = self._write(
            get_adapter("qwen"), tmp_path, _mcp_servers("a", env={"K": "v"})
        )
        assert data["mcpServers"]["a"]["env"] == {"K": "v"}

    def test_no_declaration_writes_nothing_at_all(self, tmp_path):
        """没配 mcp 时连 ``.qwen/`` 目录都不该建 —— 免得在产物目录里留空壳。"""
        assert self._write(get_adapter("qwen"), tmp_path, None)[0] == []
        assert self._write(get_adapter("qwen"), tmp_path, [])[0] == []
        assert not (tmp_path / ".qwen").exists()

    def test_targets_project_scope_not_the_users_global_config(self, tmp_path, monkeypatch):
        """绝不能写用户级的 ``~/.qwen/settings.json``。

        那是全局文件：并发节点会互相覆盖，还会污染用户自己的环境；而且它正是
        ``_provision_qwen_config`` 放 ``modelProviders.aicu`` 的地方，混着写容易把
        模型路由搞坏。这里把 HOME 指到 tmp 来证明实现没碰它。
        """
        fake_home = tmp_path / "home"
        (fake_home / ".qwen").mkdir(parents=True)
        global_file = fake_home / ".qwen" / "settings.json"
        global_file.write_text('{"modelProviders": {"aicu": []}}', encoding="utf-8")
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: fake_home)

        self._write(get_adapter("qwen"), tmp_path / "cc_0", _mcp_servers("echo-probe"))

        assert global_file.read_text(encoding="utf-8") == '{"modelProviders": {"aicu": []}}'


class TestGeminiMcpAllowlist:
    """gemini 的 MCP 注入要**两条路一起走**：项目 settings.json + argv 允许名单。

    少了文件就没有 server 可加载；少了名单则可能被用户级的 ``mcp.allowed`` 挡在门外
    （那个值是跨作用域取交集，项目级写什么都加不回来）—— 所以两条各测各的。
    """

    def test_settings_written_into_the_workspace_it_was_given(self, tmp_path):
        ws = tmp_path / "cc_0"
        paths = get_adapter("gemini").mcp_workspace_files(ws, _mcp_servers("echo-probe"))
        assert paths == [ws / ".gemini" / "settings.json"]

    def test_file_shape_is_the_plain_mcpservers_wrapper(self, tmp_path):
        paths = get_adapter("gemini").mcp_workspace_files(tmp_path, _mcp_servers("echo-probe"))
        assert json.loads(paths[0].read_text(encoding="utf-8")) == {
            "mcpServers": {
                "echo-probe": {"command": "python3", "args": ["/tmp/echo.py"]}
            }
        }

    def test_the_allowlist_is_not_written_into_the_file(self, tmp_path):
        """``mcp.allowed`` 不落盘 —— 它不是「配了就该生效」的那条路。

        getConsolidatedAllowedMcpServers 对每个作用域取值再取**交集**：用户级只要有一份
        不含我们的名单，项目级怎么补都只会更小。写进去的唯一效果是让人误以为项目文件
        能自证其说，从而在排查时看错地方。真正生效的是 --allowed-mcp-server-names。
        """
        paths = get_adapter("gemini").mcp_workspace_files(tmp_path, _mcp_servers("a"))
        assert "allowed" not in paths[0].read_text(encoding="utf-8")

    def test_each_server_gets_its_own_flag(self):
        """逐个给 flag，不拼逗号 —— server 名里可能有逗号，拼串会被切错。"""
        flags = get_adapter("gemini").mcp_flags(_mcp_servers("a", "b"))
        assert flags == [
            "--allowed-mcp-server-names", "a",
            "--allowed-mcp-server-names", "b",
        ]

    def test_no_mcp_no_flags(self):
        assert get_adapter("gemini").mcp_flags([]) == []

    def test_base_command_is_untouched_by_mcp(self):
        """gemini 的注入全在 mcp_flags 上，``build_base_command`` 逐字节不变。

        （这家的 mcp 不需要提前排进 argv，与 codex 的 ``-c`` 相反。）
        """
        adapter = get_adapter("gemini")
        kwargs = dict(model="gemini-2.5-pro", skip_permissions=True, enable_project_skills=True)
        assert adapter.build_base_command(**kwargs, mcp_servers=None) == [
            "gemini", "--model", "gemini-2.5-pro", "--yolo", "--skip-trust"
        ]
        assert adapter.build_base_command(**kwargs, mcp_servers=_mcp_servers("a")) == [
            "gemini", "--model", "gemini-2.5-pro", "--yolo", "--skip-trust"
        ]


class TestCursorApproveMcps:
    """cursor 的项目级 MCP 需要显式批准，而 AICU 跑无头、没人点。"""

    def test_mcp_json_written_into_the_workspace_it_was_given(self, tmp_path):
        ws = tmp_path / "cc_0"
        paths = get_adapter("cursor").mcp_workspace_files(ws, _mcp_servers("echo-probe"))
        assert paths == [ws / ".cursor" / "mcp.json"]

    def test_file_shape_is_the_plain_mcpservers_wrapper(self, tmp_path):
        paths = get_adapter("cursor").mcp_workspace_files(tmp_path, _mcp_servers("echo-probe"))
        assert json.loads(paths[0].read_text(encoding="utf-8")) == {
            "mcpServers": {
                "echo-probe": {"command": "python3", "args": ["/tmp/echo.py"]}
            }
        }

    def test_approve_mcps_appears_only_when_mcp_is_present(self):
        adapter = get_adapter("cursor")
        kwargs = dict(model="gpt-5", skip_permissions=False, enable_project_skills=False)
        # 无 mcp 时与改动前逐字节相同 —— 老版本 agent 不认这个 flag，普通运行不该受影响
        assert adapter.build_base_command(**kwargs, mcp_servers=None) == ["agent", "--model", "gpt-5"]
        assert adapter.build_base_command(**kwargs, mcp_servers=[]) == ["agent", "--model", "gpt-5"]
        assert adapter.build_base_command(**kwargs, mcp_servers=_mcp_servers("a")) == [
            "agent", "--model", "gpt-5", "--approve-mcps"
        ]

    def test_skip_permissions_still_adds_nothing(self):
        """``--approve-mcps`` 不是免审批 flag 的马甲，别把它接到 skip_permissions 上。

        cursor 官方页没有 skip-permissions 类 flag，加了会让命令直接报错；而
        --approve-mcps 只管 MCP 服务器批准这一件事（`agent --help`：「Automatically
        approve all MCP servers」）。两者混接就会在有 mcp 时凭空多出一个免审批语义。
        """
        adapter = get_adapter("cursor")
        cmd = adapter.build_base_command(
            model=None, skip_permissions=True, enable_project_skills=False
        )
        assert cmd == ["agent"]


# 走「项目级配置文件」这条路的 CLI，按 (id, 相对 workspace 的路径) 参数化。
#
# 这几家的落盘内容**逐字节同形**（都是 {"mcpServers": {name: {command, args, env?}}}），
# 差别只有路径 —— 所以形状断言共用一份：哪天某一家悄悄分叉，共享的用例会先红，
# 而不是各自绿着、实际已经写坏。
# 不在这里的：grok 是 TOML、antigravity 不接 MCP（各自另有说明）。
_PROJECT_FILE_CLIS = [
    ("gemini", (".gemini", "settings.json")),
    ("cursor", (".cursor", "mcp.json")),
    ("pi", (".pi", "mcp.json")),
]


class TestProjectFileMcp:
    """共同的路径/形状/不落盘三条性质，逐家参数化验一遍。"""

    @pytest.fixture(autouse=True)
    def _pi_gate_open(self, monkeypatch):
        """pi 的 supports_mcp 要看本机装没装扩展，这里统一当作装了。

        本类问的是「路径拼对了没、内容形状对不对」，与本机装了什么无关；不拨这一下，
        用例的结论就会跟着开发机走（装了扩展绿、没装红，而产线代码两种情况下都对）。
        门禁本身另有用例守。
        """
        monkeypatch.setattr(cli_adapters, "_mcp_capability", lambda *a, **k: True)

    @pytest.mark.parametrize("cli_id,parts", _PROJECT_FILE_CLIS)
    def test_written_to_the_project_path_that_cli_documents(self, tmp_path, cli_id, parts):
        """必须落在**本次 CC 的 cwd**（这些路径都是相对 cwd 解析的），且逐段对上。"""
        ws = tmp_path / "cc_0"
        paths = get_adapter(cli_id).mcp_workspace_files(ws, _mcp_servers("echo-probe"))
        assert paths == [ws.joinpath(*parts)]

    @pytest.mark.parametrize("cli_id,parts", _PROJECT_FILE_CLIS)
    def test_shape_is_the_plain_mcpservers_wrapper(self, tmp_path, cli_id, parts):
        paths = get_adapter(cli_id).mcp_workspace_files(tmp_path, _mcp_servers("echo-probe"))
        assert json.loads(paths[0].read_text(encoding="utf-8")) == {
            "mcpServers": {
                "echo-probe": {"command": "python3", "args": ["/tmp/echo.py"]}
            }
        }

    @pytest.mark.parametrize("cli_id,parts", _PROJECT_FILE_CLIS)
    def test_env_key_only_appears_when_there_is_one(self, tmp_path, cli_id, parts):
        """没 env 就别写 ``env`` 键 —— 写个空对象是另一种意思。"""
        paths = get_adapter(cli_id).mcp_workspace_files(tmp_path, _mcp_servers("a"))
        entry = json.loads(paths[0].read_text(encoding="utf-8"))["mcpServers"]["a"]
        assert "env" not in entry

    @pytest.mark.parametrize("cli_id,parts", _PROJECT_FILE_CLIS)
    def test_env_round_trips_when_present(self, tmp_path, cli_id, parts):
        paths = get_adapter(cli_id).mcp_workspace_files(
            tmp_path, _mcp_servers("a", env={"K": "v"})
        )
        entry = json.loads(paths[0].read_text(encoding="utf-8"))["mcpServers"]["a"]
        assert entry["env"] == {"K": "v"}

    @pytest.mark.parametrize("cli_id,parts", _PROJECT_FILE_CLIS)
    def test_no_declaration_writes_nothing_at_all(self, tmp_path, cli_id, parts):
        """没配 mcp 时连目录都不该建 —— 免得在产物目录里留下空壳。"""
        adapter = get_adapter(cli_id)
        assert adapter.mcp_workspace_files(tmp_path, None) == []
        assert adapter.mcp_workspace_files(tmp_path, []) == []
        assert not tmp_path.joinpath(*parts[:-1]).exists()


class TestRemoteMcpSerialization:
    """远程（Streamable HTTP）条目在七家序列化器里的形状。

    **各家对「远程地址」那个键的叫法并不统一**，这是接远程 MCP 最容易写错的一处：
    ``url``（claude/codebuddy/codex/kimi/pi/cursor/opencode）与 ``httpUrl``
    （gemini/qwen）。写错不报错、不警告，只是静默连到另一条传输上（这两家的 ``url``
    表示 SSE）或让条目被整条跳过。所以下面把每一家的形状逐字钉住，而不是只测
    「url 出现在某处」—— 那种断言对上面两种错法都是绿的。

    gemini 与 grok 不在下拉里（``_HIDDEN_CLI_IDS``），但仍然测：隐藏只影响界面，
    不妨碍有人把它们翻出来用；而且 gemini 正是 ``httpUrl`` 那个坑本身。
    """

    @pytest.fixture(autouse=True)
    def _pi_gate_open(self, monkeypatch):
        """pi 的 supports_mcp 要看本机装没装扩展，这里统一当作装了（同 TestProjectFileMcp）。"""
        monkeypatch.setattr(cli_adapters, "_mcp_capability", lambda *a, **k: True)

    @pytest.mark.parametrize("cli_id", ["claude", "codebuddy"])
    def test_cc_family_needs_the_type_discriminator(self, cli_id):
        """``type: "http"`` 是必填：claude 把「有 url 却没有 type」当配置错误，跳过整个 server。

        走 ``mcp_flags`` 这个**真入口**而不是直接调 ``_cc_mcp_json`` —— 后者测不到
        「flag 拼没拼对、有没有被 ``--strict-mcp-config`` 一起带上」。
        """
        flags = get_adapter(cli_id).mcp_flags(_remote_servers("remote"))
        assert flags[1] == "--strict-mcp-config"
        config = json.loads(flags[0].removeprefix("--mcp-config="))
        assert config == {
            "mcpServers": {
                "remote": {
                    "type": "http",
                    "url": REMOTE_URL,
                    "headers": {"Authorization": "Bearer t"},
                }
            }
        }

    def test_codex_uses_url_and_http_headers(self):
        pairs = self._codex_pairs(_remote_servers("remote", headers={"X-Api-Key": "k"}))
        assert f'mcp_servers.remote.url="{REMOTE_URL}"' in pairs
        assert 'mcp_servers.remote.http_headers={"X-Api-Key": "k"}' in pairs

    def test_codex_never_sends_a_command_for_a_remote_entry(self):
        """``.command`` 与 ``.url`` 是两条并列的连接方式，同时给等于让 codex 自己挑一条。

        那一挑就不再由我们决定：它可能真去起一个 ``""`` 的子进程，也可能报一句与
        MCP 无关的错。远程条目一个 command 相关的键都不发。
        """
        pairs = self._codex_pairs(_remote_servers("remote"))
        assert not [p for p in pairs if ".command=" in p]
        assert not [p for p in pairs if ".args=" in p]
        assert not [p for p in pairs if ".env." in p]

    def test_codex_headers_key_only_when_there_are_headers(self):
        """``http_headers`` 空就不发；注意**不是** ``env_http_headers`` —— 后者的值是
        环境变量名，用错等于把 token 换成它的名字发出去。"""
        pairs = self._codex_pairs(_remote_servers("remote", headers={}))
        assert pairs == [f'mcp_servers.remote.url="{REMOTE_URL}"']
        assert not [p for p in pairs if "headers" in p]

    @pytest.mark.parametrize("cli_id,parts", [
        ("kimi", (".kimi-code", "mcp.json")),
        ("pi", (".pi", "mcp.json")),
        ("cursor", (".cursor", "mcp.json")),
    ])
    def test_project_file_clis_write_url_without_a_type(self, tmp_path, cli_id, parts):
        """这三家按「给了哪个连接字段」推传输，**没有 type 字段**可写。"""
        entry = self._project_entries(get_adapter(cli_id), tmp_path, parts)["remote"]
        assert entry == {"url": REMOTE_URL, "headers": {"Authorization": "Bearer t"}}

    def test_gemini_uses_http_url_not_url(self, tmp_path):
        """**这是本轮最容易写错的一处。** gemini 没有 type 字段、传输由键名决定：
        ``url`` 是 SSE（被 Streamable HTTP 取代的旧传输），``httpUrl`` 才是我们要的。

        写成 ``url`` 不报错，只是连到一条我们并不支持的传输上 —— 单测里唯一的防线
        就是这条断言。
        """
        entry = self._project_entries(
            get_adapter("gemini"), tmp_path, (".gemini", "settings.json")
        )["remote"]
        assert "url" not in entry
        assert entry == {"httpUrl": REMOTE_URL, "headers": {"Authorization": "Bearer t"}}

    def test_qwen_uses_http_url_not_url(self, tmp_path):
        """qwen 与 gemini 同一个坑（没有 type、键名即传输），且还要带 ``trust``。"""
        data = self._qwen_settings(tmp_path, _remote_servers("remote"))
        entry = data["mcpServers"]["remote"]
        assert "url" not in entry
        assert entry == {
            "httpUrl": REMOTE_URL,
            "headers": {"Authorization": "Bearer t"},
            "trust": True,
        }

    def test_qwen_keeps_trust_on_remote_entries_too(self, tmp_path):
        """``trust`` 是批准门禁、与传输无关：远程条目漏了它，工具同样永远批不下来。"""
        data = self._qwen_settings(tmp_path, _remote_servers("a"))
        assert data["mcpServers"]["a"]["trust"] is True

    def test_opencode_uses_the_remote_variant_with_type(self):
        """opencode 的 ``local`` / ``remote`` 是两个形状不同的联合类型，键不能混。

        顶层键也别忘了是 ``mcp`` 而不是别人用的 ``mcpServers``。
        """
        env = get_adapter("opencode").build_env(
            {}, api_base_url=None, api_key_env=None, mcp_servers=_remote_servers("remote")
        )
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert config["mcp"]["remote"] == {
            "type": "remote",
            "url": REMOTE_URL,
            "headers": {"Authorization": "Bearer t"},
            "enabled": True,
        }

    def test_opencode_remote_entry_has_no_command_key(self):
        """``command`` 是 ``local`` 那一支的键，混进来 opencode 会拒掉整份配置。"""
        env = get_adapter("opencode").build_env(
            {}, api_base_url=None, api_key_env=None, mcp_servers=_remote_servers("remote")
        )
        entry = json.loads(env["OPENCODE_CONFIG_CONTENT"])["mcp"]["remote"]
        assert "command" not in entry and "environment" not in entry

    def test_headers_key_is_omitted_when_empty_everywhere(self, tmp_path):
        """空 headers 不写空对象：写个 ``{}`` 是另一种意思，而且会进每一份配置文件。"""
        servers = _remote_servers("remote", headers={})
        assert get_adapter("claude").mcp_flags(servers)[0].count('"headers"') == 0
        assert "headers" not in self._project_entries(
            get_adapter("cursor"), tmp_path, (".cursor", "mcp.json"), servers=servers
        )["remote"]
        assert "headers" not in self._qwen_settings(tmp_path, servers)["mcpServers"]["remote"]

    def test_one_list_can_hold_both_kinds(self, tmp_path):
        """同一次下发里两种混着来是常态（角色既挂本地探针又挂远程服务），
        所以分支必须写在**每条条目**上，而不是「这批全是本地就按本地来」。"""
        entries = self._project_entries(
            get_adapter("cursor"),
            tmp_path,
            (".cursor", "mcp.json"),
            servers=_mcp_servers("local") + _remote_servers("remote"),
        )
        assert entries["local"] == {"command": "python3", "args": ["/tmp/echo.py"]}
        assert entries["remote"] == {"url": REMOTE_URL, "headers": {"Authorization": "Bearer t"}}

    # ---- 取配置的三个小工具（各家注入点不同，取法也就不同） ----

    @staticmethod
    def _codex_pairs(servers) -> list:
        cmd = get_adapter("codex").build_base_command(
            model=None, skip_permissions=False, enable_project_skills=False, mcp_servers=servers
        )
        return [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-c"]

    @staticmethod
    def _project_entries(adapter, tmp_path, parts, servers=None) -> dict:
        """落盘那份配置里的 ``mcpServers`` 段（这几家都是这个顶层键）。

        按相对路径挑而不是断言「列表恰好一项」：kimi 除了配置还会写一条 workspace
        信任记录（那是它自己另一件事，见 KimiAdapter），列表本来就不止一项。
        """
        paths = adapter.mcp_workspace_files(tmp_path, servers or _remote_servers("remote"))
        matched = [p for p in paths if p.parts[-len(parts):] == parts]
        assert len(matched) == 1, f"没找到 {parts}，实际写了 {paths}"
        return json.loads(matched[0].read_text(encoding="utf-8"))["mcpServers"]

    @staticmethod
    def _qwen_settings(tmp_path, servers) -> dict:
        paths = get_adapter("qwen").mcp_workspace_files(tmp_path, servers)
        return json.loads(paths[0].read_text(encoding="utf-8"))


class TestGrokToml:
    """grok 是这批里唯一读 TOML 的，形状与转义都得逐字节对。

    注意本类问的是**序列化本身对不对**，与「现在接没接线」是两件事：grok 的
    ``supports_mcp`` 当前是 False（模型侧没验过，见 GrokAdapter），而基类的落盘函数
    会先看这个开关，所以这里统一把它拨开。不拨的话下面每一条都会拿到空列表，
    红的是「开关关了」而不是「TOML 写错了」，等于白测。
    """

    @pytest.fixture(autouse=True)
    def _gate_open(self, monkeypatch):
        monkeypatch.setattr(cli_adapters.GrokAdapter, "supports_mcp", True)

    def _text(self, tmp_path, servers) -> str:
        paths = get_adapter("grok").mcp_workspace_files(tmp_path, servers)
        assert paths == [tmp_path / ".grok" / "config.toml"]
        return paths[0].read_text(encoding="utf-8")

    def test_matches_the_shape_in_groks_bundled_manual(self, tmp_path):
        """``~/.grok/docs/user-guide/07-mcp-servers.md`` 里就是这个形状。

        自己编一套的风险是「看着对、grok 不认」，而这种失败在无头模式下不报错，
        只表现为工具不存在。
        """
        assert self._text(tmp_path, _mcp_servers("echo-probe")) == (
            "[mcp_servers.echo-probe]\n"
            'command = "python3"\n'
            'args = ["/tmp/echo.py"]\n'
        )

    def test_env_is_an_inline_table(self, tmp_path):
        """行内表而不是子表：写成 ``[mcp_servers.x.env]`` 之后父表就不能再补键了。"""
        text = self._text(tmp_path, _mcp_servers("a", env={"K": "v", "K2": "w"}))
        assert 'env = { K = "v", K2 = "w" }\n' in text

    def test_no_env_no_line(self, tmp_path):
        assert "env" not in self._text(tmp_path, _mcp_servers("a"))

    def test_remote_entry_raises_instead_of_writing_an_empty_command(self, tmp_path):
        """远程条目在这里**当场炸**，而不是写出一份 ``command = ""`` 的配置。

        今天不可达（grok 的 supports_mcp 是 False，基类门禁先挡住了），所以本类把
        开关拨开才走得到——拨开之后正是将来真去接线时的处境。若不拦，下面几行会照常
        执行：grok 读得进去、不报错、server 永远连不上，用户手里唯一的线索是空命令。
        """
        with pytest.raises(McpConfigError) as excinfo:
            self._text(tmp_path, _remote_servers("remote"))
        assert "remote" in str(excinfo.value)

    def test_remote_and_local_mixed_still_raises(self, tmp_path):
        """混着来时也必须炸——写一半的配置比整份不写更糟：看起来接上了。"""
        with pytest.raises(McpConfigError):
            self._text(tmp_path, _mcp_servers("local") + _remote_servers("remote"))
        assert not (tmp_path / ".grok" / "config.toml").exists()

    def test_empty_args_stay_an_empty_array(self, tmp_path):
        """``args = []`` 而不是省略这一行：grok 的 command 是必填、args 可选，
        但省略和「空数组」在排查时看不出区别，写全了省得下次对着配置猜。"""
        servers = [McpServer(name="a", command="python3", args=[])]
        assert 'args = []\n' in self._text(tmp_path, servers)

    def test_servers_are_separated_by_a_blank_line(self, tmp_path):
        """两个块之间必须有空行，否则第二段的 ``[mcp_servers.b]`` 表头会紧贴上一段的
        最后一个键，人眼读不了 —— TOML 本身不在乎，但这份文件是要给人看的。"""
        text = self._text(tmp_path, _mcp_servers("a", "b"))
        assert 'args = ["/tmp/echo.py"]\n\n[mcp_servers.b]\n' in text

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("plain", "plain"),
            ('qu"ote', 'qu\\"ote'),
            ("back\\slash", "back\\\\slash"),
            ("new\nline", "new\\nline"),
            ("cr\rhere", "cr\\rhere"),
            ("tab\there", "tab\\there"),
            ("nul\x00byte", "nul\\u0000byte"),
            # DEL 是这条用例的重点：JSON 默认**不**转义它，而 TOML 里它是非法字面字符。
            # 照抄 json.dumps 的转义表就会在这里写出一份 grok 解析不了的配置。
            ("del\x7fchar", "del\\u007Fchar"),
            # 非 ASCII 必须原样：\uXXXX 对 astral 字符要写成代理对，而代理对是非法 TOML。
            ("中文", "中文"),
            ("emoji 😀", "emoji 😀"),
        ],
    )
    def test_string_escaping(self, raw, expected):
        assert cli_adapters._toml_string(raw) == '"' + expected + '"'

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("plain", "plain"),
            ("with-dash_1", "with-dash_1"),
            # 带点的名字不加引号会被拆成嵌套表 mcp_servers → my → server，
            # grok 于是找不到这个 server，且不会有任何报错。
            ("my.server", '"my.server"'),
            ("with space", '"with space"'),
            ("中文名", '"中文名"'),
            # 尾随换行：``^...$`` 的 ``$`` 也匹配「结尾换行之前」，所以旧写法会把它
            # 判成裸键、把换行原样吐进表名 —— 生成的 TOML 整份解析失败，同文件里
            # 别的 server 一起丢。这类名字在 ``mcp_servers.json`` 里写作 "abc\n"。
            ("abc\n", '"abc\\n"'),
            ("\n", '"\\n"'),
        ],
    )
    def test_table_keys_are_quoted_when_they_have_to_be(self, name, expected):
        assert cli_adapters._toml_key(name) == expected

    def test_dotted_server_name_does_not_become_a_nested_table(self, tmp_path):
        assert '[mcp_servers."my.server"]\n' in self._text(tmp_path, _mcp_servers("my.server"))

    def test_round_trips_through_a_real_toml_parser(self, tmp_path):
        """用真 TOML 解析器验结构（只用于校验，不给运行时加依赖）。"""
        tomllib = pytest.importorskip("tomllib")
        servers = [
            McpServer(name="plain", command="python3", args=["/tmp/echo.py"]),
            McpServer(name="my.server", command="npx", args=["-y", "a b"], env={"K": "v"}),
            # 名字里带尾随换行：裸键会把这行拆掉，整份文档解析失败 —— 同文件里上面
            # 两个 server 也一起丢。这条比 ``_toml_key`` 的单测更贴近真实后果。
            McpServer(name="tail\n", command="python3", args=[], env={"K\n": "v"}),
        ]
        assert tomllib.loads(self._text(tmp_path, servers)) == {
            "mcp_servers": {
                "plain": {"command": "python3", "args": ["/tmp/echo.py"]},
                "my.server": {"command": "npx", "args": ["-y", "a b"], "env": {"K": "v"}},
                "tail\n": {"command": "python3", "args": [], "env": {"K\n": "v"}},
            }
        }

    def test_control_characters_survive_a_real_parser(self, tmp_path):
        """最容易写坏的一类：DEL 没转义、代理对写法，解析器都会直接拒绝。"""
        tomllib = pytest.importorskip("tomllib")
        servers = [McpServer(name="x", command="c\x7fd\x00e", args=["\n", "😀"])]
        data = tomllib.loads(self._text(tmp_path, servers))
        assert data["mcp_servers"]["x"]["command"] == "c\x7fd\x00e"
        assert data["mcp_servers"]["x"]["args"] == ["\n", "😀"]


class TestGrokAndAntigravityAdapters:
    """这两家从 GenericExecAdapter 的**实例**升格成了子类，flag 行为必须一字不差。"""

    def test_still_generic_exec_adapters(self):
        assert isinstance(get_adapter("antigravity"), GenericExecAdapter)
        assert isinstance(get_adapter("grok"), GenericExecAdapter)

    @pytest.mark.parametrize(
        "cli_id,command,key_env",
        [("antigravity", "agy", "GEMINI_API_KEY"), ("grok", "grok", "XAI_API_KEY")],
    )
    def test_identity_unchanged(self, cli_id, command, key_env):
        adapter = get_adapter(cli_id)
        assert adapter.command == command
        assert adapter.api_key_env == key_env

    @pytest.mark.parametrize("cli_id", ["antigravity"])
    def test_base_command_identical_to_before_the_upgrade(self, cli_id):
        """升格前 GenericExecAdapter 只产出 ``[command, --model, <m>]``（免审批不加）。

        grok 不在这条里：它后来按实测补了 ``--always-approve``（见
        ``TestGrokHeadlessContract``），flag 形状是有意改过的。
        """
        adapter = get_adapter(cli_id)
        assert adapter.build_base_command(
            model="m", skip_permissions=True, enable_project_skills=True
        ) == [adapter.command, "--model", "m"]

    def test_grok_trusts_the_folder_only_when_mcp_is_present(self, monkeypatch):
        """没有 mcp 就不加 --trust：它会把当前目录**持久化**进 grok 的信任库，
        为一次普通运行留下这条记录没有理由。"""
        monkeypatch.setattr(cli_adapters.GrokAdapter, "supports_mcp", True)
        adapter = get_adapter("grok")
        kwargs = dict(model=None, skip_permissions=False, enable_project_skills=False)
        assert adapter.build_base_command(**kwargs, mcp_servers=None) == ["grok"]
        assert adapter.build_base_command(**kwargs, mcp_servers=[]) == ["grok"]
        assert adapter.build_base_command(**kwargs, mcp_servers=_mcp_servers("a")) == [
            "grok", "--trust"
        ]

    def test_grok_never_trusts_a_folder_it_is_not_wiring(self, monkeypatch):
        """接线关掉时一个 --trust 都不许加。

        --trust 会把**当前目录持久化**写进 grok 的信任库，而这时基类根本不会落盘
        配置文件 —— 白留一条记录，什么都换不来。产线上这条靠 claude_code_agent 的
        守卫挡在前面（角色声明 mcp 落到未接线的 adapter 上会直接报错），但那个守卫
        在别处，这里守的是 adapter 自己也得自洽。
        """
        monkeypatch.setattr(cli_adapters.GrokAdapter, "supports_mcp", False)
        adapter = get_adapter("grok")
        assert adapter.build_base_command(
            model=None, skip_permissions=False, enable_project_skills=False,
            mcp_servers=_mcp_servers("a"),
        ) == ["grok"]

    def test_grok_writes_toml_not_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli_adapters.GrokAdapter, "supports_mcp", True)
        paths = get_adapter("grok").mcp_workspace_files(tmp_path, _mcp_servers("a"))
        assert paths[0].read_text(encoding="utf-8").startswith("[mcp_servers.a]")

    def test_grok_gate_is_closed_by_default(self, tmp_path):
        """常量 False，且关掉之后四个注入口子一个都不产出东西。

        与 AntigravityAdapter 那条同类：作用是「别好心加回来」—— 哪天有人看到
        ``grok mcp doctor`` 通了就把开关拨回 True，先红一次，回去读 docstring 里
        「还差模型侧端到端」那一段。
        """
        adapter = get_adapter("grok")
        assert adapter.supports_mcp is False
        assert adapter.mcp_workspace_files(tmp_path, _mcp_servers("a")) == []
        assert adapter.mcp_flags(_mcp_servers("a")) == []
        assert not (tmp_path / ".grok").exists()

    def test_antigravity_writes_nothing(self, tmp_path):
        """实测项目级插件不生效，所以这里必须一个文件都不写（见 AntigravityAdapter）。

        这条用例的作用是「别好心加回来」：哪天有人看到 agy 文档说支持项目级插件、
        顺手补上注入，先红一次，回去读那个 docstring 里的 A/B 结论。
        """
        adapter = get_adapter("antigravity")
        assert adapter.mcp_workspace_files(tmp_path, _mcp_servers("a")) == []
        assert adapter.mcp_flags(_mcp_servers("a")) == []
        assert not (tmp_path / ".agents").exists()


class TestGrokHeadlessContract:
    """grok 的 headless 接口（1.0.5 实测）。

    原先 grok 用的是 GenericExecAdapter 那套「常见约定」默认值，而真机上：
    ``--json`` 这个参数**不存在**（``grok --json -p hi`` → exit 2
    ``unexpected argument``），裸位置参数进的是交互式 TUI，输出的键名也全是另一套。
    这一类的失败很难在生产里被发现 —— 报错在 stderr、进程没崩、上层只看到「空回复」。
    """

    def test_headless_uses_the_single_prompt_flag(self):
        """``-p/--single`` 才是无头入口；裸位置参数是 TUI 的初始提问。"""
        assert get_adapter("grok").prompt_args("你好") == (["-p", "你好"], False)

    def test_output_format_is_a_two_token_flag(self):
        adapter = get_adapter("grok")
        assert adapter.print_flags(streaming=False) == ["--output-format", "json"]
        assert adapter.print_flags(streaming=True) == ["--output-format", "streaming-json"]

    def test_the_generic_json_flag_is_never_emitted(self):
        """``--json`` 是基类默认值，在 grok 上会被参数解析器直接拒掉 —— 别让它回来。"""
        adapter = get_adapter("grok")
        for streaming in (False, True):
            assert "--json" not in adapter.print_flags(streaming=streaming)

    def test_always_approve_follows_skip_permissions(self):
        adapter = get_adapter("grok")
        kwargs = dict(model=None, enable_project_skills=False)
        assert adapter.build_base_command(**kwargs, skip_permissions=True) == [
            "grok", "--always-approve"
        ]
        assert adapter.build_base_command(**kwargs, skip_permissions=False) == ["grok"]

    def test_parses_the_single_json_object(self):
        raw = json.dumps({
            "text": "答案在这里",
            "stopReason": "end_turn",
            "sessionId": "abc123",
            "requestId": "xyz789",
        })
        parsed = get_adapter("grok").parse_result(raw)
        assert parsed["result"] == "答案在这里"
        # 驼峰 sessionId：丢了它 --resume 就永远拿不到会话，多轮对话会静默退化
        assert parsed["session_id"] == "abc123"

    def test_parses_the_streaming_ndjson(self):
        """streaming-json 的正文散在若干 text 事件里，会话 id 只在末条 end 上。"""
        raw = "\n".join([
            json.dumps({"type": "thought", "data": "想想"}),
            json.dumps({"type": "text", "data": "前半"}),
            json.dumps({"type": "text", "data": "后半"}),
            json.dumps({"type": "end", "stopReason": "end_turn", "sessionId": "s-1"}),
        ])
        parsed = get_adapter("grok").parse_result(raw)
        assert parsed["result"] == "前半后半"
        assert parsed["session_id"] == "s-1"

    def test_surfaces_the_error_object(self):
        """失败时 grok 的收尾是 {"type":"error","message":...}，不能把整段原始 JSON 当回复。"""
        raw = json.dumps({"type": "error", "message": "Not signed in."})
        parsed = get_adapter("grok").parse_result(raw)
        assert parsed["result"] == "Not signed in."
        assert parsed["errors"] == ["Not signed in."]

    def test_non_json_output_falls_back_to_text(self):
        assert get_adapter("grok").parse_result("就是一段纯文本") is None

    def test_stream_event_reads_the_data_key(self):
        """正文的键叫 ``data``，而 ``type`` 的值才叫 ``text`` —— 基类正好反着找。"""
        adapter = get_adapter("grok")
        assert adapter.format_stream_event(
            json.dumps({"type": "text", "data": "片段"})
        ) == "片段"
        assert adapter.format_stream_event(
            json.dumps({"type": "end", "sessionId": "s"})
        ) == ""
        assert adapter.format_stream_event(
            json.dumps({"type": "error", "message": "boom"})
        ) == "[grok error] boom"
        # 非 JSON 行（启动日志、panic）原样显示
        assert adapter.format_stream_event("starting up") == "starting up"


class TestKimiWorkspaceTrust:
    """kimi 落一份 project 层 mcp.json 之外，还必须补一条 workspace 信任记录。

    kimi 引擎走 ``loadMcpServers({..., includeProject: this.trust.isTrusted()})``，未授信的
    workspace 只读 user 层 —— project 层那份配置会被**静默**丢掉。而无头模式（``-p``）
    永远走不到 TUI 的授权提示，所以这份信任只能由 AICU 写。

    本类守的核心是「写出来的 key 与 kimi 查的那个逐字一致」。这条特别值得守，因为不一致时
    的失败形态是**静默的**：记录写了、没人读、MCP 不加载、全程没有任何报错。

    「supports_mcp 现在是常量」也有用例守着 —— 这里曾经是个探测 ``kimi --help`` 有没有
    ``mcp`` 子命令的 property，而那个子命令 0.43.1 与 2.0.0 都没有，于是恒为假、
    kimi 永远显示「MCP 未接入」。
    """

    @pytest.fixture(autouse=True)
    def _isolated_home(self, _kimi_home_is_never_the_users):
        """kimi home 的隔离由 conftest 的全局 fixture 兜底，这里只是把路径交出来。

        隔离本身不在本类做：这条覆写**会往 home 里写东西**，是本仓库里唯一碰 home 的
        代码路径，而它不止被本类走到 —— ``TestMcpSupportFlag`` 那种「挨个问 adapter
        注入口子」的用例也会间接撞上。隔离放在 conftest 才守得住。
        """
        home = _kimi_home_is_never_the_users
        home.mkdir(parents=True, exist_ok=True)
        return home

    # 真机黄金样本。来自 ~/.kimi-code/workspace-trust/ 里一条**已经存在**的记录，
    # 内容 {"root":"/Users/heyongjun/Desktop/AICU/final_solution","trustedAt":1789700854694}。
    # 用它而不是自造样本：这是唯一一条从真机反推出来的判据，算法一漂移它就红。
    _GOLDEN_ROOT = "/Users/heyongjun/Desktop/AICU/final_solution"
    _GOLDEN_KEY = "wd_final_solution_5b262f6d586a"

    def test_supports_mcp_is_a_constant(self):
        """曾经的探测恒为假 —— 探的是 ``kimi --help`` 里的 mcp 子命令，而两版都没有它。

        把「没有 CLI 管理子命令」当成「引擎不会加载 MCP」是两件事被当成一件，代价是
        kimi 明明能跑 MCP 却一直报未接入。
        """
        assert KimiAdapter.supports_mcp is True
        assert get_adapter("kimi").supports_mcp is True

    def test_key_matches_a_real_record_from_the_machine(self):
        assert cli_adapters._kimi_trust_key(self._GOLDEN_ROOT) == self._GOLDEN_KEY

    @pytest.mark.parametrize("name, expected_slug", [
        ("final_solution", "final_solution"),
        ("My Workspace", "my-workspace"),   # 大写转小写、空格转短横线
        ("a" * 60, "a" * 40),               # 截到 MAX_WORKDIR_SLUG_LENGTH
        ("工作区", "workspace"),             # 非 ASCII 全变短横线，再剥光 → 兜底串
        ("--edge--", "edge"),               # 首尾短横线要剥掉
    ])
    def test_slug_rules(self, tmp_path, name, expected_slug):
        """slug 段要与 kimi 的 ``slugifyWorkDirName`` 同规则；key 形如 ``wd_<slug>_<hash12>``。"""
        key = cli_adapters._kimi_trust_key(tmp_path / name)
        assert key.startswith("wd_") and len(key) - len("wd_") - 13 == len(expected_slug)
        assert key[3:-13] == expected_slug

    def test_a_root_that_slugs_to_nothing_falls_back(self):
        """兜底串那一路：``/`` 的目录名是空的，kimi 用 ``"workspace"`` 顶上。"""
        assert cli_adapters._kimi_trust_key("/")[3:-13] == "workspace"

    def test_key_follows_the_physical_path_not_the_literal_one(self, tmp_path):
        """key 必须按**物理路径**算 —— 这条是实测出来的，不是推理。

        kimi 那边 key 的输入是子进程的 ``process.cwd()``，而 ``getcwd()`` 返回的是已化解
        软链的物理路径，Node 的 ``path.resolve`` 又不会再解析一次。2026-09-18 三臂对照实测
        （唯一变量 = 记录怎么写）：不写记录全 0、用 ``abspath`` 算的 key 全 0（kimi 找不到）、
        用 ``realpath`` 算的 拉起=1 握手=1 tools/list=1 真调用=1。

        本机 ``/tmp → /private/tmp`` 就是现成的软链样本，所以这条不是纸上推演。
        """
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        assert cli_adapters._kimi_trust_key(link) == cli_adapters._kimi_trust_key(real)
        physical = os.path.realpath(str(link))
        assert physical != str(link)  # 前提：这个断言成立才谈得上区分两者
        assert cli_adapters._kimi_trust_key(link).endswith(
            hashlib.sha256(physical.encode()).hexdigest()[:12])

    def test_nothing_is_written_without_mcp_servers(self, tmp_path, _isolated_home):
        """没配 mcp 的运行不该在用户 home 里留下任何痕迹。"""
        ws = tmp_path / "ws"
        assert get_adapter("kimi").mcp_workspace_files(ws, []) == []
        assert list(_isolated_home.rglob("*")) == []
        assert not ws.exists()

    def test_writes_the_project_config_and_a_trust_record(self, tmp_path, _isolated_home):
        ws = tmp_path / "ws"
        paths = get_adapter("kimi").mcp_workspace_files(ws, _mcp_servers("echo-probe"))

        project = ws / ".kimi-code" / "mcp.json"
        assert project in paths
        assert json.loads(project.read_text(encoding="utf-8"))["mcpServers"].keys() == {"echo-probe"}

        record = _isolated_home / "workspace-trust" / cli_adapters._kimi_trust_key(ws)
        assert record in paths

    def test_the_record_is_byte_for_byte_like_kimis_own(self, tmp_path, _isolated_home):
        """形状必须与 kimi 自己写的那条同形：紧凑 JSON、无尾换行、两个键、毫秒 int。

        比着真机那条写：``{"root":"...","trustedAt":1789700854694}``。老实说内容读的时候
        并不校验（判据只是 key 在不在），但写一个自己都不认识的形状没有好处 —— 哪天
        kimi 开始校验了，这里不用再改。
        """
        ws = tmp_path / "ws"
        get_adapter("kimi").mcp_workspace_files(ws, _mcp_servers("echo-probe"))

        raw = (_isolated_home / "workspace-trust" / cli_adapters._kimi_trust_key(ws)).read_text("utf-8")
        data = json.loads(raw)
        assert sorted(data) == ["root", "trustedAt"]
        assert data["root"] == cli_adapters._kimi_workspace_root(ws)
        assert isinstance(data["trustedAt"], int) and data["trustedAt"] > 10 ** 12  # 毫秒而非秒
        assert raw == json.dumps(data, separators=(",", ":"))  # 紧凑，没有缩进
        assert not raw.endswith("\n")

    def test_an_existing_record_is_neither_overwritten_nor_returned(self, tmp_path, _isolated_home):
        """用户自己在 TUI 里授过的信不能被顶掉。

        更要紧的是**不能被当成「本次创建的」而删掉** —— 返回值就是「跑完删这些」的契约，
        把别人的记录放进返回值，等于一次 AICU 运行顺手撤销了用户的授权。
        """
        ws = tmp_path / "ws"
        record = _isolated_home / "workspace-trust" / cli_adapters._kimi_trust_key(ws)
        record.parent.mkdir(parents=True)
        record.write_text('{"root":"x","trustedAt":1}', encoding="utf-8")

        paths = get_adapter("kimi").mcp_workspace_files(ws, _mcp_servers("echo-probe"))

        assert record not in paths
        assert record.read_text(encoding="utf-8") == '{"root":"x","trustedAt":1}'
        assert (ws / ".kimi-code" / "mcp.json") in paths  # 项目级那份照写，两件事互不影响

    def test_an_unwritable_home_degrades_instead_of_failing(self, tmp_path, monkeypatch):
        """写不进 home 只该退化成「MCP 不生效」，不该让整个节点炸掉。

        信任记录是**增强**，不是本次运行的前提：写不进去时最坏的结果是与改动前一样
        （kimi 不加载 project 层配置），而那远好过一个 OSError 把一次能跑完的节点带走。
        """
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("", encoding="utf-8")       # 拿文件当目录用 → mkdir 必然 OSError
        monkeypatch.setenv("KIMI_CODE_HOME", str(blocker))

        ws = tmp_path / "ws"
        paths = get_adapter("kimi").mcp_workspace_files(ws, _mcp_servers("echo-probe"))

        assert paths == [ws / ".kimi-code" / "mcp.json"]


class TestProbeStdoutRobustness:
    """``_probe_stdout`` 是能力门禁（现在只剩 pi 一条）的底座，它自己的失败面得守住。

    设计意图通篇是「探测不出来就判否」—— 二进制不在、超时都返回 None。唯一的例外曾是
    解码：默认严格解码遇到非 UTF-8 字节抛 ``UnicodeDecodeError``，而它既不是 OSError
    也不是 SubprocessError，会一路穿到 ``/v1/config``（设置页打不开）。
    """

    def _binary(self, tmp_path, body: str) -> str:
        script = tmp_path / "noisy"
        script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        script.chmod(0o755)
        return str(script)

    def test_non_utf8_output_does_not_escape(self, tmp_path):
        """CLI 往输出里混了坏字节：既不崩，也不是整段丢掉。"""
        binary = self._binary(tmp_path, 'printf "\\377\\376"; echo " mcp: Manage MCP servers."\n')
        output = cli_adapters._probe_stdout([binary])
        assert output is not None, "坏字节不该让探测抛异常"
        # 替换解码之后，坏字节后面的内容照样能判 —— 否则等于因为一个字节丢掉整段 help
        assert "mcp" in output

    def test_a_missing_binary_still_returns_none(self, tmp_path):
        assert cli_adapters._probe_stdout([str(tmp_path / "definitely-not-here")]) is None


class TestPiAdapter:
    def test_base_command_model_and_approve(self):
        adapter = get_adapter("pi")
        cmd = adapter.build_base_command(
            model="openai/gpt-4o", skip_permissions=True, enable_project_skills=False
        )
        assert cmd[0] == "pi"
        assert "--model" in cmd and "openai/gpt-4o" in cmd
        assert "--approve" in cmd

    def test_base_command_no_approve_when_not_skip_permissions(self):
        adapter = get_adapter("pi")
        cmd = adapter.build_base_command(
            model=None, skip_permissions=False, enable_project_skills=False
        )
        assert "--approve" not in cmd

    def test_print_flags_print_vs_json(self):
        adapter = get_adapter("pi")
        assert adapter.print_flags(streaming=True) == ["--mode", "json"]
        assert adapter.print_flags(streaming=False) == ["-p"]

    def test_resume_uses_session_flag(self):
        adapter = get_adapter("pi")
        assert adapter.resume_flag("s1") == ["--session", "s1"]

    def test_max_turns_not_supported(self):
        adapter = get_adapter("pi")
        assert adapter.max_turns_flag(7) == []

    def test_prompt_args_positional(self):
        adapter = get_adapter("pi")
        args, via_stdin = adapter.prompt_args("hello")
        assert args == ["hello"]
        assert via_stdin is False

    def test_api_key_env_for_model_by_vendor(self):
        adapter = get_adapter("pi")
        assert adapter.api_key_env_for_model("claude-sonnet-4-6") == "ANTHROPIC_API_KEY"
        assert adapter.api_key_env_for_model("gpt-5.6") == "OPENAI_API_KEY"
        assert adapter.api_key_env_for_model("gemini-3") == "GOOGLE_GENERATIVE_AI_API_KEY"
        assert adapter.api_key_env_for_model("auto-内部") is None

    def test_parse_result_plain_text(self):
        adapter = get_adapter("pi")
        out = adapter.parse_result("plain answer")
        assert out["result"] == "plain answer"
        assert out["session_id"] is None

    def test_parse_result_jsonl_collects_text(self):
        adapter = get_adapter("pi")
        raw = "\n".join([
            json.dumps({"type": "message", "session": "s1", "content": "first"}),
            json.dumps({"type": "message", "session": "s1", "content": "last"}),
        ])
        out = adapter.parse_result(raw)
        assert out["result"] == "first\nlast"
        assert out["session_id"] == "s1"


class TestCodeBuddyAdapter:
    def test_registered_as_claude_code_protocol(self):
        adapter = get_adapter("codebuddy")
        assert isinstance(adapter, CodeBuddyAdapter)
        assert isinstance(adapter, ClaudeCodeAdapter)
        assert adapter.id == "codebuddy"
        assert adapter.command == "codebuddy"
        assert adapter.api_key_env == "CODEBUDDY_API_KEY"

    def test_build_env_uses_codebuddy_api_key(self, monkeypatch):
        monkeypatch.setenv("CODEBUDDY_API_KEY", "secret")
        adapter = get_adapter("codebuddy")
        env = adapter.build_env({}, api_base_url=None, api_key_env="CODEBUDDY_API_KEY")
        assert env["CODEBUDDY_API_KEY"] == "secret"
        assert "ANTHROPIC_API_KEY" not in env

    def test_cross_vendor_keeps_codebuddy_api_key(self):
        """codebuddy 继承 ClaudeCodeAdapter，但认证变量是自己的，不做 Bearer 替换。

        踩过的坑：给 claude 加「跨厂商改用 ANTHROPIC_AUTH_TOKEN」时没限 id，
        codebuddy 被一并改掉 —— sk 注进了 ANTHROPIC_AUTH_TOKEN，而 CLI 读的是
        CODEBUDDY_API_KEY，于是不报错、直接返回空回复。
        """
        adapter = get_adapter("codebuddy")
        assert adapter.api_key_env_for_model("deepseek-v4-pro") == "CODEBUDDY_API_KEY"
        assert adapter.api_key_env_for_model("claude-sonnet-4-6") == "CODEBUDDY_API_KEY"
        assert adapter.api_key_env_for_model("gpt-5.6") == "CODEBUDDY_API_KEY"

    def test_base_command_uses_codebuddy_protocol(self):
        adapter = get_adapter("codebuddy")
        cmd = adapter.build_base_command(
            model="gpt-5", skip_permissions=True, enable_project_skills=False
        )
        assert cmd[0] == "codebuddy"
        assert "--model" in cmd and "gpt-5" in cmd
        assert "--allowedTools" in cmd and "--permission-mode" in cmd

    def test_allowlist_is_one_argv_item_per_name(self):
        """codebuddy 的 --allowedTools 必须**一个名字一个 argv 项**。

        它不拆串：整串会被当成**一个**工具名，于是白名单整个失效 —— 实测表现为
        dontAsk 下 Write / Bash 双双被拒、产物为空，且**与有没有声明 MCP 无关**
        （两组对照都跑过）。所以这条修复对每次 codebuddy 运行生效，不是只修 MCP。

        这条用例钉的是 argv 形态本身；语义断言在 TestMcpAllowlist。
        """
        cmd = get_adapter("codebuddy").build_base_command(
            model=None, skip_permissions=True, enable_project_skills=False,
            mcp_servers=_mcp_servers("echo-probe"),
        )
        i = cmd.index("--allowedTools")
        assert cmd[i + 1] == "Read"  # 不是 "Read Write Edit Bash mcp__echo-probe"
        assert " " not in cmd[i + 1]
        assert cmd[i + 5] == "mcp__echo-probe"
        assert cmd[i + 6] == "--permission-mode"

    @pytest.mark.parametrize("cli", ["claude"])
    def test_claude_family_keeps_the_joined_form(self, cli):
        """claude 保持整串形态 —— 别为 codebuddy 顺手把它们也改了。

        claude 的 ``--allowedTools`` 文档写明认 "comma or space-separated list"，
        真机 claude 节点用整串产物正常（端到端 passed）。形态一动，受保护的那几家
        argv 就不再逐字节一致，得重新验一遍，没有必要。
        """
        cmd = get_adapter(cli).build_base_command(
            model=None, skip_permissions=True, enable_project_skills=False,
            mcp_servers=_mcp_servers("echo-probe"),
        )
        i = cmd.index("--allowedTools")
        assert cmd[i + 1] == "Read Write Edit Bash mcp__echo-probe"
        assert cmd[i + 2] == "--permission-mode"

    def test_defer_loading_disabled_when_mcp_declared(self):
        """声明了 MCP 时关掉 codebuddy 的 MCP 工具延迟加载。

        开着时 MCP 工具得经它自己的 ``DeferExecuteTool`` 转发，而审批卡在**那层壳**
        上，非交互模式没有审批提示可用，于是必被拒（旁路调用日志实测 0 次）。关掉后
        工具以 ``mcp__<server>__<tool>`` 真名直接注册，父类下发的通配就够用 ——
        **一个权限都没放宽**，也没动用户的 settings。
        """
        env = get_adapter("codebuddy").build_env(
            {}, api_base_url=None, api_key_env=None,
            mcp_servers=_mcp_servers("echo-probe"),
        )
        assert env["CODEBUDDY_DEFER_TOOL_LOADING"] == "0"

    def test_defer_loading_untouched_without_mcp(self):
        """没声明 MCP 就不设它 —— 免得白付「所有工具 schema 常驻上下文」那笔代价。"""
        adapter = get_adapter("codebuddy")
        for servers in (None, []):
            env = adapter.build_env(
                {}, api_base_url=None, api_key_env=None, mcp_servers=servers,
            )
            assert "CODEBUDDY_DEFER_TOOL_LOADING" not in env

    def test_no_username_for_codebuddy(self):
        adapter = get_adapter("codebuddy")
        cmd = adapter.build_base_command(
            model=None, skip_permissions=False, enable_project_skills=False
        )
        assert "--username" not in cmd

    def test_print_flags_and_max_turns_claude_code_compatible(self):
        adapter = get_adapter("codebuddy")
        assert adapter.print_flags(streaming=True) == [
            "--print", "--verbose", "--output-format", "stream-json",
        ]
        assert adapter.print_flags(streaming=False) == [
            "--print", "--output-format", "json",
        ]
        assert adapter.max_turns_flag(3) == ["--max-turns", "3"]
        assert adapter.resume_flag("abc123") == ["--resume", "abc123"]


class TestGeminiAdapter:
    def test_base_command_model_and_yolo(self):
        adapter = get_adapter("gemini")
        cmd = adapter.build_base_command(
            model="gemini-3", skip_permissions=True, enable_project_skills=False
        )
        assert cmd[0] == "gemini"
        assert "--model" in cmd and "gemini-3" in cmd
        assert "--yolo" in cmd

    def test_base_command_no_yolo_when_not_skip_permissions(self):
        adapter = get_adapter("gemini")
        cmd = adapter.build_base_command(
            model=None, skip_permissions=False, enable_project_skills=False
        )
        assert "--yolo" not in cmd

    def test_print_flags_output_format_json(self):
        adapter = get_adapter("gemini")
        assert adapter.print_flags(streaming=True) == ["--output-format", "json"]
        assert adapter.print_flags(streaming=False) == ["--output-format", "json"]

    def test_resume_and_max_turns(self):
        adapter = get_adapter("gemini")
        assert adapter.resume_flag("s1") == ["--resume", "s1"]
        assert adapter.max_turns_flag(7) == []

    def test_prompt_via_flag(self):
        adapter = get_adapter("gemini")
        args, via_stdin = adapter.prompt_args("hello")
        assert args == ["--prompt", "hello"]
        assert via_stdin is False

    def test_api_key_env(self):
        adapter = get_adapter("gemini")
        assert adapter.api_key_env == "GEMINI_API_KEY"

    def test_parse_result_json_response(self):
        adapter = get_adapter("gemini")
        raw = json.dumps({"response": "OK", "sessionId": "s1"})
        out = adapter.parse_result(raw)
        assert out["result"] == "OK"
        assert out["session_id"] == "s1"

    def test_parse_result_plain_text_fallback(self):
        adapter = get_adapter("gemini")
        out = adapter.parse_result("plain answer")
        assert out["result"] == "plain answer"
        assert out["session_id"] is None


class TestCursorAdapter:
    def test_is_installed_rejects_unrelated_agent(self, monkeypatch):
        """agent 是通用命令名，需甄别：--help 不含 cursor 则判未安装。"""
        monkeypatch.setattr(cli_adapters.shutil, "which", lambda cmd: "/usr/bin/agent")
        monkeypatch.setattr(
            cli_adapters.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(stdout="Usage: agent deploy ...", stderr=""),
        )
        assert get_adapter("cursor").is_installed() is False

    def test_is_installed_accepts_cursor_agent(self, monkeypatch):
        monkeypatch.setattr(cli_adapters.shutil, "which", lambda cmd: "/usr/bin/agent")
        monkeypatch.setattr(
            cli_adapters.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(stdout="Cursor CLI usage: agent -p ...", stderr=""),
        )
        assert get_adapter("cursor").is_installed() is True

    def test_base_command_model(self):
        adapter = get_adapter("cursor")
        cmd = adapter.build_base_command(
            model="gpt-5", skip_permissions=True, enable_project_skills=False
        )
        assert cmd[0] == "agent"
        assert "--model" in cmd and "gpt-5" in cmd
        # skip-permissions flag 官方未列出，保守不加
        assert cmd == ["agent", "--model", "gpt-5"]

    def test_print_flags_print_mode(self):
        adapter = get_adapter("cursor")
        assert adapter.print_flags(streaming=True) == ["-p"]
        assert adapter.print_flags(streaming=False) == ["-p"]

    def test_resume_and_max_turns(self):
        adapter = get_adapter("cursor")
        assert adapter.resume_flag("chat-123") == ["--resume", "chat-123"]
        assert adapter.max_turns_flag(7) == []

    def test_prompt_positional(self):
        adapter = get_adapter("cursor")
        args, via_stdin = adapter.prompt_args("hello")
        assert args == ["hello"]
        assert via_stdin is False

    def test_api_key_env_none(self):
        adapter = get_adapter("cursor")
        assert adapter.api_key_env is None

    def test_parse_result_plain_text(self):
        adapter = get_adapter("cursor")
        out = adapter.parse_result("text answer")
        assert out["result"] == "text answer"
        assert out["session_id"] is None


class TestGenericAdapter:
    def test_parse_result_non_json_text(self):
        adapter = get_adapter("kimi")
        out = adapter.parse_result("plain text answer")
        assert out["result"] == "plain text answer"
        assert out["session_id"] is None


class TestApiKeyEnv:
    def test_api_key_env_declared_per_cli(self):
        """每个 CLI adapter 都声明其认证环境变量名（供 sk 注入复用）。"""
        assert get_adapter("claude").api_key_env == "ANTHROPIC_API_KEY"
        assert get_adapter("codex").api_key_env == "OPENAI_API_KEY"
        assert get_adapter("kimi").api_key_env == "KIMI_API_KEY"
        assert get_adapter("qwen").api_key_env == "BAILIAN_API_KEY"
        assert get_adapter("antigravity").api_key_env == "GEMINI_API_KEY"
        assert get_adapter("grok").api_key_env == "XAI_API_KEY"


class TestKnownModels:
    ALL = [
        "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
        "claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-haiku-4-5",
        "claude-fable-5", "claude-mythos-5",
        "gpt-5.6", "gpt-5.6-cyber", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5-4",
        "kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6", "kimi-k2.5", "kimi-vision-model",
        "grok-4.20", "grok-4.6", "grok-4.5", "grok-4.3",
        "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
        "gemini-3.5-flash-lite", "gemini-3.1-pro-preview", "gemini-3.1-flash-lite",
        "gemini-flash-latest", "gemini-pro-latest",
        "deepseek-v4-flash", "deepseek-v4-pro",
        "glm-5.2", "glm-5-turbo",
        "MiniMax-M3",
        "qwen3.8-max", "qwen3-coder-plus", "qwen3.7-plus",
    ]

    def test_all_public_clis_share_full_model_set(self):
        # 「只列得出来的」与「连得上的」要对齐：
        #  - 走跨厂商路由的 CLI（claude/codex/kimi/qwen/codebuddy/opencode/pi）能接
        #    各家 OpenAI-Chat 端点，所以全量列出；
        #  - 锁死自家协议的（gemini/antigravity/grok）只列同族模型，见
        #    TestCrossVendorEndpoint::test_protocol_bound_clis_hide_unreachable_models；
        #  - cursor 未纳入路由（其模型名自成一套），行为保持不变。
        routed = ["codebuddy", "qwen", "opencode", "pi"]
        for cli_id in routed:
            assert get_adapter(cli_id).known_models == self.ALL, f"{cli_id} 未返回全厂商模型集"
        assert get_adapter("cursor").known_models == self.ALL

    def test_list_clis_includes_models(self):
        clis = cli_adapters.list_clis()
        by_id = {item["id"]: item for item in clis}
        for cli_id in ["codebuddy", "qwen", "opencode", "pi"]:
            assert by_id[cli_id]["models"] == self.ALL, f"{cli_id} 模型清单不完整"
        assert "models" in by_id["grok"]
        assert by_id["claude"]["priority"] < by_id["codex"]["priority"]
        # 所有 CLI 都要给出非空清单（antigravity 现在在隐藏名单里，见下一条用例）
        for cli_id in ["claude", "codex", "kimi", "grok"]:
            assert by_id[cli_id]["models"], f"{cli_id} 模型清单为空"

    def test_list_clis_exposes_supports_mcp(self):
        """前端下拉靠这个字段标注「MCP 未接入」，缺了就只能在前端硬编码一份名单。

        硬编码的代价是同一份事实维护两遍：将来接一个新 CLI，后端接了、前端没改，
        用户看到的就是一句和实际不符的标注。

        这里只列**常量**的 CLI。pi 压根不是常量，它的值取决于本机装没装
        ``pi-mcp-adapter``，另有用例专门守（见下一条）；antigravity 则是常量 False，但它
        现在在隐藏名单里，取不到 by_id，所以改为直接问 adapter。
        """
        items = cli_adapters.list_clis()
        assert all("supports_mcp" in item for item in items)
        by_id = {item["id"]: item for item in items}
        for cli_id in ("claude", "codebuddy", "codex", "opencode", "qwen", "kimi"):
            assert by_id[cli_id]["supports_mcp"] is True, cli_id
        # grok 是常量 False，且它在下拉里（不在隐藏名单），所以取得到 by_id。
        # 为什么关：CLI 侧验过（``grok mcp doctor`` 握手 OK、认出 1 个工具），模型侧
        # 没验过（本机没有 XAI_API_KEY，走不到模型那一步），而本仓库「已接入」的口径是
        # 端到端跑通。实现与单测都留着，见 GrokAdapter 的 docstring。
        assert by_id["grok"]["supports_mcp"] is False
        # 隐藏项不进下拉，所以取不到上面那份 by_id —— 但「隐藏」管的是可见性，
        # 不是能力，它们的接线该为真还是为真。
        for cli_id in ("gemini", "cursor"):
            assert cli_id not in by_id
            assert get_adapter(cli_id).supports_mcp is True
        # antigravity 反过来：隐藏，且确实没接（见 AntigravityAdapter 的 eligibility 结论）。
        assert "antigravity" not in by_id
        assert get_adapter("antigravity").supports_mcp is False

    def test_capability_gated_clis_report_whatever_the_probe_says(self, monkeypatch):
        """pi 的 MCP 入口要看**本机这个二进制**，所以不能在这儿钉死一个值。

        钉死等于让用例的结论跟着开发机走：装了扩展的机器上它红、没装的机器上它绿，
        而两种情况下产线代码都是对的。这里把门禁拨到两边，断言列表如实反映结果 ——
        这才是这个字段的契约（「告诉前端能不能用」），而不是「永远是某个值」。

        kimi 曾经也在这条参数化里，现在不在：它的 MCP 支持是引擎自带的常量（见
        TestKimiWorkspaceTrust），不再随本机装的版本变。
        """
        for value in (True, False):
            monkeypatch.setattr(cli_adapters, "_mcp_capability", lambda *a, **k: value)
            by_id = {item["id"]: item for item in cli_adapters.list_clis()}
            assert by_id["pi"]["supports_mcp"] is value, value

    def test_the_reason_is_shipped_only_when_there_is_one(self, monkeypatch):
        """接不了才给原因，接得了给 None —— 那时那句话没有意义。

        ``supports_mcp`` 与它是一对**互补**字段，不是两个各自为政的字段：契约就一条
        「真假与有无原因必须同进同退」。前端据此决定要不要提示，判两次的话两个字段迟早
        会漂移。

        成因这句话必须与 adapter 的说法**逐字一致**，不是后端另写一份：它是给用户看的
        动作（pi 的那句里带着一条 ``pi install``），两边分叉就会出现「照文档装了、报错
        还指到别处」。
        """
        for item in cli_adapters.list_clis():
            assert (item["mcp_unwired_reason"] is None) is item["supports_mcp"], item["id"]
            if not item["supports_mcp"]:
                assert item["mcp_unwired_reason"], item["id"]

        # 取一个常量 True 的当「接得了」的样本，省得跟着本机装没装扩展走。
        by_id = {item["id"]: item for item in cli_adapters.list_clis()}
        assert by_id["claude"]["mcp_unwired_reason"] is None
        assert by_id["grok"]["mcp_unwired_reason"] == get_adapter("grok").mcp_unwired_reason()

        # pi 那条随门禁走：拨开是 None，拨闭是 adapter 给的那句 —— 里面得带着安装命令，
        # 否则用户读完只知道「不行」，不知道下一步干什么。
        monkeypatch.setattr(cli_adapters, "_mcp_capability", lambda *a, **k: True)
        assert {i["id"]: i for i in cli_adapters.list_clis()}["pi"]["mcp_unwired_reason"] is None
        monkeypatch.setattr(cli_adapters, "_mcp_capability", lambda *a, **k: False)
        pi = {i["id"]: i for i in cli_adapters.list_clis()}["pi"]
        assert pi["mcp_unwired_reason"] == get_adapter("pi").mcp_unwired_reason()
        assert "pi-mcp-adapter" in pi["mcp_unwired_reason"]

    def test_hidden_clis_absent_from_list_but_adapters_kept(self):
        """摘出下拉的 CLI：列表里没有，但 adapter 必须保留。

        adapter 留着 —— 将来官方支持了（gemini），或用户有账号并登录了（cursor），
        或换到 Antigravity 可用的地区（antigravity），把 id 从 _HIDDEN_CLI_IDS 里
        删掉即可放回，所以这里同时断言「列表里没有」和「get_adapter 仍取得到」。

        gemini：官方只支持 Google 自家模型/端点。
        cursor：Cursor 私有协议 + 需 Cursor 账号，接不了别家模型（见 cli_adapters 注释）。
        antigravity：登录被 eligibility 拒（账号/地区级，见同上）。
        """
        hidden = cli_adapters._HIDDEN_CLI_IDS
        assert hidden == ("gemini", "cursor", "antigravity")
        listed = {item["id"] for item in cli_adapters.list_clis()}
        assert listed.isdisjoint(hidden)
        assert isinstance(get_adapter("gemini"), GeminiAdapter)
        assert isinstance(get_adapter("cursor"), CursorAdapter)
        assert isinstance(get_adapter("antigravity"), AntigravityAdapter)

    def test_hidden_clis_do_not_perturb_priority(self):
        """priority 取 _ADAPTERS 下标而非结果下标，隐藏项不影响其余 CLI 的相对优先级。"""
        index = {a.id: i for i, a in enumerate(cli_adapters._ADAPTERS)}
        for item in cli_adapters.list_clis():
            assert item["priority"] == index[item["id"]]
        # grok 在 _ADAPTERS 里排在三个隐藏项之后，但它的 priority 仍是原始下标 ——
        # 这里拿它当「隐藏项没把后面的人往前挤」的样本。
        by_id = {item["id"]: item for item in cli_adapters.list_clis()}
        assert by_id["claude"]["priority"] < by_id["grok"]["priority"]
        assert by_id["grok"]["priority"] == index["grok"]

    def test_load_known_models_missing_file_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_adapters, "_MODELS_FILE", tmp_path / "missing.json")
        assert cli_adapters._load_known_models() == {}

    def test_load_known_models_invalid_json_returns_empty(self, monkeypatch, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(cli_adapters, "_MODELS_FILE", bad)
        assert cli_adapters._load_known_models() == {}

    def test_load_known_models_non_dict_returns_empty(self, monkeypatch, tmp_path):
        f = tmp_path / "list.json"
        f.write_text("[1, 2, 3]", encoding="utf-8")
        monkeypatch.setattr(cli_adapters, "_MODELS_FILE", f)
        assert cli_adapters._load_known_models() == {}


class TestProvision:
    def test_codex_provision_writes_config_toml(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        get_adapter("codex").provision(
            api_base_url="https://vendor.example/v1",
            api_key_env="VENDOR_API_KEY",
            model="my-model",
        )
        config = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert "[model_providers.aicu]" in config
        assert 'base_url = "https://vendor.example/v1"' in config
        assert 'env_key = "VENDOR_API_KEY"' in config
        assert 'wire_api = "responses"' in config
        assert "requires_openai_auth = false" in config
        assert 'model = "my-model"' in config
        assert 'model_provider = "aicu"' in config

    def test_codex_provision_idempotent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        adapter = get_adapter("codex")
        adapter.provision(api_base_url="https://a.example/v1", api_key_env="K1", model="m1")
        adapter.provision(api_base_url="https://b.example/v1", api_key_env="K2", model="m2")
        config = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert config.count("[model_providers.aicu]") == 1
        assert 'base_url = "https://b.example/v1"' in config
        assert 'model = "m2"' in config
        assert 'base_url = "https://a.example/v1"' not in config

    def test_codex_env_key_matches_injected_variable(self, monkeypatch, tmp_path):
        """config.toml 的 env_key 必须与 _build_process_env 实际注入 sk 的变量名一致。

        两者分叉过一次（实测）：provision 因 api_key_env=None 写了
        env_key = "OPENAI_API_KEY"，而 codex + deepseek-v4-pro 的 key 注在
        DEEPSEEK_API_KEY，于是 codex 报「Missing environment variable:
        OPENAI_API_KEY」，跨厂商 codex 100% 失败。
        """
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        adapter = get_adapter("codex")
        adapter.provision(api_base_url="https://api.deepseek.com/v1", api_key_env=None,
                          model="deepseek-v4-pro")
        config = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
        expected = adapter.api_key_env_for_model("deepseek-v4-pro")
        assert expected == "DEEPSEEK_API_KEY"
        assert f'env_key = "{expected}"' in config
        assert 'env_key = "OPENAI_API_KEY"' not in config

    def test_codex_env_key_falls_back_when_vendor_unknown(self, monkeypatch, tmp_path):
        """模型判不出厂商时（auto 等）退回显式传入的 api_key_env。"""
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        get_adapter("codex").provision(api_base_url="https://x.example/v1",
                                       api_key_env="CUSTOM_KEY", model="auto-内部")
        config = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert 'env_key = "CUSTOM_KEY"' in config

    def test_codex_provision_preserves_existing_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            'model = "old-model"\n# keep me\n[some_other]\nfoo = "bar"\n', encoding="utf-8"
        )
        get_adapter("codex").provision(
            api_base_url="https://a.example/v1", api_key_env="K", model="m"
        )
        config = (codex_dir / "config.toml").read_text(encoding="utf-8")
        assert "# keep me" in config
        assert "[some_other]" in config
        assert 'foo = "bar"' in config
        assert 'model = "m"' in config
        assert 'model = "old-model"' not in config

    def test_qwen_provision_writes_settings_json(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        get_adapter("qwen").provision(
            api_base_url="https://vendor.example/v1",
            api_key_env="BAILIAN_API_KEY",
            model="qwen3-coder-plus",
        )
        data = json.loads((tmp_path / ".qwen" / "settings.json").read_text(encoding="utf-8"))
        assert data["modelProviders"]["aicu"] == [
            {"id": "qwen3-coder-plus", "envKey": "BAILIAN_API_KEY", "baseUrl": "https://vendor.example/v1"}
        ]
        assert data["providerProtocol"]["aicu"] == "openai"

    def test_qwen_provision_merges_existing_providers(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        qwen_dir = tmp_path / ".qwen"
        qwen_dir.mkdir()
        (qwen_dir / "settings.json").write_text(
            json.dumps({"modelProviders": {"dashscope": [{"id": "qwen3-coder-plus"}]}, "providerProtocol": {"dashscope": "openai"}}),
            encoding="utf-8",
        )
        get_adapter("qwen").provision(
            api_base_url="https://x.example/v1", api_key_env="K", model="m"
        )
        data = json.loads((qwen_dir / "settings.json").read_text(encoding="utf-8"))
        assert "dashscope" in data["modelProviders"]
        assert data["modelProviders"]["aicu"][0]["id"] == "m"

    def test_provision_without_base_url_creates_nothing(self, monkeypatch, tmp_path):
        """盘上原本没有这些文件时，撤槽不该把它们造出来。"""
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        get_adapter("codex").provision(api_base_url=None, api_key_env=None, model=None)
        get_adapter("qwen").provision(api_base_url=None, api_key_env=None, model=None)
        get_adapter("pi").provision(api_base_url=None, api_key_env=None, model=None)
        assert not (tmp_path / ".codex" / "config.toml").exists()
        assert not (tmp_path / ".qwen" / "settings.json").exists()
        assert not (tmp_path / ".pi" / "agent" / "models.json").exists()

    def test_provision_default_noop_for_env_clis(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        get_adapter("claude").provision(api_base_url="https://x/v1", api_key_env="K", model="m")
        get_adapter("kimi").provision(api_base_url="https://x/v1", api_key_env="K", model="m")
        get_adapter("opencode").provision(api_base_url="https://x/v1", api_key_env="K", model="m")
        get_adapter("codebuddy").provision(api_base_url="https://x/v1", api_key_env="K", model="m")
        assert not (tmp_path / ".codex" / "config.toml").exists()
        assert not (tmp_path / ".qwen" / "settings.json").exists()
        assert not (tmp_path / ".pi" / "agent" / "models.json").exists()

    def test_pi_provision_writes_models_json(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        get_adapter("pi").provision(
            api_base_url="https://api.deepseek.com/v1", api_key_env=None,
            model="deepseek-v4-pro",
        )
        data = json.loads((tmp_path / ".pi" / "agent" / "models.json").read_text(encoding="utf-8"))
        entry = data["providers"]["aicu"]
        assert entry["baseUrl"] == "https://api.deepseek.com/v1"
        assert entry["api"] == "openai-completions"
        assert entry["models"] == [{"id": "deepseek-v4-pro", "name": "deepseek-v4-pro"}]

    def test_pi_api_key_refers_to_injected_variable(self, monkeypatch, tmp_path):
        """models.json 的 apiKey 引用的变量名必须与注入 sk 的变量名一致（同 codex 那类错配）。"""
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        adapter = get_adapter("pi")
        adapter.provision(api_base_url="https://api.deepseek.com/v1", api_key_env=None,
                          model="deepseek-v4-pro")
        expected = adapter.api_key_env_for_model("deepseek-v4-pro")
        assert expected == "DEEPSEEK_API_KEY"
        data = json.loads((tmp_path / ".pi" / "agent" / "models.json").read_text(encoding="utf-8"))
        # $VAR 是 pi 自己的环境变量插值语法，落盘的不是 key 本身
        assert data["providers"]["aicu"]["apiKey"] == f"${expected}"

    def test_pi_provision_merges_existing_providers(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        pi_dir = tmp_path / ".pi" / "agent"
        pi_dir.mkdir(parents=True)
        (pi_dir / "models.json").write_text(
            json.dumps({"providers": {"mine": {"baseUrl": "https://mine.example"}}}), encoding="utf-8"
        )
        get_adapter("pi").provision(
            api_base_url="https://x.example/v1", api_key_env="K", model="m"
        )
        data = json.loads((pi_dir / "models.json").read_text(encoding="utf-8"))
        assert data["providers"]["mine"]["baseUrl"] == "https://mine.example"
        assert data["providers"]["aicu"]["baseUrl"] == "https://x.example/v1"


class TestMcpSupportFlag:
    """``supports_mcp`` 的不变量：**声明了就必须真的注入得进去**。

    这个标志同时驱动两件事：建 CC 时放行（声明了却不注入＝静默丢失，最坏的那种失败），
    以及前端下拉标注「MCP 未接入」。所以它不能只是一个转给用户看的字符串开关 ——
    必须有测试逼着它和实现对上，否则接下一个 CLI 时漏写一半也没人发现。
    （``managed_slot`` 早就有这么一条守护，见 TestManagedSlot，这条是补上缺的那个。）
    """

    @pytest.fixture(params=[True, False])
    def capability(self, monkeypatch, request):
        """把 pi 的能力门禁拨到两边，两条不变量各跑两遍。

        不拨这一下，用例的**覆盖面跟着开发机走**：装了 pi-mcp-adapter 的机器上 pi 是
        True、会被正向那条查到；没装的机器上它是 False、正向那条直接 continue 掉，
        等于什么都没验。两种情况下用例都是绿的，但守到的东西不一样 —— 这类隐式依赖
        仓库里已经清过一次，别再加回来。拨到两边之后，两个分支在哪台机器上都跑到。
        """
        monkeypatch.setattr(cli_adapters, "_mcp_capability", lambda *a, **k: request.param)

    @staticmethod
    def _injects_anything(adapter, tmp_path, servers=None) -> bool:
        """四个注入口子挨个问一遍，任何一个产出东西就算「注入了」。

        正向与反向两条不变量共用它 —— 只查一半口子的话，一个
        ``supports_mcp=False`` 却从 ``build_base_command`` / ``build_env`` 偷偷注入的
        adapter 会在两条用例之间溜过去。
        """
        servers = _mcp_servers("guard-probe") if servers is None else servers
        base = dict(model=None, skip_permissions=False, enable_project_skills=False)
        if adapter.mcp_flags(servers):
            return True
        if adapter.build_env(
            {}, api_base_url=None, api_key_env=None, mcp_servers=servers
        ) != adapter.build_env({}, api_base_url=None, api_key_env=None):
            return True
        if adapter.mcp_workspace_files(tmp_path, servers):
            return True
        return (
            adapter.build_base_command(**base, mcp_servers=servers)
            != adapter.build_base_command(**base)
        )

    def test_declared_support_actually_injects(self, tmp_path, capability):
        for adapter in cli_adapters._ADAPTERS:
            if adapter.supports_mcp:
                assert self._injects_anything(adapter, tmp_path), (
                    f"{adapter.id}（门禁={capability}）声明了 supports_mcp=True，"
                    f"但四种注入口子都不产出任何东西 —— 角色配了 mcp 会静默失效"
                )

    def test_declared_support_actually_injects_remote_entries_too(self, tmp_path, capability):
        """同一个不变量，换成**远程**条目再问一遍。

        单有上面那条不够：一个「本地分支写好了、远程分支直接 continue 掉了」的
        adapter 在它那里照样是绿的 —— 而远程被静默丢弃正是这一轮最可能引入的坏法
        （本地那条路早就接好了，改的时候最容易只顾着一支）。
        """
        for adapter in cli_adapters._ADAPTERS:
            if adapter.supports_mcp:
                assert self._injects_anything(
                    adapter, tmp_path, servers=_remote_servers("guard-probe")
                ), (
                    f"{adapter.id}（门禁={capability}）声明了 supports_mcp=True，"
                    f"但一条远程 server 四种口子都产不出东西 —— 会被静默丢弃"
                )

    def test_undeclared_support_never_injects(self, tmp_path, capability):
        """反过来也要守：没声明的 CLI 不得偷偷注入（否则前端标了「未接入」却在用）。"""
        for adapter in cli_adapters._ADAPTERS:
            if adapter.supports_mcp:
                continue
            assert not self._injects_anything(adapter, tmp_path), (
                f"{adapter.id} 的 supports_mcp 是 False（门禁={capability}），"
                f"但有口子仍在注入 —— 前端会标「MCP 未接入」，实际却在用"
            )

    def test_hidden_clis_are_not_claimed_as_wired(self):
        """下拉看不见的 CLI 若被算进「已接入」名单，报错文案会推荐一个用户选不到的项。"""
        assert not set(cli_adapters.mcp_enabled_clis()) & set(cli_adapters._HIDDEN_CLI_IDS)

    def test_wired_list_is_derived_from_adapters(self):
        # 两个条件都要体现：既要是 adapter 自己声明的（不是写死的名单），也要能被用户
        # 在下拉里选到（隐藏的 gemini / cursor 不算，见上一条）。
        assert set(cli_adapters.mcp_enabled_clis()) == {
            a.id
            for a in cli_adapters._ADAPTERS
            if a.supports_mcp and a.id not in cli_adapters._HIDDEN_CLI_IDS
        }


class TestSkillsSupportFlag:
    """``skills_workspace_dir`` 的不变量：**声明了就必须真的能把 skill 送到 CLI 手上**。

    与 ``TestMcpSupportFlag`` 守的是同一类东西，但**判据必须另写一套**：那家的
    ``_injects_anything`` 只枚举四个 MCP 口子（mcp_flags / build_env /
    mcp_workspace_files / build_base_command+mcp_servers），一个都没覆盖 skill。

    这个属性的性质与 ``supports_mcp`` 不同：它**同时**是能力声明与落盘路径（没有 pi
    那种运行时探测的维度），所以「声明了却没实现」在类型上不可能，只可能「声明错了」
    —— 路径是对的但 CLI 不认。那一条靠真机验证守，这里守的是**接线本身是通的**。
    """

    # 与 claude 系那套 argv 通道同名，但只有它能用这个口子：其余几家有逐字节 argv
    # 断言，加 flag 会立刻红（见 test_only_claude_family_uses_the_argv_channel）。
    _ARGV_CHANNEL_IDS = {"claude", "codebuddy"}

    @staticmethod
    def _make_skill_dir(tmp_path):
        """真的建一个目录：基类实现要 ``symlink_to(src.resolve())``，源必须存在。"""
        skills_dir = tmp_path / "src-skills"
        # 一条用例里每个 adapter 调一次，同一个 tmp_path 复用 —— 幂等
        (skills_dir / "guard-probe").mkdir(parents=True, exist_ok=True)
        (skills_dir / "guard-probe" / "SKILL.md").write_text(
            "---\nname: guard-probe\ndescription: 探针\n---\n", encoding="utf-8"
        )
        return skills_dir

    def _injects_skills(self, adapter, tmp_path) -> bool:
        """两个口子挨个问一遍：落盘建链接，或 argv 里加 ``--setting-sources``。

        两个都要查 —— 只查落盘的话，一个「目录声明好了但从没被写」的 adapter 会溜过去；
        只查 argv 的话，非 claude 系全军覆没。
        """
        skills_dir = self._make_skill_dir(tmp_path)
        workspace = tmp_path / "ws"
        if adapter.skills_workspace_files(workspace, skills_dir, ["guard-probe"]):
            return True
        base = dict(model=None, skip_permissions=False, mcp_servers=None)
        return (
            adapter.build_base_command(**base, enable_project_skills=True)
            != adapter.build_base_command(**base, enable_project_skills=False)
        )

    def test_declared_support_actually_injects(self, tmp_path):
        for adapter in cli_adapters._ADAPTERS:
            if adapter.skills_workspace_dir is None:
                continue
            assert self._injects_skills(adapter, tmp_path), (
                f"{adapter.id} 声明了 skills_workspace_dir={adapter.skills_workspace_dir}，"
                f"但既没落盘也没 argv —— 角色配了 skills 会静默失效"
            )

    def test_undeclared_support_never_injects(self, tmp_path):
        """反过来也要守：没接线的 CLI 不得偷偷建目录（否则用户读到「未接入」却在生效）。"""
        for adapter in cli_adapters._ADAPTERS:
            if adapter.skills_workspace_dir is not None:
                continue
            assert not self._injects_skills(adapter, tmp_path), (
                f"{adapter.id} 的 skills_workspace_dir 是 None，但有口子仍在注入"
            )

    def test_only_claude_family_uses_the_argv_channel(self, tmp_path):
        """``--setting-sources project`` 只能出现在 claude 族的 argv 里。

        其余几家的 ``build_base_command`` 有**逐字节**断言盯着（gemini / antigravity /
        codex 各一条），有人为了「让 gemini 也生效」往那儿加 flag，那三条会立刻红。
        这条用例把同一件事再说明白一次：非 claude 系只能走工作区文件这条路。
        """
        base = dict(model=None, skip_permissions=False, mcp_servers=None)
        for adapter in cli_adapters._ADAPTERS:
            changed = (
                adapter.build_base_command(**base, enable_project_skills=True)
                != adapter.build_base_command(**base, enable_project_skills=False)
            )
            assert changed == (adapter.id in self._ARGV_CHANNEL_IDS), (
                f"{adapter.id} 的 enable_project_skills 通道与预期不符"
            )

    def test_hidden_clis_are_covered_by_the_matrix(self):
        """隐藏只是「不下发到下拉」，不等于能力缺失 —— 别把 gemini/cursor 漏出验证矩阵。

        这两家在 ``_HIDDEN_CLI_IDS`` 里，前端选不到，但 adapter 仍可被显式指定
        （会话配置里写 cli_name=gemini）。它们的 skills_workspace_dir 必须是真值，
        否则那条路径上 skills 会静默失效而没人发现。
        """
        by_id = {a.id: a for a in cli_adapters._ADAPTERS}
        for cli_id in ("gemini", "cursor"):
            assert cli_id in cli_adapters._HIDDEN_CLI_IDS
            assert by_id[cli_id].skills_workspace_dir is not None, (
                f"{cli_id} 是隐藏但可显式指定的 CLI，skills 不该是未接线状态"
            )


class TestMcpWiringGuard:
    """角色配了 mcp、落到 AICU 还没接线的 CLI 上时，必须在建 CC 这一步就报错。

    静默忽略是最坏的失败形态：任务照跑、看起来正常，工具却从来不存在，用户以为配好了。
    """

    def _build(self, tmp_path, cli_id, servers):
        return ClaudeCodeAgent(
            workspace_path=str(tmp_path / "ws"),
            cli_name=cli_id,
            mcp_servers=servers,
        )

    @pytest.fixture(autouse=True)
    def _pretend_installed(self, monkeypatch):
        """本类只关心「声明了 mcp 但 CLI 没接线」这一条，别被本机装没装挡住。"""
        for adapter_cls in (KimiAdapter, PiAdapter, OpenCodeAdapter, AntigravityAdapter):
            monkeypatch.setattr(adapter_cls, "is_installed", lambda self: True)

    @pytest.fixture(autouse=True)
    def _pin_the_gate_closed(self, monkeypatch):
        """把 pi 的能力门禁钉成 False，否则用例的结论跟着开发机走。

        pi 的 ``supports_mcp`` 是探本机二进制问出来的：装了 ``pi-mcp-adapter`` 的机器上
        它**本来就不该**报错 —— 那时用例会红，而产线代码是对的。门禁本身另有用例守
        （上面那条参数化）。

        本类现在只用 antigravity（常量 False）当未接线样本，本来不依赖这一下 —— 留着是因为
        将来有人拿 pi 写用例时，不拨它就又变成一条跟着开发机走的隐式依赖。
        """
        monkeypatch.setattr(cli_adapters, "_mcp_capability", lambda *a, **k: False)

    def test_unwired_cli_raises(self, tmp_path):
        with pytest.raises(RuntimeError) as excinfo:
            self._build(tmp_path, "antigravity", _mcp_servers("echo-probe"))
        assert "antigravity" in str(excinfo.value)

    def test_a_cli_that_got_wired_stops_raising(self, tmp_path):
        """kimi 2026-09-18 接上了线，同一个构造调用不该再报错。

        这条是那次改动的**回归哨**：kimi 的 ``supports_mcp`` 曾经是个恒为假的探测
        （探 ``kimi --help`` 有没有 mcp 子命令，而 0.43.1 与 2.0.0 都没有），
        于是角色一配 mcp 就在建 CC 这一步炸掉。现在它恒真。
        """
        agent = self._build(tmp_path, "kimi", _mcp_servers("echo-probe"))
        assert agent.adapter.supports_mcp is True

    def test_message_says_not_wired_not_unsupported(self, tmp_path):
        """文案不许说「该 CLI 不支持 MCP」。

        agy 自己就有 MCP，只是它只认全局配置文件、AICU 注入不了。说成「不支持」是关于
        CLI 的假陈述 —— 用户会跑去除错或换 CLI，而不是来问我们能不能接。
        """
        with pytest.raises(RuntimeError) as excinfo:
            self._build(tmp_path, "antigravity", _mcp_servers("echo-probe"))
        message = str(excinfo.value)
        assert "尚未接入" in message
        assert "不支持 MCP" not in message

    def test_unwired_reason_is_asked_of_the_adapter(self, tmp_path, monkeypatch):
        """原因短语由 adapter 提供，不是守卫里写死的一句。

        写死的话，一个「换个注入方式就能解决」的场景会被告知「AICU 还没接线」，用户于是
        去做一个错误的动作：等，或者换 CLI。

        这里换成拨 adapter 的方法到一句哨兵串，断言它**真的出现在报错里** ——
        比钉死某一家当前的文案更贴契约：文案本来就是要随成因变的，而「守卫会去问 adapter」
        才是这条用例该守的不变量。（kimi 曾经是这条的样本，现在它不再走这个分支；
        真在走这个分支的样本见下一条。）
        """
        monkeypatch.setattr(
            cli_adapters.AntigravityAdapter, "mcp_unwired_reason",
            lambda self: "哨兵原因-4f2a",
        )
        with pytest.raises(RuntimeError) as excinfo:
            self._build(tmp_path, "antigravity", _mcp_servers("echo-probe"))
        assert "哨兵原因-4f2a" in str(excinfo.value)

    def test_the_extension_gated_cli_points_at_the_extension(self, tmp_path):
        """pi 为假的原因是**本机没装扩展**，报错就得说扩展、说那条命令。

        这是钩子存在的理由本身：pi 的注入路径 AICU 这边已经接好了（配置照写
        ``.pi/mcp.json``），缺的是读它的 ``pi-mcp-adapter``。沿用基类默认句，用户读到的
        就是关于 AICU 的假话，于是去做错误的动作 —— 等我们接、或者换掉 CLI，而真正该做的
        是跑一条 ``pi install``。所以断言的判据是两半：**提到扩展**、**不提「尚未接入」**。
        """
        with pytest.raises(RuntimeError) as excinfo:
            self._build(tmp_path, "pi", _mcp_servers("echo-probe"))
        message = str(excinfo.value)
        assert "pi-mcp-adapter" in message
        assert "尚未接入" not in message

    def test_every_adapter_can_answer_the_reason_question(self):
        """钩子必须所有 adapter 都能答 —— 守卫在**每条**未接线路径上都会问它一遍。"""
        for adapter in cli_adapters._ADAPTERS:
            reason = adapter.mcp_unwired_reason()
            assert isinstance(reason, str) and reason

    def test_message_recommends_a_cli_that_actually_works(self, tmp_path):
        """推荐的替代品必须真的在已接线名单里，且从 adapter 派生而非写死。"""
        with pytest.raises(RuntimeError) as excinfo:
            self._build(tmp_path, "antigravity", _mcp_servers("echo-probe"))
        message = str(excinfo.value)
        wired = cli_adapters.mcp_enabled_clis()
        assert wired, "接入名单为空，文案会推荐一个空集合"
        for cli_id in wired:
            assert cli_id in message

    def test_no_mcp_declaration_does_not_raise(self, tmp_path):
        """没声明 mcp 时一切照旧 —— 这是既有节点的行为基线。"""
        agent = self._build(tmp_path, "kimi", None)
        assert agent.mcp_servers == []

    def test_wired_cli_accepts_the_same_declaration(self, tmp_path):
        """同一声明落到已接线的 CLI 上不该报错（两条分支的对照）。"""
        agent = self._build(tmp_path, "opencode", _mcp_servers("echo-probe"))
        assert agent.cli_name == "opencode"
        assert [s.name for s in agent.mcp_servers] == ["echo-probe"]


class TestManagedSlot:
    """AICU 托管槽的不变量：那个槽只有 AICU 会写，所以每次运行都要给出当前事实。

    修复前的形状是「只在改道时写」（provision 见 api_base_url 为空就早退），于是
    同族模型会沿用上一次运行留下的端点 —— qwen + qwen3-coder-plus 就是这样去要
    DEEPSEEK_API_KEY 的（AICU 注的却是 BAILIAN_API_KEY）。见 provider_slot_base_url。
    """

    def test_managed_slot_flag_matches_provision_override(self):
        """有托管槽 ⇔ 覆盖了 provision。将来加 CLI 时这条会拦住漏声明 / 漏实现。"""
        for adapter in cli_adapters._ADAPTERS:
            overrides = type(adapter).provision is not cli_adapters.CliAdapter.provision
            assert adapter.managed_slot is overrides, f"{adapter.id} 的 declared/implemented 不一致"
            if adapter.managed_slot:
                # 有槽就必须在 _CLI_NATIVE 里，否则算不出「本协议下的厂商端点」
                assert adapter.id in cli_adapters._CLI_NATIVE, adapter.id

    def test_provider_slot_base_url_matrix(self):
        slot = cli_adapters.provider_slot_base_url
        aliyun_chat = cli_adapters.protocol_endpoint("aliyun", "chat")

        # 没有托管槽的 CLI 恒为 None —— 端点走环境变量或内联配置，盘上无痕可撤
        for cli in ("claude", "kimi", "codebuddy", "opencode"):
            assert slot(cli, "glm-5.2", None) is None, cli

        # 异族：按模型所属厂商写
        assert slot("codex", "deepseek-v4-flash", None) == (
            cli_adapters.protocol_endpoint("deepseek", "responses")
        )
        assert slot("pi", "glm-5.2", None) == cli_adapters.protocol_endpoint("zhipu", "chat")

        # 同族：qwen 接管（它撤槽后没有可用的 auth type），codex 撤槽（订阅登录是主用法）
        assert slot("qwen", "qwen3-coder-plus", None) == aliyun_chat
        assert slot("codex", "gpt-5.6", None) is None

        # 该厂商没有本协议的端点 → 撤槽（智谱没有 Responses API）
        assert slot("codex", "glm-5.2", None) is None

        # 判不出厂商 → 撤槽（留残值比撤掉更坏）
        assert slot("qwen", "手工填的模型名", None) is None

        # 用户自己指定的端点一律原样写进槽（可能自架了中转）
        assert slot("codex", "gpt-5.6", "https://my.proxy/v1") == "https://my.proxy/v1"
        assert slot("qwen", "qwen3-coder-plus", "https://my.proxy/v1") == "https://my.proxy/v1"

    def test_qwen_stale_slot_overwritten_for_same_vendor_model(self, monkeypatch, tmp_path):
        """qwen 跑同族模型时也要重写槽，不能沿用上一次运行留下的端点。"""
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        adapter = get_adapter("qwen")
        # 上一次运行（deepseek）留下的残值
        adapter.provision(api_base_url="https://api.deepseek.com/v1",
                          api_key_env=None, model="deepseek-v4-flash")
        # 这一次跑同族模型、且用户没选 preset（app 里 api_base_url 默认为空）
        adapter.provision(
            api_base_url=cli_adapters.provider_slot_base_url("qwen", "qwen3-coder-plus", None),
            api_key_env=None,
            model="qwen3-coder-plus",
        )
        entry = json.loads(
            (tmp_path / ".qwen" / "settings.json").read_text(encoding="utf-8")
        )["modelProviders"]["aicu"][0]
        assert entry["id"] == "qwen3-coder-plus"
        assert entry["baseUrl"] == cli_adapters.protocol_endpoint("aliyun", "chat")
        assert entry["envKey"] == "BAILIAN_API_KEY"

    def test_codex_stale_slot_released_for_native_model(self, monkeypatch, tmp_path):
        """codex 跑自家模型（gpt-*）时撤掉 AICU 的 provider，交还它自己的认证。

        codex login 的 ChatGPT 订阅态写在 ~/.codex/auth.json，是它的主用法。AICU 若
        留着 requires_openai_auth = false + env_key = OPENAI_API_KEY 的块、并让顶层
        model_provider 指着它，订阅用户会被踢到 API-key 路径上报「Missing environment
        variable: OPENAI_API_KEY」。
        """
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            'model = "glm-5.2"\n'
            'model_provider = "aicu"\n'
            "\n"
            "[model_providers.aicu]\n"
            'name = "AICU"\n'
            'base_url = "https://open.bigmodel.cn/api/paas/v4"\n'
            'env_key = "ZHIPU_API_KEY"\n'
            'wire_api = "responses"\n'
            "requires_openai_auth = false\n"
            "\n"
            "[some_other]\n"
            'foo = "bar"\n',
            encoding="utf-8",
        )
        get_adapter("codex").provision(
            api_base_url=cli_adapters.provider_slot_base_url("codex", "gpt-5.6", None),
            api_key_env=None,
            model="gpt-5.6",
        )
        config = (codex_dir / "config.toml").read_text(encoding="utf-8")
        assert "[model_providers.aicu]" not in config
        assert "model_provider" not in config
        # 用户自己的配置一个字没动
        assert "[some_other]" in config
        assert 'foo = "bar"' in config

    def test_release_keeps_user_owned_providers(self, monkeypatch, tmp_path):
        """撤槽只删 AICU 自己那一项，用户自己的 provider 原样保留。"""
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        qwen_dir = tmp_path / ".qwen"
        qwen_dir.mkdir()
        (qwen_dir / "settings.json").write_text(
            json.dumps({
                "modelProviders": {
                    "dashscope": [{"id": "qwen3-coder-plus"}],
                    "aicu": [{"id": "deepseek-v4-flash", "baseUrl": "https://api.deepseek.com/v1"}],
                },
                "providerProtocol": {"dashscope": "openai", "aicu": "openai"},
            }),
            encoding="utf-8",
        )
        pi_dir = tmp_path / ".pi" / "agent"
        pi_dir.mkdir(parents=True)
        (pi_dir / "models.json").write_text(
            json.dumps({"providers": {
                "mine": {"baseUrl": "https://mine.example"},
                "aicu": {"baseUrl": "https://api.deepseek.com/v1"},
            }}),
            encoding="utf-8",
        )

        for cli in ("qwen", "pi"):
            get_adapter(cli).provision(
                api_base_url=cli_adapters.provider_slot_base_url(cli, "手工填的模型名", None),
                api_key_env=None,
                model="手工填的模型名",
            )

        qwen_data = json.loads((qwen_dir / "settings.json").read_text(encoding="utf-8"))
        assert "aicu" not in qwen_data["modelProviders"]
        assert "aicu" not in qwen_data["providerProtocol"]
        assert qwen_data["modelProviders"]["dashscope"] == [{"id": "qwen3-coder-plus"}]
        pi_data = json.loads((pi_dir / "models.json").read_text(encoding="utf-8"))
        assert "aicu" not in pi_data["providers"]
        assert pi_data["providers"]["mine"]["baseUrl"] == "https://mine.example"

    def test_agent_wires_slot_value_into_provision(self, monkeypatch, tmp_path):
        """接线：ClaudeCodeAgent 交给 provision 的是「槽该持有的值」，不是 self.api_base_url。

        两者在同族时不同 —— qwen + qwen3-coder-plus 的 self.api_base_url 为 None
        （同族不改 CLI 自身寻址），而槽必须写成阿里云端点。
        """
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(QwenAdapter, "is_installed", lambda self: True)
        agent = ClaudeCodeAgent(
            workspace_path=str(tmp_path), model="qwen3-coder-plus", cli_name="qwen",
            skip_permissions=False,
        )
        assert agent.api_base_url is None
        entry = json.loads(
            (tmp_path / ".qwen" / "settings.json").read_text(encoding="utf-8")
        )["modelProviders"]["aicu"][0]
        assert entry["baseUrl"] == cli_adapters.protocol_endpoint("aliyun", "chat")
        assert entry["envKey"] == "BAILIAN_API_KEY"

    def test_agent_wires_rewritten_endpoint_into_slot(self, monkeypatch, tmp_path):
        """预置地址 + 异族模型：槽里必须写**改写后**的端点，不能照抄配置里存着的预置地址。

        照抄会怎样：选 OpenAI 预置、再把模型换成 deepseek-v4-pro —— 环境变量那条通道
        按厂商改道到了 deepseek，槽里却还留着 api.openai.com，而 codex 读的正是槽
        （顶层 ``model_provider = "aicu"`` 指过去的那块），于是带着 deepseek 的模型名
        打到 openai。界面上显示的地址也一样会卡住不变，因为它是真的没变。
        """
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(CodexAdapter, "is_installed", lambda self: True)
        ClaudeCodeAgent(
            workspace_path=str(tmp_path / "ws"), model="deepseek-v4-pro", cli_name="codex",
            api_base_url="https://api.openai.com/v1",  # openai 预置声明的地址
            skip_permissions=False,
        )
        config = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert 'base_url = "https://api.deepseek.com/v1"' in config
        assert "https://api.openai.com/v1" not in config

    def test_agent_releases_slot_for_codex_native_model(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(CodexAdapter, "is_installed", lambda self: True)
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            'model_provider = "aicu"\n\n[model_providers.aicu]\nname = "AICU"\n'
            'base_url = "https://open.bigmodel.cn/api/paas/v4"\n',
            encoding="utf-8",
        )
        ClaudeCodeAgent(
            workspace_path=str(tmp_path / "ws"), model="gpt-5.6", cli_name="codex",
            skip_permissions=False,
        )
        config = (codex_dir / "config.toml").read_text(encoding="utf-8")
        assert "[model_providers.aicu]" not in config
        assert "model_provider" not in config


class TestMultiVendorCliWiring:
    """qwen / codebuddy / opencode / pi 各自接管第三方端点的方式。

    四者机制都经本机真实调用核实（deepseek-v4-pro，rc=0 且拿到模型回复）：
      - qwen：~/.qwen/settings.json 的 modelProviders.aicu（provision 写文件）
      - codebuddy：CODEBUDDY_BASE_URL 环境变量
      - opencode：OPENCODE_CONFIG_CONTENT 内联配置（不落盘），--model 要带 provider 前缀
      - pi：~/.pi/agent/models.json 的 providers.aicu，--model 要带 provider 前缀
    """

    def test_codebuddy_injects_base_url(self):
        env = get_adapter("codebuddy").build_env(
            {}, api_base_url="https://api.deepseek.com/v1", api_key_env="CODEBUDDY_API_KEY"
        )
        assert env["CODEBUDDY_BASE_URL"] == "https://api.deepseek.com/v1"

    def test_codebuddy_leaves_base_url_alone_when_unset(self):
        env = get_adapter("codebuddy").build_env(
            {}, api_base_url=None, api_key_env="CODEBUDDY_API_KEY"
        )
        assert "CODEBUDDY_BASE_URL" not in env

    def test_opencode_writes_config_content_env(self):
        env = get_adapter("opencode").build_env(
            {}, api_base_url="https://api.deepseek.com/v1", api_key_env="DEEPSEEK_API_KEY"
        )
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        provider = config["provider"]["aicu"]
        assert provider["npm"] == "@ai-sdk/openai-compatible"
        assert provider["options"]["baseURL"] == "https://api.deepseek.com/v1"
        # {env:VAR} 是 opencode 自己的插值语法；变量名取自 api_key_env
        assert provider["options"]["apiKey"] == "{env:DEEPSEEK_API_KEY}"
        # 选择器里可能出现的模型都要列上，否则 opencode 找不到该 id
        assert set(provider["models"]) == set(cli_adapters.KNOWN_MODELS["all"])

    def test_opencode_leaves_env_alone_when_unset(self):
        env = get_adapter("opencode").build_env(
            {}, api_base_url=None, api_key_env="DEEPSEEK_API_KEY"
        )
        assert "OPENCODE_CONFIG_CONTENT" not in env

    @pytest.mark.parametrize("cli", ["opencode", "pi"])
    def test_gateway_clis_prefix_model_with_provider_id(self, cli):
        """opencode / pi 要求 <provider>/<model>，前缀就是注册第三方端点用的 id。"""
        adapter = get_adapter(cli)
        assert adapter.model_arg("deepseek-v4-pro", api_base_url="https://x/v1") == (
            "aicu/deepseek-v4-pro"
        )
        # 没改道时 aicu 这个 provider 根本不存在，不能加前缀
        assert adapter.model_arg("deepseek-v4-pro", api_base_url=None) == "deepseek-v4-pro"

    @pytest.mark.parametrize("cli", ["claude", "codex", "qwen", "codebuddy"])
    def test_other_clis_keep_plain_model_name(self, cli):
        adapter = get_adapter(cli)
        assert adapter.model_arg("deepseek-v4-pro", api_base_url="https://x/v1") == "deepseek-v4-pro"

    def test_qwen_env_key_matches_injected_variable(self, monkeypatch, tmp_path):
        """qwen 的 envKey 与注入 sk 的变量名同源，跨厂商时不能仍写 BAILIAN_API_KEY。"""
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        adapter = get_adapter("qwen")
        adapter.provision(api_base_url="https://api.deepseek.com/v1", api_key_env=None,
                          model="deepseek-v4-pro")
        data = json.loads((tmp_path / ".qwen" / "settings.json").read_text(encoding="utf-8"))
        assert data["modelProviders"]["aicu"][0]["envKey"] == "DEEPSEEK_API_KEY"

    def test_qwen_native_models_keep_bailian_env(self):
        """同族（阿里云）不改写既有行为：仍是 Qwen Code 原生的 BAILIAN_API_KEY。"""
        adapter = get_adapter("qwen")
        assert adapter.api_key_env_for_model("qwen3-coder-plus") == "BAILIAN_API_KEY"
        assert adapter.api_key_env_for_model("deepseek-v4-pro") == "DEEPSEEK_API_KEY"

    def test_agent_passes_provider_prefixed_model_to_cli(self, monkeypatch, tmp_path):
        """端到端接线：ClaudeCodeAgent 交给 --model 的是换算后的名字。"""
        adapter = get_adapter("opencode")
        assert adapter.build_base_command(
            model=adapter.model_arg("deepseek-v4-pro", api_base_url="https://api.deepseek.com/v1"),
            skip_permissions=False,
            enable_project_skills=False,
        ) == ["opencode", "run", "--model", "aicu/deepseek-v4-pro"]


class TestKimiEnvChannel:
    """kimi 的模型与凭据只走 ``KIMI_MODEL_*`` 这一族环境变量（0.43.1 实测）。

    与 claude / codebuddy 的 ``*_BASE_URL`` 不同，这一族还捎带模型名：
    ``KIMI_MODEL_NAME`` 是开关，kimi 据此在内存里合成临时 provider（不落盘），
    而 ``-m`` 的优先级高于它、且只认 config.toml 里的别名。所以「开通道」与
    「不传 --model」必须同时成立 —— test_channel_and_model_arg_agree 守着这条。
    """

    def test_env_channel_carries_model_endpoint_and_provider_type(self):
        env = get_adapter("kimi").build_env(
            {}, api_base_url="https://open.bigmodel.cn/api/paas/v4",
            api_key_env="KIMI_MODEL_API_KEY", model="glm-5.2",
        )
        assert env["KIMI_MODEL_NAME"] == "glm-5.2"
        assert env["KIMI_MODEL_BASE_URL"] == "https://open.bigmodel.cn/api/paas/v4"
        assert env["KIMI_MODEL_PROVIDER_TYPE"] == "openai"
        # 老文档里那两个 shell 变量对 0.43.1 是死的（凭据只从 config.toml 读），
        # 留着只会让后人以为端点是它们带进去的
        assert "KIMI_BASE_URL" not in env
        assert "KIMI_API_KEY" not in env

    def test_env_channel_off_without_endpoint(self):
        """没有端点就不碰环境：同族模型交给 kimi 自己的 config.toml 与登录态。"""
        adapter = get_adapter("kimi")
        env = adapter.build_env(
            {}, api_base_url=None, api_key_env="KIMI_MODEL_API_KEY", model="kimi-k2.6"
        )
        assert not [k for k in env if k.startswith("KIMI_MODEL")]

    @pytest.mark.parametrize(
        "model,api_base_url",
        [
            ("glm-5.2", "https://open.bigmodel.cn/api/paas/v4"),
            ("deepseek-v4-flash", "https://api.deepseek.com/v1"),
            ("qwen3-coder-plus", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            ("kimi-k2.6", None),
            ("自定义模型名", None),
        ],
    )
    def test_channel_and_model_arg_agree(self, model, api_base_url):
        """开环境通道 ⇔ 不传 --model —— 两边都缺模型名、或两处都给了模型名都是 bug。"""
        adapter = get_adapter("kimi")
        env = adapter.build_env(
            {}, api_base_url=api_base_url, api_key_env="KIMI_MODEL_API_KEY", model=model
        )
        channel_on = "KIMI_MODEL_NAME" in env
        assert channel_on is (adapter.model_arg(model, api_base_url=api_base_url) is None)
        if channel_on:
            assert env["KIMI_MODEL_NAME"] == model

    def test_context_limit_only_for_vendors_that_measured_it(self):
        """kimi 把上下文窗口当 max_tokens 发给厂商，超了厂商直接 400。

        aliyun 的上限是实测撞出来的（131072 → 400，65536 → 通过）；没撞过的厂商
        不能凭空写字面量，留给 kimi 自己的 131072。
        """
        adapter = get_adapter("kimi")

        def ctx(model):
            return adapter.build_env(
                {}, api_base_url="https://x/v1", api_key_env="K", model=model
            ).get("KIMI_MODEL_MAX_CONTEXT_SIZE")

        assert ctx("qwen3-coder-plus") == "65536"
        for model in ("glm-5.2", "deepseek-v4-flash", "kimi-k3", "自定义模型名"):
            assert ctx(model) is None, model

    def test_key_injected_into_kimi_model_api_key(self, tmp_path):
        """接线：sk 落到 kimi 真正读的那个变量上，而不是类声明里的 KIMI_API_KEY。"""
        store = CredentialStore(str(tmp_path / "api_keys.json"))
        store.set("zhipu", "sk-secret")
        set_credential_store(store)
        try:
            agent = object.__new__(ClaudeCodeAgent)
            agent.adapter = get_adapter("kimi")
            agent.cli_name = "kimi"
            agent.model = "glm-5.2"
            agent.api_base_url = cli_adapters.protocol_endpoint("zhipu", "chat")
            agent.api_key_env = "KIMI_API_KEY"
            agent.mcp_servers = []
            env = agent._build_process_env()
        finally:
            set_credential_store(None)
        assert env["KIMI_MODEL_API_KEY"] == "sk-secret"
        assert env["KIMI_MODEL_NAME"] == "glm-5.2"
        assert env["KIMI_MODEL_PROVIDER_TYPE"] == "openai"

    def test_agent_never_passes_model_flag_when_rerouted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(KimiAdapter, "is_installed", lambda self: True)
        agent = ClaudeCodeAgent(
            workspace_path=str(tmp_path), model="deepseek-v4-flash", cli_name="kimi",
            skip_permissions=False,
        )
        assert agent.api_base_url == cli_adapters.protocol_endpoint("deepseek", "chat")
        assert agent.cli_model_arg is None
        cmd = agent._build_base_command()
        assert "--model" not in cmd and "-m" not in cmd


class TestBuildEnvSignature:
    def test_every_adapter_accepts_model(self):
        """build_env 的 model 参数是 kimi 需要的；将来加 CLI 时这条会拦住漏接的签名。"""
        for adapter in cli_adapters._ADAPTERS:
            env = adapter.build_env(
                {}, api_base_url=None, api_key_env=None, model="deepseek-v4-flash"
            )
            assert isinstance(env, dict), adapter.id


class TestCrossVendorEndpoint:
    """跨厂商模型：把 base_url 指向厂商对应协议的原生端点，不做任何协议翻译。

    端点事实全部来自本机 curl 实测（每条都带伪造路径对照组：伪造 404 / 真实 401
    才算「存在」），少数 vendor 鉴权先于路由、探测不可判的，采信其官方文档。
    """

    # 每家一个代表模型（都在 cli_models.json 的 all 列表里）
    VENDOR_MODELS = {
        "anthropic": "claude-opus-5",
        "openai": "gpt-5.6",
        "moonshot": "kimi-k3",
        "xai": "grok-4.6",
        "google": "gemini-3.7-flash",
        "deepseek": "deepseek-v4-pro",
        "zhipu": "glm-5.2",
        "minimax": "MiniMax-M3",
        "aliyun": "qwen3.8-max",
    }

    @pytest.mark.parametrize(
        "cli,model,expected",
        [
            # claude 说 Anthropic Messages
            ("claude", "glm-5.2", "https://open.bigmodel.cn/api/anthropic"),
            ("claude", "kimi-k3", "https://api.moonshot.cn/anthropic"),
            ("claude", "deepseek-v4-pro", "https://api.deepseek.com/anthropic"),
            ("claude", "MiniMax-M3", "https://api.minimaxi.com/anthropic"),
            ("claude", "qwen3.8-max", "https://dashscope.aliyuncs.com/apps/anthropic"),
            ("claude", "grok-4.6", "https://api.x.ai"),
            ("claude", "gpt-5.6", None),  # OpenAI 无 /v1/messages（实测 404）
            ("claude", "gemini-3", None),  # Google 两种协议都无（实测 404）
            # codex 说 OpenAI Responses
            ("codex", "deepseek-v4-pro", "https://api.deepseek.com/v1"),
            ("codex", "kimi-k3", "https://api.moonshot.cn/v1"),
            # 智谱没有 Responses 端点（真实调用 404），codex + glm 无路可走
            ("codex", "glm-5.2", None),
            ("codex", "grok-4.6", "https://api.x.ai/v1"),
            ("codex", "MiniMax-M3", "https://api.minimaxi.com/v1"),
            ("codex", "claude-opus-5", None),  # Anthropic 无 /v1/responses（实测 404）
            ("codex", "gemini-3", None),
            # kimi 说 OpenAI Chat
            ("kimi", "glm-5.2", "https://open.bigmodel.cn/api/paas/v4"),
            ("kimi", "gpt-5.6", "https://api.openai.com/v1"),
            ("kimi", "gemini-3", "https://generativelanguage.googleapis.com/v1beta/openai"),
            # Anthropic 的 OpenAI 兼容垫片（官方标注非生产就绪，撤掉只需删表里那个键）
            ("kimi", "claude-opus-5", "https://api.anthropic.com/v1"),
            # qwen 说 OpenAI Chat，且自家（阿里云）就是原生厂商
            ("qwen", "deepseek-v4-pro", "https://api.deepseek.com/v1"),
            ("qwen", "qwen3.8-max", None),  # 同族直连，不改写
            # codebuddy / opencode / pi 是多厂商网关，没有「原生厂商」，一律按 chat 改道
            ("codebuddy", "glm-5.2", "https://open.bigmodel.cn/api/paas/v4"),
            ("codebuddy", "deepseek-v4-pro", "https://api.deepseek.com/v1"),
            ("opencode", "glm-5.2", "https://open.bigmodel.cn/api/paas/v4"),
            ("pi", "deepseek-v4-pro", "https://api.deepseek.com/v1"),
            ("pi", "gemini-3", "https://generativelanguage.googleapis.com/v1beta/openai"),
            # 锁死自家协议的 CLI：任何异族都不改道（"native" 不在数据表里）
            ("gemini", "glm-5.2", None),
            ("antigravity", "glm-5.2", None),
            ("grok", "glm-5.2", None),
            # cursor 未纳入路由，行为不变
            ("cursor", "glm-5.2", None),
        ],
    )
    def test_matrix(self, cli, model, expected):
        assert cli_adapters.native_base_url(cli, model, "") == expected

    @pytest.mark.parametrize(
        "cli,model",
        [
            ("claude", "claude-opus-5"),
            ("codex", "gpt-5.6"),
            ("kimi", "kimi-k3"),
            ("qwen", "qwen3.8-max"),
        ],
    )
    def test_native_family_is_untouched(self, cli, model):
        """同族直连：返回 None 表示一个字节都不改，保证今天的行为不变。"""
        assert cli_adapters.native_base_url(cli, model, "") is None
        assert cli_adapters.native_base_url(cli, model, "https://api.anthropic.com") is None

    @pytest.mark.parametrize("cli", ["gemini", "antigravity", "grok", "cursor"])
    def test_protocol_locked_clis_are_untouched(self, cli):
        """只说自家协议的 CLI（gemini / antigravity / grok）与未接线的 cursor 都不改道。"""
        assert cli_adapters.native_base_url(cli, "glm-5.2", "") is None

    @pytest.mark.parametrize(
        "cli,model,configured",
        [
            ("claude", "auto", ""),  # 内部模型，判不出厂商
            ("claude", "", ""),
            ("claude", None, None),
            (None, "glm-5.2", None),
        ],
    )
    def test_undecidable_cases_are_untouched(self, cli, model, configured):
        assert cli_adapters.native_base_url(cli, model, configured) is None

    def test_custom_endpoint_is_respected(self):
        """用户自己架了中转的，不能被劫持。"""
        assert (
            cli_adapters.native_base_url("claude", "glm-5.2", "https://my-proxy.example.com/v1")
            is None
        )

    def test_known_preset_endpoint_is_still_rerouted(self):
        """预设端点不算自定义端点 —— claude 选 deepseek 时要把 chat 端点换成 anthropic 端点。"""
        assert (
            cli_adapters.native_base_url(
                "claude", "deepseek-v4-pro", "https://api.deepseek.com/v1"
            )
            == "https://api.deepseek.com/anthropic"
        )

    def test_presets_agree_with_protocol_table(self):
        """预设端点必须与协议表对同一厂商的说法逐字一致。

        preset 的值会被原样喂给 CLI（同族时 native_base_url 返回 None，不改写），
        所以它是「该 CLI 期望的完整 base」；而表里的值也是同一个东西。两者分叉
        就是 bug —— 例如 moonshot preset 曾经只写裸 host，kimi 会拼成
        /chat/completions（实测 404），而 kimi 要的是 host + /v1 + /chat/completions。
        """
        checked = 0
        for preset in cli_adapters.list_provider_presets():
            if preset.get("provider_kind") == "gateway":
                continue
            native = cli_adapters._CLI_NATIVE.get(preset.get("cli_name", ""))
            url = preset.get("api_base_url")
            if not native or not url or native[1] not in {"messages", "responses", "chat"}:
                # "native" 协议的 CLI（gemini / antigravity / grok）另有约定：
                # preset 里的 api_base_url 眼下还没有任何 adapter 会去读它。
                continue
            vendor = vendor_of_model(preset["model"])
            expected = cli_adapters.protocol_endpoint(vendor, native[1])
            assert expected == url, f"{preset['id']}: preset 写 {url}，协议表写 {expected}"
            checked += 1
        assert checked >= 7, f"只校验到 {checked} 条预设，覆盖不足"

    def test_every_preset_targets_a_visible_cli(self):
        """**可选的**预设，其 cli_name 必须是一个真实出现在 CLI 下拉里的 id。

        google 预设曾经指向 ``cli_name: "gemini"``，而 gemini 在 ``_HIDDEN_CLI_IDS``
        里、``list_clis()`` 根本不下发它。前端 ``selectedCli`` 于是解析不到，模型候选
        退化成「全部 CLI 模型的并集」—— GPT / Claude / Grok 全被带回来，正是「只显示
        能用的模型」要防的那件事。

        上面那条 preset↔协议表一致性用例抓不到它：protocol 为 ``"native"`` 的预设
        （gemini / antigravity / grok）在那里被 ``continue`` 跳过了。

        ``available: false`` 的跳过 —— 前端两处（provider 下拉、``selectedCli`` 解析）
        都用 ``preset.available !== false`` 过滤，这类预设用户根本选不到，也就无从
        触发上面那条退化。**当前被跳过的是 google 与 xai 两条**（两个 Google 系 CLI 都
        不可用；grok 是认证那条路没实测），所以下面那个下限恰好压线 —— 再加一条
        ``available: false`` 就该顺手把它调小或改成别的守法的断言，别让它变成抱怨。

        已知缺口：xai 预置指向的 grok 眼下在 CLI 下拉里是可见的，纯属巧合（它没被
        隐藏，只是 MCP 关了）。哪天按 GrokAdapter docstring 说的把 grok 藏起来，这条
        用例仍会绿 —— 它挡的是「预设指向不可见的 CLI」，而 xai 这条被 ``continue``
        跳过了，等于没在守。
        """
        visible = {cli["id"] for cli in cli_adapters.list_clis()}
        checked = 0
        for preset in cli_adapters.list_provider_presets():
            if preset.get("available") is False:
                continue
            assert preset.get("cli_name") in visible, (
                f"{preset['id']} 预设指向 CLI {preset.get('cli_name')!r}，它不在 CLI 下拉里"
                f"（可见的有 {sorted(visible)}）"
            )
            checked += 1
        assert checked >= 7, f"只校验到 {checked} 条可选预设，覆盖不足"

    def test_vendor_models_are_all_in_catalog(self):
        """VENDOR_MODELS 里的代表模型必须真的在 cli_models.json 的 all 列表里。

        否则这些用例测的是一个用户根本选不到的模型名，会随目录变化悄悄失真
        （``"google": "gemini-3"`` 就是这么烂掉的 —— 该 id 后来因线上退役被删）。
        """
        catalog = set(cli_adapters.KNOWN_MODELS.get("all", []))
        stale = {v: m for v, m in self.VENDOR_MODELS.items() if m not in catalog}
        assert not stale, f"这些代表模型已不在 cli_models.json 的 all 里：{stale}"

    def test_catalog_gemini_ids_resolve_to_google(self):
        """目录里的 gemini 系 id 必须判得出厂商 google，且 chat 类 CLI 能用。

        ``model_supported_by_cli`` 对「判不出厂商」的模型一律返回 True（保守放行），
        所以一个判不出厂商的坏 id 会绕过所有过滤直接进下拉。
        """
        gemini_ids = [
            m for m in cli_adapters.KNOWN_MODELS.get("all", []) if m.startswith("gemini")
        ]
        assert gemini_ids, "目录里一个 gemini 模型都没有？"
        for model in gemini_ids:
            assert vendor_of_model(model) == "google", f"{model} 判不出是 google"
            assert cli_adapters.model_supported_by_cli("kimi", model), (
                f"{model} 在 chat 协议下应当可用（google 有 /v1beta/openai 端点）"
            )

    def test_retired_gemini_ids_are_not_in_catalog(self):
        """已从线上退役的 gemini id 不能留在目录里 —— 选中只会撞 404。

        本机用真实 key 逐个调 ``generateContent`` 实测（2026-09-16）：
          gemini-3          404 NOT_FOUND（无对应新名，只能删）
          gemini-3.1-pro    404 NOT_FOUND → 真名 gemini-3.1-pro-preview
          gemini-3.5        404 NOT_FOUND → 真名 gemini-3.5-flash
          gemini-2.5-pro    404 "no longer available to new users"
          gemini-2.5-flash  404 "no longer available to new users"（官方指向 gemini-3.6-flash）
        """
        catalog = set(cli_adapters.KNOWN_MODELS.get("all", []))
        for dead in (
            "gemini-3", "gemini-3.1-pro", "gemini-3.5", "gemini-2.5-pro", "gemini-2.5-flash",
        ):
            assert dead not in catalog, f"{dead} 已在线上退役，不该还留在目录里"
        for live in ("gemini-3.1-pro-preview", "gemini-3.5-flash", "gemini-3.6-flash"):
            assert live in catalog, f"{live} 实测可用，应当在目录里"

    # ------------------------------------------------------------ 数据表自洽

    def test_table_covers_exactly_every_known_vendor(self):
        """表里的厂商键必须与 vendor_of_model 的取值一一对应，不多不少。"""
        assert set(cli_adapters.PROTOCOL_ENDPOINTS) == set(self.VENDOR_MODELS)
        for vendor, model in self.VENDOR_MODELS.items():
            assert vendor_of_model(model) == vendor, f"{model} 应当判为 {vendor}"

    def test_every_endpoint_is_a_real_https_url(self):
        for vendor, protocols in cli_adapters.PROTOCOL_ENDPOINTS.items():
            assert protocols, f"{vendor} 一个协议端点都没有"
            for protocol, url in protocols.items():
                assert protocol in {"messages", "responses", "chat"}, f"{vendor}/{protocol} 协议名不认识"
                assert url.startswith("https://"), f"{vendor}/{protocol} 不是 https"
                assert url == url.strip() and url, f"{vendor}/{protocol} 是空串或有空白"

    def test_protocol_endpoint_lookup(self):
        assert cli_adapters.protocol_endpoint("zhipu", "messages") == "https://open.bigmodel.cn/api/anthropic"
        # 智谱没有实现 OpenAI Responses API：真实调用（codex + glm-5.2）稳定 404，
        # coding 与非 coding 两个基址都试过。删键即「该组合不可用」。
        assert cli_adapters.protocol_endpoint("zhipu", "responses") is None
        assert cli_adapters.protocol_endpoint("anthropic", "responses") is None
        assert cli_adapters.protocol_endpoint("google", "messages") is None
        assert cli_adapters.protocol_endpoint(None, "chat") is None
        assert cli_adapters.protocol_endpoint("不存在的厂商", "chat") is None

    # ------------------------------------------------------------ 下拉过滤

    def test_protocol_bound_clis_hide_unreachable_models(self):
        claude_models = get_adapter("claude").known_models
        assert "glm-5.2" in claude_models  # 智谱有 /api/anthropic
        assert "kimi-k3" in claude_models  # Moonshot 有 /anthropic
        assert "gpt-5.6" not in claude_models  # OpenAI 没有 Messages 端点
        assert "gemini-3.7-flash" not in claude_models  # Google 只有 chat 端点

        codex_models = get_adapter("codex").known_models
        assert "deepseek-v4-pro" in codex_models
        assert "grok-4.6" in codex_models
        assert "claude-opus-5" not in codex_models  # Anthropic 没有 Responses 端点
        assert "gemini-3.7-flash" not in codex_models
        # 智谱也没实现 Responses API（真实调用 404），glm 同样掉出 codex 的列表
        assert "glm-5.2" not in codex_models
        assert "glm-5.2" in claude_models  # 但 claude 走 /api/anthropic，仍然可选

        # kimi 说的 chat 协议九家厂商都有端点，所以一个都不掉
        kimi_models = get_adapter("kimi").known_models
        assert "claude-opus-5" in kimi_models  # Anthropic 的 OpenAI 兼容垫片
        assert "gemini-3.7-flash" in kimi_models  # Google 的 /v1beta/openai
        assert kimi_models == get_adapter("opencode").known_models

    def test_supported_helper_keeps_undecidable_cases(self):
        """判不出的情形一律不过滤，避免误伤内部模型。"""
        assert cli_adapters.model_supported_by_cli("claude", "auto") is True
        assert cli_adapters.model_supported_by_cli("claude", "") is True
        assert cli_adapters.model_supported_by_cli("opencode", "gemini-3") is True
        assert cli_adapters.model_supported_by_cli("claude", "gemini-3") is False


class TestEffectiveBaseUrl:
    """界面要显示的「这次真正会用的端点」—— 换模型必须跟着变，用户自己的端点不能被动。

    这一层是给前端连接表单用的：它显示的地址必须与实际请求同源，所以判据来自
    native_base_url / provider_slot_base_url 这两个真正被 claude_code_agent 调用的函数。
    """

    def test_same_family_falls_back_to_the_cli_own_endpoint(self):
        """同族直连时两个改写函数都给 None，但界面不能就此显示空白。"""
        assert cli_adapters.effective_base_url("claude", "claude-opus-5", "") == (
            "https://api.anthropic.com",
            "cli_default",
        )
        # codex 同族时连托管槽都要撤掉（订阅登录态），所以它只能靠 CLINative 兜底
        assert cli_adapters.effective_base_url("codex", "gpt-5.6", "") == (
            "https://api.openai.com/v1",
            "cli_default",
        )

    def test_model_vendor_switch_moves_the_address(self):
        """同一个 CLI、同一个（空的）配置地址，换模型 → 地址必须跟着换。

        这正是用户提的那件事：「切换模型的时候岂不是用不了？得跟随模型」。
        """
        claude_glm = cli_adapters.effective_base_url("claude", "glm-5.2", "")
        claude_kimi = cli_adapters.effective_base_url("claude", "kimi-k3", "")
        assert claude_glm[0] == "https://open.bigmodel.cn/api/anthropic"
        assert claude_kimi[0] == "https://api.moonshot.cn/anthropic"
        assert claude_glm[0] != claude_kimi[0]
        assert {claude_glm[1], claude_kimi[1]} == {"model_vendor"}

        # codex 走托管槽（config.toml 里的 provider 块），一样要跟着模型走
        assert cli_adapters.effective_base_url("codex", "deepseek-v4-pro", "") == (
            "https://api.deepseek.com/v1",
            "model_vendor",
        )

    def test_user_endpoint_is_never_moved(self):
        """手填的网关地址必须原样显示 —— 包括换模型时。"""
        custom = "https://my-gateway.corp/v1"
        for cli, model in (("claude", "glm-5.2"), ("codex", "deepseek-v4-pro")):
            assert cli_adapters.effective_base_url(cli, model, custom) == (custom, "configured")
            assert cli_adapters.effective_base_url(cli, "gpt-5.6", custom) == (custom, "configured")

    def test_preset_address_is_shown_verbatim(self):
        """选了预置厂商时显示的必须就是预置声明的那个地址，且不属于「按模型改道」。

        预置的 (cli, model) 自洽 —— 模型厂商要么就是该 CLI 的原生厂商，要么预置地址
        本身就是该厂商在所需协议下的端点，两种情形都不该再被改写。
        """
        checked = 0
        for preset in cli_adapters.list_provider_presets():
            url = preset.get("api_base_url")
            if not url:
                # google 预置没有 api_base_url（antigravity 只认自家原生协议）
                continue
            assert cli_adapters.effective_base_url(
                preset["cli_name"], preset["model"], url
            ) == (url, "configured"), preset["id"]
            checked += 1
        assert checked >= 7, f"只校验到 {checked} 条预设，覆盖不足"

    def test_slot_cli_follows_the_model_too(self):
        """托管槽 CLI（codex）跨厂商换模型时地址也要跟着走 —— 槽里写的必须是改写后的端点。

        曾经的错法：槽照抄配置里存着的**预置地址**，于是「选 OpenAI 预置再把模型换成
        deepseek」时环境变量改道了、槽没改，而 codex 读的是槽 → 带着 deepseek 的模型名
        打到 openai，界面上的地址也卡住不变。槽里那份地址与实际请求是同一个来源，
        所以这里显示的必须是改写后的端点。
        """
        assert cli_adapters.effective_base_url(
            "codex", "deepseek-v4-pro", "https://api.openai.com/v1"
        ) == ("https://api.deepseek.com/v1", "model_vendor")
        # 同族不受影响：预置自家的模型本来就该走预置声明的那个地址
        assert cli_adapters.effective_base_url(
            "codex", "gpt-5.6", "https://api.openai.com/v1"
        ) == ("https://api.openai.com/v1", "configured")
        # 用户手填的地址同样不受影响
        assert cli_adapters.effective_base_url(
            "codex", "deepseek-v4-pro", "https://my-gw.corp/v1"
        ) == ("https://my-gw.corp/v1", "configured")

    def test_undecidable_cli_stays_honest(self):
        """没纳入路由的 CLI（cursor）不猜端点：没配就老实说不知道。"""
        assert cli_adapters.effective_base_url("cursor", "glm-5.2", "") == (None, "cli_default")
        # 但它自己配了地址时，显示的就是那个地址
        assert cli_adapters.effective_base_url(
            "cursor", "glm-5.2", "https://my-gateway.corp/v1"
        ) == ("https://my-gateway.corp/v1", "configured")
        assert cli_adapters.effective_base_url(None, None, None) == (None, "cli_default")

    def test_empty_string_is_treated_as_unset(self):
        """落盘时空串会被归一成 null（session_manager），这里也要当没配处理。"""
        assert cli_adapters.effective_base_url("claude", "glm-5.2", "") == (
            "https://open.bigmodel.cn/api/anthropic",
            "model_vendor",
        )
        assert cli_adapters.effective_base_url("claude", "glm-5.2", None) == (
            "https://open.bigmodel.cn/api/anthropic",
            "model_vendor",
        )


class TestProviderRoutes:
    @pytest.mark.parametrize(
        ("cli", "wire_api", "base_url"),
        [
            ("opencode", "chat", "https://openrouter.ai/api/v1"),
            ("pi", "chat", "https://openrouter.ai/api/v1"),
            ("qwen", "chat", "https://openrouter.ai/api/v1"),
            ("kimi", "chat", "https://openrouter.ai/api/v1"),
            ("codebuddy", "chat", "https://openrouter.ai/api/v1"),
            ("claude", "messages", "https://openrouter.ai/api"),
            ("codex", "responses", "https://openrouter.ai/api/v1"),
        ],
    )
    def test_openrouter_route_selects_cli_protocol(self, cli, wire_api, base_url):
        route = cli_adapters.provider_route("openrouter", cli)
        assert route["wire_api"] == wire_api
        assert route["api_base_url"] == base_url
        assert route["provider_id"] == "openrouter"

    def test_openrouter_credentials_are_cli_specific(self):
        assert cli_adapters.provider_route("openrouter", "claude")["auth_env"] == "ANTHROPIC_AUTH_TOKEN"
        assert cli_adapters.provider_route("openrouter", "codex")["api_key_env"] == "OPENROUTER_API_KEY"
        assert cli_adapters.provider_route("openrouter", "codebuddy")["api_key_env"] == "OPENROUTER_API_KEY"

    def test_openrouter_route_keeps_full_model_and_endpoint(self):
        model = "anthropic/claude-sonnet-4.5"
        route = cli_adapters.provider_route("openrouter", "claude")
        assert cli_adapters.effective_base_url("claude", model, route["api_base_url"], "openrouter") == (
            "https://openrouter.ai/api", "configured"
        )
        assert cli_adapters.native_base_url("claude", model, route["api_base_url"], "openrouter") is None

    def test_unknown_or_disabled_route_does_not_fallback(self, monkeypatch):
        presets = cli_adapters.list_provider_presets()
        patched = [dict(item) for item in presets]
        openrouter = next(item for item in patched if item["id"] == "openrouter")
        openrouter["routes"] = {"codex": {"enabled": False}}
        monkeypatch.setattr(cli_adapters, "list_provider_presets", lambda: patched)
        assert cli_adapters.provider_route("openrouter", "codex") is None
        assert cli_adapters.provider_route("openrouter", "gemini") is None


class TestOpenRouterAgentEnvironment:
    @pytest.fixture
    def openrouter_store(self, tmp_path):
        store = CredentialStore(str(tmp_path / "api_keys.json"))
        store.set("openrouter", "sk-or-test")
        set_credential_store(store)
        yield store
        set_credential_store(None)

    def _agent(self, cli, model, base_url, api_key_env):
        agent = object.__new__(ClaudeCodeAgent)
        agent.provider_id = "openrouter"
        agent.cli_name = cli
        agent.model = model
        agent.api_base_url = base_url
        agent.api_key_env = api_key_env
        agent.adapter = get_adapter(cli)
        agent.mcp_servers = []
        return agent

    def test_claude_uses_openrouter_auth_token_only(self, monkeypatch, openrouter_store):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-parent")
        agent = self._agent("claude", "anthropic/claude-sonnet-4.5", "https://openrouter.ai/api", "ANTHROPIC_AUTH_TOKEN")
        env = agent._build_process_env()
        assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-or-test"
        assert "ANTHROPIC_API_KEY" not in env

    def test_codebuddy_maps_openrouter_key(self, monkeypatch, openrouter_store):
        agent = self._agent("codebuddy", "openai/gpt-4.1", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY")
        env = agent._build_process_env()
        assert env["CODEBUDDY_BASE_URL"] == "https://openrouter.ai/api/v1"
        assert env["CODEBUDDY_API_KEY"] == "sk-or-test"

    def test_codex_keeps_openrouter_env_key(self, monkeypatch, openrouter_store):
        agent = self._agent("codex", "openai/gpt-4.1", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY")
        env = agent._build_process_env()
        assert env["OPENROUTER_API_KEY"] == "sk-or-test"


class TestResultEventShapes:
    """--output-format json 的三种形态都要能解析出最终文本。

    claude 是单个对象，codebuddy 与 qwen 是事件数组（实测），stream-json 是 JSONL。
    数组形态曾经完全解析不出来，AICU 会把整坨 JSON 当回复显示给用户。
    """

    ARRAY = [
        {"type": "system", "subtype": "init", "session_id": "sid-1"},
        {"type": "assistant", "message": {"role": "assistant", "content": []}},
        {"type": "result", "subtype": "success", "result": "可用", "session_id": "sid-1"},
    ]

    @pytest.mark.parametrize("cli", ["claude", "codebuddy"])
    def test_claude_family_parses_event_array(self, cli):
        out = get_adapter(cli).parse_result(json.dumps(self.ARRAY, indent=2))
        assert out == {"result": "可用", "session_id": "sid-1", "subtype": "success", "errors": []}

    def test_qwen_parses_event_array(self):
        out = get_adapter("qwen").parse_result(json.dumps(self.ARRAY, indent=2))
        assert out["result"] == "可用"
        assert out["session_id"] == "sid-1"

    @pytest.mark.parametrize("cli", ["claude", "codebuddy"])
    def test_single_object_still_parses(self, cli):
        raw = json.dumps({"subtype": "error_during_execution", "errors": "boom"})
        out = get_adapter(cli).parse_result(raw)
        assert out["subtype"] == "error_during_execution"
        assert out["errors"] == ["boom"]

    @pytest.mark.parametrize("cli", ["claude", "codebuddy"])
    def test_stream_json_lines_still_parse(self, cli):
        raw = "\n".join(
            json.dumps(event)
            for event in [{"type": "assistant", "text": "略"}, self.ARRAY[-1]]
        )
        assert get_adapter(cli).parse_result(raw)["result"] == "可用"

    @pytest.mark.parametrize("cli", ["claude", "codebuddy"])
    def test_plain_text_returns_none(self, cli):
        """纯文本不是 JSON，返回 None 由上层退回原始输出。"""
        assert get_adapter(cli).parse_result("普通文本回复") is None

    def test_qwen_plain_text_falls_back_to_raw(self):
        """qwen 另有纯文本模式，兜底把原文当回复（既有行为，不因数组支持而改变）。"""
        assert get_adapter("qwen").parse_result("普通文本回复")["result"] == "普通文本回复"


@pytest.mark.parametrize("cli_id", [a.id for a in cli_adapters._ADAPTERS])
@pytest.mark.parametrize("line", ["42", "[1, 2]", "null", "true"])
def test_format_stream_event_tolerates_non_object_json_lines(cli_id, line):
    """子进程偶尔吐出合法但不是对象的 JSON 行（计数、日志），不能让流式显示崩掉。"""
    assert isinstance(cli_adapters.get_adapter(cli_id).format_stream_event(line), str)
