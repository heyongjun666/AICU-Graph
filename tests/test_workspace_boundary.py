"""工作区域边界（前置约束）的单元测试。

背景：AICU 起 CC 子进程时只给 `--allowedTools Read Write Edit Bash
--permission-mode dontAsk`（cli_adapters.py:369-374）—— 这是**工具**白名单，
不是**路径**白名单。实测会话 0db3c675 的 14 个 CC 有 980 次 tool_use 落在会话目录之外。

因此 AICU 在**唯一**的发送口 `_cc_chat_with_retry` 给每次调用前置一段
「你只能访问这些路径」的硬约束块（`AICU._build_workspace_boundary`）。

诚实说明本文件的边界：提示词级改动**无法被单测证明改变了模型行为**。
这里只能证明块被正确构造、并且在**每一条**发送路径上都被投递 ——
后者才是本次最容易做漏的地方（生成阶段覆盖了、重试分支漏了）。
"""

import os

from aicu import AICU
from claude_code_agent import ModelImageInputError



# ============================================================================
# 构造助手
# ============================================================================


def _make_aicu(tmp_path, role="code", shared_root=None,
               shared_target=None, role_type="worker"):
    """离线构造带真实 Session Shared Workspace 布局的 AICU。

    磁盘布局（与 runtime_sessions/<sid>/workspace/ 一致）：
        <base>/nodes/<id>/    ← parent_workspace（内含 cc_0/cc_1）
        <base>/shared/        ← shared_root
        <base>/shared/<分区>  ← shared_target
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
        shared_root=str(shared_root) if shared_root else None,
        shared_target=str(shared_target) if shared_target else None,
    )


def _capture_sends(aicu):
    """把每个 CC 的 `chat` 换成记录器，返回 {cc: [实际送出的 prompt, ...]}。

    注意**不能** monkeypatch `_cc_chat_with_retry` —— 边界块就加在它内部，
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


# ============================================================================
# 1. 块内容
# ============================================================================


class TestBoundaryBlockContent:
    def test_lists_shared_root_partition_and_own_dir(self, tmp_path):
        sh = tmp_path / "workspace" / "shared"
        aicu = _make_aicu(tmp_path, shared_root=sh, shared_target=sh / "implementation")
        block = aicu.ccs[0]._workspace_boundary

        # 三个绝对路径都在，且都是 realpath（macOS 上 /tmp → /private/tmp）
        assert os.path.realpath(str(sh)) in block
        assert os.path.realpath(str(sh / "implementation")) in block
        assert os.path.realpath(str(aicu.ccs[0].workspace)) in block
        # 硬约束措辞：禁止范围必须点名，不能只说"你的工作区在哪"
        assert "宿主仓库源码" in block
        assert "用户主目录" in block
        assert "不构成许可" in block

    def test_only_user_input_is_an_escape_hatch(self, tmp_path):
        """唯一例外必须是「用户自己的输入」点名，不能留自主取用的口子。"""
        sh = tmp_path / "workspace" / "shared"
        aicu = _make_aicu(tmp_path, shared_root=sh, shared_target=sh / "impl")
        block = aicu.ccs[0]._workspace_boundary

        assert "用户自己的输入" in block
        assert "此外没有别的豁免" in block
        # 反向：不得出现"先摸清背景""自行获取"这类**许可式**表述。
        # 注意不能用裸子串"自行去范围外寻找" —— 块里那句是它的否定式
        # 「不要…自行去范围外寻找」，裸子串会把自己的正确文本判为违规。
        for bad in ("可以自主访问", "必要时可读取外部", "可自行访问共享区之外",
                    "先摸清背景"):
            assert bad not in block

    def test_no_halfwidth_brace_residue(self, tmp_path):
        """prompt 末尾要求"只输出 JSON"，块里混入 { } 会污染输出契约。"""
        sh = tmp_path / "workspace" / "shared"
        for kwargs in (
            dict(shared_root=sh, shared_target=sh / "impl"),
            dict(shared_root=sh),
            dict(),                      # Router 形态
        ):
            block = _make_aicu(tmp_path, **kwargs).ccs[0]._workspace_boundary
            assert "{" not in block and "}" not in block
            # 也不该有未替换的占位符
            assert "{cc_dir}" not in block
            assert ".." not in block

    def test_degraded_form_still_forbids(self, tmp_path):
        """Router CC 形态（无共享区）→ 不列共享区行，但禁止句必须在。

        降级不等于放弃约束：没有共享区只是少一行路径，禁止范围照旧。
        """
        aicu = _make_aicu(tmp_path, role_type="system_role", role="router")
        block = aicu.ccs[0]._workspace_boundary

        assert "会话共享区" not in block
        assert str(aicu.ccs[0].workspace) in block
        assert "只能" in block and "不得读取" in block

    def test_manager_form_has_root_without_partition(self, tmp_path):
        """Manager 节点 shared_target 为 None，但有 _shared_workspace。

        两者分离传参正是为了这个不对称：Manager 不该被声明为"负责写某分区",
        但它必须知道自己能碰共享区。
        """
        sh = tmp_path / "workspace" / "shared"
        aicu = _make_aicu(tmp_path, role_type="manager", role="default",
                          shared_root=sh)
        block = aicu.ccs[0]._workspace_boundary

        assert os.path.realpath(str(sh)) in block
        assert "专属分区" not in block
        assert "不得读取" in block


# ============================================================================
# 2. 投递：每一条发送路径都要带上块
# ============================================================================


class TestBoundaryIsDelivered:
    def test_generation_send_is_prefixed(self, tmp_path):
        sh = tmp_path / "workspace" / "shared"
        aicu = _make_aicu(tmp_path, shared_root=sh, shared_target=sh / "impl")
        sends = _capture_sends(aicu)

        aicu._cc_chat_with_retry(aicu.ccs[0], "原始任务")

        sent = sends[aicu.ccs[0]][0]
        assert sent.startswith(aicu.ccs[0]._workspace_boundary)
        assert sent.rstrip().endswith("原始任务")

    def test_continuation_prompt_also_carries_block(self, tmp_path, monkeypatch):
        """续跑（is_continuation）只回 question + 输出契约，稳定层来自 CLAUDE.md。

        gemini 不读 CLAUDE.md ⇒ 续跑会丢稳定层。块挂在发送口而非
        `_build_generation_prompt` 里，正好把这条缺口一起补掉。
        """
        sh = tmp_path / "workspace" / "shared"
        aicu = _make_aicu(tmp_path, shared_root=sh, shared_target=sh / "impl")
        sends = _capture_sends(aicu)

        prompt = aicu._build_generation_prompt("增量需求", incremental=True)
        aicu._cc_chat_with_retry(aicu.ccs[0], prompt)

        assert sends[aicu.ccs[0]][0].startswith(aicu.ccs[0]._workspace_boundary)
        assert "增量需求" in sends[aicu.ccs[0]][0]

    def test_retry_branch_keeps_block(self, tmp_path, monkeypatch):
        """读图恢复重试会**整体替换** prompt ⇒ 块必须挂在 cc.chat() 调用点。

        若把块加在函数顶部加一次，这里就会静默丢掉 —— 这条测试专门守这个回归。
        """
        monkeypatch.setattr("time.sleep", lambda s: None)
        sh = tmp_path / "workspace" / "shared"
        aicu = _make_aicu(tmp_path, shared_root=sh, shared_target=sh / "impl")
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
        # 重试那次的 prompt 已被换成恢复指令 —— 但块仍在最外层
        assert calls[1].startswith(cc._workspace_boundary)
        assert aicu._IMAGE_INPUT_RECOVERY_PROMPT in calls[1]

    def test_emit_activity_does_not_report_boundary_text(self, tmp_path):
        """活动流只该显示真实任务，不能把 519 字的边界块当活动内容刷屏。"""
        sh = tmp_path / "workspace" / "shared"
        aicu = _make_aicu(tmp_path, shared_root=sh, shared_target=sh / "impl")
        _capture_sends(aicu)

        seen = []
        aicu.output_callback = lambda kind, payload: seen.append((kind, payload))
        aicu._cc_chat_with_retry(aicu.ccs[0], "原始任务")

        blob = str(seen)
        assert "原始任务" in blob
        assert "工作区边界" not in blob


# ============================================================================
# 3. 向后兼容：没有块的环境必须行为不变
# ============================================================================


class TestBackwardCompatibility:
    def test_new_shape_aicu_sends_prompt_unchanged(self, monkeypatch):
        """`AICU.__new__` 形态（多个既有测试这么建）没有该属性 → 不加块、不报错。"""
        monkeypatch.setattr("time.sleep", lambda s: None)
        aicu = AICU.__new__(AICU)
        aicu.output_callback = None
        aicu.CC_MAX_RETRIES = 1

        calls = []

        class _CC:
            conversation_id = None

            def chat(self, prompt, max_turns=None, timeout=7200):
                calls.append(prompt)
                return "ok"

            def reset_conversation(self):
                self.conversation_id = None

        assert aicu._cc_chat_with_retry(_CC(), "普通任务") == "ok"
        assert calls[-1] == "普通任务"       # 逐字不变

    def test_router_cc_constructed_without_shared_root(self):
        """runtime/router.py 不传 shared_root → 必须能正常构造（降级版）。"""
        import inspect
        from aicu import AICU as _A

        sig = inspect.signature(_A.__init__)
        assert sig.parameters["shared_root"].default is None
