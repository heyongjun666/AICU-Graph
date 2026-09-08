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
from aicu import AICU


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
        assert cc.calls[1][1] == aicu._IMAGE_INPUT_RECOVERY_PROMPT

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
