"""
集成测试桩 — 供 v2 Worker/Manager 单元测试使用

四种行为模式：
- SuccessStub：模拟 AICU.ask() 成功执行返回
- FailureStub：模拟 AICU.ask() 执行失败/异常返回
- TimeoutStub：模拟超时行为
- NonJsonOutputStub：模拟非标准 JSON 输出场景

用法：
    stub = SuccessStub(result_data={...})
    result = stub.ask("task description")  # 返回预设的成功结果

    stub = TimeoutStub(delay=10)
    try:
        result = stub.ask("task")  # 会阻塞 delay 秒后抛出 TimeoutError
    except TimeoutError:
        pass

所有 Stub 均实现统一的 StubProtocol（ask/question/chat 接口），
可替换 v2 Worker 中的 AICU 或 ClaudeCodeAgent 实例进行单元测试。
"""

import time
import json
from typing import Optional, Dict, Any, List
from pathlib import Path


class StubProtocol:
    """
    Stub 协议接口。

    所有 Stub 实现 ask() / chat() 方法，可互换使用。
    v2 Worker 单元测试时注入任意 Stub 替换真实的 AICU/ClaudeCodeAgent。
    """

    def ask(self, question: str, max_rounds: int = 3) -> dict:
        """模拟 AICU.ask()"""
        raise NotImplementedError

    def chat(self, prompt: str, max_turns: Optional[int] = None, timeout: int = 1800) -> str:
        """模拟 ClaudeCodeAgent.chat()"""
        raise NotImplementedError


# ============================================================
# SuccessStub
# ============================================================

class SuccessStub(StubProtocol):
    """
    模拟成功执行返回。

    ask() 返回预设的 result_dict（默认结构同 AICU.ask() 真实返回值）。
    chat() 返回预设的 JSON 字符串。
    """

    def __init__(self,
                 result_dict: Optional[dict] = None,
                 chat_text: Optional[str] = None,
                 delay: float = 0.0):
        """
        :param result_dict: ask() 返回的 dict，默认模拟 AICU 成功返回结构
        :param chat_text: chat() 返回的字符串，默认模拟 --output-format json 格式
        :param delay: 模拟延迟（秒），默认 0 无延迟
        """
        self._result_dict = result_dict or {
            "final_output_path": "/tmp/stub_output/solution.py",
            "scores": {0: 9.0, 1: 8.5},
            "avg_scores": {0: 8.75},
            "best_id": 0,
            "consensus_reached": True,
            "vote_rounds": 1,
            "audit_defects": [],
            "audit_passed": True,
            "strategy": "pair (2CC: 生产+审查)",
            "role_config": {
                "output_file": "solution.py",
                "main_doc": None,
                "index_file": "_INDEX.md",
                "adversarial_role": "严格的审查专家...",
                "audit_role": "深度审计专家...",
            },
        }
        self._chat_text = chat_text or json.dumps({
            "session_id": "stub-session-001",
            "result": "模拟成功响应",
            "subtype": None,
            "errors": [],
        })
        self._delay = delay
        self.call_count = 0
        self.last_question: Optional[str] = None
        self.last_prompt: Optional[str] = None

    def ask(self, question: str, max_rounds: int = 3) -> dict:
        """返回预设的成功 dict，记录调用信息"""
        self.call_count += 1
        self.last_question = question
        if self._delay > 0:
            time.sleep(self._delay)
        # 每次返回新副本，避免调用方修改影响后续
        return dict(self._result_dict)

    def chat(self, prompt: str, max_turns: Optional[int] = None, timeout: int = 1800) -> str:
        """返回预设的成功 JSON 字符串"""
        self.call_count += 1
        self.last_prompt = prompt
        if self._delay > 0:
            time.sleep(self._delay)
        return self._chat_text

    def reset(self):
        """重置调用计数和记录"""
        self.call_count = 0
        self.last_question = None
        self.last_prompt = None


# ============================================================
# FailureStub
# ============================================================

class FailureStub(StubProtocol):
    """
    模拟执行失败/异常返回。

    ask() / chat() 抛出预设的异常，用于测试调用方的异常处理逻辑。
    """

    def __init__(self,
                 exception: Optional[Exception] = None,
                 fail_on_call: int = 1,
                 delay: float = 0.0):
        """
        :param exception: 要抛出的异常，默认 RuntimeError("CLI 执行失败 (returncode=1)")
        :param fail_on_call: 第几次调用时失败（从 1 开始），默认 1 即第一次调用就失败
        :param delay: 抛出异常前的模拟延迟（秒）
        """
        self._exception = exception or RuntimeError("CLI 执行失败 (returncode=1): mock stderr output")
        self._fail_on_call = fail_on_call
        self._delay = delay
        self.call_count = 0
        self.last_question: Optional[str] = None
        self.last_prompt: Optional[str] = None

    def ask(self, question: str, max_rounds: int = 3) -> dict:
        self.call_count += 1
        self.last_question = question
        if self._delay > 0:
            time.sleep(self._delay)
        if self.call_count >= self._fail_on_call:
            raise self._exception
        # fail_on_call > 1 时，前面的调用返回成功
        return {
            "final_output_path": "/tmp/stub_output/solution.py",
            "scores": {0: 7.0},
            "avg_scores": {0: 7.0},
            "best_id": 0,
            "consensus_reached": True,
            "vote_rounds": 1,
            "audit_defects": [],
            "audit_passed": True,
            "strategy": "solo (1CC: 生成+自审)",
            "role_config": {},
        }

    def chat(self, prompt: str, max_turns: Optional[int] = None, timeout: int = 1800) -> str:
        self.call_count += 1
        self.last_prompt = prompt
        if self._delay > 0:
            time.sleep(self._delay)
        if self.call_count >= self._fail_on_call:
            raise self._exception
        return '{"session_id": "pre-fail", "result": "ok", "subtype": null, "errors": []}'

    def reset(self):
        """重置调用计数，避免跨用例累积"""
        self.call_count = 0
        self.last_question = None
        self.last_prompt = None


# ============================================================
# TimeoutStub
# ============================================================

class TimeoutStub(StubProtocol):
    """
    模拟超时行为。

    ask() / chat() 会阻塞超过指定 timeout 后抛出 TimeoutError，
    用于测试调用方的超时处理逻辑。
    """

    class TimeoutError(Exception):
        """模拟超时异常"""
        def __init__(self, timeout: int):
            self.timeout = timeout
            super().__init__(f"超时（>{timeout}s），已 kill 整个进程组")

    def __init__(self,
                 block_duration: float = 5.0,
                 timeout: int = 3,
                 fail_on_call: int = 1):
        """
        :param block_duration: 阻塞时长（秒），超过 timeout 后抛出 TimeoutError
        :param timeout: 调用方设置的超时阈值（秒），用于异常消息
        :param fail_on_call: 第几次调用触发超时（从 1 开始）
        """
        self._block_duration = block_duration
        self._timeout = timeout
        self._fail_on_call = fail_on_call
        self.call_count = 0
        self.last_question: Optional[str] = None
        self.last_prompt: Optional[str] = None

    def _raise_timeout(self):
        """统一的超时抛出逻辑，使用 stub 配置的 self._timeout"""
        time.sleep(self._block_duration)
        raise self.TimeoutError(self._timeout)

    def ask(self, question: str, max_rounds: int = 3) -> dict:
        self.call_count += 1
        self.last_question = question
        if self.call_count >= self._fail_on_call:
            self._raise_timeout()
        return {
            "final_output_path": "/tmp/stub_output/solution.py",
            "scores": {0: 7.0},
            "avg_scores": {0: 7.0},
            "best_id": 0,
            "consensus_reached": True,
            "vote_rounds": 1,
            "audit_defects": [],
            "audit_passed": True,
            "strategy": "solo (1CC: 生成+自审)",
            "role_config": {},
        }

    def chat(self, prompt: str, max_turns: Optional[int] = None, timeout: int = 1800) -> str:
        self.call_count += 1
        self.last_prompt = prompt
        if self.call_count >= self._fail_on_call:
            self._raise_timeout()
        return '{"session_id": "pre-timeout", "result": "ok", "subtype": null, "errors": []}'

    def reset(self):
        """重置调用计数"""
        self.call_count = 0
        self.last_question = None
        self.last_prompt = None


# ============================================================
# NonJsonOutputStub
# ============================================================

class NonJsonOutputStub(StubProtocol):
    """
    模拟非标准 JSON 输出场景。

    chat() 返回非 JSON 文本（Markdown、错误栈、截断 JSON 等），
    ask() 返回格式异常的 dict（缺失字段、额外字段、None 等），
    用于测试调用方的容错处理逻辑。
    """

    # 预设的异常输出模式
    OUTPUT_MARKDOWN = """\
我来分析这个需求：

首先，我们需要理解系统的核心功能模块：
1. 用户认证模块
2. 数据处理模块
3. 结果显示模块

这是初步的设计思路。"""
    OUTPUT_ERROR_STACK = """\
Error: ENOENT: no such file or directory, open '/path/to/nonexistent/file'
    at Object.openSync (fs.js:462:3)
    at Object.readFileSync (fs.js:470:35)
    at ..."""
    OUTPUT_TRUNCATED_JSON = """\
{"session_id": "abc-123", "result": "这里是一个非常长的响应...（被截断）"""
    OUTPUT_PARTIAL_JSON = """\
一些前置日志信息...
{"session_id": "abc-123", "result": "响应内容"}
一些后置日志信息..."""

    def __init__(self,
                 output_mode: str = "markdown",
                 chat_text: Optional[str] = None,
                 result_dict: Optional[dict] = None,
                 delay: float = 0.0):
        """
        :param output_mode: 预设输出模式—
            "markdown"：返回纯 Markdown 文本（默认）
            "error_stack"：返回错误栈文本
            "truncated_json"：返回截断的 JSON
            "partial_json"：返回带前后日志的 JSON
            "custom"：使用 chat_text / result_dict 自定义
        :param chat_text: ask() 产生的 chat 返回文本（覆盖预设）
        :param result_dict: ask() 返回的异常 dict（覆盖预设）
        :param delay: 模拟延迟（秒）
        """
        self._output_mode = output_mode
        self._chat_text = chat_text
        self._result_dict = result_dict
        self._delay = delay
        self.call_count = 0
        self.last_question: Optional[str] = None
        self.last_prompt: Optional[str] = None

    def _get_chat_text(self) -> str:
        """根据 mode 返回 chat 文本"""
        if self._chat_text is not None:
            return self._chat_text
        mode_map = {
            "markdown": self.OUTPUT_MARKDOWN,
            "error_stack": self.OUTPUT_ERROR_STACK,
            "truncated_json": self.OUTPUT_TRUNCATED_JSON,
            "partial_json": self.OUTPUT_PARTIAL_JSON,
        }
        return mode_map.get(self._output_mode, self.OUTPUT_MARKDOWN)

    def _get_result_dict(self) -> dict:
        """根据 mode 返回 ask() 的异常 dict"""
        if self._result_dict is not None:
            return self._result_dict
        if self._output_mode == "truncated_json":
            return {
                "final_output_path": "/tmp/stub_output/solution.py",
                "scores": {0: 9.0},
                # 缺少 consensus_reached / audit_passed 等字段
            }
        elif self._output_mode == "error_stack":
            return {
                "final_output_path": "/tmp/stub_output/solution.py",
                "scores": None,  # 异常的 None 值
                "avg_scores": None,
                "best_id": -1,
                "consensus_reached": False,
                "vote_rounds": 0,
                "audit_defects": "CORRUPTED_DATA",  # 类型异常（应该是 list）
                "audit_passed": False,
                "strategy": "unknown",
                "role_config": {},
            }
        else:
            return {
                "final_output_path": "/tmp/stub_output/solution.py",
                "scores": {0: 9.0},
                "avg_scores": {0: 9.0},
                "best_id": 0,
                "consensus_reached": True,
                "vote_rounds": 1,
                "audit_defects": [],
                "audit_passed": True,
                "strategy": "solo (1CC: 生成+自审)",
                "role_config": {},
            }

    def ask(self, question: str, max_rounds: int = 3) -> dict:
        self.call_count += 1
        self.last_question = question
        if self._delay > 0:
            time.sleep(self._delay)
        return dict(self._get_result_dict())

    def chat(self, prompt: str, max_turns: Optional[int] = None, timeout: int = 1800) -> str:
        self.call_count += 1
        self.last_prompt = prompt
        if self._delay > 0:
            time.sleep(self._delay)
        return self._get_chat_text()

    def reset(self):
        """重置调用计数和记录"""
        self.call_count = 0
        self.last_question = None
        self.last_prompt = None
