"""CLI 适配层单元测试。"""

import json
import types

import pytest

import cli_adapters
from cli_adapters import (
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


class TestRegistry:
    def test_get_adapter_internal(self):
        adapter = get_adapter("internal")
        assert isinstance(adapter, ClaudeCodeAdapter)
        assert adapter.command == "internal"

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

    def test_priority_order_prefers_internal_and_excludes_copilot(self):
        ids = [a.id for a in cli_adapters._ADAPTERS]
        assert ids[0] == "internal"
        assert ids.index("internal") < ids.index("claude")
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


class TestInternalAdapter:
    def test_base_command_includes_saved_username(self, tmp_path):
        store = CredentialStore(str(tmp_path / "api_keys.json"))
        store.set_username("v_test_user")
        set_credential_store(store)
        try:
            cmd = get_adapter("internal").build_base_command(
                model="Claude Sonnet 4.6",
                skip_permissions=False,
                enable_project_skills=False,
            )
            assert cmd[:4] == ["internal", "--model", "Claude Sonnet 4.6", "--username"]
            assert cmd[4] == "v_test_user"
        finally:
            set_credential_store(None)

    def test_base_command_falls_back_to_environment(self, monkeypatch):
        set_credential_store(None)
        monkeypatch.setenv("INTERNAL_USERNAME", "env_user")
        cmd = get_adapter("internal").build_base_command(
            model=None, skip_permissions=False, enable_project_skills=False
        )
        assert cmd == ["internal", "--username", "env_user"]

    def test_known_models_use_internal_catalog(self):
        assert "Claude Sonnet 4.6" in get_adapter("internal").known_models
        assert "claude-sonnet-4-6" not in get_adapter("internal").known_models
        assert "claude-sonnet-4-6" in get_adapter("claude").known_models

    def test_base_command_normalizes_legacy_model_id(self, monkeypatch):
        monkeypatch.delenv("INTERNAL_USERNAME", raising=False)
        monkeypatch.delenv("CC_USERNAME", raising=False)
        cmd = get_adapter("internal").build_base_command(
            model="deepseek-v4-flash", skip_permissions=False, enable_project_skills=False
        )
        assert cmd == ["internal", "--model", "DeepSeek-V4-Flash"]

    def test_agent_does_not_inject_api_key(self, tmp_path):
        store = CredentialStore(str(tmp_path / "api_keys.json"))
        store.set("deepseek", "sk-secret")
        set_credential_store(store)
        try:
            agent = object.__new__(ClaudeCodeAgent)
            agent.adapter = get_adapter("internal")
            agent.cli_name = "internal"
            agent.model = "DeepSeek-V4-Flash"
            agent.api_base_url = None
            agent.api_key_env = None
            env = agent._build_process_env()
            assert env.get("DEEPSEEK_API_KEY") != "sk-secret"
        finally:
            set_credential_store(None)


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
        换成 ANTHROPIC_AUTH_TOKEN 才通。get_adapter("claude") 是 internal 之外的实例，
        这里断言同族/跨族两条路径。
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
    def test_is_installed_rejects_vendor_unrelated-tool_codex(self, monkeypatch):
        """本机 /usr/bin/codex 是同名的无关工具，需甄别为非 OpenAI Codex。"""
        monkeypatch.setattr(cli_adapters.shutil, "which", lambda cmd: "/usr/bin/codex")
        monkeypatch.setattr(
            cli_adapters.subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(
                stdout="EXAMPLE codex -b file.pb.log -p file.proto\nunrelated-tool proto sql",
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
        # 每个公开 CLI 都返回全厂商官方名并集
        for cli_id in ["claude", "codebuddy", "codex", "kimi", "qwen", "opencode", "pi", "gemini", "cursor", "antigravity", "grok"]:
            assert get_adapter(cli_id).known_models == self.ALL, f"{cli_id} 未返回全厂商模型集"

    def test_list_clis_includes_models(self):
        clis = cli_adapters.list_clis()
        by_id = {item["id"]: item for item in clis}
        for cli_id in ["claude", "codebuddy", "codex", "kimi", "qwen", "opencode", "pi", "gemini", "cursor", "antigravity", "grok"]:
            assert by_id[cli_id]["models"] == self.ALL, f"{cli_id} 模型清单不完整"
        assert "models" in by_id["grok"]
        assert by_id["claude"]["priority"] < by_id["codex"]["priority"]
        # 所有 CLI 都要给出非空清单
        for cli_id in ["claude", "codex", "kimi", "antigravity", "grok"]:
            assert by_id[cli_id]["models"], f"{cli_id} 模型清单为空"

    def test_hidden_clis_absent_from_list_but_adapters_kept(self):
        """摘出下拉的 CLI：列表里没有，但 adapter 必须保留。

        adapter 留着 —— 将来官方支持了（gemini），或用户有账号并登录了（cursor），
        把 id 从 _HIDDEN_CLI_IDS 里删掉即可放回，所以这里同时断言
        「列表里没有」和「get_adapter 仍取得到」。

        gemini：官方只支持 Google 自家模型/端点。
        cursor：Cursor 私有协议 + 需 Cursor 账号，接不了别家模型（见 cli_adapters 注释）。
        """
        hidden = cli_adapters._HIDDEN_CLI_IDS
        assert hidden == ("gemini", "cursor")
        listed = {item["id"] for item in cli_adapters.list_clis()}
        assert listed.isdisjoint(hidden)
        assert isinstance(get_adapter("gemini"), GeminiAdapter)
        assert isinstance(get_adapter("cursor"), CursorAdapter)

    def test_hidden_clis_do_not_perturb_priority(self):
        """priority 取 _ADAPTERS 下标而非结果下标，隐藏项不影响其余 CLI 的相对优先级。"""
        index = {a.id: i for i, a in enumerate(cli_adapters._ADAPTERS)}
        for item in cli_adapters.list_clis():
            assert item["priority"] == index[item["id"]]
        by_id = {item["id"]: item for item in cli_adapters.list_clis()}
        assert by_id["claude"]["priority"] < by_id["antigravity"]["priority"]

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
        """预设的 cli_name 必须是一个真实出现在 CLI 下拉里的 id。

        google 预设曾经指向 ``cli_name: "gemini"``，而 gemini 在 ``_HIDDEN_CLI_IDS``
        里、``list_clis()`` 根本不下发它。前端 ``selectedCli`` 于是解析不到，模型候选
        退化成「全部 CLI 模型的并集」—— GPT / Claude / Grok 全被带回来，正是「只显示
        能用的模型」要防的那件事。

        上面那条 preset↔协议表一致性用例抓不到它：protocol 为 ``"native"`` 的预设
        （gemini / antigravity / grok）在那里被 ``continue`` 跳过了。
        """
        visible = {cli["id"] for cli in cli_adapters.list_clis()}
        for preset in cli_adapters.list_provider_presets():
            assert preset.get("cli_name") in visible, (
                f"{preset['id']} 预设指向 CLI {preset.get('cli_name')!r}，它不在 CLI 下拉里"
                f"（可见的有 {sorted(visible)}）"
            )

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
        """没纳入路由的 CLI（internal / cursor）不猜端点：没配就老实说不知道。"""
        assert cli_adapters.effective_base_url("internal", "DeepSeek-V4-Flash", "") == (
            None,
            "cli_default",
        )
        assert cli_adapters.effective_base_url("cursor", "glm-5.2", "") == (None, "cli_default")
        # 但它自己配了地址时，显示的就是那个地址
        assert cli_adapters.effective_base_url(
            "internal", "DeepSeek-V4-Flash", "https://my-gateway.corp/v1"
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
