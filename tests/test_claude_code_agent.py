"""claude_code_agent.py / aicu.py 图片输入报错恢复逻辑的单元测试。

覆盖：
  - is_model_image_input_error 签名识别
  - _extract_session_id_from_output 会话 ID 提取（失败运行 stdout 无 session_id 时返回 None）
  - _cc_chat_with_retry 对 ModelImageInputError 的恢复：
      * 会话可续接（conversation_id 被保留）→ 同会话重试，不重置
      * 会话不可续接（conversation_id 为空）→ 重置 + 带『禁读图』说明重试
"""

import os
import time
from pathlib import Path

import pytest

from claude_code_agent import (
    ModelImageInputError,
    is_model_image_input_error,
    ClaudeCodeAgent,
    _drop_transient,
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


class _TransientMcpAgentFixture:
    """落盘/清理这条生命周期的公共脚手架，qwen 那组与逐 adapter 那组共用。

    抽出来是因为两组要防的坑一模一样，尤其是 ``_isolated_home``：少一次隔离，
    跑一次测试就会动到开发机真实的 CLI 配置。
    """

    def _agent(self, tmp_path, servers, cli_name="qwen") -> ClaudeCodeAgent:
        return ClaudeCodeAgent(
            workspace_path=str(tmp_path / "ws"),
            cli_name=cli_name,
            mcp_servers=servers,
        )

    @pytest.fixture(autouse=True)
    def _no_credentials(self, monkeypatch):
        """本类只关心文件生命周期，别被凭据解析挡在 chat() 门外。"""
        monkeypatch.setattr(ClaudeCodeAgent, "_build_process_env", lambda self: {})

    @pytest.fixture(autouse=True)
    def _isolated_home(self, monkeypatch, tmp_path):
        """把 ``Path.home()`` 指到临时目录 —— 否则本类会改用户真实的 CLI 配置。

        qwen / pi 是托管槽 CLI：``ClaudeCodeAgent.__init__`` 会调 ``adapter.provision()``
        （claude_code_agent.py），而本类建的 agent 没有自定义端点，于是走到各自的
        ``_release_*_config()`` —— 它们会去 ``~/.qwen/settings.json``、``~/.pi/agent/
        models.json`` 里删掉 AICU 写的槽位。

        这不是假想：本类最初没加这道隔离，跑一次测试就把开发机真实的 qwen 托管槽
        条目清掉了（用户自己的 ``hooks`` 等其它键不受影响，但 AICU 上次写的模型
        路由没了，且用户无从察觉）。任何构造托管槽 CLI agent 的测试都要隔着 HOME 跑 ——
        ``cli_adapters.Path`` 就是 ``pathlib.Path`` 本身，所以这一 patch 对整个进程
        生效，测试结束由 monkeypatch 撤销。
        """
        monkeypatch.setattr(cli_adapters.Path, "home", lambda: tmp_path / "home")

    @pytest.fixture(autouse=True)
    def _cli_counts_as_installed(self, monkeypatch):
        """本类只关心配置文件的生死，不关心 CLI 二进制在不在本机。

        显式传 ``cli_name`` 时 ``ClaudeCodeAgent`` 会额外查一次 ``is_installed()``
        （claude_code_agent.py 的「显式指定」分支），CI runner 上没装这些 CLI 就在构造期
        抛错 —— 而本类测的是 ``provision()`` 写文件、收尾删文件，与二进制在不在无关。

        为什么不放进 tests/conftest.py 的全局 fixture：那里钉的是自动探测路径
        （``detect_cli``），而这些用例走的是显式指定路径。更关键的是
        ``is_installed`` 一旦全局为真，``test_cli_adapters.py::TestDetect`` 的三个
        用例就失去意义了 —— 它们正是靠 ``shutil.which`` 的假实现来验证优先级排序和
        「都没装则抛错」的。所以在本类内收窄。
        """
        monkeypatch.setattr(cli_adapters.CliAdapter, "is_installed", lambda self: True)


class TestDropTransient:
    """``_drop_transient`` 的边界语义 —— 它删的是别人的目录树，越界就是误伤。

    逐 adapter 的端到端生命周期在上面两组用例里；这一组直接把 stop 的各种取值喂给它，
    因为「多层路径」这条分支现在没有任何 adapter 走到（见该函数 docstring），
    没有一组端到端用例会覆盖它。
    """

    def test_single_level_by_default(self, tmp_path):
        """``stop=None`` = 老行为：只删自己那层目录。"""
        config = tmp_path / "a" / "b" / "c.json"
        config.parent.mkdir(parents=True)
        config.write_text("x", encoding="utf-8")

        _drop_transient([config])

        assert not config.exists()
        assert not config.parent.exists()
        assert (tmp_path / "a").exists(), "没给边界时不该往上爬"

    def test_walks_up_to_but_not_past_the_boundary(self, tmp_path):
        """多层路径要一路删到 workspace 为止 —— 而 workspace 本身必须活着。"""
        ws = tmp_path / "ws"
        config = ws / ".agents" / "plugins" / "aicu" / "mcp_config.json"
        config.parent.mkdir(parents=True)
        config.write_text("x", encoding="utf-8")

        _drop_transient([config], stop=ws)

        assert not config.exists()
        assert not (ws / ".agents").exists(), "三层空壳都该消失"
        assert ws.exists(), "删过界就是把 CC 的工作目录端掉了"

    def test_a_busy_directory_stops_the_walk(self, tmp_path):
        """中间某层还有别人的东西时，那一层和它上面的都留下。"""
        ws = tmp_path / "ws"
        config = ws / "a" / "b" / "c.json"
        config.parent.mkdir(parents=True)
        config.write_text("x", encoding="utf-8")
        (ws / "a" / "keep.txt").write_text("别删我", encoding="utf-8")

        _drop_transient([config], stop=ws)

        assert not config.exists()
        assert not (ws / "a" / "b").exists()
        assert (ws / "a" / "keep.txt").read_text(encoding="utf-8") == "别删我"

    def test_a_boundary_off_the_path_never_removes_the_directory(self, tmp_path):
        """stop 不在这条目录链上时只删文件 —— 那条链不属于我们，一层都不该 rmdir。

        真实形态是 kimi 的 workspace 信任记录：文件由 AICU 写，落在 kimi 自己的
        ``~/.kimi-code/workspace-trust/`` 里，同一个目录还装着用户自己的授权记录。
        所以哪怕目录恰好空掉也不能端掉它 —— 这里故意用空目录，只有空目录才能把
        「只删文件」和「顺手 rmdir 一层」两种情况区分开（非空目录下 rmdir 抛
        OSError 被吞掉，两种实现的表现一样）。
        """
        trust = tmp_path / "workspace-trust"
        config = trust / "wd_ours_0123456789ab"
        config.parent.mkdir(parents=True)
        config.write_text("x", encoding="utf-8")
        unrelated = tmp_path / "ws"
        unrelated.mkdir()

        _drop_transient([config], stop=unrelated)

        assert not config.exists()
        assert trust.exists(), "这个目录是 kimi 的，不是我们建的"
        assert unrelated.exists()

    def test_missing_file_is_not_an_error(self, tmp_path):
        """adapter 没落盘（比如能力门禁关着）时列表可能是空的或指向不存在的路径。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        _drop_transient([], stop=ws)
        _drop_transient(None, stop=ws)
        _drop_transient([ws / "nope" / "x.json"], stop=ws)
        assert ws.exists()


class TestTransientMcpFiles(_TransientMcpAgentFixture):
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


# 走「项目级配置文件」这条路的 CLI 全在里面，含 qwen 这个原有实现。
# 路径逐段列出而不是只写文件名：`_drop_transient` 的 stop 边界是 workspace 根，
# 段数写错的 adapter 会在这里现形。
_TRANSIENT_CONFIG_CLIS = [
    ("qwen", (".qwen", "settings.json")),
    ("gemini", (".gemini", "settings.json")),
    ("cursor", (".cursor", "mcp.json")),
    ("pi", (".pi", "mcp.json")),
    ("kimi", (".kimi-code", "mcp.json")),
    ("grok", (".grok", "config.toml")),
]


class TestTransientMcpFilesPerAdapter(_TransientMcpAgentFixture):
    """同一套生命周期逐家验一遍：写盘 → CLI 运行期间在位 → chat() 返回后连目录都不剩。

    上一条 qwen 的用例证明的是**这套机制**成立；这一组证明的是**每家都挂在这套机制上**：
    漏接一个 adapter 的返回路径、或某家的路径多一层少一层，只有逐家跑才会现形。
    """

    @pytest.fixture(autouse=True)
    def _gate_open(self, monkeypatch):
        """pi 的 supports_mcp 是探本机二进制问出来的，grok 的当前是常量 False。

        这些用例问的是「文件生命周期」，与开发机上装没装扩展、以及某家接没接线
        无关 —— 不拨这一下，用例的结论就跟着开发机走（grok 那几条还会因为压根不落盘
        而在第一行断言上红）。门禁本身另有用例守。
        """
        monkeypatch.setattr(cli_adapters, "_mcp_capability", lambda *a, **k: True)
        monkeypatch.setattr(cli_adapters.GrokAdapter, "supports_mcp", True)

    @pytest.mark.parametrize("cli_id,parts", _TRANSIENT_CONFIG_CLIS)
    def test_config_lives_exactly_as_long_as_the_cli_process(
        self, tmp_path, monkeypatch, cli_id, parts
    ):
        seen = {}

        def fake_popen(argv, **kwargs):
            seen["during"] = (tmp_path / "ws").joinpath(*parts).exists()
            return _FakeProc(argv, **kwargs)

        monkeypatch.setattr("claude_code_agent.subprocess.Popen", fake_popen)
        self._agent(tmp_path, _mcp_servers("echo-probe"), cli_name=cli_id).chat("hi")

        assert seen["during"], f"{cli_id}: Popen 时配置还没写 —— CLI 根本读不到"
        assert not (tmp_path / "ws").joinpath(*parts).exists(), cli_id
        assert not (tmp_path / "ws").joinpath(*parts[:-1]).exists(), (
            f"{cli_id}: 文件删了但空目录留下了"
        )

    @pytest.mark.parametrize("cli_id,parts", _TRANSIENT_CONFIG_CLIS)
    def test_cleaned_up_when_the_process_fails_to_start(
        self, tmp_path, monkeypatch, cli_id, parts
    ):
        def fake_popen(argv, **kwargs):
            raise FileNotFoundError(f"{cli_id} 没装")

        monkeypatch.setattr("claude_code_agent.subprocess.Popen", fake_popen)
        with pytest.raises(FileNotFoundError):
            self._agent(tmp_path, _mcp_servers("echo-probe"), cli_name=cli_id).chat("hi")

        assert not (tmp_path / "ws").joinpath(*parts[:-1]).exists(), cli_id

    @pytest.mark.parametrize("cli_id,parts", _TRANSIENT_CONFIG_CLIS)
    def test_an_unrelated_file_in_the_same_dir_survives(
        self, tmp_path, monkeypatch, cli_id, parts
    ):
        """删空目录的循环不能端掉别人的东西 —— 非空时 rmdir 会抛，正好停下。"""
        monkeypatch.setattr(
            "claude_code_agent.subprocess.Popen",
            lambda argv, **kwargs: _FakeProc(argv, **kwargs),
        )
        ours = (tmp_path / "ws").joinpath(*parts[:-1])
        ours.mkdir(parents=True)
        (ours / "user-notes.txt").write_text("别删我", encoding="utf-8")

        self._agent(tmp_path, _mcp_servers("echo-probe"), cli_name=cli_id).chat("hi")

        assert (ours / "user-notes.txt").read_text(encoding="utf-8") == "别删我", cli_id
        assert not (tmp_path / "ws").joinpath(*parts).exists(), cli_id

    @pytest.mark.parametrize("cli_id,parts", [("pi", (".pi", "mcp.json"))])
    def test_gate_closed_refuses_before_writing_anything(
        self, tmp_path, monkeypatch, cli_id, parts
    ):
        """门禁判否时不是「悄悄写一个不生效的文件」，而是在建 CC 这一步就报错。

        这正是 supports_mcp 那条原则的落点：宁可报错说没接线，也不要写一个不生效的
        配置还让用户以为配好了。所以这里断言两件事 —— 报错，且盘上什么都没多出来。

        kimi 曾经也在这条参数化里（它的门禁当时探本机装的版本），2026-09-18 接上线后
        ``supports_mcp`` 恒真、不再走这个分支 —— 它现在的落盘与清理由下面那条守着。
        """
        monkeypatch.setattr(cli_adapters, "_mcp_capability", lambda *a, **k: False)
        with pytest.raises(RuntimeError, match="MCP"):
            self._agent(tmp_path, _mcp_servers("echo-probe"), cli_name=cli_id)
        assert not (tmp_path / "ws").joinpath(*parts[:-1]).exists(), cli_id

    def test_kimis_trust_record_is_cleaned_up_without_touching_kimis_own_dir(
        self, tmp_path, monkeypatch
    ):
        """kimi 的信任记录落在 workspace **之外**，生命周期一样要跟着进程走。

        这条路径与别家不同，值得单独守：``_drop_transient`` 的 ``stop`` 边界是 workspace
        根，而这份记录的父目录 ``<kimi home>/workspace-trust/`` 不在这条目录链上 ——
        它属于 kimi，里面还装着用户自己的记录。所以清理时必须做到「删文件、不删目录」，
        否则一次 AICU 运行会顺手把用户其它的信任记录端掉（``rmdir`` 对空目录是会成功的，
        一旦场景凑巧就是静默的数据损坏）。

        kimi home 由 tests/conftest.py 钉在 tmp_path 下，所以这条用例碰不到真实 home。
        """
        monkeypatch.setattr(
            "claude_code_agent.subprocess.Popen",
            lambda argv, **kwargs: _FakeProc(argv, **kwargs),
        )
        # 预置一条「别人的」记录：既证明目录不被端掉，也证明只有我们写的那份被删。
        trust_dir = Path(os.environ["KIMI_CODE_HOME"]) / "workspace-trust"
        trust_dir.mkdir(parents=True)
        (trust_dir / "wd_someone_elses").write_text('{"root":"x","trustedAt":1}', encoding="utf-8")

        self._agent(tmp_path, _mcp_servers("echo-probe"), cli_name="kimi").chat("hi")

        assert [p.name for p in trust_dir.iterdir()] == ["wd_someone_elses"], (
            "AICU 写的信任记录没删干净，或者动到了用户自己的记录"
        )
        assert not (tmp_path / "ws" / ".kimi-code").exists(), "项目级配置那份也没删干净"



class TestSpawnChildConfiguration:
    def test_inherits_agent_capabilities_and_allows_overrides(self, tmp_path, monkeypatch):
        captured = {}

        class FakeAgent:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        parent = ClaudeCodeAgent.__new__(ClaudeCodeAgent)
        parent.workspace = tmp_path / "parent"
        parent.model = "deepseek-chat"
        parent.skip_permissions = True
        parent.cli_name = "qwen"
        parent.enable_project_skills = True
        parent.api_base_url = "https://example.invalid/v1"
        parent.api_key_env = "DEEPSEEK_API_KEY"
        parent.mcp_servers = _mcp_servers("echo-probe")

        monkeypatch.setattr("claude_code_agent.ClaudeCodeAgent", FakeAgent)
        parent.spawn_child(
            "child",
            model="override-model",
            api_key_env="OTHER_API_KEY",
        )

        assert captured == {
            "workspace_path": str(tmp_path / "parent" / "child"),
            "instruction_md_path": None,
            "model": "override-model",
            "skip_permissions": True,
            "cli_name": "qwen",
            "enable_project_skills": True,
            "api_base_url": "https://example.invalid/v1",
            "api_key_env": "OTHER_API_KEY",
            "mcp_servers": parent.mcp_servers,
        }


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
