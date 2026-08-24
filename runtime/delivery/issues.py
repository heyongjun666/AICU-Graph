"""runtime/delivery/issues.py — DeliveryIssue / DeliveryObservation / AssemblyResult。

Assembly 的产物：结构化 Issue（阻塞，须修复）与 Observation（信息，不阻塞）。
Manager 消费 Issue 生成 repair；Observation 只记录（如 registry_drift），不触发 repair。
"""

from dataclasses import dataclass, field

# ---- V3-2.1 Delivery Issue 三分类（enum 级最小映射；P0 主干不依赖，P1 可后置） ----
# Artifact Gate 错误按「修复策略」细化分类，供 Repair Loop 决定修复方式：
#   - ARTIFACT_NOT_CREATED：CC 根本没产物（声明路径不存在 / 分区空）→ retry worker
#   - WRONG_LOCATION：CC 写了但写错位置（outside shared / framework dir）→ revise instruction
#   - CONTRACT_FAILED：产物存在但契约不满足（type mismatch / verdict missing）→ QA repair
REPAIR_STRATEGIES = frozenset({
    "ARTIFACT_NOT_CREATED",
    "WRONG_LOCATION",
    "CONTRACT_FAILED",
})

# Artifact Gate 错误前缀 → 三分类（见 runtime/delivery/gate.py check_gate 的 error 文案）。
GATE_ERROR_TO_ISSUE_TYPE = {
    "OUTSIDE_ALLOWED_ROOT": "WRONG_LOCATION",
    "FRAMEWORK_DIR": "WRONG_LOCATION",
    "MISSING_ARTIFACT": "ARTIFACT_NOT_CREATED",
    "EMPTY_ARTIFACT": "ARTIFACT_NOT_CREATED",
    "TYPE_MISMATCH": "CONTRACT_FAILED",
    "VERDICT_MISSING": "CONTRACT_FAILED",
}


def issue_type_from_gate_error(gate_error: str) -> str:
    """把 Artifact Gate 错误消息前缀映射为修复策略分类（三分类之一）。

    Args:
        gate_error: check_gate 返回的错误消息，如 "OUTSIDE_ALLOWED_ROOT: ..."。

    Returns:
        WRONG_LOCATION / ARTIFACT_NOT_CREATED / CONTRACT_FAILED（空/未知 → CONTRACT_FAILED）。
    """
    if not gate_error:
        return "CONTRACT_FAILED"
    prefix = gate_error.split(":", 1)[0].strip()
    return GATE_ERROR_TO_ISSUE_TYPE.get(prefix, "CONTRACT_FAILED")


# issue_type → Repair Loop 修复策略（供 _repair_blocking_delivery 消费）。
# 覆盖两类来源：Assembly 四类（missing_artifact/empty_artifact/dependency_mismatch/
# output_collision）与 Artifact Gate 三分类（ARTIFACT_NOT_CREATED/WRONG_LOCATION/
# CONTRACT_FAILED）。策略即 LLM fix task 的指导关键字：
#   - retry_worker：CC 没产出 → 原地重跑（含 revise 语义）
#   - revise_instruction：CC 写错位置 → 修正指令后重跑
#   - qa_repair：产物存在但契约不满足 → 质量修复
ISSUE_TYPE_TO_REPAIR_STRATEGY = {
    "missing_artifact": "retry_worker",
    "empty_artifact": "retry_worker",
    "dependency_mismatch": "retry_worker",
    "output_collision": "revise_instruction",
    "ARTIFACT_NOT_CREATED": "retry_worker",
    "WRONG_LOCATION": "revise_instruction",
    "CONTRACT_FAILED": "qa_repair",
}

# 未知/缺省 issue_type 的兜底策略。
DEFAULT_REPAIR_STRATEGY = "retry_worker"


def repair_strategy_for(issue_type: str) -> str:
    """把 issue_type 映射为 Repair Loop 修复策略关键字。

    Args:
        issue_type: Assembly 四类或 Artifact Gate 三分类之一。

    Returns:
        retry_worker / revise_instruction / qa_repair（未知 → retry_worker）。
    """
    return ISSUE_TYPE_TO_REPAIR_STRATEGY.get(issue_type, DEFAULT_REPAIR_STRATEGY)


@dataclass
class DeliveryIssue:
    """Assembly 阻塞问题：存在时必须修复，否则不能 Delivery。

    Args:
        severity: 固定 "blocking"（第一版仅 blocking 一级）。
        issue_type: Assembly 四类 missing_artifact / empty_artifact / dependency_mismatch /
            output_collision；V3-2.1 三分类 ARTIFACT_NOT_CREATED / WRONG_LOCATION / CONTRACT_FAILED。
        node_id: 出问题节点 ID。
        step_ref: 出问题节点 step_ref。
        expected: 期望值（如 partition 路径 / 产物路径）。
        actual: 实际值。
        reason: 人类可读原因。
    """

    severity: str
    issue_type: str
    node_id: str
    step_ref: str
    expected: str
    actual: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "issue_type": self.issue_type,
            "node_id": self.node_id,
            "step_ref": self.step_ref,
            "expected": self.expected,
            "actual": self.actual,
            "reason": self.reason,
        }


@dataclass
class DeliveryObservation:
    """Assembly 非阻塞观察（信息性，不触发 repair）。

    registry_drift：Registry Snapshot 过期是正常信号（兄弟节点改写 shared 文件后
    Snapshot 不自动更新），不改变 Source of Truth，不触发修复。
    """

    obs_type: str
    node_id: str
    step_ref: str
    detail: str

    def to_dict(self) -> dict:
        return {
            "obs_type": self.obs_type,
            "node_id": self.node_id,
            "step_ref": self.step_ref,
            "detail": self.detail,
        }


@dataclass
class AssemblyResult:
    """Assembly Gate 输出。

    判定语义：passed = blocking_issues == []。Observation 不参与 passed。
    """

    passed: bool
    issues: list = field(default_factory=list)
    observations: list = field(default_factory=list)
    manifest: dict = field(default_factory=dict)

    @property
    def blocking_issues(self) -> list:
        return [i for i in self.issues if i.severity == "blocking"]
