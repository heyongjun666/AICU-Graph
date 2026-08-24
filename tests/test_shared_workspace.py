"""
tests/test_shared_workspace.py — Session 级单根共享工作区（Shared Workspace 扁平化 + 全节点自动挂载）

覆盖 V3.0 共享工作区（Session-level auto-mount）方案：
  1. _prepare_node_config 无条件 auto-mount：所有节点（含 root/后代）挂载同一会话根
     `{workspace_root}/shared`，`_output_path` 为分工分区
  2. output_path 防穿越（相对分区、禁绝对路径/..段）+ 组 ID 防御性校验
  3. 共享分区所有权提示（同一非空 output_path 被多节点声明仅告警不再硬拒，
     allow_shared_output 静默告警，空 output_path 是公共读区）
  4. submit_fix_plan 保留失败节点的共享配置
  5. update_node_status 共享根放行 + 绝对路径存储
  6. _normalize_worker_artifact 共享并入（幂等主路径 + 兜底拷贝）
  7. get_artifact 共享根读 + 越界拒绝
  8. _build_worker_context 共享块（会话级完全共享：互读互写，不加锁；空分区=全局写高权限）
  9. _build_tiered_reading_instructions（多依赖分级阅读）
  10. Manager snapshot 含兄弟 _shared_workspace 与协作区提示
  11. 跨层可见性 + 实时共享 + Runtime 不变量 + 兄弟真实协作 e2e
"""

import os
import tempfile

import pytest

from executables.base import Executable, ExecutableContext
from executables.manager import Manager
from executables.worker import Worker, _normalize_worker_artifact
from runtime.models import ExecutionPlan, NodeStatus, Step
from runtime.runtime import Runtime


class _TestExecutable(Executable):
    """Minimal executable for scheduler (auto-completes immediately)."""

    def __init__(self, context=None, **kwargs):
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


@pytest.fixture
def runtime(tmp_path):
    """Real Runtime with registered test executables and a temp workspace root."""
    rt = Runtime(workspace_root=str(tmp_path))
    rt.registry.register("manager", _TestExecutable)
    rt.registry.register("worker", _TestExecutable)
    return rt


def _step(step_id: str, config: dict, **kw) -> Step:
    kw.setdefault("exec_type", "worker")
    kw.setdefault("role", "default")
    kw.setdefault("task", f"task {step_id}")
    kw.setdefault("task_summary", f"summary {step_id}")
    kw.setdefault("depends_on", [])
    return Step(step_id=step_id, config=config, **kw)


def _session_shared_root(runtime) -> str:
    """会话级共享根：{workspace_root}/shared。"""
    return os.path.realpath(os.path.join(runtime.workspace_root, "shared"))


# ============================================================================
# 1. _prepare_node_config 无条件 auto-mount（所有节点挂载同一会话根）
# ============================================================================

class TestAutoMount:
    def test_all_nodes_auto_mount_session_root(self, runtime):
        """所有节点（即使不声明）都获得 `_shared_workspace`/`_output_path`。"""
        root = runtime.submit_root_manager("build app")
        shared_root = _session_shared_root(runtime)
        assert runtime.graph.get_node(root).config["_shared_workspace"] == shared_root
        assert runtime.graph.get_node(root).config["_output_path"] == ""
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {"output_path": "backend"}),
            _step("frontend", {"output_path": "frontend"}),
        ]))
        for n in nodes:
            assert n.config["_shared_workspace"] == shared_root
        assert nodes[0].config["_output_path"] == "backend"
        assert nodes[1].config["_output_path"] == "frontend"
        # 分区目录自动创建于会话根下（不再有 {父ws}/shared/{group} 嵌套）
        assert os.path.isdir(os.path.join(shared_root, "backend"))
        assert os.path.isdir(os.path.join(shared_root, "frontend"))

    def test_descendants_auto_mount_same_session_root(self, runtime):
        """后代节点同样自动挂载同一会话根（不再是"不递归继承"）。"""
        root = runtime.submit_root_manager("build app")
        shared_root = _session_shared_root(runtime)
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("impl", {}, exec_type="manager"),
        ]))
        impl = next(n for n in nodes if n.step_ref == "impl")
        deep_nodes = runtime.inject_plan(impl.id, ExecutionPlan(steps=[
            _step("deep_worker", {"output_path": "deep"}),
        ]))
        assert deep_nodes[0].config["_shared_workspace"] == shared_root
        assert deep_nodes[0].config["_output_path"] == "deep"

    def test_root_manager_auto_mounts(self, runtime):
        """root manager 自动挂载（不再 ValueError）；声明的组字段被接受但忽略。"""
        root = runtime.submit_root_manager(
            "app", config={"shared_workspace": "app"}
        )
        assert runtime.graph.get_node(root).config["_shared_workspace"] == \
            _session_shared_root(runtime)

    def test_legacy_declared_group_field_ignored_for_path(self, runtime):
        """旧 plan 声明的 shared_workspace 组值接受但不再参与路径派生。"""
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("w", {"shared_workspace": "legacy_group", "output_path": "p"}),
        ]))
        assert nodes[0].config["_shared_workspace"] == _session_shared_root(runtime)
        assert nodes[0].config["shared_workspace"] == "legacy_group"  # 保留字段
        assert nodes[0].config["_output_path"] == "p"


# ============================================================================
# 2. output_path / 组 ID 防穿越
# ============================================================================

class TestPathTraversal:
    @pytest.mark.parametrize("bad", [
        "../..",
        "../../etc",
        "/absolute",
        "a/b",
        "a\\b",
        ".",
        "..",
        "",
    ])
    def test_invalid_group_id_still_rejected_defensively(self, runtime, bad):
        """shared_workspace 组字段仍做防御性简单标识符校验（向后兼容）。"""
        root = runtime.submit_root_manager("app")
        with pytest.raises(ValueError):
            runtime.inject_plan(root, ExecutionPlan(steps=[
                _step("evil", {"shared_workspace": bad, "output_path": "p"}),
            ]))

    @pytest.mark.parametrize("good", ["backend", "backend/api", ""])
    def test_valid_output_path_accepted(self, runtime, good):
        """output_path 合法相对分区通过：单段 / 多段 / 空串。"""
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("ok", {"output_path": good}),
        ]))
        assert nodes[0].config["_output_path"] == good
        assert nodes[0].config["_shared_workspace"] == _session_shared_root(runtime)

    @pytest.mark.parametrize("bad", ["../secret", "/tmp/foo", "../../foo"])
    def test_traversal_output_path_rejected(self, runtime, bad):
        """output_path 路径穿越必须被拒绝（用户必加项）。"""
        root = runtime.submit_root_manager("app")
        with pytest.raises(ValueError):
            runtime.inject_plan(root, ExecutionPlan(steps=[
                _step("evil", {"output_path": bad}),
            ]))

    @pytest.mark.parametrize("bad", ["../x", "a/../../b", "/abs/path", "..\\win", "/tmp/x"])
    def test_invalid_output_path_rejected_with_legacy_group(self, runtime, bad):
        """带旧组字段时 output_path 穿越同样拒绝。"""
        root = runtime.submit_root_manager("app")
        with pytest.raises(ValueError):
            runtime.inject_plan(root, ExecutionPlan(steps=[
                _step("evil", {"shared_workspace": "app", "output_path": bad}),
            ]))


# ============================================================================
# 3. 全局分区所有权冲突检测
# ============================================================================

class TestOutputConflict:
    def test_same_partition_shared_allowed(self, runtime):
        """同一非空 output_path 被多个节点声明 → 允许（共享 FS 多写者，仅告警）。"""
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("a", {"output_path": "same"}),
            _step("b", {"output_path": "same"}),
        ]))
        assert len(nodes) == 2
        for n in nodes:
            assert n.config["_output_path"] == "same"

    def test_same_partition_different_legacy_groups_allowed(self, runtime):
        """不同 legacy 组值 + 相同非空 output_path → 允许（仅告警）。"""
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("a", {"shared_workspace": "g1", "output_path": "shared_code"}),
            _step("b", {"shared_workspace": "g2", "output_path": "shared_code"}),
        ]))
        assert len(nodes) == 2

    def test_empty_output_path_no_conflict(self, runtime):
        """空 output_path（共享根公共读区）多节点不冲突。"""
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("a", {"shared_workspace": "g1"}),
            _step("b", {"shared_workspace": "g2"}),
            _step("c", {"output_path": ""}),
        ]))
        assert len(nodes) == 3
        for n in nodes:
            assert n.config["_shared_workspace"] == _session_shared_root(runtime)

    def test_allow_shared_output_permits_same_partition(self, runtime):
        """allow_shared_output: true 允许共享同一分区所有权（非并发写锁）。"""
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("a", {"output_path": "same", "allow_shared_output": True}),
            _step("b", {"output_path": "same", "allow_shared_output": True}),
        ]))
        assert len(nodes) == 2
        assert nodes[0].config["_output_path"] == "same"

    def test_failed_validation_does_not_pollute_graph(self, runtime):
        """前置校验失败不得留下任何子节点（非法 output_path 仍硬拒）。"""
        root = runtime.submit_root_manager("app")
        with pytest.raises(ValueError):
            runtime.inject_plan(root, ExecutionPlan(steps=[
                _step("a", {"output_path": "/abs"}),
                _step("b", {"output_path": "same"}),
            ]))
        assert runtime.get_children(root) == []


# ============================================================================
# 4. submit_fix_plan 保留共享配置
# ============================================================================

class TestFixPlanPreservesSharedConfig:
    def test_continued_node_keeps_shared_config(self, runtime):
        """A1: 同角色失败子节点原地续接（不删除、不重建、保留共享配置与会话）。"""
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"shared_workspace": "app", "output_path": "backend"}),
        ]))
        old = nodes[0]
        runtime.update_node_status(old.id, NodeStatus.COMPLETED, artifact_path="out.txt")
        new_nodes = runtime.submit_fix_plan(
            root,
            [{"step_ref": "be", "fix": {"role": "default", "task": "rebuild"}}],
            1,
        )
        assert len(new_nodes) == 1
        new = new_nodes[0]
        # 原地续接：同一节点，不清除 _aicu_cache 会话
        assert new.id == old.id
        assert new.status == NodeStatus.READY
        assert new.task == "rebuild"
        assert new.pending_context["collaboration_type"] == "repair"
        assert new.pending_context["defects"][0]["fix_task"] == "rebuild"
        # 共享配置保留（旧组字段 + 分区；会话根 auto-mount 下新旧值一致）
        assert new.config["shared_workspace"] == "app"
        assert new.config["output_path"] == "backend"
        assert new.config["_output_path"] == "backend"
        assert new.config["_shared_workspace"] == old.config["_shared_workspace"]
        assert new.config["_shared_workspace"] == _session_shared_root(runtime)
        # 旧产出被清空，等待重新交付
        assert new.artifact_path is None
        assert new.error is None

    def test_role_change_continues_in_place(self, runtime):
        """A1: fix.role 与子节点角色不同（能力集不同）→ 原地续接（不删除节点）。

        新架构原则：清理子节点是严重操作，只有用户在前端手动操作；代码层面不存在
        任何删除子节点的机制。角色变化 = 新能力 → 原地更新 role/exec_type（并清
        _aicu_cache 开新会话），保留节点身份/工作区/历史，绝不删除重建。
        """
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"shared_workspace": "app", "output_path": "backend"},
                  role="dev_member"),
        ]))
        old = nodes[0]
        new_nodes = runtime.submit_fix_plan(
            root,
            [{"step_ref": "be", "fix": {"role": "test", "task": "verify"}}],
            1,
        )
        assert len(new_nodes) == 1
        new = new_nodes[0]
        # 原地续接：同一节点身份保留，role/exec_type 更新到修复角色
        assert new.id == old.id
        assert new.role == "test"
        assert new.task == "verify"
        assert new.status == NodeStatus.READY
        # 共享配置仍保留
        assert new.config["shared_workspace"] == "app"
        assert new.config["output_path"] == "backend"
        # 原节点未被删除（仍为 root 子节点）
        child_ids = {c.id for c in runtime.graph.children(root)}
        assert old.id in child_ids
        assert len(child_ids) == 1


# ============================================================================
# 5. update_node_status 共享根放行 + 绝对路径存储
# ============================================================================

class TestUpdateNodeStatusSharedRoot:
    def test_shared_root_artifact_stored_absolute(self, runtime):
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"output_path": "backend"}),
        ]))
        node = nodes[0]
        shared_file = os.path.join(
            node.config["_shared_workspace"], "backend", "api.yaml"
        )
        os.makedirs(os.path.dirname(shared_file), exist_ok=True)
        with open(shared_file, "w") as f:
            f.write("yaml")
        runtime.update_node_status(node.id, NodeStatus.COMPLETED, artifact_path=shared_file)
        assert os.path.isabs(node.artifact_path)
        assert node.artifact_path == os.path.realpath(shared_file)

    def test_shared_root_itself_allowed(self, runtime):
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"output_path": ""}),
        ]))
        node = nodes[0]
        shared_root = node.config["_shared_workspace"]
        runtime.update_node_status(node.id, NodeStatus.COMPLETED, artifact_path=shared_root)
        assert node.artifact_path == os.path.realpath(shared_root)

    def test_private_workspace_artifact_still_relative(self, runtime):
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"output_path": "backend"}),
        ]))
        node = nodes[0]
        ws = runtime.get_node_workspace(node.id)
        priv_file = os.path.join(ws, "inner.txt")
        os.makedirs(ws, exist_ok=True)
        with open(priv_file, "w") as f:
            f.write("x")
        runtime.update_node_status(node.id, NodeStatus.COMPLETED, artifact_path=priv_file)
        assert not os.path.isabs(node.artifact_path)
        assert node.artifact_path == os.path.relpath(priv_file, ws)

    def test_outside_both_roots_rejected(self, runtime):
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"output_path": "backend"}),
        ]))
        node = nodes[0]
        with pytest.raises(ValueError):
            runtime.update_node_status(
                node.id, NodeStatus.COMPLETED, artifact_path="/etc/passwd"
            )


# ============================================================================
# 6. _normalize_worker_artifact 共享并入
# ============================================================================

class TestNormalizeWorkerArtifactShared:
    def test_direct_write_idempotent(self, runtime):
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"output_path": "backend"}),
        ]))
        node = nodes[0]
        partition = os.path.join(node.config["_shared_workspace"], "backend")
        os.makedirs(partition, exist_ok=True)
        w = os.path.join(partition, "api.yaml")
        with open(w, "w") as f:
            f.write("yaml")
        ctx = ExecutableContext(node=node, runtime=runtime, config=node.config)
        assert _normalize_worker_artifact(ctx, w) == os.path.realpath(w)

    def test_external_artifact_returns_partition(self, runtime):
        """V3-2.1：分区外声明 → 产物权威 = 分区（Runtime 事实），不复制。

        原 test_dir_fallback_copied_into_partition（copy 兜底）已删除——
        Runtime 只证明事实，不修复事实；私有目录产物不再被复制进共享分区。
        """
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"output_path": "backend"}),
        ]))
        node = nodes[0]
        ws = runtime.get_node_workspace(node.id)
        priv = os.path.join(ws, "outdir")
        os.makedirs(os.path.join(priv, "sub"), exist_ok=True)
        with open(os.path.join(priv, "sub", "f.txt"), "w") as f:
            f.write("x")
        ctx = ExecutableContext(node=node, runtime=runtime, config=node.config)
        dest = _normalize_worker_artifact(ctx, priv)
        partition = os.path.join(node.config["_shared_workspace"], "backend")
        # 产物权威 = 分区本身（不复制外部私有目录内容）
        assert dest == os.path.realpath(partition)
        assert not os.path.exists(os.path.join(partition, "outdir", "sub", "f.txt"))
        # 私有源目录保持原样（Runtime 不移动/不删除事实）
        assert os.path.exists(os.path.join(priv, "sub", "f.txt"))

    def test_external_file_returns_partition_no_routing_copy(self, runtime):
        """V3-2.1：分区外单文件声明 → 返回分区，不复制；routing 由 CC 自写分区，不携带。

        原 test_file_fallback_copies_routing_json（copy + routing 携带兜底）已删除。
        """
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"output_path": "backend"}),
        ]))
        node = nodes[0]
        ws = runtime.get_node_workspace(node.id)
        pf = os.path.join(ws, "single.md")
        with open(pf, "w") as f:
            f.write("# md")
        os.makedirs(os.path.join(ws, "output"), exist_ok=True)
        with open(os.path.join(ws, "output", "routing.json"), "w") as f:
            f.write("{}")
        ctx = ExecutableContext(node=node, runtime=runtime, config=node.config)
        dest = _normalize_worker_artifact(ctx, pf)
        partition = os.path.join(node.config["_shared_workspace"], "backend")
        assert dest == os.path.realpath(partition)
        # 不复制产物、不携带 routing（routing 应写在分区内，由 _extract_routing 搜索命中）
        assert not os.path.exists(os.path.join(partition, "single.md"))
        assert not os.path.exists(os.path.join(partition, "routing.json"))

    def test_no_shared_config_uses_old_logic(self, runtime):
        root = runtime.submit_root_manager("app")
        n2 = runtime.create_manual_node(task="x", role="default", parent_id=root)
        # 模拟"无共享配置"节点（绕过 _prepare_node_config 的持久化/手工构造）：
        # 剥掉 auto-mount 注入的共享键后应回退到私有 workspace 旧逻辑。
        n2.config = dict(n2.config or {})
        n2.config.pop("_shared_workspace", None)
        n2.config.pop("_output_path", None)
        ws = runtime.get_node_workspace(n2.id)
        w = os.path.join(ws, "inner.txt")
        os.makedirs(ws, exist_ok=True)
        with open(w, "w") as f:
            f.write("in")
        ctx = ExecutableContext(node=n2, runtime=runtime, config=n2.config)
        assert _normalize_worker_artifact(ctx, w) == os.path.realpath(w)


# ============================================================================
# 7. get_artifact 共享根读
# ============================================================================

class TestGetArtifactSharedRoot:
    def test_shared_absolute_path_readable(self, runtime):
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"output_path": "backend"}),
        ]))
        node = nodes[0]
        shared_file = os.path.join(
            node.config["_shared_workspace"], "backend", "api.yaml"
        )
        os.makedirs(os.path.dirname(shared_file), exist_ok=True)
        with open(shared_file, "w") as f:
            f.write("yaml")
        assert runtime.get_artifact(node.id, shared_file) == "yaml"

    def test_outside_both_roots_rejected(self, runtime, tmp_path):
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"output_path": "backend"}),
        ]))
        node = nodes[0]
        escape = os.path.join(tmp_path, "..", "..", "etc", "passwd")
        with pytest.raises(ValueError):
            runtime.get_artifact(node.id, escape)


# ============================================================================
# 8. _build_worker_context 共享块
# ============================================================================

class TestWorkerContextSharedBlock:
    def test_shared_block_contains_partition_and_permissions(self, runtime):
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"output_path": "backend"}),
        ]))
        node = nodes[0]
        ctx = ExecutableContext(node=node, runtime=runtime, config=node.config)
        text = Worker._build_worker_context(ctx)
        partition = os.path.join(node.config["_shared_workspace"], "backend")
        assert "共享工作区" in text
        assert "Session Shared Workspace" in text
        assert partition in text
        # 会话级完全共享：默认互读互写、可互相修改兄弟分区
        assert "完全共享" in text
        assert "互相修改" in text
        assert "只读" not in text
        assert "write_access" not in text

    def test_nonempty_partition_block_wording(self, runtime):
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"output_path": "backend"}),
        ]))
        node = nodes[0]
        ctx = ExecutableContext(node=node, runtime=runtime, config=node.config)
        text = Worker._build_worker_context(ctx)
        assert "你主要负责写该分区" in text
        assert "会话级全量可读" in text

    def test_empty_partition_block_shows_global_write(self, runtime):
        """output_path="" → 共享块提示全局写高权限模式。"""
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("w", {}),
        ]))
        node = nodes[0]
        ctx = ExecutableContext(node=node, runtime=runtime, config=node.config)
        text = Worker._build_worker_context(ctx)
        assert "全局写权限" in text
        assert "高权限模式" in text

    def test_no_shared_config_no_shared_block(self, runtime):
        root = runtime.submit_root_manager("app")
        n2 = runtime.create_manual_node(task="x", role="default", parent_id=root)
        # 模拟"无共享配置"节点（绕过 _prepare_node_config 的持久化/手工构造）：
        # 剥掉 auto-mount 注入的共享键后，共享块守卫 `if not shared_root: return ""` 生效。
        n2.config = dict(n2.config or {})
        n2.config.pop("_shared_workspace", None)
        n2.config.pop("_output_path", None)
        ctx = ExecutableContext(node=n2, runtime=runtime, config=n2.config)
        text = Worker._build_worker_context(ctx)
        assert "共享工作区" not in text


# ============================================================================
# 9. _build_tiered_reading_instructions
# ============================================================================

class TestTieredReading:
    def _deps(self, runtime, n: int):
        """构造 n 个已完成依赖节点。"""
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step(f"dep{i}", {}) for i in range(n)
        ]))
        for nd in nodes:
            runtime.update_node_status(
                nd.id, NodeStatus.COMPLETED,
                artifact_path=os.path.join(runtime.get_node_workspace(nd.id), "out.txt"),
            )
        return root, nodes

    def test_tiered_when_many_deps(self, runtime):
        root, deps = self._deps(runtime, 5)
        text = Worker._build_tiered_reading_instructions(deps[:12])
        assert "分级读取" in text
        assert "第一级" in text and "第三级" in text

    def test_full_read_when_few_deps(self, runtime):
        # 三个 step 在同一 plan 内：dep0/dep1 与 consumer 由 depends_on 建立 DAG 边。
        root = runtime.submit_root_manager("app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("dep0", {}),
            _step("dep1", {}),
            _step("consumer", {}, depends_on=["dep0", "dep1"]),
        ]))
        dep0 = next(n for n in nodes if n.step_ref == "dep0")
        dep1 = next(n for n in nodes if n.step_ref == "dep1")
        for nd in (dep0, dep1):
            runtime.update_node_status(
                nd.id, NodeStatus.COMPLETED,
                artifact_path=os.path.join(runtime.get_node_workspace(nd.id), "out.txt"),
            )
        consumer = next(n for n in nodes if n.step_ref == "consumer")
        ctx = ExecutableContext(node=consumer, runtime=runtime, config=consumer.config)
        text = Worker._build_worker_context(ctx)
        assert "完整读取所有上游依赖产出" in text


# ============================================================================
# 10. Manager snapshot
# ============================================================================

class TestManagerSnapshotShared:
    def test_snapshot_shows_sibling_shared_workspace_fact(self, runtime):
        root = runtime.submit_root_manager("build a web app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("sub", {}, exec_type="manager"),
            _step("be", {"output_path": "backend"}),
        ]))
        sub = next(n for n in nodes if n.step_ref == "sub")
        sib = next(n for n in nodes if n.step_ref == "be")
        shared_file = os.path.join(
            sib.config["_shared_workspace"], "backend", "api.yaml"
        )
        os.makedirs(os.path.dirname(shared_file), exist_ok=True)
        with open(shared_file, "w") as f:
            f.write("yaml")
        runtime.update_node_status(sib.id, NodeStatus.COMPLETED, artifact_path=shared_file)

        mgr = Manager.__new__(Manager)
        ctx = ExecutableContext(node=sub, runtime=runtime, config=sub.config)
        snap = mgr._build_execution_context_snapshot(ctx)
        # 共享工作区协议/协作区路径已移入 yaml generate_instruction，快照只展示兄弟事实
        assert "共享工作区协议" not in snap
        assert "协作区位于" not in snap
        assert "📁 共享工作区: " in snap
        assert sib.config["_shared_workspace"] in snap
        assert "集成入口" not in snap


# ============================================================================
# 11. 跨层可见性 / 实时共享 / Runtime 不变量 / 兄弟真实协作
# ============================================================================

class TestCrossLayerVisibility:
    def test_deep_worker_to_downstream_direct_read(self, runtime):
        """核心验收：深层 worker 写 shared/backend/api.py → 验收节点从会话根直接读。"""
        root = runtime.submit_root_manager("build blog")
        shared_root = _session_shared_root(runtime)
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("impl", {}, exec_type="manager"),
            _step("acceptance", {"output_path": ""}),
        ]))
        impl = next(n for n in nodes if n.step_ref == "impl")
        acceptance = next(n for n in nodes if n.step_ref == "acceptance")
        deep = runtime.inject_plan(impl.id, ExecutionPlan(steps=[
            _step("backend", {"output_path": "backend"}),
        ]))[0]

        # 深层 worker 把产物发布到会话根 shared/backend/api.py
        api = os.path.join(shared_root, "backend", "api.py")
        os.makedirs(os.path.dirname(api), exist_ok=True)
        with open(api, "w") as f:
            f.write("def handler(): pass")
        runtime.update_node_status(deep.id, NodeStatus.COMPLETED, artifact_path=api)

        # 根层验收节点从会话根直接读到（无复制/上合）
        assert runtime.get_artifact(acceptance.id, api) == "def handler(): pass"

        # 无 nodes/{id}/shared/ 嵌套（节点私有区不含 shared/）
        nodes_root = os.path.join(runtime.workspace_root, "nodes")
        if os.path.isdir(nodes_root):
            for dirpath, dirnames, _ in os.walk(nodes_root):
                assert "shared" not in dirnames, (
                    f"nested shared dir found: {os.path.join(dirpath, 'shared')}"
                )

    def test_runtime_invariant_all_nodes_share_session_root(self, runtime):
        """Runtime 不变量：∀ node ∈ session: node._shared_workspace == session root。"""
        root = runtime.submit_root_manager("build app")
        shared_root = _session_shared_root(runtime)
        runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("impl", {}, exec_type="manager"),
            _step("a", {"output_path": "a"}),
            _step("b", {}),
        ]))
        impl = next(n for n in runtime.graph.all_nodes() if n.step_ref == "impl")
        runtime.inject_plan(impl.id, ExecutionPlan(steps=[
            _step("c", {"output_path": "c"}),
        ]))
        assert runtime.graph.get_node(root).config["_shared_workspace"] == shared_root
        for n in runtime.graph.all_nodes():
            assert n.config["_shared_workspace"] == shared_root

    def test_legacy_group_roots_do_not_escape_session(self, runtime):
        """旧组字段不再产生 {父ws}/shared/{group} 嵌套目录。"""
        root = runtime.submit_root_manager("build app")
        shared_root = _session_shared_root(runtime)
        runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {"shared_workspace": "app", "output_path": "backend"}),
        ]))
        assert os.path.isdir(shared_root)
        # 会话根下只有分区，没有 {ws}/shared/app 组嵌套
        assert not os.path.isdir(os.path.join(shared_root, "app"))


class TestRealTimeSharedUpdates:
    def test_read_after_update_sees_latest(self, runtime):
        """实时共享回归：Backend 写 v1 → Frontend 读 v1 → Backend 写 v2 → Frontend 读 v2。"""
        root = runtime.submit_root_manager("fullstack")
        shared_root = _session_shared_root(runtime)
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {"output_path": "backend"}),
            _step("frontend", {"output_path": "frontend"}),
        ]))
        backend = next(n for n in nodes if n.step_ref == "backend")
        frontend = next(n for n in nodes if n.step_ref == "frontend")
        api = os.path.join(shared_root, "backend", "api.py")
        os.makedirs(os.path.dirname(api), exist_ok=True)

        with open(api, "w") as f:
            f.write("V1")
        assert runtime.get_artifact(frontend.id, api) == "V1"

        # 同一实时 Workspace：第二次写必须是新内容（非初始快照拷贝）
        with open(api, "w") as f:
            f.write("V2")
        assert runtime.get_artifact(frontend.id, api) == "V2"


class TestSiblingSharedCollaboration:
    def test_two_siblings_collaborate_via_shared_root(self, runtime):
        root = runtime.submit_root_manager("build fullstack app")
        shared_root = _session_shared_root(runtime)
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("backend", {"output_path": "backend"}),
            _step("frontend", {"output_path": "frontend"}),
        ]))
        backend = next(n for n in nodes if n.step_ref == "backend")
        frontend = next(n for n in nodes if n.step_ref == "frontend")
        assert backend.config["_shared_workspace"] == shared_root
        assert frontend.config["_shared_workspace"] == shared_root

        # A（backend）写共享区 backend/api.yaml
        api = os.path.join(shared_root, "backend", "api.yaml")
        os.makedirs(os.path.dirname(api), exist_ok=True)
        with open(api, "w") as f:
            f.write("openapi: 3.0")
        runtime.update_node_status(backend.id, NodeStatus.COMPLETED, artifact_path=api)

        # B（frontend）读 A 的共享文件后写 frontend/client.ts
        assert runtime.get_artifact(frontend.id, api) == "openapi: 3.0"
        client = os.path.join(shared_root, "frontend", "client.ts")
        os.makedirs(os.path.dirname(client), exist_ok=True)
        with open(client, "w") as f:
            f.write("const api = 'https://x'")
        runtime.update_node_status(frontend.id, NodeStatus.COMPLETED, artifact_path=client)

        # 两分区并存，验证共享区解决兄弟协作
        assert os.path.exists(api)
        assert os.path.exists(client)
        assert os.path.isdir(os.path.join(shared_root, "backend"))
        assert os.path.isdir(os.path.join(shared_root, "frontend"))
        # Manager 侧（root）可读两者绝对路径产物
        assert runtime.get_artifact(root, api) == "openapi: 3.0"
        assert runtime.get_artifact(root, client) == "const api = 'https://x'"
