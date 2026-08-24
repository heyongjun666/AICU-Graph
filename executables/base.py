"""
executables/base.py — v2 Executable 抽象基类与协议定义

定义 Executable ABC（5 个抽象方法 + 1 个可选 hook）
以及 RuntimeGateway / SchedulerDelegate 两个 Protocol。
"""  # """  # noqa: D202

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from runtime.models import ExecutionPlan, Node, NodeStatus


def normalize_exec_mode(mode: str) -> str:
    """Normalize supported execution modes and reject unsafe typos."""
    if not isinstance(mode, str):
        raise ValueError("EXEC_MODE must be 'stub', 'mock', or 'real'")
    normalized = mode.strip().lower()
    if normalized == "mock":
        normalized = "stub"
    if normalized not in {"stub", "real"}:
        raise ValueError(
            f"Unsupported EXEC_MODE '{mode}'; expected 'stub', 'mock', or 'real'"
        )
    return normalized


# ============================================================================
# RuntimeGateway Protocol
# ============================================================================


@runtime_checkable
class RuntimeGateway(Protocol):
    """Runtime 暴露给 Executable 的网关接口。

    使用 structural subtyping（Protocol），无需继承即可满足。
    """

    def submit_plan(
        self,
        manager_node_id: str,
        plan: ExecutionPlan,
        iteration_count: int = 0,
    ) -> list[Node]:
        """提交 ExecutionPlan 到 Graph。"""
        ...

    def submit_fix_plan(
        self,
        manager_node_id: str,
        failed_assessments: list[dict],
        iteration_count: int,
    ) -> list[Node]:
        """提交精准修复计划 — 仅替换失败子节点，保留合格子节点。

        Args:
            manager_node_id: Manager 节点 ID。
            failed_assessments: 失败评估列表，每项含 step_ref, fix.role, fix.task。
            iteration_count: 当前迭代次数。

        Returns:
            新创建的子节点列表。
        """
        ...

    def update_node_status(
        self,
        node_id: str,
        status: NodeStatus,
        **updates: Any,
    ) -> None:
        """更新节点状态（含状态机校验）。

        Args:
            node_id: 目标节点 ID。
            status: 目标状态。
            **updates: 额外的字段更新（如 artifact_path, log, error 等）。
        """
        ...

    def get_node(self, node_id: str) -> Optional[Node]:
        """获取节点信息。"""
        ...

    def get_children(self, node_id: str) -> list[Node]:
        """获取子节点列表。"""
        ...

    def get_dependencies(self, node_id: str) -> list[Node]:
        """获取依赖节点列表（depends_on 上游）。"""
        ...

    def read_artifact(self, node_id: str, path: str) -> str:
        """读取节点产出文件内容。

        Args:
            node_id: 目标节点 ID。
            path: 产出文件路径（相对或绝对）。

        Returns:
            文件内容字符串。
        """
        ...

    def register_external_input_wait(
        self,
        node_id: str,
        input_schema: Optional[dict] = None,
    ) -> str:
        """注册外部输入等待（如人工审核）。

        Args:
            node_id: 等待外部输入的节点 ID。
            input_schema: 期望的输入格式描述。

        Returns:
            wait_token: 用于 deliver_external_input 匹配的令牌。
        """
        ...

    def deliver_external_input(
        self,
        wait_token: str,
        input_data: Any,
    ) -> None:
        """投递外部输入到等待中的节点。

        Args:
            wait_token: register_external_input_wait 返回的令牌。
            input_data: 输入数据。
        """
        ...


# ============================================================================
# SchedulerDelegate Protocol
# ============================================================================


@runtime_checkable
class SchedulerDelegate(Protocol):
    """Scheduler 回调委托接口。"""

    def on_manager_children_completed(self, manager_node_id: str) -> None:
        """Manager 的所有子节点已完成。"""
        ...

    def on_node_status_changed(
        self,
        node_id: str,
        status: NodeStatus,
    ) -> None:
        """节点状态变更通知。"""
        ...

    def get_active_execution_count(self) -> int:
        """获取当前活跃执行数。"""
        ...


# ============================================================================
# ExecutableContext
# ============================================================================


@dataclass
class ExecutableContext:
    """Executable 运行上下文。

    字段:
        node: 当前执行的 Node 对象。
        runtime: RuntimeGateway 实例。
        config: 配置覆盖项（白名单字段）。
    """
    node: Node
    runtime: "RuntimeGateway"
    config: dict = field(default_factory=dict)


# ============================================================================
# Executable ABC
# ============================================================================


class Executable(ABC):
    """Executable 抽象基类。

    所有可执行节点（Manager / Worker）必须继承此类并实现 5 个抽象方法：

        - run(ctx):                主执行逻辑
        - stop(ctx):               停止执行
        - pause(ctx):              暂停执行
        - resume(ctx):             恢复执行
        - cleanup_resources(ctx):  清理资源

    可选 hook:
        - wait_for_external_input(): 等待外部输入（默认返回 None 表示不等待）
    """

    def __init__(self, context: Optional[ExecutableContext] = None):
        """初始化 Executable。

        Args:
            context: ExecutableContext 实例。
        """
        self.context = context

    # ------------------------------------------------------------------
    # Abstract methods
    # ------------------------------------------------------------------

    @abstractmethod
    def run(self, ctx: ExecutableContext) -> NodeStatus:
        """主执行逻辑。

        Args:
            ctx: 执行上下文（含 Node、RuntimeGateway、配置）。

        Returns:
            执行结束后的 NodeStatus（COMPLETED / FAILED）。
        """
        ...

    @abstractmethod
    def stop(self, ctx: ExecutableContext) -> None:
        """停止执行。"""
        ...

    @abstractmethod
    def pause(self, ctx: ExecutableContext) -> None:
        """暂停执行。"""
        ...

    @abstractmethod
    def resume(self, ctx: ExecutableContext) -> NodeStatus:
        """恢复执行。

        Returns:
            恢复后的 NodeStatus。
        """
        ...

    @abstractmethod
    def cleanup_resources(self, ctx: ExecutableContext) -> None:
        """清理资源。"""
        ...

    # ------------------------------------------------------------------
    # Optional hooks
    # ------------------------------------------------------------------

    def wait_for_external_input(
        self,
        expected_event_type: str,
        timeout: Optional[float] = None,
    ) -> Optional[Any]:
        """等待外部输入。

        非抽象方法，默认返回 None 表示不等待。
        需要等待外部输入的 Executable（如 Manager review 阶段）可覆写此方法。

        Args:
            expected_event_type: 期待的事件类型。
            timeout: 超时时间（秒）。

        Returns:
            外部输入 payload 或 None（超时）。
        """
        return None
