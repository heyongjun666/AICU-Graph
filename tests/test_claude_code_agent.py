"""claude_code_agent.py / aicu.py 图片输入报错恢复逻辑的单元测试。

覆盖：
  - is_model_image_input_error 签名识别
  - _extract_session_id_from_output 会话 ID 提取（失败运行 stdout 无 session_id 时返回 None）
  - _cc_chat_with_retry 对 ModelImageInputError 的恢复：
      * 会话可续接（conversation_id 被保留）→ 同会话重试，不重置
      * 会话不可续接（conversation_id 为空）→ 重置 + 带『禁读图』说明重试
"""

import time

import pytest

from claude_code_agent import (
    ModelImageInputError,
    is_model_image_input_error,
    ClaudeCodeAgent,
)
from runtime.mcp_manager import McpServer
from aicu import AICU
import cli_adapters


def _mcp_servers(*names) -> list:
    return [
        McpServer(name=name, command="python3", args=["/tmp/echo.py"]) for name in names
    ]


class _FakeProc:
    """够 chat() 走完正常路径的假进程：记录 argv/cwd，communicate 返回合法 JSON。"""

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.cwd = kwargs.get("cwd")
        self.pid = 4242
        self.returncode = 0

    def communicate(self, input=None, timeout=None):
        return ('{"result": "ok", "session_id": "s1"}', "")


# ---- 随 CLI 进程起落的临时 MCP 配置文件 ----
#
# qwen 只能从 cwd 下的项目级文件读 MCP（见 QwenAdapter.mcp_workspace_files），
# 落盘与删除分属两处（adapter 写、agent 删），所以只测 adapter 那一半证明不了文件
# 会被清掉。这里从 chat() 这一层验证生命周期。
#
# 为什么删不干净是**缺陷**而不只是「脏」：产物收集按 rglob 扫 workspace，而 .qwen
# 不在 NONCODE_DIRS 里、.json 也不在 NONCODE_EXTENSIONS 里，于是这个文件会被当成
# CC 写的代码文件复制进最优方案目录、并出现在 _INDEX.md 的文件清单里。


class TestTransientMcpFiles:
    def _agent(self, tmp_path, servers) -> ClaudeCodeAgent:
        return ClaudeCodeAgent(
            workspace_path=str(tmp_path / "ws"),
            cli_name="qwen",
            mcp_servers=servers,
        )

    @pytest.fixture(autouse=True)
    def _no_credentials(self, monkeypatch):
        """本类只关心文件生命周期，别被凭据解析挡在 chat() 门外。"""
        monkeypatch.setattr(ClaudeCodeAgent, "_build_process_env", lambda self: {})

    @pytest.fixture(autouse=True)
    def _isolated_home(self, monkeypatch, tmp_path):
        """把 ``Path.home()`` 指到临时目录 —— 否则本类会改用户真实的 qwen 配置。

        qwen 是托管槽 CLI：``ClaudeCodeAgent.__init__`` 会调 ``adapter.provision()``
        （claude_code_agent.py），而本类建的 agent 没有自定义端点，于是走到
        ``_release_qwen_config()`` —— 它会去 ``~/.qwen/settings.json`` 里删掉
        ``modelProviders.aicu`` / ``providerProtocol.aicu``。

        这不是假想：本类最初没加这道隔离，跑一次测试就把开发机真实的 qwen 托管槽
        条目清掉了（用户自己的 ``hooks`` 等其它键不受影响，但 AICU 上次写的模型
        路由没了，且用户无从察觉）。任何构造托管槽 CLI agent 的测试都要隔着 HOME 跑 ——
        ``cli_adapters.Path`` 就是 ``pathlib.Path`` 本身，所以这一 patch 对整个进程
        生效，测试结束由 monkeypatch 撤销。
        """
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path / "home")

    @pytest.fixture(autouse=True)
    def _qwen_counts_as_installed(self, monkeypatch):
        """本类只关心 ``.qwen/`` 配置文件的生死，不关心 qwen 二进制在不在本机。

        显式传 ``cli_name`` 时 ``ClaudeCodeAgent`` 会额外查一次 ``is_installed()``
        （claude_code_agent.py 的「显式指定」分支），CI runner 上没装 qwen 就在构造期
        抛错 —— 而本类测的是 ``provision()`` 写文件、收尾删文件，与二进制在不在无关。

        为什么不放进 tests/conftest.py 的全局 fixture：那里钉的是自动探测路径
        （``detect_cli``），而这四个用例走的是显式指定路径。更关键的是
        ``is_installed`` 一旦全局为真，``test_cli_adapters.py::TestDetect`` 的三个
        用例就失去意义了 —— 它们正是靠 ``shutil.which`` 的假实现来验证优先级排序和
        「都没装则抛错」的。所以在本类内收窄。
        """
        monkeypatch.setattr(cli_adapters.CliAdapter, "is_installed", lambda self: True)

    def test_config_exists_while_the_cli_runs_then_is_gone(self, tmp_path, monkeypatch):
        """文件必须在 Popen 之前就位（CLI 启动时读），且 chat() 返回后不留在盘上。"""
        seen = {}

        def fake_popen(argv, **kwargs):
            config = tmp_path / "ws" / ".qwen" / "settings.json"
            seen["during"] = config.exists()
            return _FakeProc(argv, **kwargs)

        monkeypatch.setattr("claude_code_agent.subprocess.Popen", fake_popen)
        agent = self._agent(tmp_path, _mcp_servers("echo-probe"))
        agent.chat("hi")

        assert seen["during"], "Popen 时配置还没写 —— CLI 根本读不到"
        assert not (tmp_path / "ws" / ".qwen" / "settings.json").exists()
        assert not (tmp_path / "ws" / ".qwen").exists(), "空目录也不该留下"

    def test_cleaned_up_when_the_process_itself_fails_to_start(self, tmp_path, monkeypatch):
        """Popen 抛异常时也要清 —— 这条路径最容易漏，而漏了同样是交付物污染。"""
        def fake_popen(argv, **kwargs):
            raise FileNotFoundError("qwen 没装")

        monkeypatch.setattr("claude_code_agent.subprocess.Popen", fake_popen)
        agent = self._agent(tmp_path, _mcp_servers("echo-probe"))
        with pytest.raises(FileNotFoundError):
            agent.chat("hi")

        assert not (tmp_path / "ws" / ".qwen").exists()

    def test_no_mcp_declaration_never_creates_the_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "claude_code_agent.subprocess.Popen",
            lambda argv, **kwargs: _FakeProc(argv, **kwargs),
        )
        self._agent(tmp_path, None).chat("hi")

        assert not (tmp_path / "ws" / ".qwen").exists()

    def test_keeps_a_pre_existing_dir_that_is_not_ours(self, tmp_path, monkeypatch):
        """目录里还有别的东西时只删自己那个文件，不许把整个目录端掉。"""
        monkeypatch.setattr(
            "claude_code_agent.subprocess.Popen",
            lambda argv, **kwargs: _FakeProc(argv, **kwargs),
        )
        qwen_dir = tmp_path / "ws" / ".qwen"
        qwen_dir.mkdir(parents=True)
        (qwen_dir / "user-notes.txt").write_text("别删我", encoding="utf-8")

        self._agent(tmp_path, _mcp_servers("echo-probe")).chat("hi")

        assert (qwen_dir / "user-notes.txt").read_text(encoding="utf-8") == "别删我"
        assert not (qwen_dir / "settings.json").exists()


# ---- is_model_image_input_error ----


class TestIsModelImageInputError:
    def test_detects_image_input_signature(self):
        assert is_model_image_input_error(
            "internal 执行失败 (returncode=1; stdout_tail=... "
            "API Error: 400 Model do not support image input. Request id: ...)"
        )

    def test_passes_through_other_errors(self):
        assert not is_model_image_input_error("普通执行失败")
        assert not is_model_image_input_error("AICU execution error: mock failure")
        assert not is_model_image_input_error("")


# ---- _extract_session_id_from_output ----


class TestExtractSessionId:
    def test_none_for_empty_output(self):
        assert ClaudeCodeAgent._extract_session_id_from_output("") is None
        assert ClaudeCodeAgent._extract_session_id_from_output(None) is None

    def test_extracts_from_init_event_line(self):
        # 失败运行 stdout 通常是一组 stream-json 行，init 事件携带 sessionId
        stdout = (
            '{"type":"system","subtype":"init","sessionId":"abc-123"}\n'
            '{"type":"result","result":"done"}\n'
        )
        assert ClaudeCodeAgent._extract_session_id_from_output(stdout) == "abc-123"

    def test_extracts_session_id_key(self):
        stdout = '{"session_id":"xyz-9","result":"ok"}'
        assert ClaudeCodeAgent._extract_session_id_from_output(stdout) == "xyz-9"

    def test_extracts_conversation_id_fallback(self):
        stdout = '{"conversation_id":"conv-1","result":"ok"}'
        assert ClaudeCodeAgent._extract_session_id_from_output(stdout) == "conv-1"

    def test_none_when_result_event_has_no_session(self):
        # 真实失败 payload：result 事件不含 session_id（仅有 terminal_reason 等）
        stdout = (
            '{"type":"result","subtype":"success","terminal_reason":"api_error",'
            '"api_error_status":400,"result":"API Error: 400 Model do not support image input"}'
        )
        assert ClaudeCodeAgent._extract_session_id_from_output(stdout) is None

    def test_none_for_garbage(self):
        assert ClaudeCodeAgent._extract_session_id_from_output("not json at all") is None


# ---- _cc_chat_with_retry 恢复逻辑 ----


class _FakeCC:
    """模拟 ClaudeCodeAgent：首次 chat() 抛图片输入错误并保留/清空会话 ID。"""

    def __init__(self, preserve_session: bool):
        self.conversation_id = None
        self.preserve_session = preserve_session
        self.calls = []  # (conversation_id_at_call, prompt)
        self.reset_count = 0

    def chat(self, prompt: str, max_turns=None, timeout=7200) -> str:
        self.calls.append((self.conversation_id, prompt))
        if len(self.calls) == 1:
            # 模拟 chat() 失败：图片输入限制，conversation_id 尽力保留或不可恢复
            self.conversation_id = "abc-123" if self.preserve_session else None
            raise ModelImageInputError(
                "internal 执行失败 (returncode=1; stdout_tail=... "
                "API Error: 400 Model do not support image input. Request id: ...)"
            )
        return "recovered"

    def reset_conversation(self):
        self.reset_count += 1
        self.conversation_id = None


def _make_aicu(monkeypatch) -> AICU:
    aicu = AICU.__new__(AICU)
    aicu.output_callback = None
    aicu.CC_MAX_RETRIES = 1  # 1 次原始 + 1 次重试
    # 避免重试分支里的递增 sleep 拖慢测试
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return aicu


class TestCCChatWithRetryImageRecovery:
    def test_recovers_same_conversation_when_session_preserved(self, monkeypatch):
        """会话 ID 被保留 → 同会话 --resume 续接重试，不重置、不重发原任务。"""
        aicu = _make_aicu(monkeypatch)
        cc = _FakeCC(preserve_session=True)

        out = aicu._cc_chat_with_retry(cc, "原始任务")

        assert out == "recovered"
        assert len(cc.calls) == 2
        assert cc.reset_count == 0  # 未重置会话
        # 第二次调用沿用保留的会话 ID，且不再重发原始任务
        assert cc.calls[1][0] == "abc-123"
        assert cc.calls[1][1].startswith(aicu._IMAGE_INPUT_RECOVERY_PROMPT)
        # 恢复指令必须携带具体报错文本，让 CC 明确"为什么失败"，避免再次读图
        assert "[具体报错]" in cc.calls[1][1]
        assert "Model do not support image input" in cc.calls[1][1]

    def test_falls_back_to_fresh_conversation_with_guidance(self, monkeypatch):
        """会话不可恢复（conversation_id 为空）→ 重置后带『禁读图』说明重试原任务。"""
        aicu = _make_aicu(monkeypatch)
        cc = _FakeCC(preserve_session=False)

        out = aicu._cc_chat_with_retry(cc, "原始任务")

        assert out == "recovered"
        assert len(cc.calls) == 2
        assert cc.reset_count == 1  # 走了 reset_conversation()
        assert cc.calls[1][0] is None  # 全新会话
        assert cc.calls[1][1] != "原始任务"
        assert "不支持图片输入" in cc.calls[1][1]
        assert "原始任务" in cc.calls[1][1]
        # 恢复指令必须携带具体报错文本
        assert "[具体报错]" in cc.calls[1][1]
        assert "Model do not support image input" in cc.calls[1][1]

    def test_raises_after_retries_exhausted(self, monkeypatch):
        """重试耗尽仍失败 → 如实抛出 ModelImageInputError（不再静默跳过）。"""

        class _AlwaysFailCC:
            def __init__(self):
                self.conversation_id = None
                self.reset_count = 0
                self.call_count = 0

            def chat(self, prompt: str, max_turns=None, timeout=7200) -> str:
                self.call_count += 1
                self.conversation_id = None
                raise ModelImageInputError(
                    "internal 执行失败 (returncode=1; ... support image input ...)"
                )

            def reset_conversation(self):
                self.reset_count += 1
                self.conversation_id = None

        aicu = _make_aicu(monkeypatch)
        cc = _AlwaysFailCC()

        with pytest.raises(ModelImageInputError):
            aicu._cc_chat_with_retry(cc, "原始任务")
        assert cc.call_count == 2  # 1 次原始 + 1 次重试（CC_MAX_RETRIES=1）
        assert cc.reset_count == 1  # 仅在可重试的那次失败时重置


class TestCCChatWithRetryStickyImageBlock:
    """粘性防读图：触发过读图错误后，该 CC 后续所有调用都携带禁止读图硬约束。"""

    def test_subsequent_calls_carry_no_image_constraint(self, monkeypatch):
        """第一次读图报错恢复后，后续（如 revise）调用自动携带硬约束前缀。"""
        aicu = _make_aicu(monkeypatch)
        cc = _FakeCC(preserve_session=True)

        # 第一次：读图报错 → 恢复重试成功，同时标记粘性标志
        out1 = aicu._cc_chat_with_retry(cc, "任务一")
        assert out1 == "recovered"
        assert getattr(cc, "_image_input_blocked", False) is True

        # 后续调用（例如 revise 轮次）：prompt 必须以禁止读图硬约束开头
        out2 = aicu._cc_chat_with_retry(cc, "任务二（revise）")
        assert out2 == "recovered"
        last_prompt = cc.calls[-1][1]
        assert last_prompt.startswith(aicu._IMAGE_INPUT_BLOCKED_CONSTRAINT)
        assert "禁止" in last_prompt and "png" in last_prompt
        assert "任务二（revise）" in last_prompt

    def test_never_errors_gets_no_sticky_constraint(self, monkeypatch):
        """未触发读图错误的 CC，后续调用不注入额外约束。"""

        class _GoodCC:
            def __init__(self):
                self.conversation_id = None
                self.calls = []

            def chat(self, prompt, max_turns=None, timeout=7200):
                self.calls.append(prompt)
                return "ok"

            def reset_conversation(self):
                self.conversation_id = None

        aicu = _make_aicu(monkeypatch)
        cc = _GoodCC()
        out = aicu._cc_chat_with_retry(cc, "普通任务")
        assert out == "ok"
        assert cc.calls[-1] == "普通任务"  # 无额外前缀
        assert not getattr(cc, "_image_input_blocked", False)
