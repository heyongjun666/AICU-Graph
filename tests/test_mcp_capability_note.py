"""MCP 能力说明块的测试（渲染 + 投递路径 + 空块不改字节）。

背景：MCP 工具是以真名 `mcp__<server>__<tool>` 注册进 CLI 工具表的，模型每轮都看得见
—— 缺的从来不是「存在性」，而是**规范性**。没有这段说明时，一个挂着 github MCP 的
代码角色遇到「提个 issue」，完全可能自己拼个 curl 绕过去，而且不解释为什么不用；
产物里看不出它曾经考虑过。本仓库之前两个真用上 MCP 的角色（mcp_probe / mcp_lab_*）
都是靠在 role_md 里手写「必须真的调用工具」做到的 —— 那是逐角色维护，这里做统一兜底。

本文件证明三件事，也只证明这三件事：
  1. 块被正确构造（探到 / 没连通 / 连上但零工具，三态分开写）；
  2. 块在**每一条**发送路径上都被投递 —— 与 `_workspace_boundary` 同理，
     「生成阶段覆盖了、审查/审计/续跑漏了」是这类改动最容易做漏的地方；
  3. 没挂 MCP 时，发送口产出的字节与加这段之前**逐字相同**（绝大多数角色都是
     `mcp: []`，它们的提示词一个字节都不能变）。

诚实说明本文件的边界：提示词级改动**无法被单测证明改变了模型行为**。
"""

import os

import pytest

import aicu as aicu_module

from aicu import AICU, _render_mcp_capability_note
from claude_code_agent import ModelImageInputError
from runtime.mcp_client import McpProbeError, McpProbeReport
from runtime.mcp_manager import McpServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ============================================================================
# 构造助手（与 test_workspace_boundary.py 同形）
# ============================================================================


def _make_aicu(tmp_path, role="code", shared_root=None,
               shared_target=None, mcp=None, role_type="worker"):
    """离线构造带真实 Session Shared Workspace 布局的 AICU。

    ``mcp`` 传名字列表（走真实注册表 + 真实预检），默认不挂 —— 绝大多数角色的现状。
    """
    base = tmp_path / "workspace"
    node_ws = base / "nodes" / "n1"
    os.makedirs(node_ws, exist_ok=True)
    return AICU(
        role_type=role_type,
        role=role,
        parent_workspace=str(node_ws),
        skills=[],          # 跳过 skills_dir 校验
        modular_output=True,
        output_file=None,
        mcp=mcp,
        shared_root=str(shared_root) if shared_root else None,
        shared_target=str(shared_target) if shared_target else None,
    )


def _capture_sends(aicu):
    """把每个 CC 的 `chat` 换成记录器，返回 {cc: [实际送出的 prompt, ...]}。

    注意**不能** monkeypatch `_cc_chat_with_retry` —— 块就加在它内部，
    换掉它就等于绕过了被测逻辑，断言恒真。必须打在 `cc.chat()` 这一层。
    """
    sends = {}
    for cc in aicu.ccs:
        calls = []
        sends[cc] = calls

        def _fake_chat(prompt, max_turns=None, timeout=7200, _calls=calls):
            _calls.append(prompt)
            return '{"approved": true, "defects": [], "summary": "ok"}'

        cc.chat = _fake_chat
    return sends


def _server(name="lab", transport="stdio"):
    return McpServer(name=name, command="python3", args=[], transport=transport)


NOTE_HEAD = "# 可用工具通道 (MCP)"


# ============================================================================
# 1. 渲染：三态
# ============================================================================


class TestRenderNote:
    def test_no_server_returns_empty_string(self):
        """空列表 ⇒ 空串。调用方靠这个「不注入」，所以它必须是 "" 而不是含标题的块。"""
        assert _render_mcp_capability_note([], {}) == ""

    def test_lists_tool_names_verbatim(self):
        """工具真名要原样列出 —— 模型不用自己从工具表反推这个 server 能干什么。"""
        note = _render_mcp_capability_note([_server()], {"lab": ["probe_echo", "lab_add"]})
        assert "probe_echo / lab_add" in note
        assert "本地 stdio" in note

    def test_remote_transport_is_labelled(self):
        note = _render_mcp_capability_note([_server(transport="http")], {"lab": ["a"]})
        assert "远程 Streamable HTTP" in note

    def test_unreachable_server_is_called_out(self):
        """配了但没连通（预检失败的 required=False server 不在 probed 里）。

        这一态必须明说：角色的 role_md 可能整篇在讲这条通道的工作流，而它手上
        根本没有这些工具 —— 不说清，模型只能对着空工具表猜，然后绕路或编一次调用。
        """
        note = _render_mcp_capability_note([_server()], {})
        assert "本次未连通" in note
        assert "假装调用过" in note

    def test_connected_but_no_tools_is_its_own_state(self):
        """「连上了但零工具」与「没连通」是两回事，不能塌成一句话。"""
        note = _render_mcp_capability_note([_server()], {"lab": []})
        assert "没有暴露任何工具" in note
        assert "本次未连通" not in note

    def test_two_servers_keep_their_own_state(self):
        """一个通一个不通时，两行各写各的 —— 状态不能串到隔壁。"""
        note = _render_mcp_capability_note(
            [_server("up"), _server("down")], {"up": ["t1"]}
        )
        # 只取 server 清单那一段（后面的用法规则也是 "- " 开头，别混进来）
        listing = note.split("动手之前先扫一眼")[0]
        up_line, down_line = [ln for ln in listing.splitlines() if ln.startswith("- ")]
        assert "t1" in up_line and "未连通" not in up_line
        assert "未连通" in down_line and "t1" not in down_line

    def test_requires_evidence_not_paraphrase(self):
        """「我调用成功了」是自述，原始返回才是证据。"""
        note = _render_mcp_capability_note([_server()], {"lab": ["a"]})
        assert "原始返回的逐字片段" in note

    def test_forbids_using_mcp_just_for_the_sake_of_it(self):
        """只写「要优先用」会诱发为用而用 —— 硬凑的调用会污染产物，比不用更糟。

        反向判据必须与正向判据同时在场，否则这一段会制造新的缺陷类别。
        """
        note = _render_mcp_capability_note([_server()], {"lab": ["a"]})
        assert "不要为了用而用" in note
        assert "照常用你自己的工具" in note


# ============================================================================
# 2. 没挂 MCP：字节逐字不变
# ============================================================================


class TestEmptyNoteChangesNothing:
    def test_prompt_is_not_padded(self, tmp_path):
        """空块必须被**过滤掉**，不能在 prompt 前多留空行。

        这是本次最容易踩的坑：顺手写成 f"{boundary}\\n\\n{note}\\n\\n{prompt}"，
        note 为空时就会多出两个换行 —— 而绝大多数角色都是 mcp: []。
        """
        sh = tmp_path / "workspace" / "shared"
        aicu = _make_aicu(tmp_path, shared_root=sh, shared_target=sh / "impl")
        assert aicu._mcp_capability_note == ""
        sends = _capture_sends(aicu)

        aicu._cc_chat_with_retry(aicu.ccs[0], "原始任务")

        sent = sends[aicu.ccs[0]][0]
        # 与加这段之前的拼法逐字一致
        assert sent == f"{aicu.ccs[0]._workspace_boundary}\n\n原始任务"

    def test_claude_md_has_no_mcp_section(self, tmp_path):
        """没挂 MCP 时 CLAUDE.md 里不得出现这一节。"""
        base = tmp_path / "workspace"
        node_ws = base / "nodes" / "n1"
        os.makedirs(node_ws, exist_ok=True)
        aicu = _make_aicu(tmp_path)

        claude_md = aicu.ccs[0].workspace / "CLAUDE.md"
        assert NOTE_HEAD not in claude_md.read_text(encoding="utf-8")


# ============================================================================
# 3. 挂了 MCP：真实注册表 + 真实预检走通全程
# ============================================================================


class TestRealAicuWithMcp:
    """用仓库自带的 echo-probe（真子进程、真 tools/list）跑一遍完整构造。

    这一组覆盖的是「接住 probe_servers 的返回值」那段 —— 纯函数测试覆盖不到它。
    """

    def test_scalar_server_name_is_not_split_into_characters(self, tmp_path):
        aicu = _make_aicu(tmp_path, mcp="echo-probe")

        assert aicu.mcp == ["echo-probe"]
        assert [server.name for server in aicu._mcp_servers] == ["echo-probe"]

    def test_non_list_mcp_config_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="mcp 配置"):
            _make_aicu(tmp_path, mcp={"name": "echo-probe"})

    def test_note_carries_real_tool_names(self, tmp_path):
        aicu = _make_aicu(tmp_path, mcp=["echo-probe"])

        assert NOTE_HEAD in aicu._mcp_capability_note
        assert "echo-probe" in aicu._mcp_capability_note
        assert "probe_echo" in aicu._mcp_capability_note      # tools/list 回来的真名
        assert "本次未连通" not in aicu._mcp_capability_note

    def test_claude_md_places_note_before_stable_rules(self, tmp_path):
        """夹在 role_md 与「长期生成规则」之间。

        排在稳定生成规则后面等于埋掉 —— Manager 那份有 300 行。
        """
        aicu = _make_aicu(tmp_path, mcp=["echo-probe"])
        text = (aicu.ccs[0].workspace / "CLAUDE.md").read_text(encoding="utf-8")

        assert NOTE_HEAD in text
        assert text.index(NOTE_HEAD) > text.index(aicu.role_md_content.strip()[:40])
        stable_head = "# 长期生成规则 (Stable Generation Rules)"
        if stable_head in text:
            assert text.index(NOTE_HEAD) < text.index(stable_head)

    def test_note_is_prefix_of_every_send(self, tmp_path):
        aicu = _make_aicu(tmp_path, mcp=["echo-probe"])
        sends = _capture_sends(aicu)

        aicu._cc_chat_with_retry(aicu.ccs[0], "原始任务")

        sent = sends[aicu.ccs[0]][0]
        assert sent.startswith(aicu.ccs[0]._workspace_boundary)
        assert NOTE_HEAD in sent
        assert sent.rstrip().endswith("原始任务")


class TestAicuMcpProbeStates:
    """AICU 构造期必须区分 configured / active / tools / failures。"""

    def test_optional_failure_stays_in_note_but_is_not_sent_to_cli(self, tmp_path, monkeypatch):
        active = _server("active")
        optional = _server("optional")
        monkeypatch.setattr(
            aicu_module,
            "resolve_servers",
            lambda names: [active, optional],
        )
        monkeypatch.setattr(
            aicu_module,
            "probe_servers_detailed",
            lambda servers: McpProbeReport(
                configured=[active, optional],
                active=[active],
                tools={"active": ["probe_echo"]},
                failures={"optional": "连接失败（测试）"},
            ),
        )

        built = _make_aicu(tmp_path, mcp=["active", "optional"])

        assert [server.name for server in built._mcp_servers] == ["active"]
        assert [server.name for server in built.ccs[0].mcp_servers] == ["active"]
        # mcp_flags() 是真实 CLI 下发入口；仅检查能力说明会漏掉 active 列表接错的回归。
        command = built.ccs[0].adapter.mcp_flags(built.ccs[0].mcp_servers)
        inline = next(arg for arg in command if arg.startswith("--mcp-config="))
        assert '"active"' in inline
        assert '"optional"' not in inline
        assert "active" in built._mcp_capability_note
        assert "probe_echo" in built._mcp_capability_note
        assert "optional" in built._mcp_capability_note
        assert "本次未连通" in built._mcp_capability_note

    def test_required_failure_still_aborts_before_cc_construction(self, tmp_path, monkeypatch):
        required = _server("required")
        monkeypatch.setattr(aicu_module, "resolve_servers", lambda names: [required])

        def fail(_servers):
            raise McpProbeError("required server failed")

        monkeypatch.setattr(aicu_module, "probe_servers_detailed", fail)
        with pytest.raises(McpProbeError, match="required server failed"):
            _make_aicu(tmp_path, mcp=["required"])

    def test_empty_tools_remains_active_and_is_not_reported_as_unreachable(
        self, tmp_path, monkeypatch
    ):
        empty = _server("empty")
        monkeypatch.setattr(aicu_module, "resolve_servers", lambda names: [empty])
        monkeypatch.setattr(
            aicu_module,
            "probe_servers_detailed",
            lambda servers: McpProbeReport(
                configured=[empty], active=[empty], tools={"empty": []}, failures={}
            ),
        )

        built = _make_aicu(tmp_path, mcp=["empty"])

        assert [server.name for server in built.ccs[0].mcp_servers] == ["empty"]
        assert "没有暴露任何工具" in built._mcp_capability_note
        assert "本次未连通" not in built._mcp_capability_note

    def test_mixed_empty_active_and_optional_failure_do_not_cross_contaminate(
        self, tmp_path, monkeypatch
    ):
        empty = _server("empty")
        active = _server("active")
        optional = _server("optional")
        configured = [empty, active, optional]
        monkeypatch.setattr(aicu_module, "resolve_servers", lambda names: configured)
        monkeypatch.setattr(
            aicu_module,
            "probe_servers_detailed",
            lambda servers: McpProbeReport(
                configured=configured,
                active=[empty, active],
                tools={"empty": [], "active": ["tool_a"]},
                failures={"optional": "只用于本测试"},
            ),
        )

        built = _make_aicu(tmp_path, mcp=[server.name for server in configured])
        listing = built._mcp_capability_note.split("动手之前先扫一眼")[0]
        lines = {
            line.split("（", 1)[0].removeprefix("- "): line
            for line in listing.splitlines()
            if line.startswith("- ")
        }
        assert "没有暴露任何工具" in lines["empty"]
        assert "tool_a" in lines["active"]
        assert "未连通" in lines["optional"]
        assert "tool_a" not in lines["optional"]
        assert "optional" not in [server.name for server in built.ccs[0].mcp_servers]


# ============================================================================
# 4. 投递：每一条发送路径都要带上（同形于 TestBoundaryIsDelivered）
# ============================================================================


class TestNoteIsDelivered:
    """四条路径各验一次。

    用直接赋值而不是真挂 MCP：这一组测的是**投递管道**（发送口拼不拼这个块），
    真实构造路径由 TestRealAicuWithMcp 覆盖，两边分工不重叠、也各自跑得快。
    """

    @staticmethod
    def _with_note(tmp_path):
        sh = tmp_path / "workspace" / "shared"
        aicu = _make_aicu(tmp_path, shared_root=sh, shared_target=sh / "impl")
        aicu._mcp_capability_note = f"{NOTE_HEAD}\n\n- lab：probe_echo"
        return aicu

    def test_generation_send_carries_note(self, tmp_path):
        aicu = self._with_note(tmp_path)
        sends = _capture_sends(aicu)

        aicu._cc_chat_with_retry(aicu.ccs[0], "原始任务")

        sent = sends[aicu.ccs[0]][0]
        assert sent.startswith(aicu.ccs[0]._workspace_boundary)   # 边界块仍在最前
        assert NOTE_HEAD in sent
        assert sent.rstrip().endswith("原始任务")

    def test_continuation_prompt_also_carries_note(self, tmp_path):
        """续跑只回 question + 输出契约，稳定层来自 CLAUDE.md —— 而 gemini 不读
        CLAUDE.md（见 test_workspace_boundary.py），所以发送口这份是它唯一的来源。"""
        aicu = self._with_note(tmp_path)
        sends = _capture_sends(aicu)

        prompt = aicu._build_generation_prompt("增量需求", incremental=True)
        aicu._cc_chat_with_retry(aicu.ccs[0], prompt)

        sent = sends[aicu.ccs[0]][0]
        assert NOTE_HEAD in sent
        assert "增量需求" in sent

    def test_retry_branch_keeps_note(self, tmp_path, monkeypatch):
        """读图恢复重试会**整体替换** prompt ⇒ 块必须挂在 cc.chat() 调用点。

        与边界块守的是同一个回归：加在函数顶部就静默丢了。
        """
        monkeypatch.setattr("time.sleep", lambda s: None)
        aicu = self._with_note(tmp_path)
        aicu.CC_MAX_RETRIES = 1

        cc = aicu.ccs[0]
        cc.conversation_id = "abc-123"      # 会话保留 → 走 resume 重试
        calls = []

        def _chat(prompt, max_turns=None, timeout=7200):
            calls.append(prompt)
            if len(calls) == 1:
                raise ModelImageInputError("Model do not support image input")
            return "recovered"

        cc.chat = _chat
        out = aicu._cc_chat_with_retry(cc, "原始任务")

        assert out == "recovered"
        assert len(calls) == 2
        assert NOTE_HEAD in calls[1]        # 被换成恢复指令后，块仍在最外层
