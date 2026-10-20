"""
runtime/state_machine.py — v1 状态转换规则校验引擎

定义 StateMachine 类，用于校验状态转换是否合法，
并提供指定状态下的合法转换列表。

与 runtime/models.py 中的 NodeStatus 枚举配合使用。
Node.__setattr__ 做终端状态的基础拦截（Completed 不可转换，
Failed/Stopped 允许 → Waiting 的原地重试），
StateMachine.validate_transition 提供完整的状态转换规则校验（含 Manager 两段式）。
"""

from typing import Optional

from runtime.models import TERMINAL_STATUSES, NodeStatus


class IllegalTransitionError(Exception):
    """状态转换非法异常。

    字段:
        from_status: 当前状态。
        to_status: 目标状态。
        reason: 非法原因描述。
    """

    def __init__(self, from_status: NodeStatus, to_status: NodeStatus, reason: str):
        self.from_status = from_status
        self.to_status = to_status
        self.reason = reason
        super().__init__(
            f"Illegal transition: {from_status.value} -> {to_status.value}: {reason}"
        )


# ---------------------------------------------------------------------------
# 通用转换矩阵 — 所有 Executable 类型共用
# ---------------------------------------------------------------------------
_COMMON_TRANSITIONS: set[tuple[NodeStatus, NodeStatus]] = {
    # Waiting 态转换
    (NodeStatus.WAITING, NodeStatus.READY),       # 依赖全部 Completed
    (NodeStatus.WAITING, NodeStatus.STOPPED),     # 用户停止
    # Ready 态转换
    (NodeStatus.READY, NodeStatus.RUNNING),       # Scheduler 调度
    (NodeStatus.READY, NodeStatus.STOPPED),       # 用户停止
    # Running 态转换
    (NodeStatus.RUNNING, NodeStatus.COMPLETED),   # 执行成功
    (NodeStatus.RUNNING, NodeStatus.FAILED),      # 执行失败
    (NodeStatus.RUNNING, NodeStatus.STOPPED),     # 用户停止
    (NodeStatus.RUNNING, NodeStatus.PAUSED),      # 用户暂停
    # Paused 态转换
    (NodeStatus.PAUSED, NodeStatus.RUNNING),      # 用户恢复
    # 终端态原地重试（先回 WAITING 再走正常调度链，而非直跳 READY）
    (NodeStatus.FAILED, NodeStatus.WAITING),      # 原地重试：重置回等待态
    (NodeStatus.STOPPED, NodeStatus.WAITING),     # 原地重试：重置回等待态
    (NodeStatus.COMPLETED, NodeStatus.WAITING),   # 路由重激活：被下游验收节点发现缺陷后回退重跑
    # 终端态重启（直跳 READY，向后兼容旧路径）
    (NodeStatus.STOPPED, NodeStatus.READY),       # 用户编辑后重启
    (NodeStatus.FAILED, NodeStatus.READY),        # 用户/自动重试
    (NodeStatus.COMPLETED, NodeStatus.READY),     # 路由重激活：与 Node.__setattr__ 放宽保持一致
}


# Manager 特有的非通用转换（phase 维度）
#   Running(expand) → Waiting        Plan 提交后等子节点完成
#   Running(review) → Running(expand) Review 不通过，迭代（不经 Ready）
# 注意: Running(expand) → Completed  对 Manager 是非法转换
#       Running(review) → Completed  由通用矩阵中的 Running→Completed 覆盖


class StateMachine:
    """状态转换规则校验引擎。

    所有方法均为静态方法，无实例状态。
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def validate_transition(
        current_status: NodeStatus,
        current_phase: Optional[str],
        target_status: NodeStatus,
        target_phase: Optional[str],
        exec_type: str,
    ) -> bool:
        """校验状态转换是否合法，非法转换抛出 IllegalTransitionError。

        Args:
            current_status: 当前状态。
            current_phase: 当前阶段（仅 Manager 使用：expand/review）。
            target_status: 目标状态。
            target_phase: 目标阶段（仅 Manager 使用）。
            exec_type: Executable 类型（manager/worker）。

        Returns:
            True（合法时返回）。

        Raises:
            IllegalTransitionError: 非法转换时抛出，含原因描述。
        """
        # === 1. Manager 特有转换（最高优先级，在通用矩阵之前拦截） ===
        if exec_type == "manager":
            # 1a. Running(expand) → Waiting: Plan 提交后等子节点完成
            if (current_status == NodeStatus.RUNNING
                    and current_phase == "expand"
                    and target_status == NodeStatus.WAITING):
                return True

            # 1b. Running(review) → Running(expand): Review 不通过，迭代（不经 Ready）
            if (current_status == NodeStatus.RUNNING
                    and current_phase == "review"
                    and target_status == NodeStatus.RUNNING
                    and target_phase == "expand"):
                return True

            # 1c. Running(expand) → Completed 对 Manager 非法（必须经 review）
            if (current_status == NodeStatus.RUNNING
                    and current_phase == "expand"
                    and target_status == NodeStatus.COMPLETED):
                raise IllegalTransitionError(
                    from_status=current_status,
                    to_status=target_status,
                    reason=(
                        "Manager cannot transition from Running(expand) "
                        "directly to Completed; must go through "
                        "Running(expand) → Waiting → Ready → Running(review) → Completed"
                    ),
                )

        # === 2. 通用转换矩阵（包含 Failed→Ready, Stopped→Ready） ===
        if (current_status, target_status) in _COMMON_TRANSITIONS:
            return True

        # === 3. 明确语义的非法转换（在终端状态检查之前，给出详细原因） ===
        # Waiting → Running（必须经过 Ready）
        if current_status == NodeStatus.WAITING and target_status == NodeStatus.RUNNING:
            raise IllegalTransitionError(
                from_status=current_status,
                to_status=target_status,
                reason="Waiting must transition through Ready before Running",
            )

        # Failed → Running（必须经过 Ready）
        if current_status == NodeStatus.FAILED and target_status == NodeStatus.RUNNING:
            raise IllegalTransitionError(
                from_status=current_status,
                to_status=target_status,
                reason="Failed must transition through Ready before Running",
            )

        # Stopped → Running（必须经过 Ready）
        if current_status == NodeStatus.STOPPED and target_status == NodeStatus.RUNNING:
            raise IllegalTransitionError(
                from_status=current_status,
                to_status=target_status,
                reason="Stopped must transition through Ready before Running",
            )

        # Paused → Completed/Failed/Waiting/Ready（只能到 Running）
        if current_status == NodeStatus.PAUSED and target_status in (
            NodeStatus.COMPLETED,
            NodeStatus.FAILED,
            NodeStatus.WAITING,
            NodeStatus.READY,
        ):
            raise IllegalTransitionError(
                from_status=current_status,
                to_status=target_status,
                reason=(
                    f"Cannot transition from Paused to '{target_status.value}'; "
                    f"only Paused → Running is allowed"
                ),
            )

        # === 4. 终端状态不允许转换 ===
        if current_status in TERMINAL_STATUSES:
            raise IllegalTransitionError(
                from_status=current_status,
                to_status=target_status,
                reason=(
                    f"Cannot transition from terminal state "
                    f"'{current_status.value}' to '{target_status.value}'"
                ),
            )

        # === 5. 兜底：未匹配到任何合法转换 ===
        raise IllegalTransitionError(
            from_status=current_status,
            to_status=target_status,
            reason=(
                f"Transition from '{current_status.value}' "
                f"to '{target_status.value}' is not allowed"
            ),
        )

    @staticmethod
    def get_allowed_transitions(
        status: NodeStatus,
        phase: Optional[str],
        exec_type: str,
    ) -> list[dict]:
        """获取指定状态下的合法转换列表。

        Args:
            status: 当前状态。
            phase: 当前阶段（仅 Manager 使用）。
            exec_type: Executable 类型（manager/worker）。

        Returns:
            合法转换列表，每个元素为 dict:
            {to_status: NodeStatus, to_phase: Optional[str], description: str}。
        """
        candidates: list[dict] = []

        for target_status in NodeStatus:
            # 对 Manager 不跳过相同 status（因为 phase 变化 review→expand 是合法转换）
            if target_status == status and exec_type != "manager":
                continue

            # 尝试无 phase 转换
            try:
                StateMachine.validate_transition(
                    status, phase, target_status, None, exec_type,
                )
                candidates.append({
                    "to_status": target_status,
                    "to_phase": None,
                    "description": _build_description(
                        status, phase, target_status, None,
                    ),
                })
                continue  # 已匹配，跳过带 phase 的尝试
            except IllegalTransitionError:
                pass

            # Manager 特有：尝试带 phase 的转换
            if exec_type == "manager":
                for target_phase in ("expand", "review"):
                    try:
                        StateMachine.validate_transition(
                            status, phase, target_status, target_phase, exec_type,
                        )
                        candidates.append({
                            "to_status": target_status,
                            "to_phase": target_phase,
                            "description": _build_description(
                                status, phase, target_status, target_phase,
                            ),
                        })
                    except IllegalTransitionError:
                        pass

        return candidates


# ============================================================================
# Internal helpers
# ============================================================================


def _build_description(
    from_status: NodeStatus,
    from_phase: Optional[str],
    to_status: NodeStatus,
    to_phase: Optional[str],
) -> str:
    """生成人类可读的转换描述。"""
    parts = [f"{_fmt_status(from_status, from_phase)} → {_fmt_status(to_status, to_phase)}"]
    return " ".join(parts)


def _fmt_status(status: NodeStatus, phase: Optional[str]) -> str:
    if phase:
        return f"{status.value}({phase})"
    return status.value
