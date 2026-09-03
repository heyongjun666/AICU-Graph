"""
Tests for executables/manager.py — Manager 注册与集成

验证:
1. Manager 类实现了 Executable ABC 的全部抽象方法
2. Manager 可被 ExecutableRegistry 注册和创建
3. Manager expand 阶段生成 ExecutionPlan 并提交
4. Manager review 阶段评估子节点结果
"""

import json
import os
from typing import Any, Optional

import pytest

from executables.base import Executable, ExecutableContext, RuntimeGateway
from executables.manager import Manager
from executables.registry import ExecutableRegistry, RegistryError
from runtime.models import (
    ErrorObject,
    ExecutionPlan,
    Node,
    NodeStatus,
    Step,
)


# ============================================================================
# Mock AICU stubs
# ============================================================================

class _ExpandStub:
    """Stub AICU that returns a valid ExecutionPlan dict for expand phase."""

    def __init__(self, role: str = "dev", task: Optional[str] = None):
        self._role = role
        self._task = task

    def ask(self, prompt: str) -> dict:
        return {
            "steps": [
                {
                    "step_id": "step_1",
                    "exec_type": "worker",
                    "role": self._role,
                    "task": self._task or (prompt if len(prompt) < 200 else "Execute task"),
                    "config": {},
                },
            ],
        }


class _ReviewStub:
    """Stub AICU for review phase — configurable pass/fail."""

    def __init__(self, review_passed: bool = True):
        self._passed = review_passed

    def ask(self, prompt: str) -> dict:
        return {
            "review_passed": self._passed,
            "summary": "All good" if self._passed else "Issues found",
            "defects": [] if self._passed else ["defect_1"],
        }


class _DualPhaseStub:
    """Stub AICU that handles both expand and review phases.

    Expand phase returns an ExecutionPlan dict with the configured role.
    Review phase returns a configurable pass/fail result.
    Phase detection is based on prompt prefix.
    """

    def __init__(self, review_passed: bool = True, role: str = "dev",
                 task: Optional[str] = None):
        self._review_passed = review_passed
        self._role = role
        self._task = task

    def ask(self, prompt: str) -> dict:
        if prompt.startswith("## Review Task"):
            return {
                "review_passed": self._review_passed,
                "summary": "All good" if self._review_passed else "Issues found",
                "defects": [] if self._review_passed else ["defect_1"],
            }
        return {
            "steps": [
                {
                    "step_id": "step_1",
                    "exec_type": "worker",
                    "role": self._role,
                    "task": self._task or (prompt if len(prompt) < 200 else "Execute task"),
                    "config": {},
                },
            ],
        }


# ============================================================================
# Mock RuntimeGateway
# ============================================================================

class _MockRuntimeGateway:
    """RuntimeGateway 协议的模拟实现，记录调用历史。"""

    def __init__(self):
        self.submitted_plans: list[tuple[str, ExecutionPlan]] = []
        self.status_updates: list[tuple[str, NodeStatus, dict]] = []
        self._nodes: dict[str, Node] = {}
        self._children: dict[str, list[Node]] = {}

    def add_node(self, node: Node) -> None:
        self._nodes[node.id] = node

    def add_children(self, parent_id: str, children: list[Node]) -> None:
        self._children.setdefault(parent_id, []).extend(children)
        for child in children:
            self._nodes[child.id] = child

    def submit_plan(
        self,
        manager_node_id: str,
        plan: ExecutionPlan,
        iteration_count: int = 0,
    ) -> list[Node]:
        self.submitted_plans.append((manager_node_id, plan))
        # 模拟 Planner 的行为：为每个 step 创建 Node
        child_nodes = []
        for step in plan.steps:
            child = Node(
                id=f"child_{step.step_id}",
                parent_id=manager_node_id,
                exec_type=step.exec_type,
                role=step.role,
                task=step.task,
                config=step.config,
                status=NodeStatus.WAITING,
            )
            child_nodes.append(child)
        self.add_children(manager_node_id, child_nodes)
        return child_nodes

    def submit_fix_plan(
        self,
        manager_node_id: str,
        failed_assessments: list,
        iteration_count: int,
    ) -> list[Node]:
        # 模拟 Runtime.submit_fix_plan：为每个失败子节点创建替换 Node
        fix_nodes = []
        for assessment in failed_assessments:
            fix = assessment.get("fix") or {}
            child = Node(
                id=f"child_{assessment.get('step_ref', 'fix')}_{iteration_count}",
                parent_id=manager_node_id,
                exec_type="worker",
                role=fix.get("role") or "dev",
                task=fix.get("task") or assessment.get("task") or "rework",
                config=fix.get("config") or {},
                status=NodeStatus.WAITING,
                iteration_count=iteration_count,
            )
            fix_nodes.append(child)
        self.add_children(manager_node_id, fix_nodes)
        return fix_nodes

    def update_node_status(
        self, node_id: str, status: NodeStatus, **updates: Any
    ) -> None:
        self.status_updates.append((node_id, status, updates))
        if node_id in self._nodes:
            node = self._nodes[node_id]
            for key, value in updates.items():
                setattr(node, key, value)
            node.status = status

    def get_node(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(node_id)

    def get_children(self, node_id: str) -> list[Node]:
        return self._children.get(node_id, [])

    def get_dependencies(self, node_id: str) -> list[Node]:
        return []

    def read_artifact(self, node_id: str, path: str) -> str:
        return ""

    def register_external_input_wait(
        self, node_id: str, input_schema: Optional[dict] = None
    ) -> str:
        return "token"

    def deliver_external_input(self, wait_token: str, input_data: Any) -> None:
        pass


# ============================================================================
# Helper
# ============================================================================

def _make_manager_node(
    node_id: str = "mgr1",
    status: NodeStatus = NodeStatus.READY,
    manager_phase: Optional[str] = "expand",
    task: str = "Do something",
    role: str = "coder",
    iteration_count: int = 0,
    max_iterations: int = 3,
) -> Node:
    return Node(
        id=node_id,
        exec_type="manager",
        role=role,
        task=task,
        config={},
        status=status,
        manager_phase=manager_phase,
        iteration_count=iteration_count,
        max_iterations=max_iterations,
    )


# ============================================================================
# Tests
# ============================================================================

class TestManagerClassStructure:

    def test_manager_is_executable_subclass(self):
        """Manager 必须是 Executable 的子类。"""
        assert issubclass(Manager, Executable), \
            "Manager must inherit from Executable"

    def test_manager_implements_all_abstract_methods(self):
        """Manager 必须实现 Executable 的全部抽象方法。"""
        assert not getattr(Manager, "__abstractmethods__", None), \
            f"Manager has unimplemented abstract methods: {Manager.__abstractmethods__}"

    def test_manager_can_be_instantiated(self):
        """Manager 可被实例化。"""
        node = _make_manager_node()
        ctx = ExecutableContext(node=node, runtime=_MockRuntimeGateway())
        mgr = Manager(context=ctx)
        assert isinstance(mgr, Manager)
        assert isinstance(mgr, Executable)

    def test_manager_has_required_methods(self):
        """Manager 实例应具有 run/stop/pause/resume/cleanup_resources 方法。"""
        node = _make_manager_node()
        ctx = ExecutableContext(node=node, runtime=_MockRuntimeGateway())
        mgr = Manager(context=ctx)
        assert hasattr(mgr, "run") and callable(mgr.run)
        assert hasattr(mgr, "stop") and callable(mgr.stop)
        assert hasattr(mgr, "pause") and callable(mgr.pause)
        assert hasattr(mgr, "resume") and callable(mgr.resume)
        assert hasattr(mgr, "cleanup_resources") and callable(mgr.cleanup_resources)


class TestManagerRegistry:

    def setup_method(self):
        self.registry = ExecutableRegistry()
        self.runtime = _MockRuntimeGateway()

    def test_register_manager_type(self):
        """Manager 类型可被注册到 ExecutableRegistry。"""
        self.registry.register("manager", Manager)
        types = self.registry.get_types()
        assert "manager" in types

    def test_create_manager_via_registry(self):
        """通过 Registry 可创建 Manager 实例。"""
        self.registry.register("manager", Manager)
        node = _make_manager_node()
        ctx = ExecutableContext(node=node, runtime=self.runtime)
        instance = self.registry.create("manager", ctx)
        assert isinstance(instance, Manager)
        assert isinstance(instance, Executable)

    def test_manager_registry_roundtrip(self):
        """Manager 注册→创建→run 完整路径。"""
        self.registry.register("manager", Manager)
        node = _make_manager_node()
        self.runtime.add_node(node)
        ctx = ExecutableContext(node=node, runtime=self.runtime)
        mgr = self.registry.create("manager", ctx, aicu_instance=_ExpandStub())

        result = mgr.run(ctx)
        assert result == NodeStatus.WAITING
        assert len(self.runtime.submitted_plans) == 1
        plan_node_id, plan = self.runtime.submitted_plans[0]
        assert plan_node_id == "mgr1"
        assert len(plan.steps) >= 1

    def test_manager_registry_separate_from_worker(self):
        """Manager 和 Worker 在 Registry 中各自独立注册。"""
        self.registry.register("manager", Manager)
        self.registry.register("worker", Manager)

        types = self.registry.get_types()
        assert "manager" in types
        assert "worker" in types


class TestManagerExpandPhase:

    def setup_method(self):
        self.runtime = _MockRuntimeGateway()

    def _make_manager(self, ctx) -> Manager:
        return Manager(
            context=ctx,
            aicu_instance=_ExpandStub(role=ctx.node.role, task=ctx.node.task),
        )

    def test_expand_generates_execution_plan(self):
        """expand 阶段应生成 ExecutionPlan。"""
        node = _make_manager_node()
        self.runtime.add_node(node)
        ctx = ExecutableContext(node=node, runtime=self.runtime)
        mgr = self._make_manager(ctx)

        result = mgr.run(ctx)

        assert result == NodeStatus.WAITING
        assert len(self.runtime.submitted_plans) == 1
        plan = self.runtime.submitted_plans[0][1]
        assert isinstance(plan, ExecutionPlan)
        assert len(plan.steps) > 0

    def test_expand_submits_plan_to_runtime(self):
        """expand 阶段应通过 submit_plan 提交计划。"""
        node = _make_manager_node()
        self.runtime.add_node(node)
        ctx = ExecutableContext(node=node, runtime=self.runtime)
        mgr = self._make_manager(ctx)

        mgr.run(ctx)

        submitted_node_id, _ = self.runtime.submitted_plans[0]
        assert submitted_node_id == node.id

    def test_expand_creates_worker_step(self):
        """expand 阶段应创建包含 worker step 的 ExecutionPlan。"""
        node = _make_manager_node(task="Build a web app", role="frontend")
        self.runtime.add_node(node)
        ctx = ExecutableContext(node=node, runtime=self.runtime)
        mgr = self._make_manager(ctx)

        mgr.run(ctx)

        plan = self.runtime.submitted_plans[0][1]
        step = plan.steps[0]
        assert step.exec_type == "worker"
        assert step.role == "frontend"
        assert step.task == "Build a web app"

    def test_expand_sets_waitting_status(self):
        """expand 完成后节点应处于 WAITING 状态。"""
        node = _make_manager_node()
        self.runtime.add_node(node)
        ctx = ExecutableContext(node=node, runtime=self.runtime)
        mgr = self._make_manager(ctx)

        mgr.run(ctx)

        waiting_updates = [
            u for u in self.runtime.status_updates
            if u[1] == NodeStatus.WAITING
        ]
        assert len(waiting_updates) >= 1


class TestManagerPlanParsing:
    """真实 AICU 汇总 metadata 中的规划结果解析。"""

    @staticmethod
    def _step() -> dict:
        return {
            "step_id": "step_1",
            "exec_type": "worker",
            "role": "dev",
            "task": "implement the requested change",
            "config": {},
        }

    def _manager(self) -> Manager:
        node = _make_manager_node()
        return Manager(
            context=ExecutableContext(node=node, runtime=_MockRuntimeGateway())
        )

    def test_parse_plan_from_real_aicu_content_parsed_metadata(self):
        """AICU 汇总 dict 应从 content_parsed 提取 execution_plan_json。"""
        payload = {
            "final_output_path": "/missing/plan_result",
            "content": json.dumps(
                {"execution_plan_json": {"steps": [self._step()]}},
                ensure_ascii=False,
            ),
            "content_parsed": {
                "execution_plan_json": {"steps": [self._step()]},
            },
            "scores": {},
            "audit_passed": True,
        }

        plan = self._manager()._parse_plan(payload)

        assert len(plan.steps) == 1
        assert plan.steps[0].step_id == "step_1"
        assert plan.steps[0].task == "implement the requested change"

    def test_parse_plan_from_content_string(self):
        """没有 content_parsed 时应从 content JSON 字符串提取计划。"""
        payload = {
            "content": json.dumps(
                {"execution_plan_json": {"steps": [self._step()]}},
                ensure_ascii=False,
            ),
        }

        plan = self._manager()._parse_plan(payload)

        assert [step.step_id for step in plan.steps] == ["step_1"]

    def test_parse_plan_from_final_output_file(self, tmp_path):
        """没有内嵌内容时仍应从 final_output_path 文件提取计划。"""
        output_path = tmp_path / "plan_result"
        output_path.write_text(
            json.dumps({"execution_plan_json": {"steps": [self._step()]}}, ensure_ascii=False),
            encoding="utf-8",
        )

        plan = self._manager()._parse_plan({"final_output_path": str(output_path)})

        assert [step.step_id for step in plan.steps] == ["step_1"]


class TestManagerReviewPhase:

    def setup_method(self):
        self.runtime = _MockRuntimeGateway()

    def _make_manager(self, ctx, review_passed: bool = True) -> Manager:
        return Manager(context=ctx, aicu_instance=_ReviewStub(review_passed=review_passed))

    def test_review_all_completed(self):
        """review 阶段：所有子节点 COMPLETED → Manager COMPLETED。"""
        node = _make_manager_node(manager_phase="review")
        self.runtime.add_node(node)

        children = [
            Node(id="c1", exec_type="worker", role="dev", task="t1",
                 config={}, status=NodeStatus.COMPLETED),
            Node(id="c2", exec_type="worker", role="dev", task="t2",
                 config={}, status=NodeStatus.COMPLETED),
        ]
        self.runtime.add_children(node.id, children)

        ctx = ExecutableContext(node=node, runtime=self.runtime)
        mgr = self._make_manager(ctx, review_passed=True)

        result = mgr.run(ctx)

        assert result == NodeStatus.COMPLETED

    def test_review_some_failed_within_iterations(self):
        """review 阶段：有子节点 FAILED 但未超迭代上限 → 迭代重试。"""
        node = _make_manager_node(
            manager_phase="review",
            iteration_count=0,
            max_iterations=3,
        )
        self.runtime.add_node(node)

        children = [
            Node(id="c1", exec_type="worker", role="dev", task="t1",
                 config={}, status=NodeStatus.COMPLETED),
            Node(id="c2", exec_type="worker", role="dev", task="t2",
                 config={}, status=NodeStatus.FAILED),
        ]
        self.runtime.add_children(node.id, children)

        ctx = ExecutableContext(node=node, runtime=self.runtime)
        # _DualPhaseStub: review returns not-passed, expand returns valid plan
        mgr = Manager(context=ctx, aicu_instance=_DualPhaseStub(review_passed=False))

        result = mgr.run(ctx)

        assert result == NodeStatus.WAITING


    def test_review_exceeds_max_iterations(self):
        """review 阶段：超出迭代上限 → FAILED。"""
        node = _make_manager_node(
            manager_phase="review",
            iteration_count=2,
            max_iterations=3,
        )
        self.runtime.add_node(node)

        children = [
            Node(id="c1", exec_type="worker", role="dev", task="t1",
                 config={}, status=NodeStatus.FAILED),
        ]
        self.runtime.add_children(node.id, children)

        ctx = ExecutableContext(node=node, runtime=self.runtime)
        mgr = self._make_manager(ctx, review_passed=False)

        result = mgr.run(ctx)

        assert result == NodeStatus.FAILED

    def test_review_all_stopped_within_iterations(self):
        """review 阶段：有子节点 STOPPED 但未超迭代上限 → 迭代重试。"""
        node = _make_manager_node(
            manager_phase="review",
            iteration_count=0,
            max_iterations=3,
        )
        self.runtime.add_node(node)

        children = [
            Node(id="c1", exec_type="worker", role="dev", task="t1",
                 config={}, status=NodeStatus.COMPLETED),
            Node(id="c2", exec_type="worker", role="dev", task="t2",
                 config={}, status=NodeStatus.STOPPED),
        ]
        self.runtime.add_children(node.id, children)

        ctx = ExecutableContext(node=node, runtime=self.runtime)
        # _DualPhaseStub: review returns not-passed, expand returns valid plan
        mgr = Manager(context=ctx, aicu_instance=_DualPhaseStub(review_passed=False))

        result = mgr.run(ctx)

        assert result == NodeStatus.WAITING


    def test_review_with_no_children(self):
        """review 阶段：无子节点 → COMPLETED（无子节点视为全部完成）。"""
        node = _make_manager_node(manager_phase="review")
        self.runtime.add_node(node)

        ctx = ExecutableContext(node=node, runtime=self.runtime)
        mgr = self._make_manager(ctx, review_passed=True)

        result = mgr.run(ctx)

        assert result == NodeStatus.COMPLETED

    def test_review_prompt_omits_main_task(self):
        """review prompt 不重新注入 Manager 的完整 Main Task，只展示子节点元数据。

        Context 规则：Main Task 只在同一 AICU 会话首次初始化注入一次；review 是
        增量阶段，只能注入子节点元数据 + 按需核验提示，不得重新拼接 Main Task。
        """
        node = _make_manager_node(
            manager_phase="review", task="MAIN_TASK_MUST_NOT_REPEAT"
        )
        self.runtime.add_node(node)
        ctx = ExecutableContext(node=node, runtime=self.runtime)

        children_info = [{
            "step_ref": "s1",
            "node_id": "c1",
            "exec_type": "worker",
            "role": "dev",
            "task": "child task",
            "status": NodeStatus.COMPLETED,
            "artifact_path": "/tmp/out/c1",
            "shared_workspace": "/tmp/shared",
            "output_path": "code",
            "artifact_content": "",
        }]

        prompt = Manager._build_review_prompt(ctx, children_info)

        assert "## Review Task" in prompt
        assert "MAIN_TASK_MUST_NOT_REPEAT" not in prompt, (
            "review prompt 不得重新注入 Main Task"
        )
        assert "Artifact Path" in prompt, "review prompt 应保留子节点产物路径"
        assert "Shared Partition" in prompt, "review prompt 应保留共享分区根"
        assert "未预加载" in prompt, "review prompt 应提示按需读取产物"


class _UnparseableAuditPassStub:
    """Review AICU：final_output_path 内容不可解析（产物回显），但 audit 通过。

    模拟真实 AICU 在 Review 阶段把产物代码回显到 plan_result 而非输出
    要求的 assessments JSON 契约（E2E 冒烟中的实际失败形态）。
    修复性重试同样返回不可解析产物，验证 Manager 走到审计降级路径。
    """

    def __init__(self, tmp_path: str):
        self._tmp = tmp_path
        self.calls = 0

    def ask(self, prompt: str) -> dict:
        self.calls += 1
        fpath = os.path.join(self._tmp, "plan_result")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("// artifact echo — not review JSON\nconst x = 1;\n")
        return {
            "final_output_path": fpath,
            "audit_passed": True,
            "audit_defects": [],
            "consensus_reached": True,
            "strategy": "pair (2CC: 生产+审查)",
        }


class _FixPlanRecordingGateway(_MockRuntimeGateway):
    """带 submit_fix_plan 记录的 RuntimeGateway，用于断言"绝不进入重建循环"。"""

    def __init__(self):
        super().__init__()
        self.fix_plans: list[tuple[str, list, int]] = []

    def submit_fix_plan(
        self,
        manager_node_id: str,
        failed_assessments: list,
        iteration_count: int,
    ) -> list[Node]:
        self.fix_plans.append((manager_node_id, failed_assessments, iteration_count))
        return []


class TestManagerReviewDegradation:
    """Review 输出格式失败（JSON 契约未满足）时的降级路径。

    背景（E2E 冒烟实测）：Review AICU 可能把产物代码回显到 final_output_path，
    而不是输出 {overall_passed, assessments:[...]} JSON 契约。旧逻辑无条件降级为
    "全量失败 → 重建"，导致 Manager 反复重建至 max_iterations 而 FAILED。
    修复：先修复性重试一次；仍失败则按 AICU 审计元数据降级（审计通过→全量通过
    完成；审计失败→快速失败不重建；无审计信号→保持旧行为迭代修复）。
    """

    # ------------------------------------------------------------------
    # _parse_review_assessments 状态语义
    # ------------------------------------------------------------------

    def test_parse_valid_assessments_status_parsed(self):
        """合法 assessments JSON → (assessments, 'parsed')。"""
        children = [
            {"step_ref": "s1", "role": "dev", "task": "t1"},
            {"step_ref": "s2", "role": "dev", "task": "t2"},
        ]
        raw = {
            "overall_passed": False,
            "assessments": [
                {"step_ref": "s1", "passed": True, "reason": "ok"},
                {"step_ref": "s2", "passed": False, "reason": "bad",
                 "fix": {"role": "dev", "task": "rework"}},
            ],
        }
        assessments, status = Manager._parse_review_assessments(raw, children)
        assert status == "parsed"
        assert [a["passed"] for a in assessments] == [True, False]

    def test_parse_non_json_string_status_unparseable(self):
        """不可解析字符串 → 全量失败 + status='unparseable'。"""
        children = [{"step_ref": "s1", "role": "dev", "task": "t1"}]
        assessments, status = Manager._parse_review_assessments(
            "## Review\n这不是 JSON\njust narrative", children
        )
        assert status == "unparseable"
        assert all(not a["passed"] for a in assessments)
        assert assessments[0]["reason"] == "AICU output unparseable"

    def test_parse_verdict_passed_maps_to_all_pass(self):
        """dict 带 review_passed=True（legacy 桩契约）→ 全量通过 + status='parsed'。"""
        children = [
            {"step_ref": "s1", "role": "dev", "task": "t1"},
            {"step_ref": "s2", "role": "dev", "task": "t2"},
        ]
        assessments, status = Manager._parse_review_assessments(
            {"review_passed": True, "summary": "All good", "defects": []},
            children,
        )
        assert status == "parsed"
        assert all(a["passed"] for a in assessments)

    def test_parse_verdict_failed_maps_to_full_fail(self):
        """dict 带 review_passed=False → 全量失败 + status='parsed'（走迭代修复）。"""
        children = [{"step_ref": "s1", "role": "dev", "task": "t1"}]
        assessments, status = Manager._parse_review_assessments(
            {"review_passed": False, "defects": ["issue_1"]},
            children,
        )
        assert status == "parsed"
        assert not assessments[0]["passed"]
        assert "issue_1" in assessments[0]["reason"]

    def test_parse_overall_passed_false_without_assessments_status_parsed(self):
        """dict 带 overall_passed=False 但无 assessments → 全量失败 + status='parsed'。"""
        children = [{"step_ref": "s1", "role": "dev", "task": "t1"}]
        assessments, status = Manager._parse_review_assessments(
            {"overall_passed": False},
            children,
        )
        assert status == "parsed"
        assert not assessments[0]["passed"]

    def test_parse_no_verdict_no_assessments_status_unparseable(self):
        """dict 无 assessments 且无 verdict/audit 信号 → status='unparseable'。"""
        children = [{"step_ref": "s1", "role": "dev", "task": "t1"}]
        assessments, status = Manager._parse_review_assessments(
            {"steps": [{"step_id": "x"}]},
            children,
        )
        assert status == "unparseable"
        assert all(not a["passed"] for a in assessments)

    # ------------------------------------------------------------------
    # _degrade_review 降级决策
    # ------------------------------------------------------------------

    def test_degrade_audit_pass_degrades_all_pass(self):
        """audit_passed=True 且无缺陷 → 全量通过 + status='degraded_pass'。"""
        children = [
            {"step_ref": "s1", "role": "dev", "task": "t1"},
            {"step_ref": "s2", "role": "dev", "task": "t2"},
        ]
        assessments, status = Manager._degrade_review(
            {"audit_passed": True, "audit_defects": []}, children
        )
        assert status == "degraded_pass"
        assert all(a["passed"] for a in assessments)

    def test_degrade_audit_fail_fast_fail(self):
        """audit_passed=False / 有缺陷 → 全量失败 + status='unparseable_fail'（不重建）。"""
        children = [{"step_ref": "s1", "role": "dev", "task": "t1"}]
        assessments, status = Manager._degrade_review(
            {"audit_passed": False, "audit_defects": ["org_risk"]}, children
        )
        assert status == "unparseable_fail"
        assert not assessments[0]["passed"]

    def test_degrade_defects_present_fast_fail(self):
        """audit_defects 非空 → 即便 audit_passed=True 也快速失败。"""
        children = [{"step_ref": "s1", "role": "dev", "task": "t1"}]
        assessments, status = Manager._degrade_review(
            {"audit_passed": True, "audit_defects": ["d1"]}, children
        )
        assert status == "unparseable_fail"

    def test_degrade_no_audit_signal_keeps_old_behavior(self):
        """无任何 audit 信号 → 全量失败 + status='parsed'（保持旧行为迭代修复）。"""
        children = [{"step_ref": "s1", "role": "dev", "task": "t1"}]
        assessments, status = Manager._degrade_review(
            {"steps": [{"step_id": "x"}]}, children
        )
        assert status == "parsed"
        assert not assessments[0]["passed"]

    # ------------------------------------------------------------------
    # 修复性重试 prompt
    # ------------------------------------------------------------------

    def test_repair_prompt_demands_strict_json(self):
        """修复性重试 prompt 必须要求严格 JSON、禁止 markdown。"""
        prompt = Manager._build_repair_review_prompt("original")
        assert "只" in prompt and "JSON" in prompt
        assert "overall_passed" in prompt and "assessments" in prompt
        assert "markdown" in prompt

    # ------------------------------------------------------------------
    # E2E: _review 格式失败 + 审计通过 → COMPLETED，绝不进入重建循环
    # ------------------------------------------------------------------

    def test_review_unparseable_with_audit_pass_completes(self, tmp_path):
        """Review 输出不可解析但 audit_passed=True → Manager COMPLETED（降级通过）。"""
        node = _make_manager_node(manager_phase="review", max_iterations=3)
        runtime = _FixPlanRecordingGateway()
        runtime.add_node(node)
        children = [
            Node(id="c1", exec_type="worker", role="dev", task="t1",
                 config={}, status=NodeStatus.COMPLETED),
        ]
        runtime.add_children(node.id, children)

        ctx = ExecutableContext(
            node=node,
            runtime=runtime,
            config={"workspace_dir": str(tmp_path)},
        )
        stub = _UnparseableAuditPassStub(str(tmp_path))
        mgr = Manager(context=ctx, aicu_instance=stub)

        result = mgr.run(ctx)

        assert result == NodeStatus.COMPLETED
        # 初次 + 修复性重试各一次 AICU 调用
        assert stub.calls == 2
        # 绝不进入重建循环（无 submit_fix_plan）
        assert runtime.fix_plans == []

    def test_review_unparseable_with_audit_defects_fails_fast(self, tmp_path):
        """Review 输出不可解析且审计有缺陷 → 快速 FAILED，绝不进入重建循环。"""
        node = _make_manager_node(manager_phase="review", max_iterations=3)
        runtime = _FixPlanRecordingGateway()
        runtime.add_node(node)
        children = [
            Node(id="c1", exec_type="worker", role="dev", task="t1",
                 config={}, status=NodeStatus.COMPLETED),
        ]
        runtime.add_children(node.id, children)

        class _UnparseableAuditFailStub:
            """final_output_path 不可解析且 audit 有缺陷。"""

            def __init__(self, tmp):
                self._tmp = tmp
                self.calls = 0

            def ask(self, prompt: str) -> dict:
                self.calls += 1
                fpath = os.path.join(self._tmp, "plan_result")
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write("// artifact echo\nconst z = 3;\n")
                return {
                    "final_output_path": fpath,
                    "audit_passed": False,
                    "audit_defects": ["org_risk: missing contract"],
                    "consensus_reached": True,
                }

        ctx = ExecutableContext(
            node=node,
            runtime=runtime,
            config={"workspace_dir": str(tmp_path)},
        )
        stub = _UnparseableAuditFailStub(str(tmp_path))
        mgr = Manager(context=ctx, aicu_instance=stub)

        result = mgr.run(ctx)

        assert result == NodeStatus.FAILED
        assert stub.calls == 2
        assert runtime.fix_plans == []  # 格式失败 + 审计缺陷 → 不重建

    def test_review_unparseable_repair_retry_gets_json(self, tmp_path):
        """修复性重试成功拿到 JSON → 按真实评估走正常逻辑（不降级）。"""
        node = _make_manager_node(manager_phase="review", max_iterations=3)
        runtime = _FixPlanRecordingGateway()
        runtime.add_node(node)
        children = [
            Node(id="c1", exec_type="worker", role="dev", task="t1",
                 config={}, status=NodeStatus.COMPLETED),
        ]
        runtime.add_children(node.id, children)

        class _RepairSucceedsStub:
            """首次不可解析，修复重试输出合法 assessments JSON。"""

            def __init__(self, tmp):
                self._tmp = tmp
                self.calls = 0

            def ask(self, prompt: str) -> dict:
                self.calls += 1
                if self.calls == 1:
                    fpath = os.path.join(self._tmp, "plan_result")
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write("// artifact echo\nconst y = 2;\n")
                    return {
                        "final_output_path": fpath,
                        "audit_passed": True,
                        "audit_defects": [],
                    }
                # 修复重试：输出合法契约
                return {
                    "final_output_path": os.path.join(self._tmp, "plan_result"),
                    "assessments": [
                        {"step_ref": "c1", "passed": True, "reason": "ok after repair"},
                    ],
                }

        ctx = ExecutableContext(
            node=node,
            runtime=runtime,
            config={"workspace_dir": str(tmp_path)},
        )
        stub = _RepairSucceedsStub(str(tmp_path))
        mgr = Manager(context=ctx, aicu_instance=stub)

        result = mgr.run(ctx)

        assert result == NodeStatus.COMPLETED
        assert stub.calls == 2


class TestManagerStopPauseResume:

    def setup_method(self):
        self.runtime = _MockRuntimeGateway()

    def test_stop(self):
        """stop 应幂等不抛出异常。"""
        node = _make_manager_node()
        ctx = ExecutableContext(node=node, runtime=self.runtime)
        mgr = Manager(context=ctx)
        mgr.stop(ctx)

    def test_pause(self):
        """pause 应幂等不抛出异常。"""
        node = _make_manager_node()
        ctx = ExecutableContext(node=node, runtime=self.runtime)
        mgr = Manager(context=ctx)
        mgr.pause(ctx)

    def test_resume(self):
        """resume 应返回 NodeStatus。"""
        node = _make_manager_node()
        self.runtime.add_node(node)
        ctx = ExecutableContext(node=node, runtime=self.runtime)
        mgr = Manager(context=ctx)
        result = mgr.resume(ctx)
        assert isinstance(result, NodeStatus)

    def test_cleanup_resources(self):
        """cleanup_resources 应幂等不抛出异常。"""
        node = _make_manager_node()
        ctx = ExecutableContext(node=node, runtime=self.runtime)
        mgr = Manager(context=ctx)
        mgr.cleanup_resources(ctx)


class TestManagerEdgeCases:

    def setup_method(self):
        self.runtime = _MockRuntimeGateway()

    def test_missing_node_still_expands(self):
        """run() 只按 ctx.node.manager_phase 分派，不要求节点已注册到 Runtime。

        Manager 可执行体只依赖传入的 ctx.node 工作；节点是否存在于 Runtime
        Graph 是 Scheduler 的职责，不是可执行体自身需要校验的防御点。
        """
        node = _make_manager_node(node_id="nonexistent")
        ctx = ExecutableContext(node=node, runtime=self.runtime)
        mgr = Manager(context=ctx, aicu_instance=_ExpandStub())

        result = mgr.run(ctx)
        assert result == NodeStatus.WAITING

    def test_unknown_phase_returns_failed(self):
        """未知 phase 值应返回 FAILED。"""
        node = _make_manager_node(manager_phase="unknown_phase")
        self.runtime.add_node(node)
        ctx = ExecutableContext(node=node, runtime=self.runtime)
        mgr = Manager(context=ctx)

        result = mgr.run(ctx)
        assert result == NodeStatus.FAILED
