"""
Tests for executables/worker.py — Worker 执行单元（双模式）

测试策略：
- Slice 1: Worker 构造与身份（Executable 子类）
- Slice 2: Stub 成功路径（SuccessStub → COMPLETED + artifact_path）
- Slice 3: Stub 失败路径（FailureStub → FAILED + ErrorObject）
- Slice 4: Stub 超时路径（TimeoutStub → FAILED + ErrorObject）
- Slice 5: 真实模式（mock AICU → COMPLETED/FAILED）
- Slice 6: 日志推送（LogEntry 写入 node.log）
"""

import json
import os
import shutil
import time
from datetime import datetime
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from claude_code_agent import ClaudeCodeAgent
from executables.base import Executable, ExecutableContext
import aicu as aicu_module
import executables.worker as worker_module
from executables.worker import Worker
from runtime.models import Node, NodeStatus, ErrorObject, LogEntry, LogLevel
from tests.integration_stubs import (
    SuccessStub,
    FailureStub,
    TimeoutStub,
    NonJsonOutputStub,
)


# ============================================================================
# Helper: MockRuntimeGateway
# ============================================================================

class MockRuntimeGateway:
    """RuntimeGateway 协议的模拟实现，捕获所有状态更新调用。"""

    def __init__(self):
        self.status_updates: list[tuple[str, NodeStatus, dict]] = []
        self.nodes: dict[str, Node] = {}

    def update_node_status(self, node_id: str, status: NodeStatus, **updates: Any) -> None:
        self.status_updates.append((node_id, status, updates))

    def submit_plan(self, node_id: str, plan: Any) -> None:
        pass

    def get_node(self, node_id: str) -> Optional[Node]:
        return self.nodes.get(node_id)

    def get_children(self, node_id: str) -> list:
        return []

    def read_artifact(self, node_id: str, path: str) -> str:
        return ""

    def get_node_workspace(self, node_id: str) -> str:
        return f"/tmp/workspace/{node_id}"

    def append_node_log(self, node_id: str, level: Any, message: str) -> None:
        self.status_updates.append((node_id, NodeStatus.RUNNING, {
            "log_level": level,
            "log_message": message,
        }))

    def register_external_input_wait(self, node_id: str, input_schema: Optional[dict] = None) -> str:
        return "token"

    def deliver_external_input(self, wait_token: str, input_data: Any) -> None:
        pass


# ============================================================================
# Helper: MockAICU
# ============================================================================

class MockAICU:
    """AICU 模拟，用于测试真实模式。"""

    def __init__(self, result: Optional[dict] = None, fail: bool = False):
        self.result = result or {"final_output_path": "/tmp/real_output.py"}
        self.fail = fail
        self.call_count = 0
        self.last_question: Optional[str] = None

    def ask(self, question: str, max_rounds: int = 3) -> dict:
        self.call_count += 1
        self.last_question = question
        if self.fail:
            raise RuntimeError("AICU execution error: mock failure")
        return dict(self.result)


# ============================================================================
# Helper factories
# ============================================================================

def _make_node(**overrides) -> Node:
    defaults = dict(
        id="worker-n1",
        exec_type="worker",
        role="dev",
        task="Write a Python function to add two numbers",
        config={},
        status=NodeStatus.READY,
    )
    defaults.update(overrides)
    return Node(**defaults)


def _make_context(node: Optional[Node] = None,
                  runtime: Optional[MockRuntimeGateway] = None,
                  config: Optional[dict] = None) -> ExecutableContext:
    return ExecutableContext(
        node=node or _make_node(),
        runtime=runtime or MockRuntimeGateway(),
        config=config or {},
    )



# ============================================================================
# AICU 构造期失败的回填（_fail_startup）
# ============================================================================

class TestStartupFailure:
    """构造 AICU 失败必须落到节点上，而不是穿到 scheduler。

    `_create_real_aicu` 在 `Worker.run()` 的 try 之外调用。改动前它一旦抛异常，
    异常会直接上抛、被 scheduler 的 wrapper 收成一行服务端 stdout 日志，节点上只剩
    「Failed / 日志空 / error 空」—— 而 MCP 预检连不上、skills_dir 不存在、角色 yaml
    非法这些恰恰都要用户去改环境才能修，看不见原因就等于修不了。
    """

    def _explode(self, monkeypatch, exc: Exception):
        def _boom(_ctx):
            raise exc

        monkeypatch.setattr(worker_module, "_create_real_aicu", _boom)

    def test_returns_failed_instead_of_raising(self, monkeypatch):
        runtime = MockRuntimeGateway()
        ctx = _make_context(runtime=runtime)
        self._explode(monkeypatch, RuntimeError("MCP server 'x' 预检失败"))

        assert Worker(ctx).run(ctx) == NodeStatus.FAILED

    def test_log_carries_the_reason(self, monkeypatch):
        """日志里必须有原始异常文本 —— 这是用户唯一能看到的诊断信息。"""
        runtime = MockRuntimeGateway()
        ctx = _make_context(runtime=runtime)
        self._explode(
            monkeypatch,
            RuntimeError("MCP server 'echo-probe' 启动后立刻退出（命令: /nope）"),
        )

        assert Worker(ctx).run(ctx) == NodeStatus.FAILED

        node_id, status, updates = runtime.status_updates[-1]
        assert node_id == ctx.node.id
        assert status == NodeStatus.FAILED
        assert updates["error"] is not None
        assert len(updates["log"]) == 1
        entry = updates["log"][0]
        assert entry.level == LogLevel.ERROR
        assert "Worker startup failed" in entry.message
        assert "启动后立刻退出" in entry.message

    def test_exception_supplied_error_is_kept(self, monkeypatch):
        """异常自带 ErrorObject 时沿用，与执行期失败的兜底形状保持一致。"""
        runtime = MockRuntimeGateway()
        ctx = _make_context(runtime=runtime)
        exc = RuntimeError("boom")
        exc.error = ErrorObject.create_worker_execution_error()
        self._explode(monkeypatch, exc)

        assert Worker(ctx).run(ctx) == NodeStatus.FAILED
        assert runtime.status_updates[-1][2]["error"] is exc.error

    def test_backfill_failure_still_returns_failed(self, monkeypatch):
        """回填本身抛异常也不能再往上冒，否则又退回「日志为空、原因不可见」。"""
        class _BrokenGateway(MockRuntimeGateway):
            def update_node_status(self, *args, **kwargs):
                raise RuntimeError("runtime 已断开")

        ctx = _make_context(runtime=_BrokenGateway())
        self._explode(monkeypatch, RuntimeError("原始构造失败"))

        assert Worker(ctx).run(ctx) == NodeStatus.FAILED

# ============================================================================
# Stream output formatting
# ============================================================================

class TestStreamOutputFormatting:
    def test_assistant_stream_event_uses_real_newlines(self):
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


# ============================================================================
# Slice 1: Worker 构造与身份
# ============================================================================

class TestWorkerConstruction:
    """Worker 应正确继承 Executable 并响应生命周期方法。"""

    def test_worker_is_executable_subclass(self):
        """Worker 是 Executable 的非抽象子类"""
        assert issubclass(Worker, Executable)

    def test_worker_can_be_instantiated(self):
        """Worker 可正常实例化"""
        ctx = _make_context()
        worker = Worker(ctx, aicu_instance=SuccessStub())
        assert isinstance(worker, Worker)
        assert isinstance(worker, Executable)

    def test_worker_has_all_abstract_methods(self):
        """Worker 实现 Executable 的抽象方法 run()"""
        assert hasattr(Worker, "run")
        assert callable(Worker.run)


# ============================================================================
# Slice 2: Stub 成功路径
# ============================================================================

class TestStubSuccessPath:
    """Stub 模式下 SuccessStub → COMPLETED + artifact_path 回填。"""

    def test_stub_success_returns_completed(self):
        """SuccessStub 成功后返回 COMPLETED 状态"""
        runtime = MockRuntimeGateway()
        stub = SuccessStub(result_dict={"final_output_path": "/tmp/stub/output.py"})
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        result = worker.run(ctx)
        assert result == NodeStatus.COMPLETED

    def test_stub_success_updates_node_status(self):
        """SuccessStub 成功后通过 RuntimeGateway 更新状态"""
        runtime = MockRuntimeGateway()
        stub = SuccessStub(result_dict={"final_output_path": "/tmp/stub/output.py"})
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        worker.run(ctx)
        assert len(runtime.status_updates) >= 1
        last_node_id, last_status, last_updates = runtime.status_updates[-1]
        assert last_status == NodeStatus.COMPLETED

    def test_stub_success_sets_artifact_path(self):
        """SuccessStub 成功后 artifact_path 被回填"""
        runtime = MockRuntimeGateway()
        stub = SuccessStub(result_dict={"final_output_path": "/tmp/stub/output.py"})
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        worker.run(ctx)
        _, _, updates = runtime.status_updates[-1]
        assert updates.get("artifact_path") == "/tmp/stub/output.py"

    def test_stub_success_default_result_dict(self):
        """SuccessStub 使用默认 result_dict 也能成功"""
        runtime = MockRuntimeGateway()
        stub = SuccessStub()
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        result = worker.run(ctx)
        assert result == NodeStatus.COMPLETED
        _, _, updates = runtime.status_updates[-1]
        assert updates.get("artifact_path") == "/tmp/stub_output/solution.py"

    def test_stub_success_routes_via_config_mode(self):
        """mode 可通过 ctx.config 设定"""
        runtime = MockRuntimeGateway()
        stub = SuccessStub(result_dict={"final_output_path": "/tmp/stub/output.py"})
        ctx = _make_context(runtime=runtime, config={})
        worker = Worker(ctx, aicu_instance=stub)
        result = worker.run(ctx)
        assert result == NodeStatus.COMPLETED


# ============================================================================
# Slice 3: Stub 失败路径
# ============================================================================

class TestStubFailurePath:
    """Stub 模式下 FailureStub → FAILED + ErrorObject 回填。"""

    def test_stub_failure_returns_failed(self):
        """FailureStub 失败后返回 FAILED 状态"""
        runtime = MockRuntimeGateway()
        stub = FailureStub()
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        result = worker.run(ctx)
        assert result == NodeStatus.FAILED

    def test_stub_failure_updates_node_status(self):
        """FailureStub 失败后通过 RuntimeGateway 更新状态为 FAILED"""
        runtime = MockRuntimeGateway()
        stub = FailureStub()
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        worker.run(ctx)
        assert len(runtime.status_updates) >= 1
        last_node_id, last_status, last_updates = runtime.status_updates[-1]
        assert last_status == NodeStatus.FAILED

    def test_stub_failure_sets_error_object(self):
        """FailureStub 失败后 error 为 ErrorObject 实例"""
        runtime = MockRuntimeGateway()
        stub = FailureStub()
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        worker.run(ctx)
        _, _, updates = runtime.status_updates[-1]
        error = updates.get("error")
        assert error is not None
        assert isinstance(error, ErrorObject)
        assert "Worker execution failed" in error.message
        assert error.cause
        assert error.suggestion

    def test_stub_failure_custom_exception(self):
        """FailureStub 使用自定义异常时 error 仍为 ErrorObject"""
        runtime = MockRuntimeGateway()
        stub = FailureStub(exception=ValueError("custom error"))
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        result = worker.run(ctx)
        assert result == NodeStatus.FAILED
        _, _, updates = runtime.status_updates[-1]
        error = updates.get("error")
        assert isinstance(error, ErrorObject)

    def test_stub_failure_on_later_call(self):
        """fail_on_call=1 时第一次调用失败（Worker 每次 run 只调用一次）"""
        runtime = MockRuntimeGateway()
        stub = FailureStub(fail_on_call=1)
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        result = worker.run(ctx)
        assert result == NodeStatus.FAILED


# ============================================================================
# Slice 4: Stub 超时路径
# ============================================================================

class TestStubTimeoutPath:
    """Stub 模式下 TimeoutStub → FAILED + ErrorObject。"""

    def test_stub_timeout_returns_failed(self):
        """TimeoutStub 超时后返回 FAILED 状态"""
        runtime = MockRuntimeGateway()
        stub = TimeoutStub(block_duration=0.05, timeout=1)
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        result = worker.run(ctx)
        assert result == NodeStatus.FAILED

    def test_stub_timeout_sets_error_object(self):
        """TimeoutStub 超时后 error 为 ErrorObject"""
        runtime = MockRuntimeGateway()
        stub = TimeoutStub(block_duration=0.05, timeout=1)
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        worker.run(ctx)
        _, _, updates = runtime.status_updates[-1]
        error = updates.get("error")
        assert error is not None
        assert isinstance(error, ErrorObject)

    def test_stub_timeout_fail_on_call_1(self):
        """fail_on_call=1 时第一次调用就超时"""
        runtime = MockRuntimeGateway()
        stub = TimeoutStub(block_duration=0.05, timeout=1, fail_on_call=1)
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        result = worker.run(ctx)
        assert result == NodeStatus.FAILED


# ============================================================================
# Slice 5: 真实模式（mock AICU）
# ============================================================================

class TestRealMode:
    """真实模式通过 MockAICU 验证成功/失败路径。"""

    def test_real_aicu_receives_stream_callback_and_node_config(self, monkeypatch):
        captured = {}

        class CapturingAICU:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        runtime = MockRuntimeGateway()
        node = _make_node(config={
            "model": "claude-sonnet-4-6",
            "cli_name": "claude",
            "api_base_url": "https://api.example.test",
            "api_key_env": "TEST_API_KEY",
            "skills": ["review"],
            "skills_dir": "/tmp/skills",
            "skip_permissions": False,
            # 单节点 agent 数量策略钉死后的值（runtime._apply_num_ccs_policy 写入）
            "num_ccs": 1,
        })
        ctx = _make_context(node=node, runtime=runtime)
        monkeypatch.setattr(aicu_module, "AICU", CapturingAICU)
        Worker._aicu_cache.pop(node.id, None)

        aicu = worker_module._create_real_aicu(ctx)

        assert aicu is not None
        assert captured["model"] == "claude-sonnet-4-6"
        assert captured["cli_name"] == "claude"
        assert captured["api_base_url"] == "https://api.example.test"
        assert captured["api_key_env"] == "TEST_API_KEY"
        assert captured["skills"] == ["review"]
        assert captured["skills_dir"] == "/tmp/skills"
        assert captured["skip_permissions"] is False
        assert captured["num_ccs"] == 1

        captured["output_callback"]("stdout", "incremental output")
        assert runtime.status_updates[-1][2]["log_message"] == "incremental output"
        assert runtime.status_updates[-1][2]["log_level"] == LogLevel.INFO
        Worker._aicu_cache.pop(node.id, None)

    def test_real_mode_success(self):
        """真实模式 AICU 成功→COMPLETED"""
        runtime = MockRuntimeGateway()
        aicu = MockAICU(result={"final_output_path": "/tmp/real/output.py"})
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=aicu)
        result = worker.run(ctx)
        assert result == NodeStatus.COMPLETED
        _, _, updates = runtime.status_updates[-1]
        assert updates.get("artifact_path") == "/tmp/real/output.py"
        assert aicu.call_count == 1

    def test_real_mode_failure(self):
        """真实模式 AICU 失败→FAILED + ErrorObject"""
        runtime = MockRuntimeGateway()
        aicu = MockAICU(fail=True)
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=aicu)
        result = worker.run(ctx)
        assert result == NodeStatus.FAILED
        _, _, updates = runtime.status_updates[-1]
        error = updates.get("error")
        assert isinstance(error, ErrorObject)
        assert aicu.call_count == 1

    def test_image_input_error_fails_honestly(self):
        """『模型不支持图片输入』报错不再被跳过：重试耗尽后如实标记 FAILED + ErrorObject。

        跳过逻辑已移除：图片输入失败属于执行失败，与普通错误一致落到 FAILED，
        不再静默标记 COMPLETED 掩盖缺失的产出。
        """
        runtime = MockRuntimeGateway()

        class ImageErrorAICU(MockAICU):
            def ask(self, question: str, max_rounds: int = 3) -> dict:
                self.call_count += 1
                self.last_question = question
                raise RuntimeError(
                    "internal 执行失败 (returncode=1; stdout_tail=... "
                    "API Error: 400 Model do not support image input. Request id: ... ; stderr=...)"
                )

        aicu = ImageErrorAICU()
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=aicu)
        result = worker.run(ctx)

        assert result == NodeStatus.FAILED
        _, status, updates = runtime.status_updates[-1]
        assert status == NodeStatus.FAILED
        assert isinstance(updates.get("error"), ErrorObject)
        # 真实失败细节（含图片输入签名）保留在日志中，未再被静默吞掉
        log = updates.get("log") or []
        assert log and log[0].level == LogLevel.ERROR
        assert "support image input" in log[0].message

    def test_real_mode_non_image_error_not_swallowed(self):
        """非图片输入错误不被跳过逻辑吞掉，仍标记 FAILED + ErrorObject"""
        runtime = MockRuntimeGateway()
        aicu = MockAICU(fail=True)  # raises "AICU execution error: mock failure"
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=aicu)
        result = worker.run(ctx)
        assert result == NodeStatus.FAILED
        _, status, updates = runtime.status_updates[-1]
        assert status == NodeStatus.FAILED
        assert isinstance(updates.get("error"), ErrorObject)

    def test_real_mode_with_config(self):
        """真实模式可通过 ctx.config 传递 mode"""
        runtime = MockRuntimeGateway()
        aicu = MockAICU()
        ctx = _make_context(runtime=runtime, config={"mode": "real"})
        worker = Worker(ctx, aicu_instance=aicu)
        result = worker.run(ctx)
        assert result == NodeStatus.COMPLETED

    def test_real_mode_passes_task_to_aicu(self):
        """真实模式将 node.task 传递给 AICU.ask()（task 原样出现在 question 中）。

        方案三：发布默认开启 → 每次 ask 的 question 都带事件发布上下文前缀，
        task 作为「## 你的任务」主体透传，不再要求整段 question 与 task 严格相等。
        """
        runtime = MockRuntimeGateway()
        aicu = MockAICU()
        node = _make_node(task="Special task for worker")
        ctx = _make_context(node=node, runtime=runtime)
        worker = Worker(ctx, aicu_instance=aicu)
        worker.run(ctx)
        assert "Special task for worker" in aicu.last_question


# ============================================================================
# Slice 6: 日志推送
# ============================================================================

class TestLogHook:
    """Worker 运行时日志应写入 node.log。"""

    def test_success_path_logs_info(self):
        """成功路径应有 INFO 级别日志"""
        runtime = MockRuntimeGateway()
        stub = SuccessStub()
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        worker.run(ctx)
        _, _, updates = runtime.status_updates[-1]
        log_entries = updates.get("log", [])
        assert len(log_entries) >= 1
        # 至少有一条 info 日志
        has_info = any(
            (isinstance(e, LogEntry) and e.level == LogLevel.INFO) or
            (isinstance(e, dict) and e.get("level") == "INFO")
            for e in log_entries
        )
        assert has_info

    def test_failure_path_logs_error(self):
        """失败路径应有 ERROR 级别日志"""
        runtime = MockRuntimeGateway()
        stub = FailureStub()
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        worker.run(ctx)
        _, _, updates = runtime.status_updates[-1]
        log_entries = updates.get("log", [])
        assert len(log_entries) >= 1

    def test_log_entries_have_timestamp(self):
        """日志条目应包含时间戳"""
        runtime = MockRuntimeGateway()
        stub = SuccessStub()
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        worker.run(ctx)
        _, _, updates = runtime.status_updates[-1]
        log_entries = updates.get("log", [])
        for entry in log_entries:
            assert entry.ts is not None


# ============================================================================
# Slice 7: NonJsonOutputStub 兼容性
# ============================================================================

class TestNonJsonOutputStubCompatibility:
    """NonJsonOutputStub 的 ask() 返回 dict，Worker 应成功处理。"""

    def test_nonjson_markdown_ask_succeeds(self):
        """NonJsonOutputStub(markdown).ask() 返回 dict，Worker 成功"""
        runtime = MockRuntimeGateway()
        stub = NonJsonOutputStub(output_mode="markdown")
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        result = worker.run(ctx)
        # ask() 返回了一个有 final_output_path 的 dict
        assert result == NodeStatus.COMPLETED

    def test_nonjson_truncated_ask_succeeds(self):
        """NonJsonOutputStub(truncated).ask() 返回缺少字段的 dict，Worker 应能处理"""
        runtime = MockRuntimeGateway()
        stub = NonJsonOutputStub(output_mode="truncated_json")
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        result = worker.run(ctx)
        # 虽然缺少 consensus_reached，但 final_output_path 存在，Worker 应成功
        assert result == NodeStatus.COMPLETED
        _, _, updates = runtime.status_updates[-1]
        assert updates.get("artifact_path") == "/tmp/stub_output/solution.py"


# ============================================================================
# Slice 8: 验收标准全覆盖
# ============================================================================

class TestAcceptanceCriteria:
    """验收标准 1-7 全覆盖。"""

    # AC 1: run() 在线程池中执行（双模式切换）
    def test_ac1_dual_mode_switching(self):
        """AC1: Worker 支持 real/stub 模式切换"""
        runtime = MockRuntimeGateway()
        stub = SuccessStub()
        ctx = _make_context(runtime=runtime)
        worker_stub = Worker(ctx, aicu_instance=stub)
        assert worker_stub.run(ctx) == NodeStatus.COMPLETED

        aicu = MockAICU()
        worker_real = Worker(ctx, aicu_instance=aicu)
        assert worker_real.run(ctx) == NodeStatus.COMPLETED

    # AC 2: 真实模式异常→Failed + ErrorObject
    def test_ac2_real_mode_error_conversion(self):
        """AC2: 真实模式异常转为 Failed + ErrorObject"""
        runtime = MockRuntimeGateway()
        aicu = MockAICU(fail=True)
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=aicu)
        result = worker.run(ctx)
        assert result == NodeStatus.FAILED
        _, _, updates = runtime.status_updates[-1]
        assert isinstance(updates.get("error"), ErrorObject)

    # AC 3: 测试桩模式验证成功/失败路径
    def test_ac3_stub_success_failure_paths(self):
        """AC3: 桩模式验证成功和失败路径"""
        runtime_s = MockRuntimeGateway()
        stub_s = SuccessStub()
        ctx_s = _make_context(runtime=runtime_s)
        w_s = Worker(ctx_s, aicu_instance=stub_s)
        assert w_s.run(ctx_s) == NodeStatus.COMPLETED

        runtime_f = MockRuntimeGateway()
        stub_f = FailureStub()
        ctx_f = _make_context(runtime=runtime_f)
        w_f = Worker(ctx_f, aicu_instance=stub_f)
        assert w_f.run(ctx_f) == NodeStatus.FAILED

    # AC 4: 成功→Completed + artifact_path
    def test_ac4_success_artifact_path(self):
        """AC4: 成功回填 artifact_path"""
        runtime = MockRuntimeGateway()
        stub = SuccessStub(result_dict={"final_output_path": "/tmp/ac4/output.py"})
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        worker.run(ctx)
        _, _, updates = runtime.status_updates[-1]
        assert updates.get("artifact_path") == "/tmp/ac4/output.py"

    # AC 5: 失败→Failed + 结构化 ErrorObject
    def test_ac5_failure_error_object(self):
        """AC5: 失败回填 ErrorObject"""
        runtime = MockRuntimeGateway()
        stub = FailureStub()
        ctx = _make_context(runtime=runtime)
        worker = Worker(ctx, aicu_instance=stub)
        worker.run(ctx)
        _, _, updates = runtime.status_updates[-1]
        error = updates.get("error")
        assert isinstance(error, ErrorObject)
        assert error.message  # 非空
        assert error.cause  # 非空
        assert error.suggestion  # 非空


# ============================================================================
# 领域无关化：路由/事件提示词（Route-and-Revision 通用化）
# ============================================================================

class TestRoutingPromptDomainNeutral:
    """路由修复机制领域无关化——提示词不得再出现开发领域专属词，任何领域可复用。"""

    def test_context_renders_list_and_dict(self):
        """路由激活上下文：list 渲染编号清单、dict 渲染嵌套行；措辞中性。"""
        from types import SimpleNamespace
        node = SimpleNamespace(pending_context={
            "activated_by": "aabbccdd-0000-0000-0000-000000000000",
            "issues": [
                {"severity": "high", "description": "章节 3 数据口径与上游不一致"},
                {"severity": "low", "description": "术语表缺 2 个缩写"},
            ],
            "expected_action": "按评审意见修订文档并重新交付",
            "detail": {"章节": 3, "范围": "第三章"},
        })
        text = Worker._build_routing_context(node)
        assert "1. severity: high；description: 章节 3 数据口径与上游不一致" in text
        assert "2. severity: low；description: 术语表缺 2 个缩写" in text
        assert "**expected_action**: 按评审意见修订文档并重新交付" in text
        assert "- 章节: 3" in text
        assert "只改受影响的部分，不要重跑全套验证" in text
        assert "请基于以上反馈修订你的产出" not in text

    def test_context_no_pending_empty(self):
        """无 pending_context → 返回空串（回归保护）。"""
        from types import SimpleNamespace
        text = Worker._build_routing_context(SimpleNamespace(pending_context=None))
        assert text == ""

    def test_event_publish_example_neutral(self):
        """事件发布示例值领域中性：不再用 api_ready/API/契约，也无 target/type。"""
        from types import SimpleNamespace
        node = SimpleNamespace(config={"events": {"publish": ["ready"]}})
        text = Worker._build_event_publish_instructions(node)
        for w in ("api_ready", "API", "契约"):
            assert w not in text
        # 方案三：发布示例只含 objective 等结构化字段，无 target/type/payload
        assert '"objective"' in text
        assert "不要指定目标" in text
        assert "target" not in text
        assert "payload" not in text

    def test_event_publish_reactivation_semantics(self):
        """事件发布指令含「不要指定目标/由系统路由」的 content-only 契约。"""
        from types import SimpleNamespace
        node = SimpleNamespace(
            config={"events": {"publish": ["feedback"]}},
        )
        text = Worker._build_event_publish_instructions(node)
        assert "不要指定目标" in text
        assert '"objective"' in text
        assert "事件只用来表达依赖关系覆盖不到的协作" in text
        assert "跨链影响" in text
        assert "需要对方行动" in text
        assert "依赖图会自动推进" in text
        assert "都不命中" in text
        # 不再出现 target/type/payload 反馈契约
        assert "带 target 的事件" not in text
        assert "payload.feedback" not in text
        assert "payload.issues" not in text


class TestParseAdversarialVoteFallback:
    """_parse_adversarial_vote —— 结构化结论是唯一来源，没有结论即 None。

    旧实现回退到 {"approved": False, "defects": [], ...}，那与「解析成功且无缺陷」
    在下游不可区分：调用方只看 defects 是否为空，于是解析失败 = 审查通过。
    现在解析失败返回 None，由调用方 fail-closed。仍然不做关键词推断——既不猜
    approved，也不猜 defects。
    """

    @staticmethod
    def _parsed(text):
        a = aicu_module.AICU.__new__(aicu_module.AICU)
        return a._parse_adversarial_vote(text)

    def test_json_path_untouched(self):
        """结构化 JSON 路径保持原样。"""
        p = self._parsed(
            '{"approved": false, "defects": [{"check": "c", "severity": "high"}], "summary": "s"}'
        )
        assert p["approved"] is False
        assert len(p["defects"]) == 1
        assert p["defects"][0]["severity"] == "high"

    def test_pass_phrasing_returns_none(self):
        """自由文本「全部通过」不构成结论：None 不是通过。"""
        assert self._parsed("审查完成，所有检查项均通过。") is None

    def test_defect_wording_not_inferred_defects(self):
        """自由文本提到「严重缺陷」也不产出结构化 defects（不做关键词猜测）。"""
        assert self._parsed("发现严重缺陷：模块 A 返回值错误，请修复。") is None

    def test_json_without_required_keys_is_none(self):
        """有 JSON 但缺 approved/defects 判据键 → 同样视为未给出结论。"""
        assert self._parsed('{"summary": "已全部通过"}') is None

    def test_example_payload_in_prose_does_not_hijack_verdict(self):
        """散文先引用示例负载、结论在后 → 必须取结论而非示例（回归主用例）。"""
        p = self._parsed(
            '接口返回 {"message": "login success"} 后跳转首页。\n'
            '审查结论：\n'
            '{"approved": false, "defects": ['
            '{"check": "鉴权", "description": "未校验 token", "severity": "high"}], '
            '"summary": "s"}'
        )
        assert p is not None
        assert p["approved"] is False
        assert p["defects"][0]["severity"] == "high"


class TestBuildResumeTaskBody:
    """Worker._build_resume_task_body 续接轮任务体（增量 Context 语义）。

    已初始化的 AICU 会话只接收本轮增量，不重复注入原始 Main Task：
    - 有路由/事件上下文时只注入增量；
    - 无上下文时注入 Continue 最小指令「开始进行主任务」。
    """

    def test_with_event_publish_block_included(self):
        from types import SimpleNamespace
        node = SimpleNamespace(config={})
        body = Worker._build_resume_task_body(
            node, "fix stuff", "## 反馈", "## 事件", "## 📣 事件发布（Event Publish）"
        )
        assert "事件发布" in body
        assert "## 反馈" in body
        assert "## 事件" in body
        assert "fix stuff" not in body, "续接轮不得重复注入原始 Main Task"

    def test_without_blocks_no_contract(self):
        from types import SimpleNamespace
        node = SimpleNamespace(config={})
        body = Worker._build_resume_task_body(node, "fix stuff", "## 反馈", "", "")
        assert "routing.json" not in body
        assert "fix stuff" not in body, "续接轮不得重复注入原始 Main Task"
        assert "## 反馈" in body

    def test_continue_without_feedback_uses_minimal_instruction(self):
        """无路由/事件上下文时，续接只注入 Continue 最小指令「开始进行主任务」。"""
        from types import SimpleNamespace
        node = SimpleNamespace(config={})
        body = Worker._build_resume_task_body(node, "original task", "", "", "")
        assert "开始进行主任务" in body
        assert "original task" not in body, "Continue 续接不得重新拼接 Main Task"

    def test_pending_context_feedback_preserved(self):
        from types import SimpleNamespace
        node = SimpleNamespace(config={})
        body = Worker._build_resume_task_body(
            node, "task", "🔄 路由激活上下文\n上游反馈", "", ""
        )
        assert "路由激活上下文" in body
        assert "上游反馈" in body
        assert "task" not in body, "有事件/反馈上下文时只注入增量，不重复 Main Task"


# ============================================================================
# Slice 9: CC 流式日志粒度过滤（AICU_NODE_LOG_STREAMING）
# ============================================================================

class _LogRecorder:
    """记录 append_node_log / update_cc_activity 调用的假 runtime。"""

    def __init__(self):
        self.logs: list[dict] = []
        self.activities: list[dict] = []

    def append_node_log(self, node_id, level, message, **kwargs):
        self.logs.append({"node_id": node_id, "level": level, "message": message, **kwargs})

    def update_cc_activity(self, node_id, cc_id, status, **kwargs):
        self.activities.append({"node_id": node_id, "cc_id": cc_id, "status": status, **kwargs})


class TestCcStreamOutputFilter:
    """real 模式 CC 流式输出 → node.log 写入的粒度过滤。

    前端 log 面板默认只展示粗粒度日志：CC 生命周期（activity）+ 错误（stderr）
    + Worker/Manager 自身里程碑日志。stdout 流式事件（每次工具调用 / 每段
    assistant 文本）粒度太细，默认不写 node.log；AICU_NODE_LOG_STREAMING=1
    时全量写入供排查。
    """

    def _run(self, stream, message):
        runtime = _LogRecorder()
        worker_module._handle_cc_stream_output("n1", "my task", runtime, stream, message)
        return runtime

    def test_stdout_streaming_hidden_by_default(self, monkeypatch):
        monkeypatch.delenv("AICU_NODE_LOG_STREAMING", raising=False)
        runtime = self._run("CC-0:stdout", "Tool: Read")
        assert runtime.logs == []  # 默认不写 node.log（粒度太细）
        # 但活动视图 latest_output 仍更新，方便在 Activity 面板查看
        assert runtime.activities[-1]["latest_output"] == "Tool: Read"

    def test_stdout_streaming_shown_when_flag_on(self, monkeypatch):
        monkeypatch.setenv("AICU_NODE_LOG_STREAMING", "1")
        runtime = self._run("CC-0:stdout", "Tool: Write")
        assert len(runtime.logs) == 1
        assert runtime.logs[0]["message"] == "Tool: Write"
        assert runtime.logs[0]["level"] == LogLevel.INFO
        assert runtime.logs[0]["phase"] == "streaming"

    def test_stderr_always_logged(self, monkeypatch):
        monkeypatch.delenv("AICU_NODE_LOG_STREAMING", raising=False)
        runtime = self._run("CC-0:stderr", "boom")
        assert len(runtime.logs) == 1
        assert runtime.logs[0]["level"] == LogLevel.ERROR
        assert runtime.logs[0]["phase"] == "streaming"

    def test_activity_updates_cc_only(self, monkeypatch):
        monkeypatch.delenv("AICU_NODE_LOG_STREAMING", raising=False)
        payload = json.dumps({"cc_id": "CC-0", "status": "completed", "phase": "completed"})
        runtime = self._run("activity", payload)
        assert runtime.logs == []
        assert runtime.activities == [{
            "node_id": "n1", "cc_id": "CC-0", "status": "completed",
            "phase": "completed", "task": "my task",
        }]

    def test_activity_malformed_json_no_crash(self, monkeypatch):
        runtime = self._run("activity", "not json")
        assert runtime.logs == []
        assert runtime.activities == []

    def test_unrecognized_stream_logged_as_info(self, monkeypatch):
        """非 stdout/stderr 的未知流名不匹配 gating 条件，按 INFO 落库（不崩溃）。"""
        monkeypatch.delenv("AICU_NODE_LOG_STREAMING", raising=False)
        runtime = self._run("", "x")
        assert len(runtime.logs) == 1
        assert runtime.logs[0]["level"] == LogLevel.INFO


class TestPromptCostGuidance:
    """省 token 的提示词约束：合并检查、少发事件。"""

    def test_acceptance_principles_ask_to_batch_checks(self):
        text = worker_module._PRODUCER_ACCEPTANCE_PRINCIPLES
        assert "一条命令" in text
        assert "并行调用" in text

    def test_event_publish_discourages_casual_events(self):
        node = Node(id="n1", exec_type="worker", role="dev", task="t", config={}, status=NodeStatus.READY)
        text = Worker._build_event_publish_instructions(node)
        assert "自己的问题自己修，不发事件" in text
        assert "不要连环发" in text
        assert "collaboration | escalation" in text
        assert "notification" not in text
