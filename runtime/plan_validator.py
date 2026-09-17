"""
runtime/plan_validator.py — PlanValidator：ExecutionPlan 的确定性结构校验器

职责边界（与 Manager/Reviewer 的分工）：
  - 确定性 / 机械规则 → 本模块（代码校验，绝不误报）
      * config.num_ccs 合法性（若存在必须为 int ∈ {1,2,3,4}，Runtime 合法性校验，非资源分配）
      * exec_type ↔ role 一致性（角色集由 RolesManager.list_roles() 提供；未提供角色集时跳过）
  - 语义判断（集成 Worker 是否合理、Terminal 判据、Role 必要性、是否拆 Manager）→ 留在 Manager + Reviewer

本模块只做机械结构校验，不做任何"聪明判断"，绝不进化成"第二个 Manager"。

告警（软警告，仅日志可观测性，不拒绝）：
  - 单层直接 worker 6-8 个 → 建议分组到 sub-manager
    （>8 是硬错误，由 Planner.MAX_DIRECT_WORKERS 拦截，此处不重复上报。）
"""

from typing import Optional

from runtime.models import NUM_CCS_TIERS, ExecutionPlan


def validate_errors(
    plan: ExecutionPlan,
    *,
    manager_roles: Optional[set[str]] = None,
    worker_roles: Optional[set[str]] = None,
) -> list[str]:
    """确定性结构校验，返回硬错误列表（空列表 = 通过）。

    Args:
        plan: ExecutionPlan。
        manager_roles: Manager 角色 ID 集合（不含 default；default 作为基类对两类 exec_type 均合法）。
        worker_roles:  Worker 角色 ID 集合（不含 default；default 作为基类对两类 exec_type 均合法）。
            两者均未提供时跳过 exec_type↔role 校验（validate_plan 保持纯净，
            不依赖 roles 目录）；提供其一则仅校验对应 exec_type 的步骤。

    Returns:
        错误信息列表，空列表表示校验通过。
    """
    errors: list[str] = []
    if not plan.steps:
        return errors

    for i, step in enumerate(plan.steps):
        prefix = f"step[{i}]"
        sid = step.step_id or "<no_id>"

        # ---- config.num_ccs 合法性（Manager 的资源决策；此处仅做合法性校验） ----
        num_ccs = (step.config or {}).get("num_ccs")
        if num_ccs is not None:
            if not isinstance(num_ccs, int) or num_ccs not in NUM_CCS_TIERS:
                errors.append(
                    f"{prefix} '{sid}': config.num_ccs must be an int in "
                    f"{sorted(NUM_CCS_TIERS)} (got {num_ccs!r})"
                )

        # ---- exec_type ↔ role 一致性 ----
        # 角色集来自 RolesManager.list_roles()；"default" 在两目录均存在，两类均合法。
        if step.exec_type == "manager" and manager_roles is not None:
            valid = set(manager_roles) | {"default"}
            if step.role not in valid:
                errors.append(
                    f"{prefix} '{sid}': exec_type=manager with role '{step.role}', "
                    f"must be a Manager role (one of {sorted(valid)})"
                )
        elif step.exec_type == "worker" and worker_roles is not None:
            valid = set(worker_roles) | {"default"}
            if step.role not in valid:
                errors.append(
                    f"{prefix} '{sid}': exec_type=worker with role '{step.role}', "
                    f"must be a Worker role (one of {sorted(valid)})"
                )

        # ---- task_summary 必填（复用节点豁免：职责沿用原节点） ----
        # task_summary 是 Router 路由匹配的职责索引（~100 字），由 manager expand 时产出。
        # 缺失时 Router 只能靠 task 截断，语义糊化 → 硬错误，报错信息给出正例便于 manager 自愈。
        # 复用步骤（reuse_step_ref 非空）继承原节点职责，不要求重写摘要。
        summary = (getattr(step, "task_summary", "") or "").strip()
        if not summary and not getattr(step, "reuse_step_ref", None):
            errors.append(
                f"{prefix} '{sid}': task_summary is required; provide a ≤100-char "
                f"responsibility summary (e.g. '负责 auth 模块的 JWT 登录/注册接口')"
            )

    return errors


def validate_warnings(plan: ExecutionPlan) -> list[str]:
    """确定性结构告警（软警告，不拒绝）。

    仅包含可靠的结构性告警：单层直接 worker 6-8 个 → 建议分组到 sub-manager。
    （>8 是硬错误，由 Planner.MAX_DIRECT_WORKERS=8 拦截，此处不重复上报。）
    其余组织级质量（集成 Worker 合理性、Terminal 判据、Role 必要性、是否拆 Manager）
    由既有 Manager review → submit_fix_plan 闭环承担，Validator 不越权。
    """
    warnings: list[str] = []
    if not plan.steps:
        return warnings

    worker_count = sum(1 for s in plan.steps if s.exec_type == "worker")
    if 5 < worker_count <= 8:
        warnings.append(
            f"Single layer has {worker_count} direct workers (exceeds the prompt "
            f"guideline of 5); consider grouping related workers under sub-manager nodes."
        )

    return warnings
