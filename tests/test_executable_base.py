"""
Tests for executables/base.py — Executable ABC, RuntimeGateway Protocol,
SchedulerDelegate Protocol, ExecutableContext dataclass.
"""

import pytest
from typing import Any, Optional
from dataclasses import dataclass

from executables.base import (
    Executable,
    ExecutableContext,
    RuntimeGateway,
    SchedulerDelegate,
)
from runtime.models import Node, NodeStatus


# ============================================================
# ExecutableContext 测试
# ============================================================

class TestExecutableContext:
    """ExecutableContext 数据类的基本行为。"""

    def test_create_with_all_fields(self):
        """应能用 node / runtime / config 创建。"""
        node = Node(id="n1", exec_type="worker", role="dev", task="do it",
                    config={}, status=NodeStatus.READY)
        runtime = _MockRuntimeGateway()
        config = {"timeout": 60}
        ctx = ExecutableContext(node=node, runtime=runtime, config=config)
        assert ctx.node.id == "n1"
        assert ctx.runtime is runtime
        assert ctx.config == {"timeout": 60}

    def test_default_config_is_empty_dict(self):
        """config 默认应为空 dict。"""
        node = Node(id="n1", exec_type="worker", role="dev", task="do it",
                    config={}, status=NodeStatus.READY)
        runtime = _MockRuntimeGateway()
        ctx = ExecutableContext(node=node, runtime=runtime)
        assert ctx.config == {}

    def test_config_can_be_modified(self):
        """config 应为可变 dict。"""
        node = Node(id="n1", exec_type="worker", role="dev", task="do it",
                    config={}, status=NodeStatus.READY)
        runtime = _MockRuntimeGateway()
        ctx = ExecutableContext(node=node, runtime=runtime, config={"key": "val"})
        ctx.config["new_key"] = "new_val"
        assert ctx.config["new_key"] == "new_val"


# ============================================================
# RuntimeGateway Protocol 测试
# ============================================================

class TestRuntimeGatewayProtocol:
    """RuntimeGateway 的结构子类型（structural subtyping）测试。"""

    def test_protocol_is_structural(self):
        """只要实现全部方法即视为 RuntimeGateway。"""
        gw = _MockRuntimeGateway()
        assert isinstance(gw, RuntimeGateway)

    def test_all_methods_signatures(self):
        """验证所有 7 个方法签名正确。"""
        gw = _MockRuntimeGateway()
        # submit_plan
        gw.submit_plan("n1", None)
        assert gw.submit_plan_called
        # update_node_status
        gw.update_node_status("n1", NodeStatus.RUNNING)
        assert gw.update_node_status_called
        # get_node
        node = gw.get_node("n1")
        assert node is not None
        # get_children
        children = gw.get_children("n1")
        assert isinstance(children, list)
        # read_artifact
        text = gw.read_artifact("n1", "/path/to/file")
        assert isinstance(text, str)
        # register_external_input_wait
        token = gw.register_external_input_wait("n1", {"type": "object"})
        assert gw.register_external_input_wait_called
        assert isinstance(token, str)
        # deliver_external_input
        gw.deliver_external_input(token, {"key": "val"})
        assert gw.deliver_external_input_called

    # ------------------------------------------------------------
    # 回归测试：验证接口签名符合任务规范
    # ------------------------------------------------------------

    def test_update_node_status_accepts_updates_kwargs(self):
        """update_node_status 应接收 **updates 并透传给 Runtime。"""
        gw = _MockRuntimeGateway()
        gw.update_node_status("n1", NodeStatus.COMPLETED, artifact_path="/tmp/out", error=None)
        assert gw.update_node_status_called
        assert gw.update_node_status_updates == {"artifact_path": "/tmp/out", "error": None}

    def test_read_artifact_accepts_node_id_and_path(self):
        """read_artifact 应接收 node_id + path 两个参数。"""
        gw = _MockRuntimeGateway()
        gw.read_artifact("n1", "output/result.json")
        assert gw.read_artifact_node_id == "n1"
        assert gw.read_artifact_path == "output/result.json"

    def test_register_external_input_wait_accepts_input_schema_and_returns_token(self):
        """register_external_input_wait 应接收 input_schema 并返回 wait_token。"""
        gw = _MockRuntimeGateway()
        schema = {"type": "object", "properties": {"approve": {"type": "boolean"}}}
        token = gw.register_external_input_wait("n1", input_schema=schema)
        assert gw.register_external_input_wait_schema == schema
        assert token == "wait_token_n1"

    def test_deliver_external_input_uses_wait_token(self):
        """deliver_external_input 第一个参数应为 wait_token 而非 node_id。"""
        gw = _MockRuntimeGateway()
        token = "wait_token_abc"
        gw.deliver_external_input(token, {"data": "value"})
        assert gw.deliver_external_input_called
        assert gw.deliver_external_input_token == token


class TestSchedulerDelegateProtocol:
    """SchedulerDelegate 的结构子类型测试。"""

    def test_protocol_is_structural(self):
        """只要实现全部方法即视为 SchedulerDelegate。"""
        sd = _MockSchedulerDelegate()
        assert isinstance(sd, SchedulerDelegate)

    def test_all_methods_signatures(self):
        """验证所有 3 个方法签名正确。"""
        sd = _MockSchedulerDelegate()
        sd.on_manager_children_completed("n1")
        assert sd.on_manager_children_completed_called
        sd.on_node_status_changed("n1", NodeStatus.COMPLETED)
        assert sd.on_node_status_changed_called
        count = sd.get_active_execution_count()
        assert isinstance(count, int)

    def test_on_node_status_changed_accepts_status_no_old_status(self):
        """on_node_status_changed 应只接收 node_id + status（不带 old_status）。"""
        sd = _MockSchedulerDelegate()
        sd.on_node_status_changed("n1", NodeStatus.FAILED)
        assert sd.on_node_status_changed_called
        assert sd.on_node_status_changed_status == NodeStatus.FAILED


# ============================================================
# Executable ABC 测试
# ============================================================

class TestExecutableABC:
    """Executable 抽象基类的约束与默认行为。"""

    def test_cannot_instantiate_abc(self):
        """不能直接实例化 Executable。"""
        with pytest.raises(TypeError, match="abstract"):
            Executable()  # noqa: E1120

    def test_concrete_subclass_must_implement_abstract_methods(self):
        """缺少抽象方法的子类不能实例化。"""
        with pytest.raises(TypeError, match="abstract"):
            _IncompleteExecutable()  # noqa: E1120

    def test_concrete_subclass_can_instantiate(self):
        """实现抽象方法 run() 的子类可以实例化。"""
        exe = _ConcreteExecutable()
        assert isinstance(exe, Executable)

    def test_run_returns_node_status(self):
        """run() 应返回 NodeStatus。"""
        exe = _ConcreteExecutable()
        ctx = _make_context()
        status = exe.run(ctx)
        assert isinstance(status, NodeStatus)
        assert status == NodeStatus.COMPLETED

    def test_wait_for_external_input_default_returns_none(self):
        """wait_for_external_input 默认返回 None（非抽象钩子）。"""
        exe = _ConcreteExecutable()
        result = exe.wait_for_external_input("test_event")
        assert result is None

    def test_wait_for_external_input_can_be_overridden(self):
        """wait_for_external_input 可被子类覆盖。"""
        exe = _OverrideWaitExecutable()
        result = exe.wait_for_external_input("test_event")
        assert result == "external_data"

    def test_executable_receives_ctx_with_correct_types(self):
        """验证传给 run() 的 ctx 各字段类型正确。"""
        exe = _TypeCheckExecutable()
        node = Node(id="n1", exec_type="worker", role="dev", task="do it",
                    config={}, status=NodeStatus.READY)
        runtime = _MockRuntimeGateway()
        config = {"max_retries": 3}
        ctx = ExecutableContext(node=node, runtime=runtime, config=config)
        status = exe.run(ctx)
        assert status == NodeStatus.COMPLETED


# ============================================================
# Helper: mock objects
# ============================================================

class _MockRuntimeGateway:
    """RuntimeGateway 协议的模拟实现。"""

    def __init__(self):
        self.submit_plan_called = False
        self.update_node_status_called = False
        self.update_node_status_updates = {}
        self.register_external_input_wait_called = False
        self.deliver_external_input_called = False

    def submit_plan(self, node_id: str, plan: Any) -> None:
        self.submit_plan_called = True

    def submit_fix_plan(self, node_id: str, failed_assessments: list, iteration_count: int) -> list:
        self.submit_fix_plan_called = True
        self.submit_fix_plan_assessments = failed_assessments
        return []

    def update_node_status(self, node_id: str, status: NodeStatus, **updates: Any) -> None:
        self.update_node_status_called = True
        self.update_node_status_updates = updates

    def get_node(self, node_id: str) -> Node:
        return Node(id=node_id, exec_type="worker", role="dev", task="test",
                    config={}, status=NodeStatus.READY)

    def get_children(self, node_id: str) -> list[Node]:
        return []

    def get_dependencies(self, node_id: str) -> list[Node]:
        return []

    def read_artifact(self, node_id: str, path: str) -> str:
        self.read_artifact_node_id = node_id
        self.read_artifact_path = path
        return "artifact content"

    def register_external_input_wait(self, node_id: str, input_schema: Optional[dict] = None) -> str:
        self.register_external_input_wait_called = True
        self.register_external_input_wait_schema = input_schema
        return f"wait_token_{node_id}"

    def deliver_external_input(self, wait_token: str, input_data: Any) -> None:
        self.deliver_external_input_called = True
        self.deliver_external_input_token = wait_token


class _MockSchedulerDelegate:
    """SchedulerDelegate 协议的模拟实现。"""

    def __init__(self):
        self.on_manager_children_completed_called = False
        self.on_node_status_changed_called = False

    def on_manager_children_completed(self, manager_node_id: str) -> None:
        self.on_manager_children_completed_called = True

    def on_node_status_changed(self, node_id: str, status: NodeStatus) -> None:
        self.on_node_status_changed_called = True
        self.on_node_status_changed_status = status

    def get_active_execution_count(self) -> int:
        return 3


class _IncompleteExecutable(Executable):
    """未实现全部抽象方法，预期不能实例化。"""
    pass


class _ConcreteExecutable(Executable):
    """实现了抽象方法 run() 的完整子类。"""

    def __init__(self, context: ExecutableContext = None):
        super().__init__(context)

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.COMPLETED


class _OverrideWaitExecutable(_ConcreteExecutable):
    """覆盖 wait_for_external_input 的子类。"""

    def wait_for_external_input(self, expected_event_type: str, timeout: float = None) -> Any:
        return "external_data"


class _TypeCheckExecutable(Executable):
    """验证 ctx 字段类型的子类。"""

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        assert isinstance(ctx.node, Node)
        assert isinstance(ctx.runtime, RuntimeGateway)
        assert isinstance(ctx.config, dict)
        return NodeStatus.COMPLETED


def _make_context(**overrides) -> ExecutableContext:
    """创建默认的 ExecutableContext 测试实例。"""
    node = overrides.get("node") or Node(
        id="n1", exec_type="worker", role="dev", task="do it",
        config={}, status=NodeStatus.READY,
    )
    runtime = overrides.get("runtime") or _MockRuntimeGateway()
    config = overrides.get("config") or {}
    return ExecutableContext(node=node, runtime=runtime, config=config)
