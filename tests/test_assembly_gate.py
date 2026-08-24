"""tests/test_assembly_gate.py — Phase 3 支柱 C：Assembly Gate（最终总装核对门）。

覆盖（V3-2 计划 test_assembly_gate.py 用例 + 补充）：
- verify_assembly 全过（无 blocking Issue → passed=True；manifest=scan(shared_root)）
- 产物缺失 → missing_artifact blocking
- 目录空 → empty_artifact blocking
- 输出碰撞 → output_collision blocking（共享根自身除外）
- 依赖不匹配 → dependency_mismatch blocking / 依赖满足不误报
- registry_drift → Observation 非 blocking，仍 PASS
- registry 双向漂移核对（快照多文件 / 快照少文件都报 drift）
- manifest=scan(shared_root) 而非 registry.entries()（Registry 缺失仍生成完整 manifest）
- Assembly 只读不写 Node（纯函数不改传入 children dict —— 无 runtime 通路）
- Manager 集成：Assembly blocking → WAITING + submit_fix_plan 被调 + delivery_state 不落 delivered
- Manager 集成：Assembly 通过 → COMPLETED + delivery_state=delivered + 权威 manifest 落盘
- verify_existence=False 字节级一致（test_closed_loop 宽松语义保留）
- Issue 类型映射 / DeliveryIssue 序列化
- compute_session_delivery_state 三态
"""

import copy
import json
import os

import pytest

from executables.base import ExecutableContext
from executables.manager import Manager
from runtime.closed_loop import build_deliverable_manifest
from runtime.delivery.assembly import (
    build_dir_index,
    compute_session_delivery_state,
    scan_shared_root,
    verify_assembly,
)
from runtime.delivery.contracts import ArtifactContract
from runtime.delivery.registry import FilesystemRegistry, build_artifact_entry
from runtime.models import Node, NodeStatus


# ============================================================================
# Helpers（沿用 test_artifact_gate / test_artifact_registry 模式）
# ============================================================================

def _node(node_id="n1", step_ref="step_1_v1", role="code",
          status=NodeStatus.COMPLETED) -> Node:
    return Node(
        id=node_id, exec_type="worker", role=role, task="t",
        config={}, status=status, step_ref=step_ref,
    )


def _make_manager(shared=None, iteration=0, max_iterations=3) -> Node:
    cfg = {"_shared_workspace": shared} if shared else {}
    return Node(
        id="mgr1", exec_type="manager", role="engineering_coord",
        task="Build app", config=cfg, status=NodeStatus.READY,
        manager_phase="review", iteration_count=iteration,
        max_iterations=max_iterations,
    )


def _make_child(step_ref, role="code", artifact_path=None,
                output_path=None, depends_on=None) -> Node:
    cfg: dict = {}
    if output_path:
        cfg["_output_path"] = output_path
    if depends_on:
        cfg["depends_on"] = depends_on
    return Node(
        id=f"node_{step_ref}", exec_type="worker", role=role,
        task=f"task {step_ref}", config=cfg, status=NodeStatus.COMPLETED,
        step_ref=step_ref, artifact_path=artifact_path,
    )


class _Gateway:
    """最小 RuntimeGateway 实现：记录状态更新 + submit_fix_plan 调用。"""

    def __init__(self, workspace: str):
        self._workspace = workspace
        self._nodes: dict[str, Node] = {}
        self._children: dict[str, list[Node]] = {}
        self.status_updates: list[tuple[str, NodeStatus, dict]] = []
        self.fix_plans: list[tuple[str, list, int]] = []

    def add_node(self, node: Node) -> None:
        self._nodes[node.id] = node

    def add_children(self, parent_id: str, children: list[Node]) -> None:
        self._children.setdefault(parent_id, []).extend(children)
        for c in children:
            self._nodes[c.id] = c

    def get_node_workspace(self, node_id: str) -> str:
        return os.path.join(self._workspace, "nodes", node_id)

    def get_node(self, node_id: str):
        return self._nodes.get(node_id)

    def get_children(self, node_id: str) -> list[Node]:
        return self._children.get(node_id, [])

    def get_dependencies(self, node_id: str) -> list[Node]:
        return []

    def read_artifact(self, node_id: str, path: str) -> str:
        return ""

    def update_node_status(self, node_id: str, status: NodeStatus, **updates) -> None:
        self.status_updates.append((node_id, status, updates))
        node = self._nodes.get(node_id)
        if node is not None:
            for k, v in updates.items():
                if hasattr(node, k):
                    setattr(node, k, v)
            node.status = status

    def submit_plan(self, node_id: str, plan) -> None:
        pass

    def submit_fix_plan(self, node_id: str, failed: list, iteration: int) -> list[Node]:
        self.fix_plans.append((node_id, failed, iteration))
        return []


class _PassStub:
    """Manager review 全通过 stub（AICU_DELIVERY=1 下 Assembly 决定最终交付）。"""

    def __init__(self, step_refs: list[str]):
        self._refs = list(step_refs)

    def ask(self, prompt: str) -> dict:
        return {
            "overall_passed": True,
            "assessments": [
                {"step_ref": r, "passed": True, "reason": "ok"} for r in self._refs
            ],
        }


def _write(ws: str, name: str, content: str = "x=1\n") -> None:
    os.makedirs(ws, exist_ok=True)
    with open(os.path.join(ws, name), "w", encoding="utf-8") as f:
        f.write(content)


# ============================================================================
# 纯 Assembly 测试
# ============================================================================

def test_verify_assembly_all_pass(tmp_path):
    shared = os.path.join(str(tmp_path), "shared")
    app_ws = os.path.join(shared, "app_ws")
    _write(app_ws, "main.py", "def main(): pass\n")

    children = [
        {"step_ref": "app_v1", "node_id": "n1", "role": "code",
         "artifact_path": app_ws, "config": {"_output_path": "app_ws"}},
    ]
    result = verify_assembly(children, shared)
    assert result.passed is True
    assert result.blocking_issues == []
    # manifest = scan(shared_root)，描述真实存在文件
    assert result.manifest["type"] == "delivery_manifest"
    assert result.manifest["shared_root"] == os.path.realpath(shared)
    assert result.manifest["summary"]["file_count"] == 1
    assert result.manifest["files"][0]["relpath"] == os.path.join("app_ws", "main.py")


def test_verify_assembly_dependency_satisfied_no_false_positive(tmp_path):
    shared = os.path.join(str(tmp_path), "shared")
    app_ws = os.path.join(shared, "app_ws")
    test_ws = os.path.join(shared, "test_ws")
    _write(app_ws, "main.py")
    _write(test_ws, "test.py")
    children = [
        {"step_ref": "app_v1", "node_id": "n1", "role": "code",
         "artifact_path": app_ws, "config": {"_output_path": "app_ws"}},
        {"step_ref": "test_v1", "node_id": "n2", "role": "test",
         "artifact_path": test_ws,
         "config": {"_output_path": "test_ws", "depends_on": ["app_v1"]}},
    ]
    result = verify_assembly(children, shared)
    assert result.passed is True
    assert not [i for i in result.blocking_issues if i.issue_type == "dependency_mismatch"]


def test_missing_artifact_blocking(tmp_path):
    shared = os.path.join(str(tmp_path), "shared")
    os.makedirs(shared)
    ghost = os.path.join(shared, "ghost_ws")
    children = [{
        "step_ref": "ghost_v1", "node_id": "n1", "role": "code",
        "artifact_path": ghost, "config": {"_output_path": "ghost_ws"},
    }]
    result = verify_assembly(children, shared)
    assert result.passed is False
    issue = result.blocking_issues[0]
    assert issue.issue_type == "missing_artifact"
    assert issue.severity == "blocking"
    assert issue.step_ref == "ghost_v1"


def test_empty_artifact_blocking(tmp_path):
    shared = os.path.join(str(tmp_path), "shared")
    empty_ws = os.path.join(shared, "empty_ws")
    os.makedirs(empty_ws)
    children = [{
        "step_ref": "empty_v1", "node_id": "n1", "role": "code",
        "artifact_path": empty_ws, "config": {"_output_path": "empty_ws"},
    }]
    result = verify_assembly(children, shared)
    assert result.passed is False
    assert result.blocking_issues[0].issue_type == "empty_artifact"


def test_output_collision_blocking(tmp_path):
    shared = os.path.join(str(tmp_path), "shared")
    _write(os.path.join(shared, "ws_a"), "a.py")
    _write(os.path.join(shared, "ws_b"), "b.py")
    children = [
        {"step_ref": "a_v1", "node_id": "n1", "role": "code",
         "artifact_path": os.path.join(shared, "ws_a"),
         "config": {"_output_path": "same_ws"}},
        {"step_ref": "b_v1", "node_id": "n2", "role": "code",
         "artifact_path": os.path.join(shared, "ws_b"),
         "config": {"_output_path": "same_ws"}},
    ]
    result = verify_assembly(children, shared)
    assert result.passed is False
    collisions = [i for i in result.blocking_issues if i.issue_type == "output_collision"]
    assert collisions, "两个子节点写同一 output_path 必须报 output_collision"
    assert "same_ws" in collisions[0].actual


def test_dependency_mismatch_blocking(tmp_path):
    shared = os.path.join(str(tmp_path), "shared")
    _write(os.path.join(shared, "app_ws"), "main.py")
    children = [{
        "step_ref": "app_v1", "node_id": "n1", "role": "code",
        "artifact_path": os.path.join(shared, "app_ws"),
        "config": {"_output_path": "app_ws", "depends_on": ["test_v1"]},
    }]
    result = verify_assembly(children, shared)
    assert result.passed is False
    dm = [i for i in result.blocking_issues if i.issue_type == "dependency_mismatch"]
    assert dm and dm[0].step_ref == "app_v1"


def test_registry_drift_observation_non_blocking(tmp_path):
    shared = os.path.join(str(tmp_path), "shared")
    ws = os.path.join(shared, "ws_a")
    _write(ws, "a.py", "v1\n")
    reg = FilesystemRegistry(shared)
    reg.write(build_artifact_entry(
        _node("n1", "step_1_v1", "code"), ws,
        ArtifactContract(artifact_type="code"), shared, "ws_a",
    ))
    # 兄弟节点改写共享文件 → Snapshot 过期（快照 1 文件，实际 2 文件）
    _write(ws, "b.py", "v2\n")
    children = [{
        "step_ref": "step_1_v1", "node_id": "n1", "role": "code",
        "artifact_path": ws, "config": {"_output_path": "ws_a"},
    }]
    result = verify_assembly(children, shared, registry=reg)
    assert result.passed is True, "registry_drift 是 Observation，不得阻断 Delivery"
    assert result.blocking_issues == []
    drifts = [o for o in result.observations if o.obs_type == "registry_drift"]
    assert len(drifts) == 1
    assert "step_1_v1" in drifts[0].detail


def test_registry_drift_bidirectional(tmp_path):
    """双向漂移核对：快照多文件（文件被删）与快照少文件（文件新增）都报 drift。"""
    shared = os.path.join(str(tmp_path), "shared")
    ws = os.path.join(shared, "ws_a")
    _write(ws, "a.py")
    _write(ws, "stale.py", "old\n")
    reg = FilesystemRegistry(shared)
    reg.write(build_artifact_entry(
        _node("n1", "step_1_v1", "code"), ws,
        ArtifactContract(artifact_type="code"), shared, "ws_a",
    ))
    children = [{
        "step_ref": "step_1_v1", "node_id": "n1", "role": "code",
        "artifact_path": ws, "config": {"_output_path": "ws_a"},
    }]
    # 删除 stale.py → filesystem 比 Snapshot 少文件（反向 drift）
    os.remove(os.path.join(ws, "stale.py"))
    result = verify_assembly(children, shared, registry=reg)
    assert [o for o in result.observations if o.obs_type == "registry_drift"]
    # 快照一致（重建条目后无 drift）
    reg.write(build_artifact_entry(
        _node("n1", "step_1_v1", "code"), ws,
        ArtifactContract(artifact_type="code"), shared, "ws_a",
    ))
    result2 = verify_assembly(children, shared, registry=reg)
    assert not [o for o in result2.observations if o.obs_type == "registry_drift"]


def test_manifest_from_scan_not_registry_entries(tmp_path):
    """manifest 内容 = scan(shared_root)，Registry 缺失仍生成完整 manifest。"""
    shared = os.path.join(str(tmp_path), "shared")
    _write(os.path.join(shared, "ws_a"), "a.py")
    children = [{
        "step_ref": "step_1_v1", "node_id": "n1", "role": "code",
        "artifact_path": os.path.join(shared, "ws_a"),
        "config": {"_output_path": "ws_a"},
    }]
    # Registry 完全没有条目
    result = verify_assembly(children, shared, registry=FilesystemRegistry(shared))
    assert result.passed is True
    assert result.manifest["files"], "Registry 缺失仍应生成完整 manifest（来自 scan）"
    assert any(f["relpath"] == os.path.join("ws_a", "a.py") for f in result.manifest["files"])
    assert not os.path.exists(os.path.join(shared, ".artifacts")), \
        "manifest 不是 registry.entries()：registry 为空但 manifest 完整"

    # Registry 有条目 → 仅补 metadata（deliverables），files 仍来自 filesystem 扫描
    reg = FilesystemRegistry(shared)
    reg.write(build_artifact_entry(
        _node("n1", "step_1_v1", "code"), os.path.join(shared, "ws_a"),
        ArtifactContract(artifact_type="code"), shared, "ws_a",
    ))
    result2 = verify_assembly(children, shared, registry=reg)
    assert result2.manifest["files"] == result.manifest["files"]
    metas = result2.manifest.get("deliverables", [])
    assert metas and metas[0]["step_ref"] == "step_1_v1"
    assert metas[0]["role"] == "code"
    assert metas[0]["artifact_type"] == "code"


def test_assembly_read_only_no_node_mutation(tmp_path):
    """Assembly 只读不写 Node：纯函数，无 update_node_status 通路，且不改传入 children。"""
    shared = os.path.join(str(tmp_path), "shared")
    _write(os.path.join(shared, "ws_a"), "a.py")
    children = [{
        "step_ref": "step_1_v1", "node_id": "n1", "role": "code",
        "artifact_path": os.path.join(shared, "ws_a"),
        "config": {"_output_path": "ws_a", "depends_on": ["ghost_v1"]},
    }]
    snapshot = copy.deepcopy(children)
    verify_assembly(children, shared)
    assert children == snapshot, "Assembly 只读：不得 mutate 传入 children（Node metadata 只读）"


def test_issue_type_mapping_and_serialization(tmp_path):
    shared = os.path.join(str(tmp_path), "shared")
    os.makedirs(shared)
    _write(os.path.join(shared, "ws_a"), "a.py")
    children = [
        {"step_ref": "ghost_v1", "node_id": "n1", "role": "code",
         "artifact_path": os.path.join(shared, "ghost_ws"),
         "config": {"_output_path": "ghost_ws"}},
        {"step_ref": "a_v1", "node_id": "n2", "role": "code",
         "artifact_path": os.path.join(shared, "ws_a"),
         "config": {"_output_path": "same_ws"}},
        {"step_ref": "b_v1", "node_id": "n3", "role": "code",
         "artifact_path": os.path.join(shared, "ws_a"),
         "config": {"_output_path": "same_ws", "depends_on": ["ghost_v1"]}},
    ]
    result = verify_assembly(children, shared)
    types = {i.issue_type for i in result.blocking_issues}
    assert {"missing_artifact", "output_collision", "dependency_mismatch"} <= types
    for issue in result.blocking_issues:
        d = issue.to_dict()
        assert d["severity"] == "blocking"
        assert all(
            k in d for k in ("issue_type", "node_id", "step_ref",
                             "expected", "actual", "reason")
        )


# ============================================================================
# build_deliverable_manifest 兼容：verify_existence=False 字节级一致
# ============================================================================

def test_verify_existence_false_byte_identical():
    mgr = _make_manager()
    c1 = _make_child("c1")
    c1.artifact_path = "/definitely/not/exists/c1.txt"  # 真实不存在
    # 默认 verify_existence=False：宽松语义（test_closed_loop 行为）→ complete 仅判非空
    loose = build_deliverable_manifest(mgr, [c1])
    assert loose["deliverables"][0]["complete"] is True
    assert loose["summary"]["delivery_complete"] is True
    # verify_existence=True：判真实存在 → incomplete
    strict = build_deliverable_manifest(mgr, [c1], verify_existence=True)
    assert strict["deliverables"][0]["complete"] is False
    assert strict["summary"]["delivery_complete"] is False


# ============================================================================
# compute_session_delivery_state 三态
# ============================================================================

def test_compute_session_delivery_state():
    # delivered：root COMPLETED + 交付链全部 delivered
    assert compute_session_delivery_state(
        "COMPLETED", ["delivered", "delivered"], False) == "delivered"
    # pending：root 未完成
    assert compute_session_delivery_state("RUNNING", ["delivered"], False) == "pending"
    # pending：root COMPLETED 但交付链未全部 delivered
    assert compute_session_delivery_state(
        "COMPLETED", ["delivered", "validated"], False) == "pending"
    # pending：无交付链（无交付证据）
    assert compute_session_delivery_state("COMPLETED", [], False) == "pending"
    # invalid：存在 blocking Issue（Gate FAIL / Assembly BLOCK），任何状态都优先
    assert compute_session_delivery_state("COMPLETED", ["delivered"], True) == "invalid"
    assert compute_session_delivery_state("FAILED", ["delivered"], True) == "invalid"
    # NodeStatus 枚举兼容
    assert compute_session_delivery_state(
        NodeStatus.COMPLETED, ["delivered"], False) == "delivered"


# ============================================================================
# build_dir_index（从 manager 下沉到 runtime/delivery/assembly，行为不变）
# ============================================================================

def test_build_dir_index_moved():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        _write(d, "a.py")
        idx = build_dir_index(d)
        assert "- a.py" in idx
    assert build_dir_index("/definitely/not/a/dir") == "(not a directory: /definitely/not/a/dir)"


# ============================================================================
# Manager 集成：Assembly Gate 插入 review 全通过分支
# ============================================================================

def test_integration_assembly_blocking_repair(tmp_path, monkeypatch):
    """Assembly blocking → WAITING + submit_fix_plan 被调 + delivery_state 不落 delivered。"""
    monkeypatch.setenv("AICU_DELIVERY", "1")
    shared = os.path.join(str(tmp_path), "shared")
    os.makedirs(shared)
    node = _make_manager(shared=shared, iteration=0, max_iterations=3)
    gw = _Gateway(str(tmp_path))
    gw.add_node(node)
    # LLM review 全通过，但产物真实缺失 → Assembly missing_artifact blocking
    ghost = os.path.join(shared, "ghost_ws")
    c1 = _make_child("c1", role="code", artifact_path=ghost, output_path="ghost_ws")
    gw.add_children(node.id, [c1])
    ctx = ExecutableContext(node=node, runtime=gw,
                            config={"workspace_dir": str(tmp_path), "mode": "stub"})
    mgr = Manager(context=ctx, stub=_PassStub(["c1"]))
    result = mgr.run(ctx)
    assert result == NodeStatus.WAITING
    assert gw.fix_plans, "Assembly blocking 必须驱动 submit_fix_plan"
    assert gw.fix_plans[0][1][0]["step_ref"] == "c1"
    assert gw.fix_plans[0][1][0]["fix"]["role"] == "code"
    assert "[Assembly]" in gw.fix_plans[0][1][0]["fix"]["task"]
    # delivery_state 不落 delivered
    assert not any(
        u.get("delivery_state") == "delivered"
        for _, _, u in gw.status_updates
    )


def test_integration_assembly_pass_delivers(tmp_path, monkeypatch):
    """Assembly 通过 → COMPLETED + delivery_state=delivered + 权威 manifest 落盘。"""
    monkeypatch.setenv("AICU_DELIVERY", "1")
    shared = os.path.join(str(tmp_path), "shared")
    ws = os.path.join(shared, "app_ws")
    _write(ws, "main.py", "def main(): pass\n")
    node = _make_manager(shared=shared)
    gw = _Gateway(str(tmp_path))
    gw.add_node(node)
    c1 = _make_child("c1", role="code", artifact_path=ws, output_path="app_ws")
    gw.add_children(node.id, [c1])
    ctx = ExecutableContext(node=node, runtime=gw,
                            config={"workspace_dir": str(tmp_path), "mode": "stub"})
    mgr = Manager(context=ctx, stub=_PassStub(["c1"]))
    result = mgr.run(ctx)
    assert result == NodeStatus.COMPLETED
    completed = [u for _, s, u in gw.status_updates if s == NodeStatus.COMPLETED]
    assert completed and completed[-1].get("delivery_state") == "delivered"
    # 权威清单 = scan(shared_root)，真实存在语义
    manifest_path = os.path.join(
        str(tmp_path), "nodes", node.id, "output", "deliverable_manifest.json")
    assert os.path.exists(manifest_path)
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["type"] == "delivery_manifest"
    assert data["summary"]["file_count"] >= 1
    assert any(f["relpath"].endswith("main.py") for f in data["files"])


def test_integration_assembly_delivery_off_unchanged(tmp_path):
    """Delivery flag off → 无 Assembly，delivery_state 不写入，行为字节级不变。"""
    node = _make_manager()
    gw = _Gateway(str(tmp_path))
    gw.add_node(node)
    c1 = _make_child("c1")
    c1.artifact_path = "output/c1.txt"  # 相对路径（宽松语义，不判真实存在）
    gw.add_children(node.id, [c1])
    ctx = ExecutableContext(node=node, runtime=gw,
                            config={"workspace_dir": str(tmp_path), "mode": "stub"})
    mgr = Manager(context=ctx, stub=_PassStub(["c1"]))
    result = mgr.run(ctx)
    assert result == NodeStatus.COMPLETED
    # 无 delivery_state 注入
    assert not any("delivery_state" in u for _, _, u in gw.status_updates)
    # 落盘的是旧版 per-child manifest（summary.complete 结构）
    manifest_path = os.path.join(
        str(tmp_path), "nodes", node.id, "output", "deliverable_manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["summary"]["delivery_complete"] is True
