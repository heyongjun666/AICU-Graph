"""
executables/registry.py — v2 Executable 注册中心（工厂模式）

提供按类型名注册/创建 Executable 实例的能力。
"""  # """  # noqa: D202

from typing import Any, Type

from executables.base import Executable, ExecutableContext


class RegistryError(Exception):
    """ExecutableRegistry 操作异常。

    字段:
        message: 错误描述。
    """

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ExecutableRegistry:
    """Executable 注册中心。

    职责:
        - register:   注册 Executable 子类到指定类型名
        - create:     按类型名创建 Executable 实例
        - get_types:  获取所有已注册的类型名
        - unregister: 取消注册指定类型名
    """

    def __init__(self):
        self._registry: dict[str, Type[Executable]] = {}

    def register(self, exec_type: str, cls: Type[Executable]) -> None:
        """注册 Executable 子类。

        Args:
            exec_type: 类型名称（必须非空）。
            cls: Executable 子类（必须为 Executable 子类且非抽象）。

        Raises:
            RegistryError: 参数校验失败。
        """
        if not exec_type or not exec_type.strip():
            raise RegistryError("exec_type must be a non-empty string")

        if not isinstance(cls, type) or not issubclass(cls, Executable):
            raise RegistryError(
                f"cls must be a subclass of Executable, got {cls}"
            )

        if cls is Executable:
            raise RegistryError("Cannot register the abstract base class Executable directly")

        # 检查是否仍有抽象方法未实现
        if cls.__abstractmethods__:
            raise RegistryError(
                f"Cannot register abstract class {cls.__name__} "
                f"with unimplemented abstract methods: {sorted(cls.__abstractmethods__)}"
            )

        self._registry[exec_type.strip()] = cls

    def create(
        self,
        exec_type: str,
        context: ExecutableContext,
        **kwargs: Any,
    ) -> Executable:
        """按类型名创建 Executable 实例。

        Args:
            exec_type: 已注册的类型名称。
            context: ExecutableContext 实例。
            **kwargs: 传递给构造函数的额外参数。

        Returns:
            Executable 实例。

        Raises:
            RegistryError: 类型未注册。
        """
        cls = self._registry.get(exec_type)
        if cls is None:
            raise RegistryError(
                f"Executable type '{exec_type}' is not registered. "
                f"Available types: {sorted(self._registry.keys())}"
            )
        return cls(context=context, **kwargs)

    def get_types(self) -> list[str]:
        """获取所有已注册的类型名。"""
        return list(self._registry.keys())

    def unregister(self, exec_type: str) -> None:
        """取消注册指定类型名。

        Args:
            exec_type: 要取消注册的类型名称。
        """
        self._registry.pop(exec_type, None)
