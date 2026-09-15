"""单节点 agent（CC）数量策略：动态配置开关的运行时语义。

覆盖：
1.  开关关闭 → 所有节点（含 root Manager 自身）的 num_ccs 被钉死为 default_num_ccs，
    压过 plan 传入值与角色 YAML 值
2.  开关开启（默认）→ 行为与改动前完全一致，plan 值原样保留
3.  取值为配置的 default_num_ccs，而非硬编码 1
4.  非法取值（0 / 超上限 / 非数字 / None）被 clamp 到 1..4
5.  两个绕过 _prepare_node_config 的路径同样受策略约束：
    revise_manager_node、submit_fix_plan
6.  Runtime() 默认值保持向后兼容（动态 + 1）
"""

from typing import Optional

import pytest

from executables.base import Executable, ExecutableContext
from runtime.models import ExecutionPlan, NodeStatus, Step
from runtime.persistence import Persistence
from runtime.runtime import Runtime


# ============================================================================
# Helpers
# ============================================================================

class _NoopExecutable(Executable):
    """最小可执行体：立即完成，仅供 inject_plan 调度使用。"""

    def __init__(self, context: ExecutableContext, **kwargs):
        super().__init__()

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.COMPLETED

    def stop(self, ctx: ExecutableContext) -> None:
        pass

    def pause(self, ctx: ExecutableContext) -> None:
        pass

    def resume(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.RUNNING

    def cleanup_resources(self, ctx: ExecutableContext) -> None:
        pass


def _make_runtime(
    tmp_path,
    *,
    dynamic: Optional[bool] = None,
    default: Optional[int] = None,
) -> Runtime:
    """构造带数量策略的 Runtime；两个参数为 None 时不传（走构造函数默认）。"""
    kwargs = {}
    if dynamic is not None:
        kwargs["dynamic_num_ccs"] = dynamic
    if default is not None:
        kwargs["default_num_ccs"] = default
    rt = Runtime(
        workspace_root=str(tmp_path),
        persistence=Persistence(file_path=str(tmp_path / "state.json")),
        **kwargs,
    )
    # 关闭后台 tick 线程，使断言确定性
    rt.scheduler.start = lambda: None  # type: ignore[method-assign]
    rt.registry.register("manager", _NoopExecutable)
    rt.registry.register("worker", _NoopExecutable)
    return rt


def _step(step_id: str, config: dict, **kw) -> Step:
    kw.setdefault("exec_type", "worker")
    kw.setdefault("role", "default")
    kw.setdefault("task", f"task {step_id}")
    kw.setdefault("task_summary", f"summary {step_id}")
    kw.setdefault("depends_on", [])
    return Step(step_id=step_id, config=config, **kw)


def _completed_root(rt: Runtime) -> str:
    mgr_id = rt.submit_root_manager(task="Build feature", role="default")
    rt.graph.update_node(mgr_id, status=NodeStatus.COMPLETED)
    return mgr_id


def _num_ccs(rt: Runtime, node_id: str) -> Optional[int]:
    return (rt.graph.get_node(node_id).config or {}).get("num_ccs")


# ============================================================================
# 1. 开关关闭 → 钉死
# ============================================================================

class TestFixedPolicy:

    def test_root_manager_is_pinned(self, tmp_path):
        """root Manager 自身也被钉死（压过角色 YAML 的 num_ccs=2）。"""
        rt = _make_runtime(tmp_path, dynamic=False, default=1)
        root = rt.submit_root_manager("build app", config={"num_ccs": 3})
        assert _num_ccs(rt, root) == 1

    def test_root_manager_pinned_even_without_explicit_num_ccs(self, tmp_path):
        """未传 num_ccs 时也要写入钉死值，否则 aicu 会回退到角色 YAML 的 2。"""
        rt = _make_runtime(tmp_path, dynamic=False, default=1)
        root = rt.submit_root_manager("build app")
        assert _num_ccs(rt, root) == 1

    def test_plan_children_are_pinned(self, tmp_path):
        """Manager 规划出的每个子节点都被压成同一个值。"""
        rt = _make_runtime(tmp_path, dynamic=False, default=1)
        root = rt.submit_root_manager("build app")
        nodes = rt.inject_plan(root, ExecutionPlan(steps=[
            _step("a", {"num_ccs": 3}),
            _step("b", {"num_ccs": 4}),
            _step("c", {}),
        ]))
        assert [n.config["num_ccs"] for n in nodes] == [1, 1, 1]

    def test_uses_configured_default_not_hardcoded_one(self, tmp_path):
        """取的是配置项本身：默认 4 时，plan 说 2 也变 4。"""
        rt = _make_runtime(tmp_path, dynamic=False, default=4)
        root = rt.submit_root_manager("build app", config={"num_ccs": 2})
        assert _num_ccs(rt, root) == 4
        nodes = rt.inject_plan(root, ExecutionPlan(steps=[_step("a", {"num_ccs": 2})]))
        assert nodes[0].config["num_ccs"] == 4

    def test_nested_child_nodes_are_pinned(self, tmp_path):
        """后代节点（子 Manager 再展开）同样被钉死。"""
        rt = _make_runtime(tmp_path, dynamic=False, default=1)
        root = rt.submit_root_manager("build app")
        subs = rt.inject_plan(root, ExecutionPlan(steps=[
            _step("impl", {"num_ccs": 3}, exec_type="manager"),
        ]))
        deep = rt.inject_plan(subs[0].id, ExecutionPlan(steps=[
            _step("deep", {"num_ccs": 4}),
        ]))
        assert subs[0].config["num_ccs"] == 1
        assert deep[0].config["num_ccs"] == 1


# ============================================================================
# 2. 开关开启 → 现状不变
# ============================================================================

class TestDynamicPolicy:

    def test_plan_num_ccs_preserved(self, tmp_path):
        rt = _make_runtime(tmp_path, dynamic=True, default=1)
        root = rt.submit_root_manager("build app", config={"num_ccs": 2})
        assert _num_ccs(rt, root) == 2
        nodes = rt.inject_plan(root, ExecutionPlan(steps=[
            _step("a", {"num_ccs": 3}),
            _step("b", {"num_ccs": 4}),
        ]))
        assert [n.config["num_ccs"] for n in nodes] == [3, 4]

    def test_absent_num_ccs_stays_absent(self, tmp_path):
        """开启时不得凭空写入 num_ccs（否则会顶掉角色 YAML 的默认值）。"""
        rt = _make_runtime(tmp_path, dynamic=True, default=1)
        root = rt.submit_root_manager("build app")
        assert _num_ccs(rt, root) is None
        nodes = rt.inject_plan(root, ExecutionPlan(steps=[_step("a", {})]))
        assert _num_ccs(rt, nodes[0].id) is None

    def test_constructor_defaults_are_backward_compatible(self):
        """Runtime() 不传参数 = 动态 + 默认 1，旧调用方行为不变。"""
        rt = Runtime()
        try:
            assert rt.dynamic_num_ccs is True
            assert rt.default_num_ccs == 1
        finally:
            rt.shutdown()


# ============================================================================
# 3. 取值 clamp（plan 校验之后注入，属校验盲区，必须自行兜底）
# ============================================================================

class TestClamp:

    @pytest.mark.parametrize("raw,expected", [
        (None, 1), (0, 1), (-3, 1), (1, 1), (2, 2), (4, 4), (5, 4), (99, 4),
        ("3", 3), ("abc", 1), (2.7, 2), (True, 1),
    ])
    def test_clamp(self, raw, expected):
        assert Runtime._clamp_num_ccs(raw) == expected

    def test_out_of_range_default_is_clamped(self, tmp_path):
        """全局配置层已校验，但构造参数也可能来自别的调用方，仍需兜底。"""
        rt = _make_runtime(tmp_path, dynamic=False, default=0)
        assert rt.default_num_ccs == 1
        root = rt.submit_root_manager("build app")
        assert _num_ccs(rt, root) == 1


# ============================================================================
# 4. 绕过路径：revise_manager_node / submit_fix_plan
# ============================================================================

class TestBypassPaths:
    """这两个方法直接写回 graph，不经过 _prepare_node_config。"""

    def test_revise_manager_node_respects_policy(self, tmp_path):
        rt = _make_runtime(tmp_path, dynamic=False, default=1)
        mgr_id = _completed_root(rt)
        assert _num_ccs(rt, mgr_id) == 1

        rt.revise_manager_node(mgr_id, "Add pagination", config={"num_ccs": 3})

        assert _num_ccs(rt, mgr_id) == 1

    def test_revise_manager_node_dynamic_keeps_requested_value(self, tmp_path):
        """对照：开关开启时 revise 传入的值应当生效。"""
        rt = _make_runtime(tmp_path, dynamic=True, default=1)
        mgr_id = _completed_root(rt)

        rt.revise_manager_node(mgr_id, "Add pagination", config={"num_ccs": 3})

        assert _num_ccs(rt, mgr_id) == 3

    def test_submit_fix_plan_respects_policy(self, tmp_path):
        rt = _make_runtime(tmp_path, dynamic=False, default=1)
        root = rt.submit_root_manager("build app")
        nodes = rt.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"output_path": "backend", "num_ccs": 3}),
        ]))
        old = nodes[0]
        rt.update_node_status(old.id, NodeStatus.COMPLETED, artifact_path="out.txt")

        fixed = rt.submit_fix_plan(
            root,
            [{"step_ref": "be", "fix": {
                "role": "default", "task": "rebuild", "config": {"num_ccs": 3},
            }}],
            1,
        )

        assert len(fixed) == 1
        assert fixed[0].id == old.id
        assert _num_ccs(rt, old.id) == 1

    def test_submit_fix_plan_dynamic_keeps_requested_value(self, tmp_path):
        """对照：开关开启时修复 step 的 num_ccs 应当生效。"""
        rt = _make_runtime(tmp_path, dynamic=True, default=1)
        root = rt.submit_root_manager("build app")
        nodes = rt.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"output_path": "backend", "num_ccs": 2}),
        ]))
        old = nodes[0]
        rt.update_node_status(old.id, NodeStatus.COMPLETED, artifact_path="out.txt")

        rt.submit_fix_plan(
            root,
            [{"step_ref": "be", "fix": {
                "role": "default", "task": "rebuild", "config": {"num_ccs": 3},
            }}],
            1,
        )

        assert _num_ccs(rt, old.id) == 3


# ============================================================================
# 5. 中途切换开关
# ============================================================================

class TestPolicySwitchMidSession:

    def test_switch_off_applies_to_subsequent_nodes(self, tmp_path):
        """已建节点不回改，此后新建的节点按新策略。"""
        rt = _make_runtime(tmp_path, dynamic=True, default=1)
        root = rt.submit_root_manager("build app", config={"num_ccs": 3})
        assert _num_ccs(rt, root) == 3

        # 模拟 SessionManager 的 live-push
        rt.dynamic_num_ccs = False
        rt.default_num_ccs = 1

        assert _num_ccs(rt, root) == 3, "已建节点不应被回溯修改"
        nodes = rt.inject_plan(root, ExecutionPlan(steps=[_step("a", {"num_ccs": 3})]))
        assert nodes[0].config["num_ccs"] == 1
