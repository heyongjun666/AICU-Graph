"""
runtime/scheduler.py — Scheduler 单层图调度引擎

核心机制:
1. tick 循环（间隔 0.5s，独立管理线程），无 Ready/Running 节点时自动停止
2. Waiting 节点检查依赖 → 全部 Completed 转 Ready
3. DFS 深度优先选择 Ready 节点
4. 并发控制：slots = min(max_concurrency - running, len(ready_nodes))
5. Running → 通过 Registry 创建 Executable 实例 → 提交线程池执行
6. 线程池大小 = max_concurrency + 2
7. Manager 计入活跃执行数，Waiting 释放槽位
8. 通过 SchedulerDelegate 协议回调 Runtime
9. 支持 pause/resume 生命周期
10. Watchdog 机制：异常捕获 + 自动重启（最多 3 次，最小间隔 60s）
"""

import concurrent.futures
import glob
import json
import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Any, Optional, TYPE_CHECKING

from executables.base import ExecutableContext, SchedulerDelegate
from executables.registry import ExecutableRegistry
from runtime.agent_events import _node_workspace
from runtime.graph import Graph
from runtime.models import ManagerPhase, Node, NodeStatus, RoutingDecision
from runtime.state_machine import StateMachine

if TYPE_CHECKING:
    from runtime.planner import Planner

logger = logging.getLogger(__name__)

# Tick 循环间隔（秒）
_TICK_INTERVAL = 0.5

# Watchdog 配置
_MAX_WATCHDOG_RETRIES = 3
_WATCHDOG_MIN_INTERVAL = 60.0


class Scheduler:
    """单层图调度引擎。

    管理 Graph 中节点的执行生命周期（Waiting → Ready → Running → Completed/...），
    通过独立后台线程驱动 tick 循环实现调度决策。

    Args:
        graph:           Graph 实例。
        registry:        ExecutableRegistry 实例。
        delegate:        SchedulerDelegate 回调接口。
        runtime_gateway: RuntimeGateway 实例（传递给 ExecutableContext）。
        max_concurrency: 最大并发执行节点数（默认 4）。
        event_bus:       可选 EventBus 实例（用于发布 scheduler_tick 事件）。
        planner:         可选 Planner 实例（用于自动重试创建 Retry Node）。
        retry_delay:     自动重试间隔（秒，默认 1.0）。
    """

    def __init__(
        self,
        graph: Graph,
        registry: ExecutableRegistry,
        delegate: SchedulerDelegate,
        runtime_gateway: Any,
        max_concurrency: int = 4,
        event_bus: Any = None,
        planner: Optional["Planner"] = None,
        retry_delay: float = 1.0,
    ):
        self.graph = graph
        self.registry = registry
        self.delegate = delegate
        self.runtime_gateway = runtime_gateway
        self.max_concurrency = max_concurrency
        self.event_bus = event_bus
        self.planner = planner
        self.retry_delay = retry_delay

        # 线程池
        pool_size = max_concurrency + 2
        self._thread_pool = ThreadPoolExecutor(max_workers=pool_size)
        self._pool_shutdown = False

        # 正在执行的任务 {node_id: Future}
        self._futures: dict[str, Future] = {}
        self._futures_lock = threading.Lock()

        # tick 线程控制
        self._running = False
        self._paused = False
        self._state_lock = threading.Lock()
        self._tick_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # 已通知子节点完成的 Manager 集合（防止同一 tick 重复回调）
        self._notified_managers: set[str] = set()

        # Watchdog 状态
        self._watchdog_retries = 0
        self._last_watchdog_time = 0.0
        self._max_watchdog_retries = _MAX_WATCHDOG_RETRIES
        self._watchdog_min_interval = _WATCHDOG_MIN_INTERVAL

        # 自动重试状态
        self._retry_attempts: dict[str, int] = {}  # original_node_id -> attempt count
        self._scheduled_retries: dict[str, float] = {}  # retry_node_id -> ready_time
        # Session event recovery is performed once by the owning Scheduler,
        # after its pause gate has been checked.
        self._event_recovery_done = False

        # 升级台账：无法自动收敛时记录结构化升级信号（不静默继续循环）。
        # 最终由 closed_loop 业务产物消费，使交付审计可见"存在未闭合协作"。
        self._escalations: list[dict] = []
        self._escalations_lock = threading.Lock()

    # ========================================================================
    # Public API
    # ========================================================================

    def get_escalations(self) -> list[dict]:
        """返回 C2 升级台账（无法自动收敛的协作路由记录）。

        Returns:
            升级记录列表（副本）。元素结构:
            {"node_id","reason","kind","timestamp","budget_exceeded":bool}
        """
        with self._escalations_lock:
            return list(self._escalations)

    def start(self) -> None:
        """启动 tick 循环（后台线程）。"""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True, name="scheduler-tick")
        self._tick_thread.start()
        logger.info("Scheduler started (max_concurrency=%d, pool_size=%d)", self.max_concurrency, self._thread_pool._max_workers)

    def stop(self) -> None:
        """停止 tick 循环并清理。"""
        self._running = False
        self._stop_event.set()

        # 取消所有进行中的 Future
        with self._futures_lock:
            for node_id, future in self._futures.items():
                future.cancel()
            self._futures.clear()

        self._pool_shutdown = True
        self._thread_pool.shutdown(wait=False)
        if self._tick_thread and self._tick_thread.is_alive():
            self._tick_thread.join(timeout=2)
        logger.info("Scheduler stopped")

    def pause(self) -> None:
        """Close dispatch after any in-flight scheduling section completes."""
        with self._state_lock:
            self._paused = True
        logger.info("Scheduler paused")

    def resume(self) -> None:
        """Reopen dispatch."""
        with self._state_lock:
            self._paused = False
        logger.info("Scheduler resumed")

    def stop_node(self, node_id: str) -> None:
        """停止指定节点的执行（用于手动 pause）。

        杀死其 AICU 中正在运行的 CC 子进程，并继续跟踪 Future，
        直到执行线程真实退出。AICU 缓存保留，以便后续 resume 时
        通过 --resume 续接会话。

        Args:
            node_id: 目标节点 ID。
        """
        # Keep the Future tracked until its thread actually exits. This prevents
        # graph-level pause from claiming restart safety while work is still live.

        # 杀死 CC 子进程，使阻塞的 Worker 线程返回
        try:
            from executables.worker import Worker
            with Worker._aicu_cache_lock:
                aicu = Worker._aicu_cache.get(node_id)
            if aicu is not None:
                aicu.kill_processes()
        except Exception:
            pass

    def clear_manager_notification(self, manager_id: str) -> None:
        """清除指定 Manager 的通知记录，允许其子节点完成时重新触发回调。

        用于节点原地重试场景：当 FAILED 子节点被重置为 WAITING 时，
        其父 Manager 可能已经接收过 on_manager_children_completed 回调，
        需要清除通知记录以便子节点全部再次完成时能重新触发。

        Args:
            manager_id: Manager 节点 ID。
        """
        self._notified_managers.discard(manager_id)

    @property
    def active_execution_count(self) -> int:
        """当前活跃（RUNNING）的执行节点数量。

        排除 WAITING phase 的 Manager 节点（展开后等待子节点完成的 Manager 不占槽位）。
        """
        with self._futures_lock:
            return len(self._futures)

    @property
    def active_node_ids(self) -> list[str]:
        """Return a stable snapshot of nodes currently tracked as running."""
        with self._futures_lock:
            return list(self._futures)

    def _get_effective_running_count(self) -> int:
        """获取实际占用并发槽位的活跃节点数。

        Manager 在 WAITING phase 时不占用并发槽位，
        只有 RUNNING(expand) 和 RUNNING(review) 才计入。
        """
        count = 0
        with self._futures_lock:
            for node_id in self._futures:
                node = self.graph.get_node(node_id)
                if node is None:
                    continue
                if node.exec_type == "manager" and node.manager_phase == ManagerPhase.WAITING.value:
                    continue
                count += 1
        return count

    # ========================================================================
    # Tick 循环
    # ========================================================================

    def _recover_pending_events_once(self) -> None:
        if self._event_recovery_done:
            return
        recover = getattr(self.runtime_gateway, "recover_agent_events", None)
        if not callable(recover):
            self._event_recovery_done = True
            return
        try:
            recover()
        except Exception as exc:  # noqa: BLE001
            logger.error("Agent event recovery failed: %s", exc)
        finally:
            self._event_recovery_done = True

    def _tick_loop(self) -> None:
        """主 tick 循环，在独立后台线程中运行。"""
        while self._running and not self._stop_event.is_set():
            try:
                # Event recovery belongs to this session's active Scheduler, not
                # Runtime event-log loading. Paused recovery never reaches here.
                with self._state_lock:
                    paused = self._paused
                if not paused:
                    self._recover_pending_events_once()

                # 检查是否应自动停止（无 Ready/Running/Waiting 节点）
                # Waiting 节点也包括在内，因为它们需要被处理为 Ready
                stats = self.graph.get_stats()
                ready_count = stats.get("Ready", 0)
                running_count = stats.get("Running", 0)
                waiting_count = stats.get("Waiting", 0)

                if (ready_count == 0 and running_count == 0
                        and waiting_count == 0 and not self._scheduled_retries
                        and self.active_execution_count == 0):
                    with self._state_lock:
                        paused = self._paused
                    if paused:
                        callback = getattr(self.delegate, "on_scheduler_idle", None)
                        if callable(callback):
                            callback()
                    self._running = False
                    logger.info("Scheduler auto-stopped (no Ready/Running/Waiting nodes)")
                    break

                # 每个 tick 开始时清理 Manager 通知集合
                self._notified_managers.clear()

                with self._state_lock:
                    if not self._paused:
                        # 0. 处理已到时间的定时重试节点
                        self._process_scheduled_retries()

                # 1. 优先处理已完成的 Future，释放依赖槽位
                self._process_completed_futures()

                # 1.5 摄取并路由 Agent 事件（Worker→Runtime→Worker 动态协作信号）。
                #     仅当存在 RUNNING 节点时扫描：中途发布只可能发生在 worker 运行期间，
                #     且避免在 dispatch 前引入额外延迟（节点完成时的遗留文件由
                #     worker flush_node_events 兜底摄取）。事件不参与调度：只投递 inbox。
                if running_count > 0:
                    self._scan_agent_events()

                with self._state_lock:
                    paused = self._paused
                    if not paused:
                        # 1.8 事件→重激活桥：把投递给非运行态节点的带 target 事件解释为
                        # 「请求该节点行动/重激活」（V3.1 P2：routing.json 移除后，反馈/协作
                        # 闭环的唯一触发点）。须在 _process_waiting_nodes 前调用，保证同一
                        # tick 内重激活的节点能被正常派发。
                        self._process_reactivation_events()

                        # 2. 处理 Waiting → Ready
                        self._process_waiting_nodes()

                        # 3. 调度 Ready 节点
                        self._schedule_ready_nodes()
                if paused and self.active_execution_count == 0:
                    callback = getattr(self.delegate, "on_scheduler_idle", None)
                    if callable(callback):
                        callback()

                # 发布 tick 事件
                self._publish_tick_event()

            except Exception as exc:
                logger.error("Scheduler tick error: %s", exc)
                self._handle_watchdog_error(exc)

            # 等待下一个 tick
            self._stop_event.wait(_TICK_INTERVAL)

    def _scan_agent_events(self) -> None:
        """摄取并路由 Agent 事件发布入口（事件不参与调度，只投递 inbox）。"""
        gateway = self.runtime_gateway
        if gateway is None:
            return
        scanner = getattr(gateway, "scan_agent_events", None)
        if not callable(scanner):
            return
        try:
            scanner()
        except Exception as exc:  # noqa: BLE001
            logger.error("Agent event scan failed: %s", exc)

    def _publish_tick_event(self) -> None:
        """发布 scheduler_tick 事件。"""
        if self.event_bus is None:
            return
        try:
            from runtime.event_bus import SSEEvent
            event = SSEEvent(
                event_type="scheduler_tick",
                sequence_id=0,  # EventBus 会覆盖
                timestamp=time.time(),
                payload={
                    "active_count": self.active_execution_count,
                    "paused": self._paused,
                },
            )
            self.event_bus.publish(event)
        except Exception:
            pass

    # ========================================================================
    # Waiting → Ready
    # ========================================================================

    def _process_waiting_nodes(self) -> None:
        """扫描所有 Waiting 节点，检查依赖是否全部 Completed，若是则转 Ready。

        对 Manager 节点的特殊处理：
        - manager_phase=None 或 "expand" 的 Manager（刚创建/等待依赖，还未执行过 expand）
          → 依赖满足后允许转为 READY
        - manager_phase="waiting" 的 Manager（expand 完成后等待子节点完成）
          → 应由 on_children_completed() 回调驱动，而非由依赖检查触发
        - manager_phase="review" 的 Manager → 已在 review 阶段，不由此处理
        """
        waiting_nodes = [
            n for n in self.graph.all_nodes()
            if n.status == NodeStatus.WAITING
            and (
                n.exec_type != "manager"
                or n.manager_phase is None
                or n.manager_phase == "expand"
            )
        ]
        for node in waiting_nodes:
            # 正在等待定时重试（_try_auto_retry 记录的 ready_time）的节点，
            # 必须由 _process_scheduled_retries 到期后转为 READY，这里不得提前提升，
            # 否则 retry_delay 对无依赖叶子节点被同一 tick 绕过。
            if node.id in self._scheduled_retries:
                continue
            deps = self.graph.dependencies(node.id)
            terminal_statuses = {NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.STOPPED}
            all_terminal = all(
                dep.status in terminal_statuses for dep in deps
            )
            if all_terminal:
                self._transition_node(node.id, NodeStatus.READY)

    # ========================================================================
    # DFS Ready 节点选择与调度
    # ========================================================================

    def _schedule_ready_nodes(self) -> None:
        """选择 Ready 节点并提交线程池执行。

        Manager-aware 并发控制：
        - 使用 _get_effective_running_count() 排除 WAITING phase Manager 的槽位占用
        - 优先调度 WAITING Manager 的子节点，确保 Expand→Children→Review 序列不被阻塞
        """
        # 收集所有 Ready 节点
        ready_nodes = [
            n for n in self.graph.all_nodes()
            if n.status == NodeStatus.READY
        ]
        if not ready_nodes:
            return

        # 检测哪些 Manager 处于 WAITING phase（等待子节点完成）
        waiting_managers = {
            n.id for n in self.graph.all_nodes()
            if n.exec_type == "manager"
            and n.status == NodeStatus.WAITING
            and n.manager_phase == ManagerPhase.WAITING.value
        }

        # 按优先级排序：
        # 1. 优先：属于 WAITING Manager 的子节点（确保 Expand→Children→Review 序列）
        # 2. 其次：其他 Ready 节点按 DFS 深度降序
        def _priority_key(n: Node) -> tuple:
            is_child_of_waiting_mgr = (
                n.parent_id is not None and n.parent_id in waiting_managers
            )
            depth = self._compute_depth(n.id)
            # (非优先=0/优先=1, DFS深度降序)
            return (1 if is_child_of_waiting_mgr else 0, depth)

        ready_nodes.sort(key=_priority_key, reverse=True)

        # 并发控制：使用 Manager-aware 的有效活跃数
        effective_running = self._get_effective_running_count()
        slots = min(self.max_concurrency - effective_running, len(ready_nodes))
        if slots <= 0:
            return

        selected = ready_nodes[:slots]

        for node in selected:
            self._execute_node(node)

    def _compute_depth(self, node_id: str, _depth_cache: Optional[dict[str, int]] = None,
                       _path: Optional[set[str]] = None,
                       _recursion_depth: int = 0) -> int:
        """递归计算节点在依赖图中的深度。

        深度 = 最长依赖链长度。无依赖节点深度为 0。
        _path 跟踪当前递归路径，检测环时返回 0 避免栈溢出。
        _depth_cache 缓存已计算节点的深度，避免重复遍历（剪枝）。
        _recursion_depth 跟踪当前递归深度，超过 _MAX_DEPTH 时提前终止。

        Raises:
            RecursionError: 递归深度超过 _MAX_DEPTH，表明依赖链异常长。
        """
        _MAX_DEPTH = 500  # CPython 默认递归限制约 1000，留有余量
        if _recursion_depth > _MAX_DEPTH:
            raise RecursionError(
                f"Dependency graph too deep at node '{node_id}': "
                f"recursion depth exceeded {_MAX_DEPTH}"
            )
        if _depth_cache is None:
            _depth_cache = {}
        if _path is None:
            _path = set()
        if node_id in _path:
            logger.warning("Cycle detected in dependency graph at node '%s'", node_id)
            return 0
        if node_id in _depth_cache:
            return _depth_cache[node_id]
        deps = self.graph.dependencies(node_id)
        if not deps:
            _depth_cache[node_id] = 0
            return 0
        _path.add(node_id)
        # 使用 for 循环替代 max(genexpr) 避免额外函数帧开销
        max_depth = 0
        for dep in deps:
            d = self._compute_depth(dep.id, _depth_cache, _path, _recursion_depth + 1)
            if d > max_depth:
                max_depth = d
        _path.discard(node_id)
        depth = max_depth + 1
        _depth_cache[node_id] = depth
        return depth

    # ========================================================================
    # 节点执行
    # ========================================================================

    def _execute_node(self, node: Node) -> None:
        """将节点转为 Running，通过 Registry 创建 Executable 并提交线程池。

        注意：Manager 节点（exec_type="manager"）计入活跃执行数，
        Waiting 释放槽位。
        """
        # Ready → Running
        self._transition_node(node.id, NodeStatus.RUNNING)

        # 创建 ExecutableContext
        ctx = ExecutableContext(
            node=node,
            runtime=self.runtime_gateway,
            config=node.config,
        )

        # 通过 Registry 创建 Executable 实例
        try:
            executable = self.registry.create(node.exec_type, ctx)
        except Exception as exc:
            logger.error("Failed to create Executable for node '%s': %s", node.id, exc)
            self.graph.update_node(node.id, status=NodeStatus.FAILED)
            self._notify_status_changed(node.id, NodeStatus.FAILED)
            return

        # 提交到线程池
        future = self._thread_pool.submit(self._run_executable_wrapper, node.id, executable, ctx)

        with self._futures_lock:
            # Manager 节点也计入活跃执行数
            self._futures[node.id] = future

    @staticmethod
    def _run_executable_wrapper(node_id: str, executable: Any, ctx: ExecutableContext) -> NodeStatus:
        """在线程池中执行 Executable.run() 的包装方法。

        Returns:
            执行结果的 NodeStatus。
        """
        try:
            result = executable.run(ctx)
            return result
        except Exception as exc:
            logger.error("Executable.run() raised for node '%s': %s", node_id, exc)
            return NodeStatus.FAILED

    # ========================================================================
    # Future 处理
    # ========================================================================

    def _process_completed_futures(self) -> None:
        """检查并处理已完成的 Future。"""
        completed: list[str] = []

        with self._futures_lock:
            for node_id, future in list(self._futures.items()):
                if future.done():
                    completed.append(node_id)

        for node_id in completed:
            with self._futures_lock:
                future = self._futures.pop(node_id, None)

            if future is None:
                continue

            try:
                result_status = future.result()
            except concurrent.futures.CancelledError:
                logger.warning("Future for node '%s' was cancelled", node_id)
                result_status = NodeStatus.STOPPED
            except Exception as exc:
                logger.error("Future for node '%s' raised: %s", node_id, exc)
                result_status = NodeStatus.FAILED

            # 更新节点状态
            node = self.graph.get_node(node_id)
            if node is None:
                continue

            # 只有当前状态是 RUNNING 时才更新
            if node.status == NodeStatus.RUNNING:
                self._transition_node(node_id, result_status)

            # 自动重试：Failed 节点且配置了 Planner 时
            if result_status == NodeStatus.FAILED:
                self._try_auto_retry(node_id)

            # 检测 Manager 子节点完成情况（使用增强版本）
            self._check_manager_children_completed(node_id)

            # 路由处理：节点完成后检查是否有路由决策
            if result_status in (NodeStatus.COMPLETED, NodeStatus.FAILED):
                self._handle_routing(node_id)

    # ========================================================================
    # 状态转换
    # ========================================================================

    def _transition_node(self, node_id: str, target_status: NodeStatus) -> None:
        """将节点转换到目标状态，并通过 delegate 通知。

        尽可能通过 StateMachine 校验合法性，校验失败则降级为直接 setattr。
        """
        node = self.graph.get_node(node_id)
        if node is None:
            return

        old_status = node.status
        if old_status == target_status:
            return

        # 尝试通过 StateMachine 校验
        try:
            StateMachine.validate_transition(
                current_status=old_status,
                current_phase=getattr(node, 'manager_phase', None),
                target_status=target_status,
                target_phase=None,
                exec_type=node.exec_type,
            )
        except Exception:
            # 校验失败也继续执行，由 Node.__setattr__ 做终端状态拦截
            pass

        # 更新状态
        self.graph.update_node(node_id, status=target_status)
        self._notify_status_changed(node_id, target_status)

    def _notify_status_changed(self, node_id: str, status: NodeStatus) -> None:
        """通过 Delegate 通知 Runtime 节点状态变更。"""
        try:
            self.delegate.on_node_status_changed(node_id, status)
        except Exception as exc:
            logger.error("on_node_status_changed failed for node '%s': %s", node_id, exc)

    # ========================================================================
    # Manager 子节点完成检测
    # ========================================================================

    def _check_manager_children_completed(self, child_node_id: str) -> None:
        """检测某个子节点完成后，其父 Manager 的所有子节点是否均已达到终端状态。

        若是，则回调 delegate.on_manager_children_completed()。
        幂等保护：仅当父 Manager 仍处于 WAITING 状态时才触发回调，
        防止同一 tick 多个子节点完成时重复触发。

        Args:
            child_node_id: 刚完成的子节点 ID。
        """
        node = self.graph.get_node(child_node_id)
        if node is None or node.parent_id is None:
            return

        parent_id = node.parent_id
        parent = self.graph.get_node(parent_id)
        if parent is None or parent.exec_type != "manager":
            return

        # 幂等保护：同时使用两种机制
        # 1. Manager 状态检查（当 on_children_completed 已转换状态时生效）
        # 2. _notified_managers 集合（同一 tick 内防止重复回调）
        if parent.status != NodeStatus.WAITING:
            return
        if parent_id in self._notified_managers:
            return

        # 获取所有兄弟子节点
        siblings = self.graph.children(parent_id)
        if not siblings:
            return

        # 检查所有子节点是否均达到终端状态（COMPLETED / FAILED / STOPPED）
        terminal_statuses = {NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.STOPPED}
        all_terminal = all(s.status in terminal_statuses for s in siblings)

        if all_terminal:
            logger.info(
                "All children of manager '%s' completed, notifying delegate",
                parent_id,
            )
            try:
                self.delegate.on_manager_children_completed(parent_id)
                self._notified_managers.add(parent_id)
            except Exception as exc:
                logger.error(
                    "on_manager_children_completed failed for manager '%s': %s",
                    parent_id,
                    exc,
                )

    # ========================================================================
    # 路由处理
    # ========================================================================

    def _handle_routing(self, source_node_id: str) -> None:
        """处理节点的路由决策。

        读取节点上的 pending_routing，验证合法性后激活目标节点。
        激活语义：将目标节点从终端状态回退到 WAITING，
        注入 pending_context，使其携带上游上下文重新进入调度循环。

        安全边界：
        - 跳过不存在的目标节点

        Args:
            source_node_id: 源节点 ID（发出路由决策的节点）。
        """
        source_node = self.graph.get_node(source_node_id)
        if source_node is None:
            return

        routing = getattr(source_node, 'pending_routing', None)
        if routing is None:
            # 合法的「无需协作」场景：节点完成但未产出路由决策，不制造噪音。
            return

        # 清除 pending_routing，防止重复处理
        self.graph.update_node(source_node_id, pending_routing=None)

        if not routing.activate:
            return

        logger.info(
            "Routing from '%s': activating %s, reason=%s",
            source_node_id[:8], routing.activate, routing.reason,
        )

        now_ts = datetime.now().isoformat()

        # 构建 step_ref → UUID 映射（同父 scope 内）
        # routing.activate 中的是 step_id（来自 ExecutionPlan），需要解析为 Node UUID
        step_to_uuid: dict[str, str] = {}
        if source_node.parent_id is not None:
            siblings = self.graph.children(source_node.parent_id)
            for sib in siblings:
                sref = getattr(sib, 'step_ref', None) or ''
                if sref:
                    step_to_uuid[sref] = sib.id

        for step_id in routing.activate:
            # 解析 step_id → Node UUID（支持 "dev" 和 "dev_v1" 两种形式）
            target_uuid = step_to_uuid.get(step_id)
            if target_uuid is None:
                # 回退：可能在 routing 中用的是无后缀 step_id，但 node 的 step_ref 带 _vN
                for sref, uuid_val in step_to_uuid.items():
                    if sref == step_id or sref.startswith(step_id + '_v'):
                        target_uuid = uuid_val
                        break
            if target_uuid is None:
                logger.warning(
                    "Routing target step_id '%s' not found in sibling nodes "
                    "(from '%s'), skipped",
                    step_id, source_node_id[:8],
                )
                continue

            target = self.graph.get_node(target_uuid)
            if target is None:
                continue

            # 统一激活引擎（A3 能力校验、pending_context 注入等
            # 安全边界全部在 _activate_target 内）
            self._activate_target(
                source_node, target, step_id, routing,
                now_ts=now_ts,
            )

        # 处理 mark_terminal：标记某些节点已完成（同样需要 step_id 解析）
        self._apply_mark_terminal(routing.mark_terminal, step_to_uuid)

    def _activate_target(
        self,
        source_node: Any,
        target: Any,
        step_id: str,
        routing: RoutingDecision,
        now_ts: Optional[str] = None,
    ) -> bool:
        """激活单个目标节点（原 _handle_routing 循环体逐行搬移）。

        激活语义：将目标节点回退到 WAITING，注入 pending_context 与 routing_history，
        使其携带上游上下文重新进入调度循环。

        安全边界（方案三调整后保留）：
        - A3 目标规则：Router 是 LLM 决策（已看组织图+角色），允许任意
          Node（含 Manager）成为事件目标；Scheduler 不解析事件语义，不校验目标角色。
        - 父 Manager 通知清除 + rewind_parent_chain_for_retry。

        Args:
            source_node: 源节点（发出路由决策/事件的节点）。
            target: 目标节点（已解析）。
            step_id: 目标在 routing.activate 中的 step_id（上下文 key 与日志用）。
            routing: RoutingDecision（reason 元信息）。
            now_ts: 时间戳（同一路由决策内保持一致）。

        Returns:
            是否成功激活（False 表示被安全边界拦截）。
        """
        # A3 目标规则：Router 已结合组织图+角色做统一决策，允许任意 Node
        # （Worker/Manager）成为目标；Scheduler 不再默认拦截 Manager，也不解析/校验
        # required_role —— 角色判断完全由 Router 负责。

        # 激活次数仅作观测计数，不设上限（C2 激活预算已移除，节点可无限重激活）。
        current_activations = getattr(target, 'activation_count', 0)

        if now_ts is None:
            now_ts = datetime.now().isoformat()

        # 构建路由上下文（key 用 step_id，与 Agent 输出一致）
        ctx = routing.contexts.get(step_id, {})
        ctx['activated_by'] = source_node.id
        ctx['activation_reason'] = routing.reason
        ctx['activated_at'] = now_ts

        # A3 上下文完整性观测：目标被激活但无任何可执行反馈 → 记录警告。
        # 行为不变（仍激活），但暴露"空反馈激活"信号。事件驱动的激活天然带
        # event_content/event_producer，不触发；旧 routing 空 context 才告警。
        actionable_keys = [
            k for k in ctx
            if k not in (
                "activated_by", "activation_reason", "activated_at",
            )
        ]
        if not actionable_keys:
            logger.warning(
                "Routing from '%s' activated '%s' (step=%s) but with empty "
                "feedback context; target lacks instructions to act.",
                source_node.id[:8], target.id[:8], step_id,
            )

        # 合并到已有 pending_context（如果之前被激活过）
        existing_ctx = getattr(target, 'pending_context', None) or {}
        merged_ctx = {**existing_ctx, **ctx}

        # 记录路由历史
        history = list(getattr(target, 'routing_history', []) or [])
        history.append({
            'from': source_node.id,
            'to': target.id,
            'step_id': step_id,
            'reason': routing.reason,
            'timestamp': now_ts,
        })

        # 回退到 WAITING 并注入上下文
        self.graph.update_node(
            target.id,
            status=NodeStatus.WAITING,
            pending_context=merged_ctx,
            activation_count=current_activations + 1,
            routing_history=history,
            artifact_path=None,
            error=None,
            started_at=None,
            finished_at=None,
        )

        # 清除父 Manager 的通知记录，使其可以重新触发 on_children_completed
        if target.parent_id is not None:
            self.clear_manager_notification(target.parent_id)
            if self.planner is not None:
                self.planner.rewind_parent_chain_for_retry(
                    target.parent_id, self.clear_manager_notification,
                )

        logger.info(
            "Routing: activated '%s' (step=%s, activation #%d), context_keys=%s",
            target.id[:8], step_id, current_activations + 1,
            list(merged_ctx.keys()),
        )
        return True

    def _apply_mark_terminal(self, mark_terminal: list[str], step_to_uuid: dict) -> None:
        """处理 mark_terminal：标记某些节点已完成（同样需要 step_id 解析）。"""
        for terminal_step_id in mark_terminal:
            terminal_uuid = step_to_uuid.get(terminal_step_id)
            if terminal_uuid is None:
                for sref, uuid_val in step_to_uuid.items():
                    if sref == terminal_step_id or sref.startswith(terminal_step_id + '_v'):
                        terminal_uuid = uuid_val
                        break
            if terminal_uuid is None:
                continue
            terminal_node = self.graph.get_node(terminal_uuid)
            if terminal_node is not None and terminal_node.status != NodeStatus.COMPLETED:
                self.graph.update_node(
                    terminal_uuid,
                    status=NodeStatus.COMPLETED,
                    finished_at=datetime.now(),
                )
                self._notify_status_changed(terminal_uuid, NodeStatus.COMPLETED)
                logger.info("Routing: marked '%s' (step=%s) as terminal",
                           terminal_uuid[:8], terminal_step_id)

    # ------------------------------------------------------------------
    # 事件→重激活桥（方案三：routing.json / collaboration JSON 移除后，Event 成为
    # 唯一通信载体。Router 已把事件投递到目标 inbox；这里由 Scheduler 把「非运行态
    # 节点 inbox 有事件」解释为「请求该节点行动/重激活」，复用 _activate_target 的
    # 既有安全边界（C2 激活上限/pending_context 注入/父链通知），形成
    # 反馈/协作闭环。Scheduler 不解析 Event 语义，不做第二次路由判断。）
    # ------------------------------------------------------------------

    # 桥处理可由事件唤醒的节点：
    #   WAITING   —— 事件是阶段性产出的可执行信号 → 注入增量上下文并转 READY，
    #                 不等待依赖节点全部完成；无事件时仍由依赖门控转 READY；
    #   COMPLETED —— 收到后续事件 → 注入增量上下文并转 READY，进入 Revise；
    #   FAILED    —— 失败节点不自动复活，必须人工 continue_node → READY 后重新执行。
    # 跳过 RUNNING（运行中自行消费 inbox，不重启）、PAUSED / STOPPED
    # （尊重用户暂停/停止意图）。READY 不重复处理，避免双调度竞态。
    _REACTIVATABLE_STATUSES = (NodeStatus.WAITING, NodeStatus.COMPLETED)

    def _process_reactivation_events(self) -> None:
        """消费各节点 inbox 中的事件并复用激活引擎重激活。

        每节点每 tick 至多处理 1 条（防单次 budget 一次耗尽）；
        转换后被拦截的事件文件也一并删除（delete-on-consume：事件事实仍在全局
        events.json 留痕；被拦截的失败信号已由 _activate_target 记入升级台账，
        不重复尝试）。

        单次消费语义：inbox（``event_inbox/``）是唯一待消费队列；live 不参与扫描。
        RUNNING 节点自行消费（CC 有界轮询），此处只扫非运行态（_REACTIVATABLE）。
        """
        for node in list(self.graph.all_nodes()):
            if node.status not in self._REACTIVATABLE_STATUSES:
                continue
            path, event_data = self._peek_inbox_event(node)
            if event_data is None:
                continue
            try:
                self._consume_inbox_event(node, event_data)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to consume reactivation event for '%s': %s",
                             node.id[:8], exc)
                continue
            if path is not None:
                try:
                    os.remove(path)
                except OSError as exc:
                    logger.warning("Failed to remove consumed inbox event %s: %s",
                                   path, exc)

    def _peek_inbox_event(self, node: Any):
        """返回节点 inbox 中第一条事件 ``(path, data)``；无则 ``(None, None)``。

        任何投递进 inbox 的事件都处理（不判 target/type——投递即 Router 决策过）。
        """
        inbox_dir = os.path.join(_node_workspace(node), "event_inbox")
        matches = sorted(glob.glob(os.path.join(inbox_dir, "*.json")))
        for path in matches:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Failed to parse inbox event %s: %s", path, exc)
                continue
            if isinstance(data, dict):
                return path, data
        return None, None

    def _consume_inbox_event(self, node: Any, event_data: dict) -> None:
        """把一条 inbox 事件转换为路由决策并重激活目标节点。

        方案三：只读 event_id / producer / objective（及 intent/scope/action/
        affected_roles 参考信号）；objective 是事件本身的描述，Scheduler 不解析
        其语义（无 target/type/collaboration_type）。
        """
        event_id = event_data.get("event_id", "?")
        producer = event_data.get("producer", "")
        objective = event_data.get("objective") or event_data.get("content", "")

        # 生产者解析（可能为 node_id 或 step_ref；解析失败以 node 兜底，仍重激活）
        producer_node = self._resolve_node_for_event(producer)

        # 事件 objective → 目标节点 pending_context（自由结构：producer 想让大家知道什么）
        # event_content 保留旧 key（值 = objective），下游 Manager/Worker 仍按此读取
        ctx = {
            "event_content": objective,
            "event_producer": producer,
            "event_intent": event_data.get("intent", ""),
            "event_scope": event_data.get("scope", ""),
            "event_action": event_data.get("action", ""),
            "event_affected_roles": event_data.get("affected_roles", []),
        }

        step_id = node.step_ref or node.id
        reason = f"event {event_id} from {producer}"
        routing = RoutingDecision(
            activate=[node.id],
            contexts={step_id: ctx},
            reason=reason,
        )

        previous_status = node.status
        activated = self._activate_target(
            producer_node or node, node, step_id, routing,
        )
        if activated:
            # 已有子节点的 Manager 收到事件 → 进入 Revise（增量续跑：复用既有子节点），
            # 不能回到 manager_phase=None 后误走全量 expand 重新生成 Main Plan；
            # 也不能直接 review（子节点可能尚未全部终态）。
            if node.exec_type == "manager" and self.graph.children(node.id):
                self.graph.update_node(
                    node.id,
                    manager_phase="expand",
                    pending_revise=True,
                )

            # 事件是独立于依赖终态的可执行信号：目标 WAITING/COMPLETED
            # 注入增量上下文后直接进入 READY，不再等
            # _process_waiting_nodes() 的依赖门控。
            self._transition_node(node.id, NodeStatus.READY)
            logger.info(
                "Event reactivation: '%s' %s -> READY",
                node.id[:8], previous_status.value,
            )

    def _resolve_node_for_event(self, node_id_or_stepref: str):
        """把事件 producer/target（node_id 或 step_ref）解析为图中节点。"""
        if not node_id_or_stepref:
            return None
        node = self.graph.get_node(node_id_or_stepref)
        if node is not None:
            return node
        for n in self.graph.all_nodes():
            if (n.step_ref or "") == node_id_or_stepref:
                return n
        return None

    # ========================================================================
    # 自动重试机制
    # ========================================================================

    def _try_auto_retry(self, failed_node_id: str) -> None:
        """尝试自动原地重试失败的节点。

        条件：
        - planner 已配置
        - 节点为 Worker/Manager 类型
        - 未超出最大重试次数（由 Node.max_iterations 控制）

        原地重试策略：
        - Manager 有子节点 → READY + manager_phase="review"（仅重新审查）
        - Manager 无子节点 → WAITING + 延迟 → READY（重新 expand）
        - Worker         → WAITING + 延迟 → READY（重新执行）

        Args:
            failed_node_id: 失败节点 ID。
        """
        if self.planner is None:
            return

        failed_node = self.graph.get_node(failed_node_id)
        if failed_node is None:
            return

        if failed_node.exec_type not in ("worker", "manager"):
            return

        attempt = self._retry_attempts.get(failed_node_id, 0)
        max_retries = getattr(failed_node, "max_iterations", 3)

        if attempt >= max_retries:
            logger.info(
                "Auto-retry exhausted for node '%s': %d/%d attempts",
                failed_node_id, attempt, max_retries,
            )
            return

        new_iteration = attempt + 1

        # 清理缓存的 AICU 实例，确保重试时获得全新会话
        # （否则 --resume 会续接失败的对话，携带错误状态）
        try:
            from executables.worker import Worker
            with Worker._aicu_cache_lock:
                Worker._aicu_cache.pop(failed_node_id, None)
        except Exception:
            pass

        # 清理磁盘上的旧产物文件
        self._delete_artifact_file(failed_node.artifact_path)

        # Manager 有子节点且全部终态 → 仅重新 review，避免重复 expand 产生重复子节点；
        # 子节点未全部终态 → 置于 WAITING+waiting 等待子节点完成后由
        # on_manager_children_completed 正常触发评审（不提前 review 未完成的子节点，
        # 也不 re-expand 清掉运行中的子节点）。
        children = self.graph.children(failed_node_id)
        terminal_statuses = {NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.STOPPED}
        if (
            failed_node.exec_type == "manager"
            and children
            and all(c.status in terminal_statuses for c in children)
        ):
            self.graph.update_node(
                failed_node_id,
                status=NodeStatus.READY,
                artifact_path=None,
                error=None,
                manager_phase="review",
                started_at=None,
                finished_at=None,
                iteration_count=new_iteration,
                log=[],  # 清空上次执行日志
            )
            self._retry_attempts[failed_node_id] = new_iteration
            self.planner.rewind_parent_chain_for_retry(
                failed_node.parent_id, self.clear_manager_notification,
            )
            logger.info(
                "Auto-retry (re-review): manager=%s, attempt=%d/%d",
                failed_node_id, new_iteration, max_retries,
            )
            return

        # Manager 有子节点但未全部终态 → 不消耗重试次数，先回退 WAITING 等子节点跑完，
        # 由 _check_manager_children_completed → on_manager_children_completed 驱动评审。
        if failed_node.exec_type == "manager" and children:
            self.graph.update_node(
                failed_node_id,
                status=NodeStatus.WAITING,
                artifact_path=None,
                error=None,
                manager_phase="waiting",
                started_at=None,
                finished_at=None,
                log=[],  # 清空上次执行日志
            )
            # 清除自身通知记录，确保子节点全部终态后能重新触发 children-completed 回调
            self.clear_manager_notification(failed_node_id)
            logger.info(
                "Auto-retry paused manager=%s: %d children not all terminal; "
                "waiting for on_manager_children_completed",
                failed_node_id, len(children),
            )
            return

        # Worker 或 Manager 无子节点 → WAITING + 延迟调度
        self.graph.update_node(
            failed_node_id,
            status=NodeStatus.WAITING,
            artifact_path=None,
            error=None,
            manager_phase=None,
            started_at=None,
            finished_at=None,
            iteration_count=new_iteration,
            log=[],  # 清空上次执行日志
        )

        # 同步 step_ref
        cur_step = failed_node.step_ref
        if cur_step is not None:
            if "_v" in cur_step:
                cur_step = cur_step[:cur_step.rfind("_v")]
            self.graph.update_node(
                failed_node_id,
                step_ref=f"{cur_step}_v{new_iteration}",
            )

        # 回退父 Manager 链
        self.planner.rewind_parent_chain_for_retry(
            failed_node.parent_id, self.clear_manager_notification,
        )

        self._retry_attempts[failed_node_id] = new_iteration

        # 延迟调度：记录 ready_time，到期后由 _process_scheduled_retries 转为 READY
        ready_time = time.time() + self.retry_delay
        self._scheduled_retries[failed_node_id] = ready_time

        logger.info(
            "Auto-retry scheduled in-place: node=%s, attempt=%d/%d, delay=%.1fs",
            failed_node_id, new_iteration, max_retries, self.retry_delay,
        )

    def _process_scheduled_retries(self) -> None:
        """处理已到时间的定时重试节点。

        原地重试时节点为 WAITING 状态，到期后转为 READY 使其被正常调度。
        """
        now = time.time()
        ready_retries = [
            nid for nid, t in self._scheduled_retries.items()
            if now >= t
        ]
        for nid in ready_retries:
            del self._scheduled_retries[nid]
            # 原地重试后节点为 WAITING，到期转为 READY 进入正常调度
            node = self.graph.get_node(nid)
            if node is not None and node.status == NodeStatus.WAITING:
                self._transition_node(nid, NodeStatus.READY)

    # ========================================================================
    # Internal helpers
    # ========================================================================

    def _delete_artifact_file(self, artifact_path: Optional[str]) -> None:
        """删除磁盘上的产物文件（如存在）。

        Args:
            artifact_path: 产物文件路径（相对或绝对），为 None 时跳过。
        """
        if not artifact_path:
            return
        workspace_root = getattr(self.runtime_gateway, "workspace_root", None) or ""
        if not os.path.isabs(artifact_path):
            artifact_path = os.path.join(workspace_root, artifact_path)
        try:
            if os.path.exists(artifact_path):
                os.remove(artifact_path)
                logger.info("Deleted old artifact file: %s", artifact_path)
        except OSError as exc:
            logger.warning("Failed to delete artifact file '%s': %s", artifact_path, exc)

    # ========================================================================
    # Watchdog 机制
    # ========================================================================

    def _handle_watchdog_error(self, exc: Exception) -> None:
        """Watchdog 异常捕获与自动重启。

        最多重试 _max_watchdog_retries 次，最小间隔 _watchdog_min_interval 秒。
        """
        now = time.time()
        if now - self._last_watchdog_time < self._watchdog_min_interval:
            logger.warning("Watchdog: skipping restart (min interval not elapsed)")
            return

        if self._watchdog_retries >= self._max_watchdog_retries:
            logger.error("Watchdog: max retries (%d) reached, stopping scheduler", self._max_watchdog_retries)
            self._running = False
            return

        self._watchdog_retries += 1
        self._last_watchdog_time = now
        logger.info("Watchdog: restarting tick loop (retry %d/%d)", self._watchdog_retries, self._max_watchdog_retries)

        # 重启：tick 循环会在下一次迭代自动恢复
        # 如果线程池已关闭，重新创建
        if self._pool_shutdown:
            pool_size = self.max_concurrency + 2
            self._thread_pool = ThreadPoolExecutor(max_workers=pool_size)
            self._pool_shutdown = False
