"""
runtime/models.py — v1 核心数据模型与字段约束

定义系统的核心数据模型：Node、Edge、ExecutionPlan、Step、LogEntry、ErrorObject，
以及配套的枚举类型（NodeStatus、EdgeType、LogLevel）。

所有模型支持 JSON 序列化/反序列化，附带字段约束校验。
"""

import json
import warnings
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from enum import Enum
from typing import Any, Optional


# ============================================================================
# Enums
# ============================================================================

class NodeStatus(str, Enum):
    """Executable 节点状态枚举。序列化值使用 TitleCase。"""
    WAITING = "Waiting"
    READY = "Ready"
    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED = "Failed"
    STOPPED = "Stopped"
    PAUSED = "Paused"


class EdgeType(str, Enum):
    """边类型。"""
    PARENT = "parent"
    DEPENDENCY = "dependency"


class LogLevel(str, Enum):
    """日志级别。"""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class ManagerPhase(str, Enum):
    """Manager 两段式执行阶段。"""
    EXPAND = "expand"
    REVIEW = "review"
    WAITING = "waiting"


# ============================================================================
# Constants for validation
# ============================================================================

_MAX_TASK_LENGTH = 5000
_MIN_NUM_CCS = 1
_MAX_NUM_CCS = 10
_MIN_AUDIT_ROUNDS = 0
_MAX_AUDIT_ROUNDS = 10
# Node log 最大条目数
_MAX_LOG_ENTRIES = 10000
# 合法的日志级别集合（用于 LogEntry.__post_init__ 校验）
_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


# ============================================================================
# RoutingDecision
# ============================================================================

@dataclass
class RoutingDecision:
    """节点完成后做出的路由决策（方案三：协作类型已移除，目标完全由 Router 决策）。

    字段:
        activate: 需要激活的目标节点 ID 列表。
        contexts: 每个被激活节点的上下文 dict，key 为目标节点 ID。
        mark_terminal: 标记为"已完成，无需再激活"的节点 ID 列表。
        reason: 路由原因描述（用于日志和 audit trail）。
    """
    activate: list[str] = field(default_factory=list)
    contexts: dict[str, dict] = field(default_factory=dict)
    mark_terminal: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "activate": self.activate,
            "contexts": self.contexts,
            "mark_terminal": self.mark_terminal,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RoutingDecision":
        return cls(
            activate=data.get("activate", []),
            contexts=data.get("contexts", {}),
            mark_terminal=data.get("mark_terminal", []),
            reason=data.get("reason", ""),
        )


# ============================================================================
# EventMessage
# ============================================================================

@dataclass
class EventMessage:
    """Agent 事件消息（Node → Router → Node 动态协作信号）。

    Event 描述执行过程中发生了什么（事实），不参与调度决策。
    正式语义（方案三·AICU Router）：
        - ``producer`` 统一表示产生事件的 **Node 身份（node_id）**，不承担 Plan/组织关系
          身份；若上游仍以 step_ref 传入，由 ``_resolve_producer_node`` 兼容层解析成
          node_id 后再构造 Event。
        - ``objective`` 只描述事件本身（producer 想让大家知道什么，就几句话，≤3 句、≤80 字），
          **不得携带任何 Router 指令 / 目标声明**——路由决策完全由 AICU Router 基于
          「Event + Producer + 当前 Organization Graph」用 LLM 统一判断。
        - ``intent``/``affected_roles``/``scope``/``action`` 是生产者的**参考信号**
          （不指定目标、不硬约束路由），Router 仍读组织图独立决策。
        - ``routed_to`` 是 Router **决策记录**（event_id → 目标 node_id 列表），
          属框架元数据而非事件内容；投递/重放从该记录走，不需要重跑 Router。
        - ``status`` 是事件的**投递生命周期**（routed → delivered → consumed），
          用于审计与恢复（重启时据 routed_to 确定性重放未投递事件）。
    字段:
        event_id: 全局唯一事件 ID（CC 生成，缺省 Runtime 补；日志按此去重）。
        producer: 产生事件的生产者 **node_id**（不记录 step_ref）。
        objective: 想让其他 Node 知道什么（就几句话，如「后端登录接口已完成，需前端联调」，≤3 句、≤80 字）。
        intent: 事件性质——notification（仅同步状态）/ collaboration（需协作）/
                escalation（需上级协调）。
        affected_roles: 受影响的**岗位/角色** + 各自职责（list[{"role","duty"}]，参考信号；
                role 填岗位名（按任务领域自行填写），**不是节点名/节点 ID**）。
        scope: 建议搜索范围——current / subtree / related / global（强引导，可越界）。
        action: 接收节点要完成的动作类型（自由简短文本，不做枚举）。
        created_at: ISO 格式创建时间（CC 写时可不带，Runtime 摄取时补）。
        routed_to: Router 决策的目标 node_id 列表（空 = 无人接收）。
        status: 投递生命周期：routed（已决策未投递）/ delivered（已投递所有目标）
                / consumed（所有目标已消费）。旧日志缺省回退 routed。
    """

    EVENT_STATUS_ROUTED = "routed"
    EVENT_STATUS_ROUTE_FAILED = "route_failed"
    EVENT_STATUS_DELIVERED = "delivered"
    EVENT_STATUS_CONSUMED = "consumed"

    INTENT_NOTIFICATION = "notification"
    INTENT_COLLABORATION = "collaboration"
    INTENT_ESCALATION = "escalation"
    VALID_INTENTS = (INTENT_NOTIFICATION, INTENT_COLLABORATION, INTENT_ESCALATION)

    SCOPE_CURRENT = "current"
    SCOPE_SUBTREE = "subtree"
    SCOPE_RELATED = "related"
    SCOPE_GLOBAL = "global"
    VALID_SCOPES = (SCOPE_CURRENT, SCOPE_SUBTREE, SCOPE_RELATED, SCOPE_GLOBAL)

    event_id: str
    producer: str
    objective: str = ""
    intent: str = INTENT_NOTIFICATION
    affected_roles: list = field(default_factory=list)
    scope: str = SCOPE_RELATED
    action: str = ""
    created_at: str = ""
    routed_to: list[str] = field(default_factory=list)
    status: str = EVENT_STATUS_ROUTED

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "producer": self.producer,
            "objective": self.objective,
            "intent": self.intent,
            "affected_roles": list(self.affected_roles),
            "scope": self.scope,
            "action": self.action,
            "created_at": self.created_at,
            "routed_to": list(self.routed_to),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EventMessage":
        """从 dict 反序列化（容忍缺失/空字段，兼容旧 content 字段事件日志）。

        旧事件日志只有 ``content`` 字段：``objective = objective or content``，
        使历史会话可重启恢复（content 值迁入 objective）。
        """
        routed_to = data.get("routed_to") or []
        if not isinstance(routed_to, list):
            routed_to = []
        affected_roles = data.get("affected_roles") or []
        if not isinstance(affected_roles, list):
            affected_roles = []
        intent = data.get("intent") or cls.INTENT_NOTIFICATION
        if intent not in cls.VALID_INTENTS:
            intent = cls.INTENT_NOTIFICATION
        scope = data.get("scope") or cls.SCOPE_RELATED
        if scope not in cls.VALID_SCOPES:
            scope = cls.SCOPE_RELATED
        return cls(
            event_id=data.get("event_id", ""),
            producer=data.get("producer", ""),
            objective=data.get("objective") or data.get("content", ""),
            intent=intent,
            affected_roles=affected_roles,
            scope=scope,
            action=data.get("action", ""),
            created_at=data.get("created_at", ""),
            routed_to=[str(t) for t in routed_to],
            status=data.get("status", cls.EVENT_STATUS_ROUTED),
        )


# ============================================================================
# ErrorObject
# ============================================================================

@dataclass
class ErrorObject:
    """结构化错误对象。

    字段:
        message: 人类可读的错误摘要。
        cause: 错误根因（技术描述）。
        suggestion: 修复建议。
        debug_traceback: 调试跟踪信息，仅在 DEBUG 模式下暴露。
    """
    message: str
    cause: str
    suggestion: str
    debug_traceback: Optional[str] = None

    @classmethod
    def create_aicu_error(cls) -> "ErrorObject":
        return cls(
            message="AICU execution failed",
            cause="AICU 内部执行异常，可能是模型调用超时或返回格式异常",
            suggestion="检查网络连接与模型状态，重试任务",
        )

    @classmethod
    def create_plan_validation_error(cls) -> "ErrorObject":
        return cls(
            message="ExecutionPlan validation failed",
            cause="ExecutionPlan 格式校验未通过，可能缺少必填字段或字段类型不匹配",
            suggestion="检查 Manager 输出的 ExecutionPlan 是否符合协议 schema",
        )

    @classmethod
    def create_max_iteration_exceeded(cls) -> "ErrorObject":
        return cls(
            message="Max iterations exceeded",
            cause="Manager Review 反复不通过，已达到 max_iterations 上限",
            suggestion="增大 max_iterations 或人工介入判断当前产出是否可接受",
        )

    @classmethod
    def create_worker_execution_error(cls) -> "ErrorObject":
        return cls(
            message="Worker execution failed",
            cause="Worker 执行 AICU.ask() 时异常退出",
            suggestion="查看 Worker 日志确认具体错误原因，修复后重启节点",
        )

    @classmethod
    def create_artifact_gate_error(cls, errors: list[str]) -> "ErrorObject":
        return cls(
            message="Artifact gate validation failed",
            cause="; ".join(errors) if errors else "产物未通过确定性校验",
            suggestion="检查 Worker 声明的 final_output_path 是否真实存在、属于本 Session 的允许空间且满足角色产物契约",
        )

    @classmethod
    def create_manager_execution_error(cls) -> "ErrorObject":
        return cls(
            message="Manager execution failed",
            cause="Manager 执行过程中发生未预期异常",
            suggestion="查看 Manager 日志确认阶段（expand/review）与具体错误，修复后重启",
        )

    def format_for_frontend(self, debug: bool = False) -> dict:
        """格式化为前端展示用 dict。

        Args:
            debug: 是否包含 debug_traceback 字段。
        """
        result = {
            "message": self.message,
            "cause": self.cause,
            "suggestion": self.suggestion,
        }
        if debug and self.debug_traceback:
            result["debug_traceback"] = self.debug_traceback
        return result


# ============================================================================
# LogEntry
# ============================================================================

@dataclass
class LogEntry:
    """日志条目。

    字段:
        ts: ISO 格式时间戳。
        level: 日志级别（DEBUG/INFO/WARN/ERROR）。
        message: 日志内容。
    """
    ts: datetime
    level: LogLevel
    message: str
    cc_id: Optional[str] = None
    phase: Optional[str] = None
    event: Optional[str] = None
    task: Optional[str] = None

    def __post_init__(self):
        """校验日志级别。"""
        if isinstance(self.level, str):
            try:
                self.level = LogLevel(self.level)
            except ValueError:
                raise ValueError(
                    f"Invalid log level: {self.level}. "
                    f"Must be one of {_VALID_LOG_LEVELS}"
                )

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""
        return json.dumps(asdict(self), default=_json_serialize, ensure_ascii=False)


# ============================================================================
# Step & ExecutionPlan
# ============================================================================

@dataclass
class Step:
    """ExecutionPlan 中的一个逻辑步骤。

    字段:
        step_id: 局部步骤标识符（在单一 ExecutionPlan 内唯一）。
        exec_type: Executable 类型（manager/worker）。
        role: 角色 yaml 名称。
        task: 任务描述，maxLength=5000。
        task_summary: 职责摘要（~100字），供 Router 路由匹配（manager expand 时产出）。
        config: 配置覆盖项（白名单字段）。
        depends_on: 依赖的 step_id 列表。
    """
    step_id: str
    exec_type: str
    role: str
    task: str
    task_summary: str = ""
    config: dict = field(default_factory=dict)
    depends_on: list = field(default_factory=list)
    # 增量续跑（Revise）：复用 parent 既有子节点（按 step_ref/id 匹配）。
    # 仅 Manager 在「决策 0 · 增量续跑」时输出；其余场景保持 None。
    reuse_step_ref: Optional[str] = None

    def __post_init__(self):
        """校验字段约束。"""
        # Step.task maxLength=5000 — 超长时截断并标记警告
        if len(self.task) > _MAX_TASK_LENGTH:
            warnings.warn(
                f"Step.task exceeds maximum length of {_MAX_TASK_LENGTH} "
                f"(got {len(self.task)} characters); truncating to {_MAX_TASK_LENGTH}.",
                UserWarning,
                stacklevel=2,
            )
            self.task = self.task[:_MAX_TASK_LENGTH]

        # config.num_ccs 范围 1-10
        num_ccs = self.config.get("num_ccs")
        if num_ccs is not None:
            if not isinstance(num_ccs, int) or num_ccs < _MIN_NUM_CCS or num_ccs > _MAX_NUM_CCS:
                raise ValueError(
                    f"config.num_ccs must be between {_MIN_NUM_CCS} and {_MAX_NUM_CCS} "
                    f"(got {num_ccs})"
                )

        # config.audit_rounds 范围 0-10
        audit_rounds = self.config.get("audit_rounds")
        if audit_rounds is not None:
            if not isinstance(audit_rounds, int) or audit_rounds < _MIN_AUDIT_ROUNDS or audit_rounds > _MAX_AUDIT_ROUNDS:
                raise ValueError(
                    f"config.audit_rounds must be between {_MIN_AUDIT_ROUNDS} and {_MAX_AUDIT_ROUNDS} "
                    f"(got {audit_rounds})"
                )


@dataclass
class ExecutionPlan:
    """逻辑执行计划（Manager 的输出协议）。

    字段:
        steps: 逻辑步骤列表。
    """
    steps: list[Step] = field(default_factory=list)

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""
        return json.dumps(asdict(self), default=_json_serialize, ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "ExecutionPlan":
        """从 JSON 字符串反序列化。"""
        data = json.loads(json_str)
        steps = []
        for s in data.get("steps", []):
            steps.append(Step(
                step_id=s["step_id"],
                exec_type=s["exec_type"],
                role=s["role"],
                task=s["task"],
                task_summary=s.get("task_summary", ""),
                config=s.get("config", {}),
                depends_on=s.get("depends_on", []),
                reuse_step_ref=s.get("reuse_step_ref"),
            ))
        return cls(steps=steps)


# ============================================================================
# Node
# ============================================================================

@dataclass
class Node:
    """Graph 的唯一节点存储单元。

    只保存不可推导字段。children/dependencies/dependents 全部由 Edge 反查。

    字段:
        id: 全局唯一 UUID，由 Planner 生成。
        parent_id: 父 Manager 的 Node id；根节点为 None。
        exec_type: Executable 类型（manager/worker）。
        role: 角色 yaml 名称。
        task: 任务描述。
        task_summary: 职责摘要（~100字），供 Router 路由匹配。
        config: 配置覆盖项。
        status: 节点状态。
        artifact_path: 产出路径。
        execution_plan: Manager 暂存的 ExecutionPlan（提交后可清空）。
        step_ref: 逻辑 Step 标识。
        manager_phase: Manager 两段式阶段（expand/review）。
        iteration_count: 当前迭代次数。
        max_iterations: 最大迭代次数。
        log: 节点执行日志列表。
        error: 结构化错误信息。
        created_at: 创建时间。
        started_at: 开始执行时间。
        finished_at: 执行结束时间。
    """
    id: str
    exec_type: str
    role: str
    task: str
    config: dict
    status: NodeStatus
    parent_id: Optional[str] = None
    artifact_path: Optional[str] = None
    execution_plan: Optional[ExecutionPlan] = None
    step_ref: Optional[str] = None
    task_summary: Optional[str] = None
    manager_phase: Optional[str] = None
    iteration_count: int = 0
    max_iterations: int = 3
    log: list = field(default_factory=list)
    cc_activity: dict[str, dict] = field(default_factory=dict)
    error: Optional[ErrorObject] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    # === 路由相关字段 ===
    routing_history: list[dict] = field(default_factory=list)
    max_activations: int = 3
    activation_count: int = 0
    pending_context: Optional[dict] = None
    pending_routing: Optional[RoutingDecision] = None
    # 增量续跑（Revise）显式标记：区分普通 Manager Expand 与用户主动增量触发
    # 的 Manager Expand。仅由 revise_manager_node 置位，Routing/Repair 保持 False。
    pending_revise: bool = False
    # === 闭环协作台账（A2：Manager 逐轮 review 累积，业务产出 closed_loop.json） ===
    collaboration_ledger: list[dict] = field(default_factory=list)
    # === 交付状态（V3-2 Delivery Reliability，正交第二状态机，不碰 Node 状态机） ===
    # 取值：None(=pending，未验证) / "validated"（Gate 通过）/ "delivered"（Assembly 通过）
    #      / "invalid"（Gate FAIL 或 Assembly BLOCK）。COMPLETED ≠ DELIVERED。
    delivery_state: Optional[str] = None

    def __setattr__(self, name, value):
        """拦截 status 赋值，校验状态转换基础规则。"""
        if name == "status":
            # 字符串转枚举
            if isinstance(value, str):
                try:
                    value = NodeStatus(value)
                except ValueError:
                    raise ValueError(
                        f"Invalid status value: '{value}'. "
                        f"Must be one of {[s.value for s in NodeStatus]}"
                    )

            # 初始赋值（__init__ 阶段）不校验
            if "status" in self.__dict__:
                old = self.__dict__["status"]
                # 自转换（如 __post_init__ 中 string→enum）放行
                if value != old:
                    # 终端状态通常不可转换，但允许 FAILED/STOPPED/COMPLETED → WAITING/READY 的原地重试/路由重激活
                    if old in (NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.STOPPED):
                        if not (
                            old in (NodeStatus.FAILED, NodeStatus.STOPPED, NodeStatus.COMPLETED)
                            and value in (NodeStatus.WAITING, NodeStatus.READY)
                        ):
                            raise ValueError(
                                f"Cannot transition from terminal state '{old.value}' "
                                f"to '{value.value}'"
                            )
        super().__setattr__(name, value)

    def __post_init__(self):
        """初始化后处理：string→enum 转换 + log FIFO 裁剪。"""
        if isinstance(self.status, str):
            self.status = NodeStatus(self.status)
        # FIFO 裁剪 log 至 _MAX_LOG_ENTRIES 条
        if len(self.log) > _MAX_LOG_ENTRIES:
            self.log = self.log[-_MAX_LOG_ENTRIES:]

    def set_status(self, new_status: NodeStatus) -> None:
        """显式状态变更方法（推荐使用，语义清晰）。

        Args:
            new_status: 目标状态。

        Raises:
            ValueError: 状态转换不符合基础规则。
        """
        self.status = new_status

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""
        return json.dumps(asdict(self), default=_json_serialize, ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "Node":
        """从 JSON 字符串反序列化。"""
        data = json.loads(json_str)

        # 反序列化 status
        data["status"] = NodeStatus(data["status"])

        # 反序列化 log
        log_entries = []
        for entry in data.get("log", []):
            log_entries.append(LogEntry(
                ts=_parse_datetime(entry["ts"]),
                level=LogLevel(entry["level"]),
                message=entry["message"],
                cc_id=entry.get("cc_id"),
                phase=entry.get("phase"),
                event=entry.get("event"),
                task=entry.get("task"),
            ))
        data["log"] = log_entries

        # 反序列化 datetime 字段
        if isinstance(data.get("created_at"), str):
            data["created_at"] = _parse_datetime(data["created_at"])
        if isinstance(data.get("started_at"), str):
            data["started_at"] = _parse_datetime(data["started_at"])
        if isinstance(data.get("finished_at"), str):
            data["finished_at"] = _parse_datetime(data["finished_at"])

        # 反序列化 execution_plan
        if data.get("execution_plan") is not None:
            plan_data = data["execution_plan"]
            steps = []
            for s in plan_data.get("steps", []):
                steps.append(Step(
                    step_id=s["step_id"],
                    exec_type=s["exec_type"],
                    role=s["role"],
                    task=s["task"],
                    task_summary=s.get("task_summary"),
                    config=s.get("config", {}),
                    depends_on=s.get("depends_on", []),
                    reuse_step_ref=s.get("reuse_step_ref"),
                ))
            data["execution_plan"] = ExecutionPlan(steps=steps)

        # 反序列化 error
        if data.get("error") is not None and isinstance(data["error"], dict):
            err = data["error"]
            data["error"] = ErrorObject(
                message=err.get("message", ""),
                cause=err.get("cause", ""),
                suggestion=err.get("suggestion", ""),
                debug_traceback=err.get("debug_traceback"),
            )

        # 反序列化 pending_routing
        if data.get("pending_routing") is not None and isinstance(data["pending_routing"], dict):
            data["pending_routing"] = RoutingDecision.from_dict(data["pending_routing"])

        # Keep persisted graphs forward-compatible when fields are removed from Node.
        known_fields = {item.name for item in fields(cls)}
        unknown_fields = sorted(set(data) - known_fields)
        if unknown_fields:
            warnings.warn(
                "Ignoring unknown persisted Node fields: " + ", ".join(unknown_fields),
                RuntimeWarning,
                stacklevel=2,
            )
            data = {key: value for key, value in data.items() if key in known_fields}

        return cls(**data)


# ============================================================================
# Edge
# ============================================================================

@dataclass
class Edge:
    """Graph 的关系存储单元。

    字段:
        id: 边唯一标识（UUID）。
        type: 边类型（parent/dependency）。
        from_node: 源 Node id。
        to_node: 目标 Node id。
    """
    id: str
    type: EdgeType
    from_node: str
    to_node: str

    def __post_init__(self):
        if isinstance(self.type, str):
            try:
                self.type = EdgeType(self.type)
            except ValueError:
                raise ValueError(
                    f"Invalid edge type: '{self.type}'. "
                    f"Must be one of {[t.value for t in EdgeType]}"
                )

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""
        return json.dumps(asdict(self), default=_json_serialize, ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> "Edge":
        """从 JSON 字符串反序列化。"""
        data = json.loads(json_str)
        data["type"] = EdgeType(data["type"])
        return cls(**data)


# ============================================================================
# Helper functions
# ============================================================================

def _json_serialize(obj: Any) -> Any:
    """JSON 序列化辅助函数，处理 datetime 和 Enum 类型。"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (Step, ExecutionPlan)):
        return asdict(obj)
    raise TypeError(f"Type {type(obj)} not serializable")


def _parse_datetime(dt_str: str) -> datetime:
    """解析 ISO 格式时间字符串。"""
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    return datetime.fromisoformat(dt_str)
