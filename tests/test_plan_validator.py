"""
tests/test_plan_validator.py — PlanValidator 确定性结构校验测试

覆盖 V3.0 收尾 PlanValidator：
  1. config.num_ccs 合法性（非 int / 越界 → error；1-4 → 通过）
  2. exec_type↔role 一致性（manager↔Manager 角色、worker↔Worker 角色；
     "default" 两类均合法；未提供角色集时跳过）
  3. 告警：worker 平铺 6-8 → warning；≤5 无；>8 → Planner 硬错误（MAX_DIRECT_WORKERS）
  4. 防误报回归：集成 Worker 关键词、事件+depends_on 并存 → 无 warning
  5. Runtime 接入：inject_plan / submit_fix_plan / create_manual_node
     图形变更前拦截 exec_type↔role 错误，失败不污染 Graph

职责边界：PlanValidator 只做确定性结构校验，不做"聪明判断"（集成 Worker 合理性、
Terminal 判据、Role 必要性等留 Manager + Reviewer）。本文件同时锁定该边界。
"""

import os
from types import SimpleNamespace

import pytest

from executables.base import Executable, ExecutableContext
from runtime.models import ExecutionPlan, NodeStatus, Step
from runtime.plan_validator import validate_errors, validate_warnings
from runtime.planner import PlanValidationError, Planner
from runtime.runtime import Runtime


# ============================================================================
# Helpers
# ============================================================================

MANAGER_ROLES = {"engineering_coord", "release_planner"}
WORKER_ROLES = {
    "acceptance", "architecture", "backend_dev", "code", "data_analysis", "deploy",
    "dev_lead", "dev_member", "document", "frontend_dev", "grill", "incremental_plan",
    "prototype", "requirements", "test", "test_role",
}

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_ROLES_DIR = os.path.join(PROJECT_ROOT, "roles")


def _step(
    step_id: str,
    exec_type: str = "worker",
    role: str = "code",
    task: str = "do the thing",
    task_summary: str = "summarized duty",
    config: dict = None,
    depends_on: list = None,
) -> Step:
    return Step(
        step_id=step_id,
        exec_type=exec_type,
        role=role,
        task=task,
        task_summary=task_summary,
        config=config or {},
        depends_on=depends_on or [],
    )


def _raw_step(step_id, **kw) -> SimpleNamespace:
    """绕过 Step 构造校验的裸步骤对象（用于测试 Validator 自身的防御逻辑）。

    Step.__post_init__ 会拦截 num_ccs<1/>10/非 int 等值（构造期 ValueError），
    但 Validator 是 Runtime 边界的第二道防线，须独立验证其对任何脏输入的判定。
    """
    base = {
        "step_id": step_id,
        "exec_type": "worker",
        "role": "code",
        "task": "t",
        "task_summary": "s",
        "config": {},
        "depends_on": [],
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _plan(*steps) -> ExecutionPlan:
    return ExecutionPlan(steps=list(steps))


# ============================================================================
# 1. config.num_ccs 合法性
# ============================================================================

class TestNumCcs:
    def test_num_ccs_5_is_error(self):
        """1-4 是 Manager 决策 7 的合法档位；5 虽通过 Step 构造（1-10）但 Validator 拒绝。"""
        plan = _plan(_step("a", config={"num_ccs": 5}))
        errors = validate_errors(plan)
        assert any("num_ccs" in e for e in errors)

    def test_num_ccs_0_is_error(self):
        plan = _plan(_raw_step("a", config={"num_ccs": 0}))
        errors = validate_errors(plan)
        assert any("num_ccs" in e for e in errors)

    def test_num_ccs_string_is_error(self):
        plan = _plan(_raw_step("a", config={"num_ccs": "2"}))
        errors = validate_errors(plan)
        assert any("num_ccs" in e for e in errors)

    @pytest.mark.parametrize("valid", [1, 2, 3, 4])
    def test_valid_num_ccs_passes(self, valid):
        plan = _plan(_step("a", config={"num_ccs": valid}))
        assert validate_errors(plan) == []

    def test_missing_num_ccs_passes(self):
        plan = _plan(_step("a"))
        assert validate_errors(plan) == []


# ============================================================================
# 3. exec_type ↔ role 一致性
# ============================================================================

class TestExecTypeRoleConsistency:
    def test_worker_with_manager_role_is_error(self):
        plan = _plan(_step("a", exec_type="worker", role="engineering_coord"))
        errors = validate_errors(plan, manager_roles=MANAGER_ROLES, worker_roles=WORKER_ROLES)
        assert any("exec_type=worker with role 'engineering_coord'" in e for e in errors)

    def test_manager_with_worker_role_is_error(self):
        plan = _plan(_step("a", exec_type="manager", role="code"))
        errors = validate_errors(plan, manager_roles=MANAGER_ROLES, worker_roles=WORKER_ROLES)
        assert any("exec_type=manager with role 'code'" in e for e in errors)

    def test_default_role_valid_for_both(self):
        """'default' 在 Manager/Worker 两目录均存在（基类），两类 exec_type 均合法。"""
        plan = _plan(_step("a", exec_type="manager", role="default"))
        assert validate_errors(plan, manager_roles=MANAGER_ROLES, worker_roles=WORKER_ROLES) == []
        plan = _plan(_step("a", exec_type="worker", role="default"))
        assert validate_errors(plan, manager_roles=MANAGER_ROLES, worker_roles=WORKER_ROLES) == []

    def test_matching_role_passes(self):
        plan = _plan(
            _step("m", exec_type="manager", role="engineering_coord"),
            _step("w", exec_type="worker", role="dev_lead"),
        )
        assert validate_errors(plan, manager_roles=MANAGER_ROLES, worker_roles=WORKER_ROLES) == []

    def test_skipped_when_role_sets_not_provided(self):
        """未提供角色集 → exec_type↔role 校验跳过（validate_plan 纯净，不依赖 roles 目录）。"""
        plan = _plan(_step("a", exec_type="worker", role="engineering_coord"))
        assert validate_errors(plan) == []


# ============================================================================
# 3.5 task_summary 必填（Router 职责索引）
# ============================================================================

class TestTaskSummary:
    def test_missing_task_summary_is_error(self):
        """缺 task_summary → 硬错误；报错信息含正例，便于 manager 自愈。"""
        plan = _plan(_raw_step("a", task_summary=""))
        errors = validate_errors(plan)
        assert any("task_summary is required" in e for e in errors)

    def test_blank_task_summary_is_error(self):
        plan = _plan(_raw_step("a", task_summary="   "))
        errors = validate_errors(plan)
        assert any("task_summary is required" in e for e in errors)

    def test_present_task_summary_passes(self):
        plan = _plan(_step("a", task_summary="负责 auth 模块的 JWT 登录/注册接口"))
        assert validate_errors(plan) == []

    def test_reuse_step_ref_exempts_missing_summary(self):
        """增量续跑复用既有子节点（reuse_step_ref 非空）→ 职责沿用原节点，豁免摘要。"""
        plan = _plan(_raw_step("a", task_summary="", reuse_step_ref="old_step"))
        assert validate_errors(plan) == []


# ============================================================================
# 4. 告警（validate_warnings）与 >8 硬错误（Planner）
# ============================================================================

class TestWarnings:
    def test_six_workers_warns(self):
        plan = _plan(*[_step(f"w{i}") for i in range(6)])
        warnings = validate_warnings(plan)
        assert len(warnings) == 1
        assert "6 direct workers" in warnings[0]

    @pytest.mark.parametrize("count", [5, 7, 8])
    def test_range_boundaries(self, count):
        plan = _plan(*[_step(f"w{i}") for i in range(count)])
        warnings = validate_warnings(plan)
        assert len(warnings) == 1 if count > 5 else len(warnings) == 0

    def test_nine_workers_has_no_warning(self):
        """>8 是硬错误（Planner.MAX_DIRECT_WORKERS=8），Validator 不重复上报 warning。"""
        plan = _plan(*[_step(f"w{i}") for i in range(9)])
        assert validate_warnings(plan) == []

    def test_planner_hard_errors_above_eight(self):
        planner = Planner(graph=type("G", (), {"nodes": {}, "edges": {}, "add_node": lambda *a: None})())
        plan = _plan(*[_step(f"w{i}") for i in range(9)])
        errors = planner.validate_plan(plan)
        assert any("Too many direct workers (9)" in e for e in errors)


# ============================================================================
# 5. 防误报回归（Validator 不做"聪明判断"）
# ============================================================================

class TestNoFalsePositives:
    def test_integration_worker_keyword_no_warning(self):
        """「验证前后端集成是否正确」是合法 QA Worker，关键词检测必误报 → 不产生 warning。"""
        plan = _plan(
            _step("dev", role="dev_member", task="实现前后端功能"),
            _step("qa", role="test", task="验证前后端集成是否正确"),
        )
        assert validate_warnings(plan) == []
        assert validate_errors(plan) == []

    def test_event_plus_depends_on_coexist_no_warning(self):
        """depends_on=启动门禁 / Event=动态通知，两者允许并存，不是互斥。"""
        plan = _plan(
            _step(
                "producer",
                role="code",
                config={"events": {"publish": ["api_ready"]}},
            ),
            _step(
                "consumer",
                role="code",
                config={"events": {"subscribe": ["api_ready"]}},
                depends_on=["producer"],
            ),
        )
        assert validate_warnings(plan) == []
        assert validate_errors(plan) == []

    def test_terminal_judgment_not_in_validator(self):
        """Terminal 判据是 Manager 决策 3 的语义判断，Validator 不越权审查 task 措辞。"""
        plan = _plan(_step("vague", task="完善一下现有模块"))
        assert validate_errors(plan) == []
        assert validate_warnings(plan) == []


# ============================================================================
# 6. Planner / Runtime 接入
# ============================================================================

class TestPlannerWiring:
    def _make_planner(self):
        return Planner(graph=type("G", (), {
            "nodes": {}, "edges": {}, "add_node": lambda *a: None,
            "add_edge": lambda *a: None,
        })())

    def test_validate_plan_catches_num_ccs_out_of_range(self):
        plan = _plan(_step("a", config={"num_ccs": 5}))
        errors = self._make_planner().validate_plan(plan)
        assert any("num_ccs" in e for e in errors)


class _TestExecutable(Executable):
    """Minimal executable (auto-completes immediately)."""

    def __init__(self, context: ExecutableContext, **kwargs):
        super().__init__()

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.COMPLETED


@pytest.fixture
def runtime(tmp_path):
    """Real Runtime with registered test executables + real roles dir (exec_type↔role 生效)."""
    rt = Runtime(workspace_root=str(tmp_path), roles_dir=REAL_ROLES_DIR)
    rt.registry.register("manager", _TestExecutable)
    rt.registry.register("worker", _TestExecutable)
    return rt


class TestRuntimeWiring:
    def test_inject_plan_rejects_bad_role_before_graph_mutation(self, runtime):
        root = runtime.submit_root_manager(task="build a thing")
        good = _plan(_step("dev", exec_type="worker", role="dev_member"))
        runtime.inject_plan(root, good)

        before = set(runtime.graph.nodes)
        bad = _plan(_step("bad", exec_type="worker", role="engineering_coord"))
        with pytest.raises(PlanValidationError) as exc_info:
            runtime.inject_plan(root, bad)
        assert any("exec_type=worker with role 'engineering_coord'" in e for e in exc_info.value.errors)
        assert set(runtime.graph.nodes) == before

    def test_inject_plan_without_roles_dir_skips_role_check(self, tmp_path):
        """roles_dir 未注入 → exec_type↔role 校验跳过（向后兼容）。"""
        rt = Runtime(workspace_root=str(tmp_path))
        rt.registry.register("manager", _TestExecutable)
        rt.registry.register("worker", _TestExecutable)
        root = rt.submit_root_manager(task="build a thing")
        plan = _plan(_step("bad", exec_type="worker", role="engineering_coord"))
        nodes = rt.inject_plan(root, plan)
        assert len(nodes) == 1

    def test_create_manual_node_rejects_bad_role(self, runtime):
        root = runtime.submit_root_manager(task="root")
        with pytest.raises(PlanValidationError):
            runtime.create_manual_node(
                task="x", role="code", exec_type="manager", parent_id=root
            )
        # 合法角色可创建
        node = runtime.create_manual_node(
            task="x", role="engineering_coord", exec_type="manager", parent_id=root
        )
        assert node.role == "engineering_coord"

    def test_submit_fix_plan_preserves_exec_type_despite_manager_role_fix(self, runtime):
        """C: exec_type 是结构不变量，review 修复不因 fix.role 类型翻转 manager↔worker。

        回归根因：修复子节点曾被按 fix.role 类型推导 exec_type —— worker 子节点被指定
        manager 角色会升级成 manager，manager 子节点被指定 worker 角色会降级成 worker
        （有子节点时子树悬空）。现改为：有原节点时固定沿用 child.exec_type，跨类型
        fix.role 收敛回原角色、原地续接（不新建替代节点、不 STOPPED）。
        """
        root = runtime.submit_root_manager(task="root")
        old = runtime.inject_plan(
            root, _plan(_step("failed", exec_type="worker", role="dev_member"))
        )[0]
        new_nodes = runtime.submit_fix_plan(
            root,
            [{"step_ref": "failed", "fix": {"role": "engineering_coord", "task": "replan"}}],
            1,
        )
        # 原地续接（非新建替代节点）：worker 保持 worker，未被升级成 manager
        assert len(new_nodes) == 1
        assert new_nodes[0].id == old.id
        assert new_nodes[0].exec_type == "worker"
        assert new_nodes[0].role == "dev_member"
        assert new_nodes[0].status == NodeStatus.READY
        original = runtime.get_node(old.id)
        assert original is not None
        assert original.exec_type == "worker"
        assert original.role == "dev_member"

    def test_submit_fix_plan_rejects_bad_role_before_mutation(self, runtime):
        """非法角色（不在任何 Manager/Worker 角色集）在任一图变更前拦截。"""
        root = runtime.submit_root_manager(task="root")
        runtime.inject_plan(root, _plan(_step("failed", exec_type="worker", role="dev_member")))
        children_before = {c.id for c in runtime.graph.children(root)}
        assert len(children_before) == 1

        bad_assessments = [{
            "step_ref": "failed",
            "fix": {"role": "ghost_role", "task": "fix it"},
        }]
        with pytest.raises(PlanValidationError) as exc_info:
            runtime.submit_fix_plan(root, bad_assessments, iteration_count=1)
        assert any("exec_type=worker with role 'ghost_role'" in e for e in exc_info.value.errors)
        # 失败节点未被删除（校验在任何图变更前拦截）
        children_after = {c.id for c in runtime.graph.children(root)}
        assert children_after == children_before

    def test_submit_fix_plan_derives_summary_when_child_has_none(self, runtime):
        """回归：子节点 task_summary 为空时修复计划仍可提交，不得抛 PlanValidationError。

        根因：修复步骤的摘要只取 fix.task_summary 与 child.task_summary，二者皆空
        （早期创建的历史节点不会携带该字段）时撞上「task_summary 必填」硬校验 →
        异常在任何图变更前抛出 → Manager 被置 plan_validation_error，随后自动重试
        只重开评审 → 同一处再抛 → 迭代耗尽，真因被「Max iterations exceeded」掩盖。
        摘要对修复步骤无独立信息量（职责沿用原节点），修复任务文本即可充当索引。
        """
        root = runtime.submit_root_manager(task="root")
        old = runtime.inject_plan(
            root, _plan(_step("legacy", exec_type="worker", role="dev_member"))
        )[0]
        # 模拟历史节点：节点上没有持久化职责摘要
        runtime.graph.update_node(old.id, task_summary=None)

        fixed = runtime.submit_fix_plan(
            root,
            [{
                "step_ref": "legacy",
                "fix": {"role": "dev_member", "task": "补出复核结论并落盘"},
            }],
            1,
        )

        # 原地续接：不新建替代节点，原节点被重新激活并带上修复任务
        assert len(fixed) == 1
        assert fixed[0].id == old.id
        assert fixed[0].status == NodeStatus.READY
        assert runtime.get_node(old.id).task == "补出复核结论并落盘"

    def test_submit_fix_plan_replacement_node_gets_derived_summary(self, runtime):
        """角色切换新建替代节点时，用修复任务文本兜底其职责摘要（Router 索引不为空）。"""
        root = runtime.submit_root_manager(task="root")
        old = runtime.inject_plan(
            root, _plan(_step("legacy", exec_type="worker", role="dev_member"))
        )[0]
        runtime.graph.update_node(old.id, task_summary=None)

        replacements = runtime.submit_fix_plan(
            root,
            [{
                "step_ref": "legacy",
                "fix": {"role": "test", "task": "补测试覆盖"},
            }],
            2,
        )

        assert len(replacements) == 1
        assert replacements[0].id != old.id
        assert replacements[0].step_ref == "legacy_fix2"
        assert replacements[0].task_summary == "补测试覆盖"
        assert runtime.get_node(old.id).status == NodeStatus.STOPPED

    def test_real_roles_sets_match_directory(self, runtime):
        """roles_dir 注入后，manager/worker 角色 ID 集与 roles/ 目录一致（含 default 由校验器放行）。"""
        assert "engineering_coord" in runtime._manager_role_ids
        assert "release_planner" in runtime._manager_role_ids
        for role in ("code", "dev_lead", "test", "document"):
            assert role in runtime._worker_role_ids
        # 两个集合都不含 default（RolesManager.list_roles 跳过基类；校验器内部补 default）
        assert "default" not in runtime._manager_role_ids
        assert "default" not in runtime._worker_role_ids
