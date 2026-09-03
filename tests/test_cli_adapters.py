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
from runtime.credentials import CredentialStore, set_credential_store


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

    def test_print_flags_output_last_message_when_not_streaming(self):
        adapter = get_adapter("codex")
        assert adapter.print_flags(streaming=True) == ["--json"]
        assert adapter.print_flags(streaming=False) == ["--json", "--output-last-message"]

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
        "gemini-3.5", "gemini-3.7-flash", "gemini-3.1-pro", "gemini-3.1-flash-lite", "gemini-3",
        "gemini-2.5-pro", "gemini-2.5-flash",
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

    def test_provision_noop_without_base_url(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        get_adapter("codex").provision(api_base_url=None, api_key_env=None, model=None)
        get_adapter("qwen").provision(api_base_url=None, api_key_env=None, model=None)
        assert not (tmp_path / ".codex" / "config.toml").exists()
        assert not (tmp_path / ".qwen" / "settings.json").exists()

    def test_provision_default_noop_for_env_clis(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path)
        get_adapter("claude").provision(api_base_url="https://x/v1", api_key_env="K", model="m")
        get_adapter("kimi").provision(api_base_url="https://x/v1", api_key_env="K", model="m")
        assert not (tmp_path / ".codex" / "config.toml").exists()
        assert not (tmp_path / ".qwen" / "settings.json").exists()
