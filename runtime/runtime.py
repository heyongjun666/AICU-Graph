"""
runtime/runtime.py — Runtime 协调整合引擎（Basic Version）

Runtime 是系统的中枢协调器，负责：
- 初始化并管理所有核心组件（Graph/Planner/Persistence/Scheduler/EventBus/Registry）
- 实现 RuntimeGateway 协议（供 Executable 回调）
- 实现 SchedulerDelegate 协议（供 Scheduler 回调）
- 提供节点级和全局级的生命周期控制 API
- 管理持久化与崩溃恢复
"""

import logging
import os
import posixpath
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

from executables.base import RuntimeGateway, SchedulerDelegate
from executables.registry import ExecutableRegistry
from runtime.event_bus import EventBus, SSEEvent
from runtime.graph import Graph
from runtime.models import (
    Edge,
    EdgeType,
    ExecutionPlan,
    LogEntry,
    LogLevel,
    Node,
    NodeStatus,
    Step,
)
from runtime.persistence import Persistence
from runtime.planner import PlanValidationError, Planner
from runtime.scheduler import Scheduler
from runtime.workspace_resolver import _partition_of, resolve_shared_workspace

from runtime.delivery.gate import FRAMEWORK_DIR_NAMES
from runtime.delivery.preview import mime_type_for, previewable_path

logger = logging.getLogger(__name__)

# 手动 Continue（人工干预）额外授予的 Manager review 迭代预算：
# 将 max_iterations 提升为 max(原上限, 当前 iteration_count + 本常量)，
# 保证至少再多给这么多轮机会。原实现锁死为 iteration_count 会导致
# 审查守卫 `iteration_count + 1 < max_iterations` 恒 False → 立即再次 FAILED。
MANUAL_CONTINUE_BUDGET = 3


class Runtime(RuntimeGateway, SchedulerDelegate):
    """Runtime 协调整合引擎。

    实现 RuntimeGateway 和 SchedulerDelegate 双协议，
    作为中枢连接 Scheduler、Executable、Graph、EventBus 等组件。

    Args:
        graph:           Graph 实例（未提供则自动创建）。
        planner:         Planner 实例（未提供则自动创建）。
        persistence:     Persistence 实例（未提供则自动创建）。
        scheduler:       Scheduler 实例（未提供则自动创建）。
        registry:        ExecutableRegistry 实例（未提供则自动创建）。
        event_bus:       EventBus 实例（未提供则自动创建）。
        max_concurrency: Scheduler 最大并发数（默认 4）。
        workspace_root:  工作区根目录（用于 read_artifact）。
    """

    def __init__(
        self,
        graph: Optional[Graph] = None,
        planner: Optional[Planner] = None,
        persistence: Optional[Persistence] = None,
        scheduler: Optional[Scheduler] = None,
        registry: Optional[ExecutableRegistry] = None,
        event_bus: Optional[EventBus] = None,
        max_concurrency: int = 4,
        workspace_root: Optional[str] = None,
        roles_dir: Optional[str] = None,
        router_aicu_instance: Optional[Any] = None,
        router_config: Optional[dict[str, Any]] = None,
    ):
        self.max_concurrency = max_concurrency
        self.workspace_root = workspace_root or os.getcwd()

        # PlanValidator 角色集（exec_type↔role 一致性校验用）。
        # roles_dir 未注入时保持 None → 该校验跳过（向后兼容，不依赖 roles 目录）。
        self._manager_role_ids: Optional[set[str]] = None
        self._worker_role_ids: Optional[set[str]] = None
        if roles_dir:
            try:
                from runtime.roles_manager import RolesManager
                roles = RolesManager(roles_dir).list_roles()
                self._manager_role_ids = {
                    r["id"] for r in roles if r["type"] == "manager"
                }
                self._worker_role_ids = {
                    r["id"] for r in roles if r["type"] == "worker"
                }
            except Exception as e:  # 角色加载失败不阻断运行，仅记录
                logger.warning(
                    "PlanValidator: failed to load roles from %s: %s", roles_dir, e
                )

        # 初始化核心组件
        self.graph = graph or Graph()
        self.event_bus = event_bus or EventBus()
        self.registry = registry or ExecutableRegistry()
        self.planner = planner or Planner(self.graph)
        self.persistence = persistence or Persistence()
        # Agent 事件总线（Worker→Runtime→Worker 动态协作信号）
        from runtime.agent_events import AgentEventManager
        # 独立 AICU Router；attach_graph 时同步注入 Graph。
        from runtime.router import AICURouter
        self.router = AICURouter(
            aicu_instance=router_aicu_instance,
            workspace_root=self.workspace_root,
            config=router_config,
        )
        self.agent_events = AgentEventManager(
            workspace_root=self.workspace_root,
            router=self.router,
        )
        self.agent_events.attach_graph(self.graph)

        # Scheduler 需要 Runtime 作为 delegate 和 runtime_gateway
        self.scheduler = scheduler or Scheduler(
            graph=self.graph,
            registry=self.registry,
            delegate=self,
            runtime_gateway=self,
            max_concurrency=max_concurrency,
            event_bus=self.event_bus,
            planner=self.planner,
        )

        # 注册 Graph mutation → EventBus 桥接
        self.graph.set_on_mutation(self._on_graph_mutation)

        # 内部状态
        self._root_manager_id: Optional[str] = None
        self._external_input_waits: dict[str, dict] = {}
        self._shutdown = False
        self._lock = threading.Lock()
        self._global_pause_mode: Optional[str] = None
        self._pause_completion_callback: Optional[Callable[[list[str]], None]] = None
        self._node_status_callback: Optional[Callable[[str, NodeStatus], None]] = None
        self._pause_lock = threading.Lock()
        self._global_interrupted_node_ids: list[str] = []
        self.recovered_interrupted_node_ids: list[str] = []

    # ========================================================================
    # Lifecycle
    # ========================================================================

    def _ensure_scheduler_running(self) -> None:
        """Restart the idle scheduler after a legal graph mutation."""
        if not self._shutdown:
            self.scheduler.start()

    def start(self, *, paused: bool = False, safe_recovery: bool = False) -> None:
        """启动 Runtime。

        顺序：加载持久化状态 → 恢复 Graph → 崩溃降级 → 启动 Scheduler → 启动定期保存。
        ``safe_recovery`` keeps crashed nodes paused so a restored session cannot run
        until SessionManager receives an explicit resume request.
        """
        # 1. 加载持久化状态
        snapshot = self.persistence.load()
        if snapshot is not None:
            logger.info(
                "Loaded persisted state (version=%d, nodes=%d, edges=%d, crashed=%d)",
                snapshot.graph_version,
                len(snapshot.nodes),
                len(snapshot.edges),
                len(snapshot.crashed),
            )
            # 恢复 Node 和 Edge
            nodes = list(snapshot.nodes.values())
            edges = list(snapshot.edges.values())
            self.planner.restore(nodes, edges, skip_validation=True)

            # 故障安全恢复：恢复会话不能自动重跑快照中的 RUNNING 节点。
            for node in snapshot.crashed:
                live_node = self.graph.get_node(node.id)
                if live_node and live_node.status == NodeStatus.RUNNING:
                    if safe_recovery:
                        self.graph.update_node(live_node.id, status=NodeStatus.PAUSED)
                        self.recovered_interrupted_node_ids.append(live_node.id)
                        logger.info("Recovered crashed node '%s' as PAUSED", node.id)
                    else:
                        self.graph.update_node(live_node.id, status=NodeStatus.FAILED)
                        logger.info("Downgraded crashed node '%s' to FAILED", node.id)

            # 检测根 Manager
            for node in self.graph.all_nodes():
                if node.parent_id is None and node.exec_type == "manager":
                    self._root_manager_id = node.id
                    break

            # Manager Phase 恢复：根据子节点存在性确定 phase
            for node in self.graph.all_nodes():
                if node.exec_type != "manager":
                    continue

                # 仅在 manager_phase 为 None 时根据有无 children 推断初始相位
                if node.manager_phase is not None:
                    # 对 WAITING/waiting 状态下的 manager 做一次「child 全部终态」
                    # 补偿扫描；防止 Runtime 在 child 全 COMPLETED 后才崩溃，
                    # 没人触发 on_manager_children_completed → 卡死。
                    # 注意：根 Manager（parent_id=None）同样需要此补偿——它展开出
                    # 子 Manager 后进入 WAITING/waiting，若子节点全部终态后崩溃，
                    # 恢复时若不推进为 review，根会永久卡 WAITING。
                    if (
                        node.status == NodeStatus.WAITING
                        and node.manager_phase == "waiting"
                        and not getattr(node, "pending_revise", False)
                    ):
                        kids = self.graph.children(node.id)
                        terminal = {
                            NodeStatus.COMPLETED,
                            NodeStatus.FAILED,
                            NodeStatus.STOPPED,
                        }
                        if kids and all(k.status in terminal for k in kids):
                            self.graph.update_node(
                                node.id,
                                manager_phase="review",
                                status=NodeStatus.READY,
                            )
                            logger.info(
                                "Recovered waiting manager '%s' "
                                "(all %d children terminal) -> READY/review",
                                node.id, len(kids),
                            )
                    continue

                # manager_phase is None —— 从未执行过的新建 manager
                children = self.graph.children(node.id)
                if children:
                    self.graph.update_node(
                        node.id,
                        manager_phase="review",
                    )
                    logger.info(
                        "Restored manager '%s' phase=REVIEW (has %d children)",
                        node.id, len(children),
                    )
                else:
                    self.graph.update_node(
                        node.id,
                        manager_phase="expand",
                    )
                    logger.info(
                        "Restored manager '%s' phase=EXPAND (no children)",
                        node.id,
                    )

        # 重设版本号：恢复(restore)与崩溃降级/manager-phase 规整都是确定性的内部
        # 状态回放，不构成新的用户可见变更。必须等它们全部结束后再把版本号设为
        # 快照保存值，否则恢复路径会从 0 重新累加 / 被 recovery 的 update_node 误增，
        # 导致跨重启版本号漂移（AC8 契约：restore yields consistent version）。
        if snapshot is not None:
            self.graph.restore_version(snapshot.graph_version)

        # Persist recovery normalization before the scheduler can observe the graph.
        if safe_recovery and snapshot is not None:
            self.persistence.save(self.graph)

        # 恢复 Agent 事件日志（重启后继续去重/重放；遗留发布文件由调度线程 scan 摄取）
        try:
            self.agent_events.load(recover_delivery=False)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load agent events: %s", exc)

        # 2. 启动 Scheduler。恢复中的 Session 在 tick 线程启动前先关闸。
        if paused:
            self.scheduler.pause()
        self.scheduler.start()
        logger.info("Scheduler started (paused=%s)", paused)

        # 3. 启动定期持久化
        self.persistence.start_periodic_save(self.graph)

        logger.info("Runtime started")

    def _prepare_node_config(
        self,
        node_id: str,
        config: Optional[dict],
        parent_id: Optional[str] = None,
    ) -> dict:
        prepared = dict(config or {})
        if parent_id:
            parent = self.graph.get_node(parent_id)
            if parent is None:
                raise ValueError(f"Parent node '{parent_id}' not found")
            parent_config = parent.config or {}
            for key in (
                "cli_name",
                "model",
                "api_base_url",
                "api_key_env",
                "skip_permissions",
                "skills",
            ):
                if key in parent_config and key not in prepared:
                    prepared[key] = parent_config[key]
            workspace_root = parent_config.get("_workspace_root", self.workspace_root)
            parent_relpath = parent_config.get(
                "_workspace_relpath", f"nodes/{parent.id}"
            )
            relpath = os.path.join(parent_relpath, "children", node_id)
        else:
            workspace_root = self.workspace_root
            relpath = os.path.join("nodes", node_id)
        prepared["_workspace_root"] = os.path.abspath(workspace_root)
        prepared["_workspace_relpath"] = relpath

        # ---- Shared Workspace（Session 级 · 自动挂载） ----
        # 原则：一个 Session 只有一个 Shared Workspace = {workspace_root}/shared。
        # 所有节点（Manager/Worker）自动挂载；共享区默认全量可读，写入受 output_path 分区约束。
        # output_path 是节点"专属分区"（分工），缺省空串 = 共享根（全局写权限，高权限模式）。
        # 最终路径语义不变量：
        #   Session Shared Workspace = {session}/shared/          （_shared_workspace，框架决定）
        #   Node Private Workspace   = {session}/nodes/{id}/      （仅自身运行态/私有临时数据）
        #   Node 写入分区            = {session}/shared/{output_path}（_output_path，Manager 分工）
        #   nodes/{id}/output/       = Node 私有交付目录，不等于 output_path。
        # 旧 plan 声明的 shared_workspace 组字段被接受但忽略（不再参与路径派生）；
        # 仍做防御性简单标识符校验（防穿越），向后兼容旧 plan。
        if prepared.get("shared_workspace") is not None:
            self._validate_shared_group_id(prepared["shared_workspace"])
        output_path = self._validate_shared_output_path(
            prepared.get("output_path")
        )
        prepared["_output_path"] = output_path
        shared_root = resolve_shared_workspace(self.workspace_root)
        prepared["_shared_workspace"] = shared_root
        os.makedirs(_partition_of(shared_root, output_path), exist_ok=True)

        # ---- Event Bus 配置归一化 ----
        # config.events = { publish: [...], subscribe: [...] }，均为 list[str] 非空字符串。
        # 归一化保证订阅匹配（_subscribes）与 Worker prompt 注入的前提是合法形状。
        if prepared.get("events") is not None:
            prepared["events"] = self._normalize_events_config(prepared["events"])
        return prepared

    # ========================================================================
    # Shared Workspace 校验与派生（Session 级自动挂载）
    # ========================================================================

    @staticmethod
    def _validate_shared_group_id(group: str) -> None:
        """校验 shared_workspace 组 ID：必须是简单标识符。

        防路径穿越：非空、非绝对路径、不含 '/' '\\' 与 '..' 段，
        否则 ``shared_workspace: \"../../etc\"`` 会让协作区逃逸父 workspace。
        """
        if not isinstance(group, str) or not group.strip():
            raise ValueError("shared_workspace must be a non-empty string")
        g = group.strip()
        if os.path.isabs(g) or "/" in g or "\\" in g:
            raise ValueError(
                f"shared_workspace must be a simple group id "
                f"(no absolute path or separators), got: {group!r}"
            )
        if g in (".", "..") or g == "":
            raise ValueError(
                f"shared_workspace must be a simple group id "
                f"(no '.' or '..'), got: {group!r}"
            )

    @staticmethod
    def _validate_shared_output_path(output_path: Any) -> str:
        """校验并规范化 output_path 分区（相对路径，去首尾斜杠，禁绝对/..）。

        Returns:
            规范化后的分区相对路径；空串/None 表示组根（节点直接写共享根）。
        """
        if output_path is None:
            return ""
        if not isinstance(output_path, str):
            raise ValueError("output_path must be a string")
        op = output_path.strip()
        if op == "":
            return ""
        if os.path.isabs(op):
            raise ValueError(
                f"output_path must be a relative path, got: {output_path!r}"
            )
        norm = posixpath.normpath(op.replace("\\", "/")).strip("/")
        if norm == "" or norm == "." or ".." in norm.split("/"):
            raise ValueError(
                f"output_path must be a relative path without '..' segments, "
                f"got: {output_path!r}"
            )
        return norm

    @staticmethod
    def _normalize_events_config(events_cfg: Any) -> dict:
        """宽容归一化 config.events（方案三：配置已非必需，只做安全清理，绝不抛错）。

        方案三（独立 AICU Router）下 events 是历史遗留字段：
          - ``publish`` 不再参与门控（Worker prompt 无条件注入发布指令，发布默认开启）；
          - ``subscribe`` 是死代码（路由全由 Router 决定，无订阅语义）。
        此处仅做防御性清洗——容忍任何形状（非 dict / 非 list / 空串 / 非字符串项），
        提取非空字符串列表，异常形状一律降级为空列表，**保证规划器任意输出都不会让
        inject_plan 崩溃**（历史事故：LLM 产出 ``events.publish: [""]`` 触发 ValueError，
        导致 Manager 扩展示范计划失败而 FAILED 而非 WAITING）。
        """
        if not isinstance(events_cfg, dict):
            return {}
        normalized: dict = {}
        for key in ("publish", "subscribe"):
            val = events_cfg.get(key)
            if not isinstance(val, list):
                normalized[key] = []
                continue
            normalized[key] = [
                item.strip() for item in val
                if isinstance(item, str) and item.strip()
            ]
        return normalized


    @classmethod
    def _collect_shared_candidates(
        cls, configs: list[dict]
    ) -> list[tuple[str, bool]]:
        """校验并收集一组 config 的分区所有权候选（output_path 防穿越）。

        仅**显式非空 output_path** 构成"分区所有权"（全局唯一）；空串/缺省
        （共享根）是公共读区，不构成所有权，也不参与冲突检测。
        候选形状 (output_path, allow_shared_output)。
        仍保留 shared_workspace 组字段的防御性简单标识符校验（向后兼容旧 plan，
        但该字段不再参与任何路径派生）。
        """
        candidates: list[tuple[str, bool]] = []
        for config in configs:
            cfg = config or {}
            if cfg.get("shared_workspace") is not None:
                cls._validate_shared_group_id(cfg["shared_workspace"])
            output_path = cls._validate_shared_output_path(cfg.get("output_path"))
            if not output_path:
                continue  # 空分区 = 共享根公共读区，不构成所有权
            allow = bool(cfg.get("allow_shared_output", False))
            candidates.append((output_path, allow))
        return candidates

    @staticmethod
    def _validate_shared_output_conflicts(
        candidates: list[tuple[str, bool]],
    ) -> None:
        """共享分区所有权提示：同一非空 output_path 被多个节点声明时仅告警。

        2026-08-16 按用户要求放开限制：不再硬拒绝（共享 FS 天然多写者，
        同一分区被多节点协作写入是合法模式，如两 worker 拆写同一模块目录）。
        保留告警日志便于发现非预期的写冲突；并发写同一文件路径的
        正确性由协作契约/产物 Gate 负责。allow_shared_output: true 可静默告警。
        """
        seen: dict[str, bool] = {}
        for output_path, allow in candidates:
            if output_path in seen:
                if not (allow or seen[output_path]):
                    logger.warning(
                        "Shared workspace output shared by multiple nodes: "
                        "output_path=%r (allow_shared_output: true 可静默此告警)",
                        output_path,
                    )
                seen[output_path] = True
            else:
                seen[output_path] = allow

    @classmethod
    def _prevalidate_shared_configs(cls, configs: list[dict]) -> None:
        """共享配置前置校验：遍历防护 + 冲突检测（任何图变更前调用）。"""
        candidates = cls._collect_shared_candidates(configs)
        cls._validate_shared_output_conflicts(candidates)

    def _validate_plan_with_roles(self, plan: ExecutionPlan) -> None:
        """PlanValidator exec_type↔role 一致性校验（任何图变更前调用）。

        失败抛 PlanValidationError，保证校验失败不修改 Graph。
        """
        from runtime.plan_validator import validate_errors as _validate_errors

        errors = _validate_errors(
            plan,
            manager_roles=self._manager_role_ids,
            worker_roles=self._worker_role_ids,
        )
        if errors:
            raise PlanValidationError(errors)

    def _warn_plan_structure(self, plan: ExecutionPlan) -> None:
        """PlanValidator 软告警（不拒绝，仅日志可观测性）。"""
        from runtime.plan_validator import validate_warnings as _validate_warnings

        for warning in _validate_warnings(plan):
            logger.warning("PlanValidator: %s", warning)

    def _validate_manual_role(self, exec_type: str, role: str) -> None:
        """create_manual_node 的单节点 exec_type↔role 一致性校验（建节点前调用）。"""
        if exec_type == "manager" and self._manager_role_ids is not None:
            if role not in (set(self._manager_role_ids) | {"default"}):
                raise PlanValidationError(
                    [f"exec_type=manager with role '{role}', must be a Manager role"]
                )
        elif exec_type == "worker" and self._worker_role_ids is not None:
            if role not in (set(self._worker_role_ids) | {"default"}):
                raise PlanValidationError(
                    [f"exec_type=worker with role '{role}', must be a Worker role"]
                )

    def _classify_role_type(self, role: str) -> Optional[str]:
        """按角色集把 role 归类为 ``"manager"`` / ``"worker"``；default 或未知返回 None。

        用于 submit_fix_plan 把 exec_type 与 fix.role 的类型对齐（PlanValidator 一致性）：
        default 在 Manager/Worker 角色集均合法、未知角色不参与，两类都沿用子节点 exec_type。
        """
        if role == "default":
            return None
        if self._manager_role_ids is not None and role in self._manager_role_ids:
            return "manager"
        if self._worker_role_ids is not None and role in self._worker_role_ids:
            return "worker"
        return None

    def submit_root_manager(
        self, task: str, role: str = "default", config: Optional[dict] = None
    ) -> str:
        """提交根 Manager 节点。

        Args:
            task:   根 Manager 的任务描述。
            role:   角色名称（默认 "coder"）。
            config: 配置覆盖项。

        Returns:
            创建的根 Manager Node ID。

        Raises:
            RuntimeError: 根 Manager 已存在（单根保护）。
        """
        with self._lock:
            # 单根保护
            if self._root_manager_id is not None:
                raise RuntimeError(
                    f"Root manager already exists (node_id={self._root_manager_id})"
                )

            # 双重检查：Graph 中是否已有根 Manager
            for node in self.graph.all_nodes():
                if node.parent_id is None and node.exec_type == "manager":
                    self._root_manager_id = node.id
                    raise RuntimeError(
                        f"Root manager already exists (node_id={node.id})"
                    )

            node_id = str(uuid.uuid4())
            node = Node(
                id=node_id,
                parent_id=None,
                exec_type="manager",
                role=role,
                task=task,
                config=self._prepare_node_config(node_id, config),
                status=NodeStatus.READY,
                manager_phase="expand",
            )
            self.graph.add_node(node)
            self._root_manager_id = node.id

        # 确保 Scheduler 运行以调度新节点
        self._ensure_scheduler_running()

        logger.info("Root manager created: node_id=%s", node.id)
        return node.id

    def inject_plan(self, parent_node_id: str, plan: ExecutionPlan) -> list[Node]:
        """提交 ExecutionPlan，经 Planner 翻译后注入 Graph。

        全量重建通道：仅允许注入到「没有子节点」的父节点（首跑 / 无子节点重试）。
        若父节点已有子节点，必须走 Revise 通道（inject_revision_plan / submit_revision_plan）
        选择性复用子节点——代码层面不再清理既有子节点（清理是用户前端手动操作）。

        Args:
            parent_node_id: 父 Manager 节点 ID。
            plan:           ExecutionPlan。

        Returns:
            创建的 Node 列表。

        Raises:
            ValueError: 父节点已有子节点（须走 Revise，不得全量重建）。
        """
        # 共享配置前置校验（遍历防护 + 冲突检测）：在创建任何图节点之前完成，
        # 保证校验失败不污染 Graph、不留半成品子节点。
        self._prevalidate_shared_configs([s.config for s in plan.steps])

        # PlanValidator exec_type↔role 一致性：任何图变更前拦截（roles_dir 未注入时跳过）
        self._validate_plan_with_roles(plan)

        # 全量通道禁止注入到有子节点的父节点：既不清除旧子节点，也不允许新旧并存
        # （重复 step_ref 会破坏 step_ref 解析）。有子节点的 Manager 重激活一律走 Revise。
        if self.graph.children(parent_node_id):
            raise ValueError(
                f"cannot full-rebuild parent '{parent_node_id}' with existing children; "
                "use inject_revision_plan / submit_revision_plan (revise) to reuse children"
            )

        nodes = self.planner.translate(plan, parent_node_id)
        for node in nodes:
            prepared = self._prepare_node_config(
                node.id, node.config, parent_node_id
            )
            self.graph.update_node(node.id, config=prepared)
            node.config = prepared
        # PlanValidator 软告警（不拒绝，仅日志可观测性）
        self._warn_plan_structure(plan)
        logger.info(
            "Plan injected: parent=%s, nodes=%d", parent_node_id, len(nodes)
        )
        self._ensure_scheduler_running()
        return nodes

    def submit_fix_plan(
        self,
        parent_node_id: str,
        failed_assessments: list[dict],
        iteration_count: int,
    ) -> list[Node]:
        """RuntimeGateway.submit_fix_plan — 原地续接修复失败子节点（保留会话与产出）。

        与 submit_plan 的区别：
          - submit_plan → 全量 rebuild（仅允许无子节点父节点，见 inject_plan）
          - submit_fix_plan → 不删除任何子节点：每个失败子节点重置为 READY、注入修复
            上下文（pending_context，含 collaboration_type="repair" + 缺陷明细），由
            调度器原地重新拉起。worker 依据 _aicu_cache + --resume 续接原会话，保留
            节点工作区与产出（复用"路由激活"的注入通道，但语义为 Manager review 修复）。

        角色兼容规则（修复能力保障）：
          - fix.role 为空或等于子节点当前角色 → 原地续接（复用原会话，最有价值）
          - fix.role 为其他角色（能力集不同，原对象无能力修）→ 仍不删除节点：
            原地更新 role/exec_type 到修复角色 + 清除 _aicu_cache（新能力 → 新会话），
            保留节点身份/工作区/历史。代码层面不存在任何"删除子节点"的机制。

        Args:
            parent_node_id: Manager 节点 ID。
            failed_assessments: 失败评估列表 [{"step_ref", "fix": {"role", "task", ...}}, ...]
            iteration_count: 当前迭代次数。

        Returns:
            受影响的子节点列表（全部为原地续接的原节点）。
        """
        # 1. 捕获失败子节点共享配置（原地续接/重建都不得丢失
        #    shared_workspace/output_path，否则失败的共享 worker 会退出共享组）
        children = self.graph.children(parent_node_id)
        failed_step_refs = {a["step_ref"] for a in failed_assessments}
        failed_by_ref: dict[str, Node] = {}
        old_shared_by_ref: dict[str, dict] = {}
        for child in children:
            child_ref = child.step_ref or child.id
            if child_ref in failed_step_refs:
                failed_by_ref[child_ref] = child
                child_cfg = child.config or {}
                if child_cfg.get("shared_workspace"):
                    old_shared_by_ref[child_ref] = {
                        "shared_workspace": child_cfg["shared_workspace"],
                        "output_path": child_cfg.get("output_path"),
                    }
                    if child_cfg.get("allow_shared_output"):
                        old_shared_by_ref[child_ref]["allow_shared_output"] = True

        # 2. 构造修复步骤（合并旧共享配置）+ PlanValidator 早期校验（任何图变更前拦截）。
        #    原地续接时 fix_role 取原角色（或 fix.role 显式指定的同角色），
        #    校验保证 exec_type↔role 一致（如 worker 子节点不得被指定 manager 角色）。
        fix_steps: list[Step] = []
        for a in failed_assessments:
            fix = a.get("fix", {})
            config = dict(fix.get("config") or {})
            old_cfg = old_shared_by_ref.get(a["step_ref"]) or {}
            for key in ("shared_workspace", "output_path", "allow_shared_output"):
                if key in old_cfg and key not in config:
                    config[key] = old_cfg[key]
            child = failed_by_ref.get(a["step_ref"])
            fix_role = fix.get("role") or (child.role if child else "default")
            # exec_type 与角色类型对齐（PlanValidator exec_type↔role 一致性）：
            # 修复 Manager 子节点（如 engineering_coord）必须保留 exec_type="manager"，
            # 否则 PlanValidationError 会拒绝修复计划、Manager 反复 FAILED 到 max_iterations。
            # 显式指定了角色时按角色类型对齐（manager 角色→manager，worker 角色→worker）；
            # default/未知角色沿用子节点 exec_type 作为中性默认。
            fix_exec_type = child.exec_type if child else "worker"
            if fix.get("role"):
                role_type = self._classify_role_type(fix_role)
                if role_type is not None:
                    fix_exec_type = role_type
            step = Step(
                step_id=a["step_ref"],
                exec_type=fix_exec_type,
                role=fix_role,
                task=fix.get("task", a.get("reason", "Fix issue")),
                task_summary=fix.get("task_summary")
                or (child.task_summary if child else ""),
                config=config,
                depends_on=[],
            )
            fix_steps.append(step)

        fix_plan = ExecutionPlan(steps=fix_steps)
        self._validate_plan_with_roles(fix_plan)

        # 共享配置冲突预检（告警，不拒绝）：修复步骤 + 未被修复的兄弟节点配置。
        # 兄弟节点保持不动（本方法不重建/删除任何子节点），其 output_path 声明
        # 与修复步骤的声明一并检查，避免修复后出现非预期共享分区写冲突。
        preserved_configs = [
            c.config
            for c in children
            if (c.step_ref or c.id) not in failed_step_refs
        ]
        self._prevalidate_shared_configs(
            [s.config for s in fix_steps] + preserved_configs
        )

        # 3. 全部失败子节点原地续接（角色变化也原地：更新 role/exec_type + 清缓存）。
        #    role 不变 → 复用原 CC 会话（--resume）；role 变 → 新能力开新会话。
        affected: list[Node] = []
        for a, step in zip(failed_assessments, fix_steps):
            ref = a["step_ref"]
            child = failed_by_ref.get(ref)
            fix_ctx = self._build_fix_pending_context(
                parent_node_id, a, iteration_count,
            )
            updates: dict[str, Any] = {
                "status": NodeStatus.READY,
                "task": step.task,
                "pending_context": fix_ctx,
                "artifact_path": None,
                "error": None,
                "log": [],
            }
            role_changed = child is not None and step.role != child.role
            if role_changed:
                updates["role"] = step.role
                updates["exec_type"] = step.exec_type
                # 新能力开新会话：清除缓存，worker.run() 将新建会话而非 --resume
                # （与 _reactivate_reused_worker 的 role 变更语义一致）。
                try:
                    from executables.worker import Worker
                    with Worker._aicu_cache_lock:
                        Worker._aicu_cache.pop(child.id, None)
                except Exception:
                    pass
            if step.config:
                merged = dict(child.config or {}) if child is not None else {}
                merged.update(step.config)
                updates["config"] = merged
            self.graph.update_node(child.id, **updates)
            affected.append(child)
            logger.info(
                "submit_fix_plan: in-place continuation of '%s' (role=%s%s), "
                "injecting repair context (iteration=%d)",
                child.id[:8], child.role,
                "→" + step.role if role_changed else "",
                iteration_count,
            )

        # A1：修复子节点被重新激活后，回退父 Manager 链——无条件清除父节点通知记录
        # 并重放祖先，使其可在修复子节点全部完成后重新触发评审。与 _handle_routing /
        # _try_auto_retry / continue_node / revise_manager_node 几条重新激活路径保持一致。
        # 缺失时 root 会卡死 WAITING：评审刚结束时父 Manager 处于 WAITING 且位于
        # _notified_managers 集合内，_check_manager_children_completed 因
        # parent_id in _notified_managers 提前返回，修复完成后永远无法再次触发评审。
        if parent_node_id is not None:
            self.scheduler.clear_manager_notification(parent_node_id)
            self.planner.rewind_parent_chain_for_retry(
                parent_node_id, self.scheduler.clear_manager_notification,
            )
        logger.info(
            "Fix plan injected: parent=%s, continued=%d, iteration=%d",
            parent_node_id, len(affected), iteration_count,
        )
        return affected

    @staticmethod
    def _build_fix_pending_context(
        parent_node_id: str,
        assessment: dict,
        iteration_count: int,
    ) -> dict:
        """构造原地修复的 pending_context（由 worker._build_routing_context 渲染）。

        复用"路由激活上下文"注入通道：worker 的 resume/首跑都会渲染非元信息字段，
        因此 collaboration_type="repair" + defects 明细无需改动 worker.py 即可进入 prompt。
        """
        fix = assessment.get("fix") or {}
        pending = {
            "activated_by": parent_node_id,
            "activation_reason": (
                f"Manager review 第 {iteration_count} 轮发现产出缺陷，"
                "要求原地修复后重新交付（保留原工作区与产出）"
            ),
            "activated_at": datetime.now().isoformat(),
            "collaboration_type": "repair",
        }
        defect = {
            "step_ref": assessment.get("step_ref", ""),
            "reason": assessment.get("reason", ""),
            "fix_role": fix.get("role", ""),
            "fix_task": fix.get("task", ""),
        }
        # B1 契约扩展字段（file/line/desc/expected_action/check）如存在则原样透传
        for key in ("file", "line", "desc", "expected_action", "check"):
            if fix.get(key):
                defect[key] = fix[key]
        pending["defects"] = [defect]
        return pending

    # RuntimeGateway.submit_plan 协议实现：Executable 通过 ctx.runtime.submit_plan()
    # 提交计划给 Runtime，存储 plan 到节点并调用 inject_plan 翻译为 Node+Edge 注入 Graph。
    def submit_plan(self, node_id: str, plan: ExecutionPlan) -> None:
        """RuntimeGateway.submit_plan — 存储并注入 Executable 提交的 ExecutionPlan。

        将 plan 存储到 Manager 节点上，同时通过 inject_plan 翻译为 Node+Edge 注入 Graph。

        Args:
            node_id: 提交计划的 Manager 节点 ID。
            plan:    ExecutionPlan 对象。
        """
        node = self.graph.get_node(node_id)
        if node is None:
            logger.warning("submit_plan: node '%s' not found, cannot store plan", node_id)
            return
        self.graph.update_node(node_id, execution_plan=plan)
        self.inject_plan(node_id, plan)

    # ========================================================================
    # Revise 增量续跑（选择复用既有子节点，不 _clear_children）
    # ========================================================================

    def _prevalidate_reuse(
        self, plan: ExecutionPlan, parent_node_id: str
    ) -> dict[str, str]:
        """校验增量计划的 reuse_step_ref 引用合法性，返回 {step_id: 既有子节点 node_id}。

        规则（确定性机械校验，非 LLM 判断）：
          - reuse_step_ref 必须解析到 parent 的既有子节点（按 step_ref 精确 / node id 精确 /
            唯一 node id 前缀）；
          - 不得指向本 plan 的新 step；
          - 两个 step 不得复用同一子节点；
          - 复用目标 role 必须与 step.role 一致（role 变化 = 新能力，应新建而非复用）。

        Returns:
            reuse_map：被复用 step 的 step_id → 既有子节点 node_id。

        Raises:
            ValueError: 任一规则违反。
        """
        if not any(s.reuse_step_ref for s in plan.steps):
            return {}
        children = self.graph.children(parent_node_id)
        # 仅「未被复用的新 step」进入 new_step_ids —— step 自身携带 reuse_step_ref
        # （如 step_id="backend" 复用同名的既有子节点）是合法复用，不属于「指向本计划新 step」。
        new_step_ids = {
            s.step_id for s in plan.steps if not s.reuse_step_ref
        }

        def resolve(ref: str):
            # 精确 step_ref
            for c in children:
                if (c.step_ref or c.id) == ref:
                    return c
            # 精确 node id
            for c in children:
                if c.id == ref:
                    return c
            # 唯一 node id 前缀
            matches = [c for c in children if c.id.startswith(ref)]
            return matches[0] if len(matches) == 1 else None

        reuse_map: dict[str, str] = {}
        for step in plan.steps:
            ref = step.reuse_step_ref
            if not ref:
                continue
            if ref in new_step_ids:
                raise ValueError(
                    f"reuse_step_ref '{ref}' must reference an existing child node, "
                    f"not a new step in this plan"
                )
            target = resolve(ref)
            if target is None:
                raise ValueError(
                    f"reuse_step_ref '{ref}' does not match any existing child node "
                    f"of '{parent_node_id}' (matched by step_ref / node id / id prefix)"
                )
            if target.id in reuse_map.values():
                raise ValueError(
                    f"child node '{target.id}' is reused by multiple steps "
                    f"({ref} and the previous reuse_step_ref)"
                )
            if target.role != step.role:
                raise ValueError(
                    f"reuse_step_ref '{ref}' targets child role '{target.role}', "
                    f"but step role is '{step.role}'; a role change is a new capability "
                    f"and must create a new node instead of reusing"
                )
            reuse_map[step.step_id] = target.id
        return reuse_map

    @staticmethod
    def _normalize_revision_task(task: Optional[str]) -> str:
        """Normalize task text for deterministic duplicate detection."""
        return " ".join((task or "").split()).casefold()

    def is_revision_plan_noop(self, parent_node_id: str, plan: ExecutionPlan) -> bool:
        """判断增量计划是否没有改变组织结构，也没有新的可执行工作。

        这是提交前的确定性检查：计划必须逐一复用当前全部子节点，且节点身份、
        类型、角色、任务、显式配置和子节点依赖均未变化。语义相似但文字不同的
        任务不在此处猜测，仍交由 Manager 的计划生成与事件约束处理。
        """
        children = self.graph.children(parent_node_id)
        if not children or len(plan.steps) != len(children):
            return False

        try:
            reuse_map = self._prevalidate_reuse(plan, parent_node_id)
        except ValueError:
            return False
        if len(reuse_map) != len(plan.steps) or set(reuse_map.values()) != {
            child.id for child in children
        }:
            return False

        step_by_node = {reuse_map[step.step_id]: step for step in plan.steps}
        child_by_id = {child.id: child for child in children}
        for child_id, step in step_by_node.items():
            child = child_by_id[child_id]
            if (
                child.exec_type != step.exec_type
                or child.role != step.role
                or self._normalize_revision_task(child.task)
                != self._normalize_revision_task(step.task)
            ):
                return False
            # 只比较计划明确覆盖的配置；缺省字段继续继承已有配置。
            if any((child.config or {}).get(key) != value for key, value in step.config.items()):
                return False

            expected_dependencies = {
                reuse_map[dep]
                for dep in step.depends_on
                if dep in reuse_map
            }
            actual_dependencies = {
                dep.id
                for dep in self.graph.dependencies(child_id)
                if dep.id in child_by_id
            }
            if expected_dependencies != actual_dependencies:
                return False
        return True

    def _validate_revision_new_steps(
        self, plan: ExecutionPlan, parent_node_id: str
    ) -> None:
        """Reject unreferenced new steps that duplicate an existing sibling task.

        Revise plans must explicitly identify reuse.  A missing ``reuse_step_ref``
        is otherwise interpreted as a new node, so an LLM can accidentally clone
        an existing sibling.  This check is deliberately exact and mechanical;
        capability inference remains the Manager's responsibility.
        """
        children = self.graph.children(parent_node_id)
        if not children:
            return
        existing_tasks = {
            (child.role, self._normalize_revision_task(child.task), child.id)
            for child in children
        }
        existing_step_ids = {
            child.step_ref: child.id
            for child in children
            if child.step_ref
        }
        for step in plan.steps:
            if step.reuse_step_ref:
                continue
            normalized_task = self._normalize_revision_task(step.task)
            duplicate = None
            # step_id/step_ref 是 Manager 给能力节点分配的稳定标识。
            # Revise 中沿用已有标识却不填写 reuse_step_ref，必然是漏填复用字段，
            # 不能把它解释成新节点；这也覆盖任务文本被模型原样复制的情况。
            if step.step_id in existing_step_ids:
                duplicate = existing_step_ids[step.step_id]
            elif normalized_task:
                duplicate = next(
                    (
                        child_id
                        for role, task, child_id in existing_tasks
                        if role == step.role and task == normalized_task
                    ),
                    None,
                )
            if duplicate is not None:
                raise ValueError(
                    f"Revision step '{step.step_id}' duplicates existing child "
                    f"'{duplicate}' without reuse_step_ref; reference the existing "
                    "candidate or provide a genuinely new capability"
                )

    def inject_revision_plan(
        self, parent_node_id: str, plan: ExecutionPlan
    ) -> list[Node]:
        """注入增量 ExecutionPlan：选择性复用既有子节点，不删除未引用节点。

        Args:
            parent_node_id: 父 Manager 节点 ID。
            plan: 增量 ExecutionPlan（含可选的 reuse_step_ref）。

        Returns:
            受影响子节点列表（新建 + 复用的激活节点）。
        """
        self._prevalidate_shared_configs([s.config for s in plan.steps])
        self._validate_plan_with_roles(plan)
        self._validate_revision_new_steps(plan, parent_node_id)

        reuse_map = self._prevalidate_reuse(plan, parent_node_id)
        step_by_node: dict[str, Step] = {}
        for step in plan.steps:
            if step.step_id in reuse_map:
                step_by_node[reuse_map[step.step_id]] = step

        new_nodes, reused_nodes = self.planner.translate_revision(
            plan, parent_node_id, reuse_map
        )

        # 复用节点：保留工作区/产物/Agent 会话，原地重新激活执行增量
        for node in reused_nodes:
            step = step_by_node[node.id]
            prepared = self._prepare_node_config(
                node.id, {**(node.config or {}), **(step.config or {})}, parent_node_id
            )
            self._reactivate_reused_worker(node, step, prepared)

        # 新建节点：正常准备配置
        for node in new_nodes:
            prepared = self._prepare_node_config(
                node.id, node.config, parent_node_id
            )
            self.graph.update_node(node.id, config=prepared)
            node.config = prepared

        self._warn_plan_structure(plan)
        logger.info(
            "Revision plan injected: parent=%s, new=%d, reused=%d",
            parent_node_id, len(new_nodes), len(reused_nodes),
        )
        return new_nodes + reused_nodes

    def submit_revision_plan(self, node_id: str, plan: ExecutionPlan) -> None:
        """RuntimeGateway.submit_revision_plan — 存储并注入增量 ExecutionPlan。

        与 submit_plan 的区别：注入走选择性复用路径（inject_revision_plan），
        已完成的子节点在不被引用时保持不变，仅被复用的子节点原地重新激活。
        """
        node = self.graph.get_node(node_id)
        if node is None:
            logger.warning(
                "submit_revision_plan: node '%s' not found, cannot store plan", node_id
            )
            return
        # inject_revision_plan validates before mutating the graph, so an invalid
        # duplicate Revise cannot leave partial state behind.
        self.inject_revision_plan(node_id, plan)
        self.graph.update_node(node_id, execution_plan=plan)

    def _reactivate_reused_worker(
        self, node: Node, new_step: Step, prepared_config: dict
    ) -> None:
        """把被复用的子节点重置为可执行状态，注入增量上下文。

        默认复用 CC Session（Worker._aicu_cache 保留 → --resume 续接原 Agent 认知）。
        仅确定性硬条件不满足才清缓存新建会话（Runtime 确定性判断，非 LLM 语义判断）：
          1) role 变更（原会话上下文属于旧角色）；
          2) AICU 相关 config 变更（cli_name/model/api_base_url/api_key_env/skills）；
          3) 节点工作区目录不存在（workspace 失效，resume 无地基）。
        """
        from executables.worker import Worker

        aicu_keys = ("cli_name", "model", "api_base_url", "api_key_env", "skills")
        # 工作区存在性检查：不能直接用 get_node_workspace —— 它内部会
        # os.makedirs(exist_ok=True) 后返回，恒为已存在路径，导致「workspace 失效 →
        # 新建会话」兜底失效。这里从节点已持久化的 config 推导工作区路径做纯存在性判断。
        _cfg = node.config or {}
        _ws_root = _cfg.get("_workspace_root") or self.workspace_root
        _ws_rel = _cfg.get("_workspace_relpath")
        if not _ws_rel:
            _ws_rel = self._prepare_node_config(
                node.id, _cfg, node.parent_id
            )["_workspace_relpath"]
        ws_missing = not os.path.isdir(os.path.join(os.path.abspath(_ws_root), _ws_rel))
        if (
            new_step.role != node.role
            or ws_missing
            or any(
                (node.config or {}).get(k) != prepared_config.get(k) for k in aicu_keys
            )
        ):
            with Worker._aicu_cache_lock:
                Worker._aicu_cache.pop(node.id, None)
            logger.info(
                "Reused worker '%s' fresh session (role/AICU-config/workspace changed)",
                node.id,
            )

        pending_context = {
            "collaboration_type": "revise",
            "activated_by": node.parent_id or node.id,
            "activation_reason": "增量任务：复用节点，在原有产物基础上继续工作",
            "incremental_task": new_step.task,
            "prior_artifacts": [node.artifact_path] if node.artifact_path else [],
        }
        common = {
            "task": new_step.task,
            "config": prepared_config,
            "pending_revise": True,
            "pending_context": pending_context,
            # 复用即一次新的激活（与 routing 重激活一致：每次复用/路由均 +1），
            # 使增量续跑在节点生命周期中可观测、可计入激活预算。
            "activation_count": getattr(node, "activation_count", 0) + 1,
            "iteration_count": 0,
            "error": None,
        }
        if node.exec_type == "manager":
            # 复用的子 Manager → 自身重进 expand，递归复用其子节点
            self.update_node_status(
                node.id,
                NodeStatus.WAITING,
                manager_phase="expand",
                **common,
            )
        else:
            self.update_node_status(
                node.id,
                NodeStatus.WAITING,
                manager_phase=None,
                pending_routing=None,
                **common,
            )
        logger.info(
            "Reused child '%s' reactivated for incremental task (session resumed: %s)",
            node.id, new_step.role == node.role and not ws_missing,
        )

    # ========================================================================
    # 节点级控制
    # ========================================================================

    def stop_node(self, node_id: str) -> None:
        """停止指定节点。

        Args:
            node_id: 目标节点 ID。

        Raises:
            ValueError: 节点不存在。
        """
        with self._lock:
            node = self.graph.get_node(node_id)
            if node is None:
                raise ValueError(f"Node '{node_id}' not found")
            self.graph.update_node(node_id, status=NodeStatus.STOPPED)
        logger.info("Node stopped: %s", node_id)

    def pause_node(self, node_id: str) -> None:
        """暂停指定节点。

        1. 停止该节点正在运行的 Executable（杀 CC 子进程）
        2. 将节点状态切为 PAUSED
        3. AICU 缓存保留，resume 时通过 --resume 续接会话

        Args:
            node_id: 目标节点 ID。

        Raises:
            ValueError: 节点不存在或不在 RUNNING 状态。
        """
        with self._lock:
            node = self.graph.get_node(node_id)
            if node is None:
                raise ValueError(f"Node '{node_id}' not found")
            if node.status != NodeStatus.RUNNING:
                raise ValueError(
                    f"Cannot pause node '{node_id}': "
                    f"current status is {node.status.value}, expected RUNNING"
                )
            # 先停止执行（杀 CC 子进程），再改状态
            self.scheduler.stop_node(node_id)
            self.graph.update_node(node_id, status=NodeStatus.PAUSED)
        logger.info("Node paused: %s", node_id)

    def resume_node(self, node_id: str) -> None:
        """恢复指定节点。

        Args:
            node_id: 目标节点 ID。

        Raises:
            ValueError: 节点不存在或不在 PAUSED 状态。
        """
        with self._lock:
            node = self.graph.get_node(node_id)
            if node is None:
                raise ValueError(f"Node '{node_id}' not found")
            if node.status != NodeStatus.PAUSED:
                raise ValueError(
                    f"Cannot resume node '{node_id}': "
                    f"current status is {node.status.value}, expected PAUSED"
                )
            self.graph.update_node(node_id, status=NodeStatus.READY)
            self.scheduler.start()
        logger.info("Node resumed: %s", node_id)

    def continue_node(self, node_id: str) -> str:
        """继续执行 FAILED/STOPPED 节点，保留已有产出，不从头重跑。

        适用于任意节点类型（Manager/Worker）。已完成节点需要增量优化时
        使用 revise_manager_node（Revise，保留子节点/产物/Agent 会话）。

        Args:
            node_id: 目标节点 ID。

        Returns:
            原节点 ID。

        Raises:
            ValueError: 节点不存在；节点不在 FAILED/STOPPED 状态。
        """
        with self._lock:
            node = self.graph.get_node(node_id)
            if node is None:
                raise ValueError(f"Node '{node_id}' not found")
            if node.status not in (NodeStatus.FAILED, NodeStatus.STOPPED):
                raise ValueError(
                    f"Cannot continue node '{node_id}': "
                    f"current status is {node.status.value}, "
                    f"expected FAILED or STOPPED"
                )

            common_reset = {
                "error": None,
                "started_at": None,
                "finished_at": None,
            }

            if node.exec_type == "manager":
                children = self.graph.children(node_id)
                # 手动 Continue 是人工干预：额外授予 review 预算，避免原实现把
                # max_iterations 锁死为当前 iteration_count（审查守卫
                # `iteration_count + 1 < max_iterations` 恒 False → 立即再次 FAILED）。
                # 预算取「原有上限」与「当前迭代 + MANUAL_CONTINUE_BUDGET」较大值，
                # 保证至少再多给 MANUAL_CONTINUE_BUDGET 轮机会，且不缩减原上限。
                granted_max = max(
                    node.max_iterations or 0,
                    (node.iteration_count or 0) + MANUAL_CONTINUE_BUDGET,
                )
                if children:
                    # 有子节点 → 仅重新 review（保留子节点/产物），授予额外预算
                    self.graph.update_node(
                        node_id,
                        status=NodeStatus.READY,
                        manager_phase="review",
                        max_iterations=granted_max,
                        **common_reset,
                    )
                    logger.info(
                        "Manager '%s' continued (review-only, "
                        "max_iterations granted=%d, %d children preserved)",
                        node_id, granted_max, len(children),
                    )
                else:
                    # 无子节点 → 重新 expand，同样授予额外预算
                    self.graph.update_node(
                        node_id,
                        status=NodeStatus.WAITING,
                        manager_phase=None,
                        max_iterations=granted_max,
                        **common_reset,
                    )
                    logger.info(
                        "Manager '%s' continued (re-expand, no children, "
                        "max_iterations granted=%d)", node_id, granted_max,
                    )
            else:
                # Worker → 重新执行
                self.graph.update_node(
                    node_id,
                    status=NodeStatus.WAITING,
                    manager_phase=None,
                    **common_reset,
                )
                logger.info("Worker '%s' continued", node_id)

            # 回退父 Manager 链
            self.planner.rewind_parent_chain_for_retry(
                node.parent_id, self.scheduler.clear_manager_notification,
            )
            self.scheduler.start()
        return node_id

    def revise_manager_node(
        self, node_id: str, task: str, config: Optional[dict] = None
    ) -> str:
        """增量续跑已完成 Manager 节点（Revise，保留子节点/产物/Agent 会话）。

        Manager 从终态转为 WAITING + manager_phase="expand" + pending_revise=True，
        等待人工 Resume 后重新调度；expand 时 Manager 会看到「可复用候选节点」块，
        按决策 0 选择性复用既有子节点（reuse_step_ref），新能力处才产出新节点。

        Args:
            node_id: 目标 Manager 节点 ID。
            task: 增量任务描述。
            config: 配置覆盖项。

        Returns:
            原节点 ID。

        Raises:
            ValueError: 节点不存在 / 非 manager / 不处于可增量状态 / 输入超限。
        """
        if len(task) > 5000:
            raise ValueError(
                f"Task exceeds maximum length of 5000 characters (got {len(task)})"
            )
        allowed_config = {"num_ccs", "model", "skills", "audit_rounds", "max_iterations"}
        extra = {k for k in (config or {}) if k not in allowed_config}
        if extra:
            raise ValueError(
                f"Config contains non-whitelisted fields: {sorted(extra)}. "
                f"Allowed: {sorted(allowed_config)}"
            )

        with self._lock:
            node = self.graph.get_node(node_id)
            if node is None:
                raise ValueError(f"Node '{node_id}' not found")
            if node.exec_type != "manager":
                raise ValueError(
                    f"Cannot revise node '{node_id}': "
                    f"exec_type is '{node.exec_type}', expected 'manager'"
                )
            if node.status not in (
                NodeStatus.COMPLETED,
                NodeStatus.FAILED,
                NodeStatus.STOPPED,
                NodeStatus.PAUSED,
            ):
                raise ValueError(
                    f"Cannot revise node '{node_id}': "
                    f"current status is {node.status.value}, "
                    f"expected COMPLETED / FAILED / STOPPED / PAUSED"
                )
            merged_config = {**(node.config or {}), **(config or {})}
            pending_context = {
                "collaboration_type": "revise",
                "original_task": node.task,
                "incremental_task": task,
            }
            self.scheduler.clear_manager_notification(node_id)
            self.graph.update_node(
                node_id,
                status=NodeStatus.WAITING,
                manager_phase="expand",
                pending_revise=True,
                task=task,
                config=merged_config,
                pending_context=pending_context,
                iteration_count=0,
                activation_count=0,
                routing_history=[],
                pending_routing=None,
                error=None,
                started_at=None,
                finished_at=None,
            )
        logger.info(
            "Manager '%s' revised: COMPLETED/terminal → WAITING(expand) with "
            "incremental task (pending_revise=True)",
            node_id,
        )
        return node_id

    # ========================================================================
    # 全局控制
    # ========================================================================

    def set_pause_completion_callback(
        self, callback: Optional[Callable[[list[str]], None]]
    ) -> None:
        """Register the SessionManager callback used to finalize a graph pause."""
        self._pause_completion_callback = callback

    def set_node_status_callback(
        self, callback: Optional[Callable[[str, NodeStatus], None]]
    ) -> None:
        """Register a callback for reconciling session-level terminal state."""
        self._node_status_callback = callback

    def global_pause(self, mode: str = "drain") -> dict[str, Any]:
        """Pause graph dispatch, optionally interrupting running node processes."""
        if mode not in {"drain", "immediate"}:
            raise ValueError("pause mode must be 'drain' or 'immediate'")

        with self._pause_lock:
            if self._global_pause_mode is not None:
                if self._global_pause_mode != mode:
                    raise RuntimeError("graph is already pausing in another mode")
                return {
                    "mode": mode,
                    "running_count": self.scheduler.active_execution_count,
                    "interrupted_node_ids": list(self._global_interrupted_node_ids),
                }
            self._global_pause_mode = mode

        self.scheduler.pause()
        tracked = self.scheduler.active_node_ids
        graph_running = [
            node.id
            for node in self.graph.all_nodes()
            if node.status == NodeStatus.RUNNING
        ]
        interrupted: list[str] = []

        if mode == "immediate":
            interrupted = list(dict.fromkeys(graph_running + tracked))
            with self._pause_lock:
                self._global_interrupted_node_ids = list(interrupted)
            for node_id in tracked:
                self.scheduler.stop_node(node_id)
            for node_id in interrupted:
                node = self.graph.get_node(node_id)
                if node is not None and node.status == NodeStatus.RUNNING:
                    self.graph.update_node(node_id, status=NodeStatus.PAUSED)
                    self._publish_node_status_changed(node_id, NodeStatus.PAUSED)
            if self.scheduler.active_execution_count == 0:
                self._complete_global_pause(interrupted)
        elif self.scheduler.active_execution_count == 0:
            # A RUNNING node without a tracked Future is already interrupted.
            # Preserve it as an explicit task-boundary retry instead of declaring
            # the graph restart-safe with an orphaned RUNNING status.
            for node_id in graph_running:
                self.graph.update_node(node_id, status=NodeStatus.PAUSED)
                self._publish_node_status_changed(node_id, NodeStatus.PAUSED)
            self._complete_global_pause(graph_running)

        logger.info(
            "Runtime: global pause requested (mode=%s, running=%d)",
            mode,
            self.scheduler.active_execution_count,
        )
        return {
            "mode": mode,
            "running_count": self.scheduler.active_execution_count,
            "interrupted_node_ids": interrupted,
        }

    def global_resume(self, interrupted_node_ids: Optional[list[str]] = None) -> None:
        """Resume dispatch and only retry nodes interrupted by graph-level pause."""
        with self._pause_lock:
            if self._global_pause_mode is not None:
                raise RuntimeError("cannot resume while graph pause is still draining")
        if self.scheduler.active_execution_count:
            raise RuntimeError("cannot resume while previous execution is still active")
        with self._lock:
            for node_id in interrupted_node_ids or []:
                node = self.graph.get_node(node_id)
                if node is not None and node.status == NodeStatus.PAUSED:
                    self.graph.update_node(node_id, status=NodeStatus.READY)
                    self._publish_node_status_changed(node_id, NodeStatus.READY)
        with self._pause_lock:
            self._global_pause_mode = None
            self._global_interrupted_node_ids = []

        # Persist the exact retry set before reopening dispatch. If the process
        # exits after this write, SessionManager restores in recovery_required.
        self.persistence.save(self.graph)
        self.scheduler.resume()
        self.scheduler.start()
        logger.info("Runtime: global resume")

    def on_scheduler_idle(self) -> None:
        """Finalize a graph pause after every tracked Future has exited."""
        with self._pause_lock:
            mode = self._global_pause_mode
            interrupted = list(self._global_interrupted_node_ids)
        if mode in {"drain", "immediate"}:
            self._complete_global_pause(interrupted)

    def _complete_global_pause(self, interrupted_node_ids: list[str]) -> None:
        with self._pause_lock:
            if self._global_pause_mode is None:
                return
            self.persistence.save(self.graph)
            self._global_pause_mode = None
            self._global_interrupted_node_ids = []
            callback = self._pause_completion_callback
        if callback is not None:
            callback(list(dict.fromkeys(interrupted_node_ids)))

    def global_stop(self) -> None:
        """全局停止所有执行。

        停止 Scheduler 并将所有 RUNNING/READY/PAUSED 节点置为 STOPPED。
        """
        self.scheduler.stop()
        with self._lock:
            for node in self.graph.all_nodes():
                if node.status in (
                    NodeStatus.RUNNING,
                    NodeStatus.READY,
                    NodeStatus.PAUSED,
                ):
                    self.graph.update_node(node.id, status=NodeStatus.STOPPED)
        logger.info("Runtime: global stop")

    def clear_graph(self) -> None:
        """清除图（移除所有节点和边），重置根 Manager 状态。

        用于 DELETE /v1/graph 端点。移除 Graph 中所有节点和边，
        并重置 _root_manager_id 以允许后续提交新的根 Manager。
        """
        with self._lock:
            for node_id in list(self.graph.nodes.keys()):
                try:
                    self.graph.remove_node(node_id)
                except ValueError:
                    pass
            self._root_manager_id = None
        logger.info("Runtime: graph cleared")

    def shutdown(self) -> None:
        """关闭 Runtime。

        顺序：
            1. 停止 Scheduler（取消所有进行中的 Future）
            2. 清理内部资源（唤醒等待队列等）
            3. 持久化最终状态
        """
        self._shutdown = True

        # 1. 停止 Scheduler
        self.scheduler.stop()
        logger.info("Runtime: scheduler stopped")

        # 2. 清理内部资源
        self._cleanup_resources_internal()

        # 3. 持久化最终状态（先停止定时保存，再执行最终保存）
        self.persistence.stop_periodic_save()
        self.persistence.save(self.graph)

        # 4. 保存 Agent 事件日志（事实来源，重启后恢复）
        try:
            self.agent_events.save()
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to save agent events: %s", exc)

        logger.info("Runtime shutdown complete")

    def cleanup_all_processes(self) -> None:
        """清理孤儿进程（Basic Version: no-op / placeholder）。"""
        logger.info("Runtime: cleanup all processes (basic version)")

    def _cleanup_resources_internal(self) -> None:
        """内部资源清理：唤醒所有外部输入等待。"""
        with self._lock:
            for token, wait_info in self._external_input_waits.items():
                wait_info["event"].set()
            self._external_input_waits.clear()

    # ========================================================================
    # RuntimeGateway 协议实现
    # ========================================================================

    def update_node_status(
        self, node_id: str, status: NodeStatus, **updates: Any
    ) -> None:
        """RuntimeGateway.update_node_status — 更新节点状态并发布事件。"""
        if updates.get("artifact_path"):
            artifact = str(updates["artifact_path"])
            workspace = os.path.realpath(self.get_node_workspace(node_id))
            candidate = os.path.realpath(
                artifact
                if os.path.isabs(artifact)
                else os.path.join(workspace, artifact)
            )
            node = self.graph.get_node(node_id)
            allowed = [workspace]
            shared_root = (node.config or {}).get("_shared_workspace") if node else None
            if shared_root:
                allowed.append(os.path.realpath(shared_root))
            if not any(
                os.path.commonpath([root, candidate]) == root for root in allowed
            ):
                raise ValueError("Artifact path is outside the node workspace")
            if os.path.commonpath([workspace, candidate]) == workspace:
                updates["artifact_path"] = os.path.relpath(candidate, workspace)
            else:
                # 共享根内产物存绝对路径（复用 _get_node_result_root 绝对路径放行分支）
                updates["artifact_path"] = candidate
        # 时间戳填充：RUNNING 且未记录 started_at → 补 now；进入终态 → 补 finished_at。
        # E2E 并发证明（如 frontend.started_at < backend.finished_at）依赖此字段。
        if isinstance(status, str):
            status = NodeStatus(status)
        now = datetime.now()
        node = self.graph.get_node(node_id)
        if status == NodeStatus.RUNNING and node is not None and node.started_at is None:
            updates.setdefault("started_at", now)
        if status in (
            NodeStatus.COMPLETED,
            NodeStatus.FAILED,
            NodeStatus.STOPPED,
            NodeStatus.PAUSED,
        ):
            updates.setdefault("finished_at", now)
        self.graph.update_node(node_id, status=status, **updates)
        self._publish_node_status_changed(node_id, status)

    def get_node(self, node_id: str) -> Optional[Node]:
        """RuntimeGateway.get_node — 按 ID 获取节点。"""
        return self.graph.get_node(node_id)

    def get_children(self, node_id: str) -> list[Node]:
        """RuntimeGateway.get_children — 获取子节点列表。"""
        return self.graph.children(node_id)

    def get_dependencies(self, node_id: str) -> list[Node]:
        """RuntimeGateway.get_dependencies — 获取依赖节点列表。"""
        return self.graph.dependencies(node_id)

    def read_artifact(self, node_id: str, path: str) -> str:
        """RuntimeGateway.read_artifact — 读取产物文件。"""
        return self.get_artifact(node_id, path)

    # ========================================================================
    # Agent 事件总线（RuntimeGateway 扩展）
    # ========================================================================

    def publish_events(self, node_id: str, events: list[dict]) -> list[str]:
        """RuntimeGateway.publish_events — 程序化发布事件（测试/工具调用）。

        CC 主路径是写 ``{node_ws}/events/*.json`` 由调度线程 scan 摄取；
        此方法供 Runtime 内部/测试直接发布。

        Returns:
            本次新增事件的 event_id 列表。
        """
        return self.agent_events.publish_events(node_id, events)

    def flush_node_events(self, node_id: str) -> int:
        """RuntimeGateway.flush_node_events — 节点完成/失败时兜底摄取其发布入口。

        防 scheduler 已停（无 tick 扫描）导致事件滞留；返回本次摄取数。
        """
        try:
            return self.agent_events.scan_node(node_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("flush_node_events(%s) failed: %s", node_id, exc)
            return 0

    def recover_agent_events(self) -> None:
        """Recover this session's persisted event delivery after dispatch opens."""
        self.agent_events.recover_pending()

    def scan_agent_events(self) -> int:
        """扫描所有节点事件发布入口并路由投递（调度线程 tick 调用）。"""
        try:
            return self.agent_events.scan_all()
        except Exception as exc:  # noqa: BLE001
            logger.error("scan_agent_events failed: %s", exc)
            return 0

    def replay_agent_event(self, event_id: str) -> bool:
        """RuntimeGateway.replay_agent_event — 历史事件重放（方案三：订阅语义已移除）。

        事件投递即完成，无订阅者概念；恒返回 False。
        """
        return False

    def register_external_input_wait(
        self, node_id: str, input_schema: Optional[dict] = None
    ) -> str:
        """RuntimeGateway.register_external_input_wait — 注册外部输入等待。

        Returns:
            wait_token: 用于 deliver_external_input 匹配的令牌。
        """
        token = str(uuid.uuid4())
        self._external_input_waits[token] = {
            "node_id": node_id,
            "input_schema": input_schema,
            "data": None,
            "event": threading.Event(),
        }
        return token

    def deliver_external_input(self, wait_token: str, input_data: Any) -> None:
        """RuntimeGateway.deliver_external_input — 投递外部输入。"""
        wait_info = self._external_input_waits.get(wait_token)
        if wait_info is None:
            raise ValueError(f"Invalid wait token: {wait_token}")
        wait_info["data"] = input_data
        wait_info["event"].set()

    # ========================================================================
    # SchedulerDelegate 协议实现
    # ========================================================================

    def on_manager_children_completed(self, manager_node_id: str) -> None:
        """SchedulerDelegate.on_manager_children_completed — 检测 Manager 子节点是否全部达到终端状态。

        当 Manager 的所有子节点均达到终端状态（COMPLETED / FAILED / STOPPED）时，
        将 Manager 转入 REVIEW 阶段：
        - manager_phase → "review"
        - status → READY（等待 Scheduler 调度执行 Review）

        注意：与 Scheduler._check_manager_children_completed 的判定保持一致，
        只要子节点全部进入终端状态即可推进 Manager，避免因部分子节点 FAILED
        而使 Manager 永久卡在 WAITING。

        Args:
            manager_node_id: Manager 节点 ID。
        """
        manager = self.graph.get_node(manager_node_id)
        if manager is None:
            logger.warning(
                "on_manager_children_completed: manager node '%s' not found",
                manager_node_id,
            )
            return

        if manager.exec_type != "manager":
            return

        # Revise 的 Manager 必须先执行 expand；旧子节点的终态不能直接推进 review。
        if getattr(manager, "pending_revise", False):
            return

        children = self.graph.children(manager_node_id)
        if not children:
            return

        # 检查所有子节点是否均已达到终端状态（COMPLETED / FAILED / STOPPED）
        terminal_statuses = {
            NodeStatus.COMPLETED,
            NodeStatus.FAILED,
            NodeStatus.STOPPED,
        }
        all_terminal = all(
            child.status in terminal_statuses for child in children
        )

        if all_terminal:
            has_failure = any(
                child.status != NodeStatus.COMPLETED for child in children
            )
            logger.info(
                "Manager '%s' all children reached terminal state "
                "(has_failure=%s), transitioning to REVIEW phase",
                manager_node_id, has_failure,
            )
            self.graph.update_node(
                manager_node_id,
                manager_phase="review",
                status=NodeStatus.READY,
            )
            self._publish_node_status_changed(manager_node_id, NodeStatus.READY)

    def on_node_status_changed(self, node_id: str, status: NodeStatus) -> None:
        """Publish node state and reconcile the owning session lifecycle."""
        self._publish_node_status_changed(node_id, status)
        callback = self._node_status_callback
        if callback is not None:
            callback(node_id, status)

    def get_active_execution_count(self) -> int:
        """SchedulerDelegate.get_active_execution_count — 代理到 Scheduler。"""
        return self.scheduler.active_execution_count

    def get_escalations(self) -> list[dict]:
        """C2 升级台账（RuntimeGateway 扩展）— 代理到 Scheduler。

        返回本次 run 内无法自动收敛的协作路由记录（供 closed_loop 业务产物消费）。
        """
        getter = getattr(self.scheduler, "get_escalations", None)
        if callable(getter):
            return getter()
        return []

    # ========================================================================
    # v4 Query Methods
    # ========================================================================

    def get_logs(self, node_id: str, offset: int = 0, limit: int = 100) -> list:
        """获取指定节点的日志条目。

        Args:
            node_id: 目标节点 ID。
            offset: 起始偏移量。
            limit: 最大返回条数。

        Returns:
            日志条目列表。

        Raises:
            ValueError: 节点不存在。
        """
        node = self.graph.get_node(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        return node.log[offset:offset + limit]

    def append_node_log(
        self, node_id: str, level: Any, message: str, **metadata: Any
    ) -> None:
        if not message:
            return
        log_level = level if isinstance(level, LogLevel) else LogLevel(str(level))
        self.graph.append_log(
            node_id,
            LogEntry(
                ts=datetime.now(),
                level=log_level,
                message=message,
                cc_id=metadata.get("cc_id"),
                phase=metadata.get("phase"),
                event=metadata.get("event"),
                task=metadata.get("task"),
            ),
        )

    def log_cc_lifecycle(
        self,
        node_id: str,
        cc_id: str,
        event: str,
        *,
        phase: Optional[str] = None,
        task: Optional[str] = None,
        level: LogLevel = LogLevel.INFO,
    ) -> None:
        """Append one concise, structured CC lifecycle fact to Node Log."""
        messages = {
            "cc_received": "CC received task",
            "cc_started": "CC started task",
            "cc_handoff": "CC handed off result",
            "cc_completed": "CC completed task",
            "cc_failed": "CC failed task",
            "cc_paused": "CC paused",
            "cc_resumed": "CC resumed",
        }
        if event not in messages:
            raise ValueError(f"Unsupported CC lifecycle event: {event}")
        self.append_node_log(
            node_id,
            level,
            messages[event],
            cc_id=cc_id,
            phase=phase,
            event=event,
            task=task,
        )

    def update_cc_activity(
        self,
        node_id: str,
        cc_id: str,
        status: str,
        *,
        phase: Optional[str] = None,
        task: Optional[str] = None,
        latest_output: Optional[str] = None,
    ) -> None:
        """Update the observable lifecycle state of one CC inside a node."""
        node = self.graph.get_node(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        activity = dict(node.cc_activity or {})
        current = dict(activity.get(cc_id, {}))
        current.update({"cc_id": cc_id, "status": status, "updated_at": datetime.now().isoformat()})
        if phase is not None:
            current["phase"] = phase
        if task is not None:
            current["task"] = task
        if latest_output is not None:
            current["latest_output"] = latest_output[-1000:]
        if status == "running" and "started_at" not in current:
            current["started_at"] = datetime.now().isoformat()
        if status in {"completed", "failed", "idle"}:
            current["finished_at"] = datetime.now().isoformat()
        activity[cc_id] = current
        self.graph.update_node(node_id, cc_activity=activity)

    def get_cc_activity(self, node_id: str) -> dict[str, dict]:
        node = self.graph.get_node(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        return node.cc_activity or {}

    def get_node_workspace(self, node_id: str) -> str:
        node = self.graph.get_node(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        config = node.config or {}
        root = os.path.abspath(config.get("_workspace_root", self.workspace_root))
        relpath = config.get("_workspace_relpath")
        if not relpath:
            relpath = self._prepare_node_config(
                node.id, config, node.parent_id
            )["_workspace_relpath"]
        workspace = self._resolve_workspace_path(root, relpath)
        os.makedirs(workspace, exist_ok=True)
        return workspace

    def _get_node_result_root(self, node_id: str) -> Optional[str]:
        node = self.graph.get_node(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        if not node.artifact_path:
            return None
        workspace = os.path.realpath(self.get_node_workspace(node_id))
        candidate = os.path.realpath(
            node.artifact_path
            if os.path.isabs(node.artifact_path)
            else os.path.join(workspace, node.artifact_path)
        )
        # 绝对路径且存在 → 直接使用（兼容旧版不同 workspace 的产物路径）
        if os.path.isabs(node.artifact_path) and os.path.exists(candidate):
            return candidate
        # 相对路径 → 必须在节点工作空间内
        try:
            if os.path.commonpath([workspace, candidate]) != workspace:
                raise ValueError("Artifact path is outside the node workspace")
        except ValueError as error:
            raise ValueError("Artifact path is outside the node workspace") from error
        if not os.path.exists(candidate):
            raise FileNotFoundError(node.artifact_path)
        return candidate

    def list_node_results(self, node_id: str, path: str = "") -> dict[str, Any]:
        """List only the result declared by node.artifact_path."""
        root = self._get_node_result_root(node_id)
        if root is None:
            return {"artifact_path": None, "artifact_kind": None, "result_root": None, "path": ".", "entries": []}
        kind = "directory" if os.path.isdir(root) else "file"
        if kind == "file":
            if path not in ("", ".", os.path.basename(root)):
                raise FileNotFoundError(path)
            stat = os.stat(root)
            return {
                "artifact_path": root,
                "artifact_kind": kind,
                "result_root": os.path.basename(root),
                "path": ".",
                "entries": [{"name": os.path.basename(root), "path": os.path.basename(root), "type": "file", "size": stat.st_size, "modified_at": stat.st_mtime}],
            }
        target = self._resolve_workspace_path(root, path)
        if not os.path.isdir(target):
            raise FileNotFoundError(path)
        entries = []
        for entry in sorted(os.scandir(target), key=lambda item: (not item.is_dir(), item.name.lower())):
            if entry.is_symlink():
                continue
            stat = entry.stat(follow_symlinks=False)
            entries.append({"name": entry.name, "path": os.path.relpath(entry.path, root), "type": "directory" if entry.is_dir(follow_symlinks=False) else "file", "size": stat.st_size, "modified_at": stat.st_mtime})
            if len(entries) >= 1000:
                break
        return {"artifact_path": root, "artifact_kind": kind, "result_root": os.path.basename(root), "path": "." if not path else path, "entries": entries}

    def read_node_result_file(self, node_id: str, path: str, max_bytes: int = 1_000_000) -> dict[str, Any]:
        root = self._get_node_result_root(node_id)
        if root is None:
            raise FileNotFoundError("Node has no artifact")
        if os.path.isfile(root):
            target = root if path in ("", ".", os.path.basename(root)) else None
        else:
            target = self._resolve_workspace_path(root, path)
        if target is None or not os.path.isfile(target):
            raise FileNotFoundError(path)
        size = os.path.getsize(target)
        with open(target, "rb") as file:
            data = file.read(max_bytes)
        return {"path": path or os.path.basename(target), "content": data.decode("utf-8", errors="replace"), "truncated": size > len(data), "size": size}

    # ---- Session Outputs：会话级「查看产出」（V3-2.2） ----
    # 架构约束：所有 Session Outputs filesystem 操作只经 resolve_shared_workspace()
    # → shared root；业务代码不得自行推导 {workspace_root}/shared。「哪条 entry 最新」
    # 由 FilesystemRegistry.latest_per_partition() 单一权威判定（复用 resolve/@role 语义），
    # UI/Runtime 不得另定规则。Registry Protocol write/read/resolve 不变。

    @staticmethod
    def _norm_shared_rel(root: str, path: str) -> str:
        """规范化共享相对路径：'.' / 'backend' / 'backend/src'（forward slash，无 './' 前缀）。"""
        return os.path.relpath(path, root).replace(os.sep, "/")

    def _partition_metadata(self, shared_root: str) -> dict:
        """partition(realpath) → (producer, delivery)。只读 Registry snapshot（非权威）。
        latest 判定在 FilesystemRegistry.latest_per_partition（Registry 单一权威）；
        getattr 守卫 → 无该方法的 mock 后端返回 {}。
        性能边界：允许一次 list 读全部 Registry metadata；Registry entry 规模增大后
        再引入 index/cache（不现在过度优化）。"""
        from runtime.delivery.registry import FilesystemRegistry

        reg = FilesystemRegistry(shared_root)
        latest = getattr(reg, "latest_per_partition", lambda: {})() or {}
        out: dict = {}
        for partition, e in latest.items():
            if not partition.startswith(os.path.realpath(shared_root) + os.sep):
                continue  # 空分区=共享根本身（高权限模式），不出现在分区行
            node = self.graph.get_node(e.node_id)
            out[partition] = (
                {
                    "node_id": e.node_id,
                    "role": e.role,
                    "step_ref": e.step_ref,
                    "artifact_type": e.artifact_type,
                    "key_files": list(e.key_files or []),
                },
                {"state": getattr(node, "delivery_state", None)},
            )
        return out

    def list_session_shared(self, path: str = "") -> dict[str, Any]:
        """会话级「查看产出」：shared 根文件树。

        Args:
            path: 共享根下相对路径（"" = 根）。顶层列出分区（跳过框架目录，注册分区行
                带 producer+delivery，kind=artifact_partition）；子目录为普通清单
                （kind=directory/file，producer/delivery=None）。

        Returns:
            {"path": 规范化相对路径, "entries": [OutputEntry], "truncated": bool}。
            不暴露真实 shared_root（前端视其为抽象资源）。

        Raises:
            ValueError: 路径越界。
            FileNotFoundError: 目标不存在或不是目录。
        """
        root = resolve_shared_workspace(self.workspace_root)
        if not os.path.isdir(root):
            return {"path": ".", "entries": [], "truncated": False}
        target = self._resolve_workspace_path(root, path)
        if not os.path.isdir(target):
            raise FileNotFoundError(path)
        partition_level = os.path.realpath(target) == os.path.realpath(root)
        producers = self._partition_metadata(root) if partition_level else {}
        entries, truncated = [], False
        for entry in sorted(
            os.scandir(target), key=lambda it: (not it.is_dir(), it.name.lower())
        ):
            if entry.is_symlink():
                continue
            if partition_level and (
                entry.name in FRAMEWORK_DIR_NAMES or entry.name.startswith("cc_")
            ):
                # 顶层框架目录整体不显示：FRAMEWORK_DIR_NAMES 显式项
                # （.artifacts/.codegraph/.git/cc_0/cc_1/cc_2 等）+ cc_* 前缀兜底
                continue
            stat = entry.stat(follow_symlinks=False)
            is_dir = entry.is_dir(follow_symlinks=False)
            real = os.path.realpath(entry.path)
            producer, delivery = producers.get(real, (None, None))
            kind = (
                "artifact_partition"
                if (is_dir and real in producers)
                else ("directory" if is_dir else "file")
            )
            entries.append({
                "name": entry.name,
                "path": self._norm_shared_rel(root, entry.path),
                "type": "directory" if is_dir else "file",
                "kind": kind,
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
                "producer": producer,
                "delivery": delivery,
            })
            if len(entries) >= 1000:
                truncated = True
                break
        return {
            "path": self._norm_shared_rel(root, target),
            "entries": entries,
            "truncated": truncated,
        }

    MAX_PREVIEW_BYTES = 1_000_000  # 预览硬上限 = 产品安全边界，非调用方可调参数

    def read_session_shared_file(self, path: str) -> dict[str, Any]:
        """读取共享根下产物文件。文本白名单 → content+truncated；否则 previewable=false+content=null。

        Args:
            path: 共享根下相对文件路径。

        Returns:
            {"path", "size", "mime_type", "previewable", "content", "truncated"}。

        Raises:
            ValueError: 路径越界。
            FileNotFoundError: 缺失或目标为目录（禁止把目录当文件读）。
        """
        root = resolve_shared_workspace(self.workspace_root)
        target = self._resolve_workspace_path(root, path)
        if not os.path.isfile(target):
            raise FileNotFoundError(path)
        size = os.path.getsize(target)
        rel = self._norm_shared_rel(root, target)
        previewable = previewable_path(rel)
        payload = {
            "path": rel,
            "size": size,
            "mime_type": mime_type_for(rel),
            "previewable": previewable,
        }
        if not previewable:
            payload["content"] = None
            payload["truncated"] = False
            return payload
        with open(target, "rb") as f:
            data = f.read(self.MAX_PREVIEW_BYTES)
        payload["content"] = data.decode("utf-8", errors="replace")
        payload["truncated"] = size > len(data)
        return payload

    def list_workspace(
        self, path: str = "", node_id: Optional[str] = None
    ) -> dict[str, Any]:
        root = self.get_node_workspace(node_id) if node_id else self.workspace_root
        target = self._resolve_workspace_path(root, path)
        if not os.path.isdir(target):
            raise FileNotFoundError(path)
        entries = []
        for entry in sorted(os.scandir(target), key=lambda item: (not item.is_dir(), item.name.lower())):
            if entry.is_symlink():
                continue
            stat = entry.stat(follow_symlinks=False)
            entries.append({
                "name": entry.name,
                "path": os.path.relpath(entry.path, root),
                "type": "directory" if entry.is_dir(follow_symlinks=False) else "file",
                "size": stat.st_size,
                "modified_at": stat.st_mtime,
            })
            if len(entries) >= 1000:
                break
        return {
            "path": "." if target == os.path.realpath(root) else os.path.relpath(target, root),
            "entries": entries,
        }

    def read_workspace_file(
        self, path: str, node_id: Optional[str] = None, max_bytes: int = 1_000_000
    ) -> dict[str, Any]:
        root = self.get_node_workspace(node_id) if node_id else self.workspace_root
        target = self._resolve_workspace_path(root, path)
        if not os.path.isfile(target):
            raise FileNotFoundError(path)
        size = os.path.getsize(target)
        if size > max_bytes:
            raise ValueError(f"File exceeds preview limit of {max_bytes} bytes")
        with open(target, "r", encoding="utf-8", errors="replace") as file:
            content = file.read()
        return {"path": os.path.relpath(target, root), "size": size, "content": content}

    @staticmethod
    def _resolve_workspace_path(root: str, path: str) -> str:
        real_root = os.path.realpath(root)
        if os.path.isabs(path):
            raise ValueError("Workspace paths must be relative")
        target = os.path.realpath(os.path.join(real_root, path or "."))
        if target != real_root and not target.startswith(real_root + os.sep):
            raise ValueError("Workspace path escapes its root")
        return target

    def get_artifact(self, node_id: str, path: str) -> str:
        """读取节点的产物文件。

        Args:
            node_id: 目标节点 ID。
            path: 文件路径（绝对或相对 workspace_root）。

        Returns:
            文件内容。

        Raises:
            ValueError: 路径穿越。
            FileNotFoundError: 文件不存在。
        """
        workspace = self.get_node_workspace(node_id)
        real_root = os.path.realpath(workspace)
        real_path = os.path.realpath(
            path if os.path.isabs(path) else os.path.join(real_root, path)
        )
        node = self.graph.get_node(node_id)
        allowed = [real_root]
        shared_root = (node.config or {}).get("_shared_workspace") if node else None
        if shared_root:
            allowed.append(os.path.realpath(shared_root))
        if not any(
            p == real_path or real_path.startswith(p + os.sep) for p in allowed
        ):
            raise ValueError(f"Path traversal detected: {path}")
        with open(real_path, "r", encoding="utf-8", errors="replace") as file:
            return file.read()

    def get_graph_snapshot(self) -> dict:
        """获取 Graph 快照（序列化为可 JSON 的 dict）。"""
        nodes = []
        for n in self.graph.all_nodes():
            nodes.append({
                "id": n.id,
                "label": f"{n.role or n.exec_type} {n.id[:8]}",
                "exec_type": n.exec_type,
                "parent_id": n.parent_id,
                "role": n.role,
                "task": n.task,
                "task_summary": n.task_summary,
                "status": n.status.value if isinstance(n.status, NodeStatus) else str(n.status),
                "manager_phase": n.manager_phase,
                "created_at": n.created_at.isoformat() if n.created_at else None,
                "started_at": n.started_at.isoformat() if n.started_at else None,
                "finished_at": n.finished_at.isoformat() if n.finished_at else None,
                "artifact_path": n.artifact_path,
                "error": n.error.format_for_frontend() if n.error else None,
                "cc_activity": n.cc_activity or {},
                "iteration": n.iteration_count,
                "iteration_count": n.iteration_count,
                "max_iterations": n.max_iterations,
                "dependencies": [item.id for item in self.graph.dependencies(n.id)],
                "dependents": [item.id for item in self.graph.dependents(n.id)],
                "children": [item.id for item in self.graph.children(n.id)],
                "config": {
                    key: value for key, value in (n.config or {}).items()
                    if not key.startswith("_")
                },
            })
        edges = []
        for e in self.graph.all_edges():
            edges.append({
                "id": e.id,
                "source": e.from_node,
                "target": e.to_node,
                "type": e.type.value if hasattr(e.type, 'value') else str(e.type),
                "from_node": e.from_node,
                "to_node": e.to_node,
            })
        return {"nodes": nodes, "edges": edges, "stats": self.graph.get_stats()}

    def update_node_task(self, node_id: str, task: str) -> None:
        """更新节点的 task 描述。

        Args:
            node_id: 目标节点 ID。
            task: 新任务描述。

        Raises:
            ValueError: 节点不存在。
        """
        node = self.graph.get_node(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        self.graph.update_node(node_id, task=task)

    def create_manual_node(
        self,
        task: str,
        role: str = "default",
        exec_type: str = "worker",
        config: Optional[dict] = None,
        parent_id: Optional[str] = None,
        dependencies: Optional[list[str]] = None,
    ) -> Node:
        """Create a waiting node and its optional parent/dependency edges."""
        # PlanValidator exec_type↔role 一致性：建节点前拦截（roles_dir 未注入时跳过）
        self._validate_manual_role(exec_type, role)
        self.graph.validate_parent(None, parent_id)
        dependencies = dependencies or []
        for dependency_id in dependencies:
            if self.graph.get_node(dependency_id) is None:
                raise ValueError(f"Dependency node '{dependency_id}' not found")
        # 共享配置前置校验：单节点 + 与既有兄弟（遍历防护 + 冲突检测），失败不建节点
        if parent_id:
            siblings = self.graph.children(parent_id)
            self._prevalidate_shared_configs(
                [config or {}] + [c.config or {} for c in siblings]
            )
        node_id = str(uuid.uuid4())
        node = Node(
            id=node_id,
            exec_type=exec_type,
            role=role,
            task=task,
            config=self._prepare_node_config(node_id, config, parent_id),
            status=NodeStatus.WAITING,
            parent_id=parent_id,
        )
        self.graph.add_node(node)
        try:
            if parent_id:
                self.graph.add_edge(Edge(
                    id=str(uuid.uuid4()),
                    type=EdgeType.PARENT,
                    from_node=parent_id,
                    to_node=node.id,
                ))
            for dependency_id in dependencies:
                self.graph.add_edge(Edge(
                    id=str(uuid.uuid4()),
                    type=EdgeType.DEPENDENCY,
                    from_node=dependency_id,
                    to_node=node.id,
                ))
        except Exception:
            self.graph.remove_node(node.id)
            raise
        self._ensure_scheduler_running()
        return node

    def update_manual_node(self, node_id: str, **updates: Any) -> Node:
        """Update editable execution fields while a node is not active."""
        node = self.graph.get_node(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        if node.status not in {NodeStatus.WAITING, NodeStatus.PAUSED}:
            raise RuntimeError(
                f"Node '{node_id}' must be Waiting or Paused before editing"
            )
        allowed = {"task", "role", "exec_type", "config"}
        safe_updates = {key: value for key, value in updates.items() if key in allowed}
        if "exec_type" in safe_updates or "role" in safe_updates:
            self._validate_manual_role(
                safe_updates.get("exec_type", node.exec_type),
                safe_updates.get("role", node.role),
            )
        self.graph.validate_parent(node_id, node.parent_id)
        if safe_updates:
            self.graph.update_node(node_id, **safe_updates)
        return self.graph.get_node(node_id)

    def get_link_candidates(
        self, node_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        if node_id and self.graph.get_node(node_id) is None:
            raise ValueError(f"Node '{node_id}' not found")
        candidates = []
        for node in self.graph.all_nodes():
            if node.id == node_id:
                continue
            creates_cycle = bool(
                node_id and self._is_reachable(node_id, node.id)
            )
            candidates.append({
                "id": node.id,
                "label": f"{node.role or node.exec_type} {node.id[:8]}",
                "task": node.task,
                "status": node.status.value,
                "exec_type": node.exec_type,
                "eligible_as_parent": node.exec_type == "manager" and not creates_cycle,
                "eligible_as_dependency": not creates_cycle,
                "reachable": bool(
                    node_id and self._is_reachable(node.id, node_id)
                ),
            })
        return candidates

    def _is_reachable(self, start: str, target: str) -> bool:
        adjacency: dict[str, list[str]] = {}
        for edge in self.graph.all_edges():
            adjacency.setdefault(edge.from_node, []).append(edge.to_node)
        pending = [start]
        visited = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(adjacency.get(current, []))
        return False

    def add_dependency(self, from_node: str, to_node: str) -> Edge:
        """Add a dependency edge while preserving the graph DAG."""
        if from_node == to_node or self._is_reachable(to_node, from_node):
            raise ValueError("Dependency would create a cycle")
        for existing in self.graph.all_edges():
            if (
                existing.from_node == from_node
                and existing.to_node == to_node
                and existing.type == EdgeType.DEPENDENCY
            ):
                raise ValueError("Dependency already exists")
        edge = Edge(
            id=str(uuid.uuid4()),
            type=EdgeType.DEPENDENCY,
            from_node=from_node,
            to_node=to_node,
        )
        self.graph.add_edge(edge)
        return edge

    def remove_dependency(self, edge_id: str) -> None:
        edge = self.graph.edges.get(edge_id)
        if edge is None:
            raise ValueError(f"Edge '{edge_id}' not found")
        if edge.type != EdgeType.DEPENDENCY:
            raise RuntimeError("Parent edges cannot be removed directly")
        self.graph.remove_edge(edge_id)
        self._ensure_scheduler_running()

    def delete_manual_node(self, node_id: str) -> None:
        """Delete a leaf node.

        删除保护：清理子节点是严重操作，只有用户在前端手动逐叶子删除。
        代码层面不存在级联删除 —— Manager 有子节点时直接拒绝，必须先删除其子节点。
        仅删除单个叶子节点（无子节点），RUNNING 节点需先停止。
        """
        node = self.graph.get_node(node_id)
        if node is None:
            raise ValueError(f"Node '{node_id}' not found")
        children = self.graph.children(node_id)
        if children:
            raise RuntimeError(
                f"Manager '{node_id[:8]}' has {len(children)} child node(s); "
                "delete its children first (no cascade delete)"
            )
        if node.status == NodeStatus.RUNNING:
            raise RuntimeError("Running nodes must be stopped before deletion")
        self.graph.remove_node(node_id)

    # ========================================================================
    # Graph mutation → EventBus bridge
    # ========================================================================

    _EVENT_TYPE_MAP = {
        "add_node": "node_added",
        "update_node": "node_updated",
        "remove_node": "node_removed",
        "add_edge": "edge_added",
        "remove_edge": "edge_removed",
    }
    """Graph 回调事件类型 → SSE 事件类型映射。"""

    def _on_graph_mutation(self, event_type: str, payload: dict) -> None:
        """Graph mutation 回调：将变更事件通过 EventBus 发布。

        由 Graph.set_on_mutation 注册，在 Graph 的 add/update/remove 操作后触发。

        Args:
            event_type: Graph 回调事件类型（add_node / update_node 等）。
            payload: 变更负载。
        """
        # 1. 发布对应的 SSE 事件（node_added/node_updated/node_removed/edge_added/edge_removed）
        sse_type = self._EVENT_TYPE_MAP.get(event_type)
        if sse_type is not None:
            event = SSEEvent(
                event_type=sse_type,
                sequence_id=0,
                timestamp=time.time(),
                payload=payload,
            )
            self.event_bus.publish(event)

        # 2. log 追加 → log_appended 事件
        if "log" in payload:
            log_entries = payload["log"]
            if isinstance(log_entries, list):
                for log_entry in log_entries:
                    log_event = SSEEvent(
                        event_type="log_appended",
                        sequence_id=0,
                        timestamp=time.time(),
                        payload={
                            "node_id": payload.get("node_id"),
                            "level": (
                                log_entry.level.value
                                if hasattr(log_entry.level, "value")
                                else str(log_entry.level)
                            ),
                            "message": log_entry.message,
                            "ts": str(log_entry.ts),
                            "cc_id": getattr(log_entry, "cc_id", None),
                            "phase": getattr(log_entry, "phase", None),
                            "event": getattr(log_entry, "event", None),
                            "task": getattr(log_entry, "task", None),
                        },
                    )
                    self.event_bus.publish(log_event)

    # ========================================================================
    # Internal helpers
    # ========================================================================

    def _publish_node_status_changed(
        self, node_id: str, status: NodeStatus
    ) -> None:
        """发布 node_status_changed 事件到 EventBus。"""
        event = SSEEvent(
            event_type="node_status_changed",
            sequence_id=0,
            timestamp=time.time(),
            payload={
                "node_id": node_id,
                "status": (
                    status.value
                    if isinstance(status, NodeStatus)
                    else str(status)
                ),
            },
        )
        self.event_bus.publish(event)
