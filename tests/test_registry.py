"""
Tests for executables/registry.py — ExecutableRegistry 工厂注册表
"""

import pytest
from typing import Any, Optional

from executables.base import (
    Executable,
    ExecutableContext,
    RuntimeGateway,
)
from executables.registry import ExecutableRegistry, RegistryError
from runtime.models import Node, NodeStatus


# ============================================================
# Helper: mock objects
# ============================================================

class _MockRuntimeGateway:
    """RuntimeGateway 协议的模拟实现。"""

    def submit_plan(self, node_id: str, plan: Any) -> None:
        pass

    def update_node_status(self, node_id: str, status: NodeStatus, **updates: Any) -> None:
        pass

    def get_node(self, node_id: str) -> Optional[Node]:
        return None

    def get_children(self, node_id: str) -> list:
        return []

    def read_artifact(self, node_id: str, path: str) -> str:
        return ""

    def register_external_input_wait(self, node_id: str, input_schema: Optional[dict] = None) -> str:
        return "token"

    def deliver_external_input(self, wait_token: str, input_data: Any) -> None:
        pass


class _TestExecutable(Executable):
    """实现全部 5 个抽象方法的测试用 Executable 子类。"""

    def __init__(self, context: ExecutableContext, **kwargs):
        self.ctx = context
        self.kwargs = kwargs
        self.cleaned_up = False

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.COMPLETED

    def stop(self, ctx: ExecutableContext) -> None:
        pass

    def pause(self, ctx: ExecutableContext) -> None:
        pass

    def resume(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.RUNNING

    def cleanup_resources(self, ctx: ExecutableContext) -> None:
        self.cleaned_up = True


class _OtherExecutable(Executable):
    """另一个测试用 Executable 子类，用于验证多类型注册。"""

    def __init__(self, context: ExecutableContext, **kwargs):
        self.ctx = context
        self.kwargs = kwargs

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.FAILED

    def stop(self, ctx: ExecutableContext) -> None:
        pass

    def pause(self, ctx: ExecutableContext) -> None:
        pass

    def resume(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.RUNNING

    def cleanup_resources(self, ctx: ExecutableContext) -> None:
        pass


def _make_context(**overrides) -> ExecutableContext:
    """创建默认的 ExecutableContext 测试实例。"""
    node = overrides.get("node") or Node(
        id="n1", exec_type="worker", role="dev", task="do it",
        config={}, status=NodeStatus.READY,
    )
    runtime = overrides.get("runtime") or _MockRuntimeGateway()
    config = overrides.get("config") or {}
    return ExecutableContext(node=node, runtime=runtime, config=config)


# ============================================================
# ExecutableRegistry 测试
# ============================================================

class TestExecutableRegistry:

    def setup_method(self):
        """每个测试方法前创建新 registry 实例。"""
        self.registry = ExecutableRegistry()

    # ------------------------------------------------------------
    # register + create 成功路径
    # ------------------------------------------------------------

    def test_register_and_create(self):
        """register + create 应成功创建可用的 Executable 实例。"""
        self.registry.register("worker", _TestExecutable)
        ctx = _make_context()
        instance = self.registry.create("worker", ctx)
        assert isinstance(instance, _TestExecutable)
        assert isinstance(instance, Executable)
        assert instance.ctx is ctx

    def test_create_with_kwargs(self):
        """create 的 **kwargs 应透传给 Executable.__init__。"""
        self.registry.register("worker", _TestExecutable)
        ctx = _make_context()
        instance = self.registry.create("worker", ctx, timeout=60, model="gpt4")
        assert instance.kwargs == {"timeout": 60, "model": "gpt4"}

    def test_create_returns_new_instance_each_time(self):
        """每次 create 应返回新实例。"""
        self.registry.register("worker", _TestExecutable)
        ctx = _make_context()
        i1 = self.registry.create("worker", ctx)
        i2 = self.registry.create("worker", ctx)
        assert i1 is not i2

    def test_create_instance_executes_correctly(self):
        """创建的实例应能正常执行 run/stop/pause/resume。"""
        self.registry.register("worker", _TestExecutable)
        ctx = _make_context()
        instance = self.registry.create("worker", ctx)
        assert instance.run(ctx) == NodeStatus.COMPLETED
        instance.stop(ctx)  # 不应抛出
        instance.pause(ctx)  # 不应抛出
        assert instance.resume(ctx) == NodeStatus.RUNNING
        instance.cleanup_resources(ctx)
        assert instance.cleaned_up

    # ------------------------------------------------------------
    # 未注册类型错误
    # ------------------------------------------------------------

    def test_create_unregistered_type_raises_error(self):
        """创建未注册的类型应抛出可理解错误。"""
        ctx = _make_context()
        with pytest.raises(RegistryError) as excinfo:
            self.registry.create("nonexistent", ctx)
        assert "nonexistent" in str(excinfo.value)

    # ------------------------------------------------------------
    # get_types
    # ------------------------------------------------------------

    def test_get_types_returns_empty_list_initially(self):
        """初始时应返回空列表。"""
        assert self.registry.get_types() == []

    def test_get_types_after_register(self):
        """注册后应返回已注册的类型列表。"""
        self.registry.register("worker", _TestExecutable)
        types = self.registry.get_types()
        assert "worker" in types
        assert len(types) == 1

    def test_get_types_returns_multiple_types(self):
        """注册多个类型后应返回全部类型。"""
        self.registry.register("worker", _TestExecutable)
        self.registry.register("manager", _OtherExecutable)
        types = self.registry.get_types()
        assert sorted(types) == ["manager", "worker"]

    def test_get_types_returns_copy(self):
        """get_types 应返回副本，外部修改不影响内部。"""
        self.registry.register("worker", _TestExecutable)
        types = self.registry.get_types()
        types.append("hacked")
        assert self.registry.get_types() == ["worker"]

    # ------------------------------------------------------------
    # unregister
    # ------------------------------------------------------------

    def test_unregister_removes_type(self):
        """unregister 后类型应从注册表移除。"""
        self.registry.register("worker", _TestExecutable)
        assert "worker" in self.registry.get_types()
        self.registry.unregister("worker")
        assert "worker" not in self.registry.get_types()

    def test_unregister_then_create_raises_error(self):
        """unregister 后再 create 应抛出错误。"""
        self.registry.register("worker", _TestExecutable)
        self.registry.unregister("worker")
        ctx = _make_context()
        with pytest.raises(RegistryError):
            self.registry.create("worker", ctx)

    def test_unregister_nonexistent_does_not_raise(self):
        """unregister 不存在的类型不应抛出异常（幂等）。"""
        self.registry.unregister("nothing")  # 不应抛出

    # ------------------------------------------------------------
    # register 重复/覆盖
    # ------------------------------------------------------------

    def test_register_duplicate_overwrites(self):
        """重复 register 同一类型应覆盖旧类。"""
        self.registry.register("worker", _TestExecutable)
        ctx = _make_context()
        instance = self.registry.create("worker", ctx)
        assert isinstance(instance, _TestExecutable)
        # 注册另一个类覆盖
        self.registry.register("worker", _OtherExecutable)
        instance2 = self.registry.create("worker", ctx)
        assert isinstance(instance2, _OtherExecutable)

    # ------------------------------------------------------------
    # register 参数校验
    # ------------------------------------------------------------

    def test_register_non_executable_type_raises_error(self):
        """register 非 Executable 子类应抛出 RegistryError。"""
        with pytest.raises(RegistryError):
            self.registry.register("invalid", str)  # str 不是 Executable 子类

    def test_register_empty_type_string_raises_error(self):
        """register 空字符串类型名应抛出 RegistryError。"""
        with pytest.raises(RegistryError):
            self.registry.register("", _TestExecutable)

    # ------------------------------------------------------------
    # register with abstract class raises error
    # ------------------------------------------------------------

    def test_register_abstract_executable_raises_error(self):
        """register 未实现全部抽象方法的类应抛出 RegistryError。"""
        with pytest.raises(RegistryError):
            self.registry.register("abstract", Executable)
