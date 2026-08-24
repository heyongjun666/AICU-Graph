"""tests/test_artifact_gate.py — Phase 1 支柱 A：Artifact Gate 确定性产物校验门。

覆盖（V3-2 计划 test_artifact_gate.py 用例 + 补充）：
- 纯 check_gate：契约驱动通过（source .py / test verdict_block）
- 门失败：路径不存在 → MISSING_ARTIFACT；绝对路径已存在但越界仍 FAIL（containment）；
  目录空 → EMPTY_ARTIFACT；缺 verdict → VERDICT_MISSING
- check_order 断言：越界路径报「不属于 Session」而非「不存在」（containment 先于 exists）
- GateResult 结构（passed / artifact_path / validated_files / errors）
- parse_contract / resolve_required_types / extract_verdict 纯函数
- Worker 集成（AICU_DELIVERY=1）：合法产物 → COMPLETED + delivery_state=validated；
  缺失路径 → FAILED + delivery_state=invalid + error.cause 含 MISSING_ARTIFACT；
  越界+缺失 → FAILED + OUTSIDE_ALLOWED_ROOT（containment 先于 exists）
- flag 门控：AICU_DELIVERY 未设 → 字节级无 delivery_state；
  AICU_ARTIFACT_GATE=0 → 缺失路径仍 COMPLETED（Gate 真关闭）
- Scheduler 集成：Gate 失败原地 auto-retry 至 max_iterations，
  delivery_state="invalid" 跨重试保持
"""

import json
import os
import shutil
import tempfile
import time
from uuid import uuid4

import pytest

from executables.base import ExecutableContext
from executables.registry import ExecutableRegistry
from executables.worker import Worker
from runtime.delivery.contracts import (
    DEFAULT_VALIDATORS,
    ArtifactContract,
    parse_contract,
    resolve_required_types,
)
from runtime.delivery.gate import (
    ArtifactRoots,
    GateResult,
    check_gate,
    extract_verdict,
)
from runtime.graph import Graph
from runtime.models import Node, NodeStatus
from runtime.planner import Planner


# ============================================================================
# Stub AICU + Runtime mock（沿用 test_worker_retry 模式）
# ============================================================================

class _GateStubAICU:
    """可配置 final_output_path 的桩：path_builder 返回每次 ask 的产物路径。"""

    def __init__(self, path_builder):
        self.path_builder = path_builder
        self.call_count = 0

    def ask(self, question: str, max_rounds: int = 3) -> dict:
        self.call_count += 1
        return {"final_output_path": self.path_builder()}


class _TmpRuntime:
    """指向真实临时 workspace 的 runtime mock（文件需落盘供 Gate 扫描）。"""

    def __init__(self, workspace: str):
        self.workspace = workspace
        self.status_updates = []

    def get_node_workspace(self, node_id: str) -> str:
        return self.workspace

    def get_dependencies(self, node_id: str) -> list:
        return []

    def get_node(self, node_id: str):
        return None

    def update_node_status(self, node_id: str, status: NodeStatus, **updates) -> None:
        self.status_updates.append((node_id, status, updates))


def _make_node(role: str = "code", config: dict | None = None, **kw) -> Node:
    return Node(
        id="gate-node",
        exec_type="worker",
        role=role,
        task="test gate",
        config=config or {},
        status=NodeStatus.READY,
        **kw,
    )


def _make_context(runtime, node):
    return ExecutableContext(node=node, runtime=runtime, config={})


def _run_worker(workspace: str, stub, node):
    runtime = _TmpRuntime(workspace)
    ctx = _make_context(runtime, node)
    worker = Worker(ctx, mode="stub", stub=stub)
    result = worker.run(ctx)
    return result, runtime


def _write_output(workspace: str, files: dict) -> str:
    """在 workspace/output/ 下写文件 dict（name → content），返回 output 目录。"""
    out = os.path.join(workspace, "output")
    os.makedirs(out, exist_ok=True)
    for name, content in files.items():
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            f.write(content)
    return out


VERDICT_REPORT = (
    "# 测试报告\n\n"
    '<!-- VERDICT {"passed": true, "defects": []} -->\n'
)


def _root(shared=None, node=None):
    return ArtifactRoots(shared_root=shared, node_root=node)


# ============================================================================
# 纯 check_gate 测试
# ============================================================================

def test_gate_code_pass(tmp_path):
    out = os.path.join(str(tmp_path), "out")
    os.makedirs(out)
    with open(os.path.join(out, "main.py"), "w", encoding="utf-8") as f:
        f.write("def main():\n    return 1\n")
    contract = ArtifactContract(
        artifact_type="code", validators=["exists", "non_empty", "source_file"]
    )
    result = check_gate(out, _root(node=str(tmp_path)), contract)
    assert result.passed
    assert result.errors == []
    assert result.validated_files and os.path.basename(result.validated_files[0]) == "main.py"


def test_gate_document_verdict_pass(tmp_path):
    out = _write_output(str(tmp_path), {"report.md": VERDICT_REPORT})
    contract = ArtifactContract(
        artifact_type="document",
        validators=["exists", "non_empty", "verdict_block"],
    )
    result = check_gate(out, _root(node=str(tmp_path)), contract)
    assert result.passed
    assert any(os.path.basename(f) == "report.md" for f in result.validated_files)


def test_gate_existing_outside_root_fails_containment(tmp_path):
    """绝对路径已存在但属于 Session 允许空间之外 → 仍 FAIL（containment 不依赖 exists）。"""
    outside = tempfile.mkdtemp(prefix="aicu_outside_")
    try:
        f = os.path.join(outside, "foo.py")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        result = check_gate(
            f, _root(node=str(tmp_path)),
            ArtifactContract(artifact_type="code"),
        )
        assert not result.passed
        assert any("OUTSIDE_ALLOWED_ROOT" in e for e in result.errors)
        assert not any("MISSING_ARTIFACT" in e for e in result.errors)
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_gate_check_order_containment_before_exists(tmp_path):
    """越界 + 不存在的路径 → 报「不属于 Session」而非「不存在」（containment 先于 exists）。"""
    ghost = os.path.join(
        tempfile.gettempdir(), f"aicu_ghost_{uuid4().hex[:8]}"
    )
    assert not os.path.exists(ghost), "ghost path 不应存在"
    result = check_gate(ghost, _root(node=str(tmp_path)), ArtifactContract())
    assert not result.passed
    assert any("OUTSIDE_ALLOWED_ROOT" in e for e in result.errors), \
        "containment 必须先于 exists：越界路径必须报 OUTSIDE_ALLOWED_ROOT"
    assert not any("MISSING_ARTIFACT" in e for e in result.errors), \
        "越界路径不得被误报为 MISSING_ARTIFACT"


def test_gate_missing_inside_roots_fails(tmp_path):
    missing = os.path.join(str(tmp_path), "no_such_artifact")
    result = check_gate(missing, _root(node=str(tmp_path)), ArtifactContract())
    assert not result.passed
    assert any("MISSING_ARTIFACT" in e for e in result.errors)


def test_gate_empty_dir_fails(tmp_path):
    empty = os.path.join(str(tmp_path), "empty_out")
    os.makedirs(empty)
    result = check_gate(empty, _root(node=str(tmp_path)), ArtifactContract())
    assert not result.passed
    assert any("EMPTY_ARTIFACT" in e for e in result.errors)


def test_gate_verdict_missing_fails(tmp_path):
    out = _write_output(str(tmp_path), {"report.md": "# 无判定块的报告\n"})
    contract = ArtifactContract(
        artifact_type="document",
        validators=["exists", "non_empty", "verdict_block"],
    )
    result = check_gate(out, _root(node=str(tmp_path)), contract)
    assert not result.passed
    assert any("VERDICT_MISSING" in e for e in result.errors)


def test_gate_result_structure(tmp_path):
    out = os.path.join(str(tmp_path), "out")
    os.makedirs(out)
    with open(os.path.join(out, "a.py"), "w", encoding="utf-8") as f:
        f.write("pass\n")
    result = check_gate(
        out, _root(node=str(tmp_path)),
        ArtifactContract(artifact_type="code"),
    )
    assert isinstance(result, GateResult)
    assert result.passed is True
    assert result.artifact_path == out
    assert isinstance(result.validated_files, list)
    assert isinstance(result.errors, list)
    # 失败结果结构同样完整
    fail = check_gate(
        os.path.join(str(tmp_path), "missing"), _root(node=str(tmp_path)),
        ArtifactContract(),
    )
    assert fail.passed is False
    assert fail.artifact_path == os.path.join(str(tmp_path), "missing")
    assert fail.validated_files == []
    assert fail.errors


# ============================================================================
# 契约解析 / verdict 提取纯函数
# ============================================================================

def test_parse_contract_defaults_and_shapes():
    # None → 默认契约
    c = parse_contract(None)
    assert c.artifact_type == "generic"
    assert c.validators == list(DEFAULT_VALIDATORS)
    # str 简写 → artifact_type
    c = parse_contract("code")
    assert c.artifact_type == "code"
    # dict 完整形态
    c = parse_contract({
        "artifact_type": "document",
        "validators": ["exists", "non_empty", "verdict_block"],
    })
    assert c.artifact_type == "document"
    assert c.validators == ["exists", "non_empty", "verdict_block"]
    # 未知 validator 忽略、去重、空回退默认
    c = parse_contract({"validators": ["exists", "bogus", "exists", ""]})
    assert c.validators == ["exists"]
    c = parse_contract({"validators": []})
    assert c.validators == list(DEFAULT_VALIDATORS)
    # 非 dict/str → 默认
    assert parse_contract(42).validators == list(DEFAULT_VALIDATORS)


def test_resolve_required_types():
    types = resolve_required_types(ArtifactContract(artifact_type="code"))
    assert ".py" in types and ".js" in types
    assert resolve_required_types(ArtifactContract(artifact_type="generic")) == []
    assert resolve_required_types(
        ArtifactContract(artifact_type="code", required_types=[".py"])
    ) == [".py"]


def test_extract_verdict(tmp_path):
    f = tmp_path / "r.md"
    f.write_text(
        '# x\n<!-- VERDICT {"passed": false, "defects": [{"severity": "high"}]} -->\n'
    )
    verdict = extract_verdict(str(f))
    assert verdict is not None
    assert verdict["passed"] is False
    assert verdict["defects"][0]["severity"] == "high"
    # 无块
    f.write_text("# no verdict\n")
    assert extract_verdict(str(f)) is None
    # 块存在但 JSON 非法
    f.write_text("<!-- VERDICT {not-json} -->\n")
    assert extract_verdict(str(f)) is None
    # 文件不存在
    assert extract_verdict(str(tmp_path / "ghost.md")) is None


# ============================================================================
# Worker 集成（AICU_DELIVERY=1）
# ============================================================================

def test_worker_gate_valid_artifact_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("AICU_DELIVERY", "1")
    ws = str(tmp_path)
    stub = _GateStubAICU(path_builder=lambda: _write_output(ws, {"main.py": "x = 1\n"}))
    node = _make_node(role="code")  # code + [exists, non_empty, source_file]
    result, runtime = _run_worker(ws, stub, node)
    assert result == NodeStatus.COMPLETED
    _, _, updates = runtime.status_updates[-1]
    assert updates.get("delivery_state") == "validated"


def test_worker_gate_document_verdict_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("AICU_DELIVERY", "1")
    ws = str(tmp_path)
    stub = _GateStubAICU(path_builder=lambda: _write_output(ws, {"report.md": VERDICT_REPORT}))
    node = _make_node(role="test")  # document + verdict_block
    result, runtime = _run_worker(ws, stub, node)
    assert result == NodeStatus.COMPLETED
    _, _, updates = runtime.status_updates[-1]
    assert updates.get("delivery_state") == "validated"


def test_worker_gate_missing_path_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("AICU_DELIVERY", "1")
    ws = str(tmp_path)
    missing = os.path.join(ws, "output", "ghost")
    stub = _GateStubAICU(path_builder=lambda: missing)
    node = _make_node(role="dev")  # 角色不存在 → 默认契约；exists 恒被检查
    result, runtime = _run_worker(ws, stub, node)
    assert result == NodeStatus.FAILED
    _, _, updates = runtime.status_updates[-1]
    assert updates.get("delivery_state") == "invalid"
    err = updates.get("error")
    assert err is not None
    assert "MISSING_ARTIFACT" in err.cause


def test_worker_gate_outside_missing_path_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("AICU_DELIVERY", "1")
    ws = str(tmp_path)
    outside = os.path.join(
        tempfile.gettempdir(), f"aicu_outside_missing_{uuid4().hex[:8]}"
    )
    stub = _GateStubAICU(path_builder=lambda: outside)
    node = _make_node(role="dev")
    result, runtime = _run_worker(ws, stub, node)
    assert result == NodeStatus.FAILED
    _, _, updates = runtime.status_updates[-1]
    assert updates.get("delivery_state") == "invalid"
    err = updates.get("error")
    assert err is not None
    assert "OUTSIDE_ALLOWED_ROOT" in err.cause
    assert "MISSING_ARTIFACT" not in err.cause, \
        "越界路径必须报「不属于 Session」而非「不存在」"


# ============================================================================
# flag 门控
# ============================================================================

def test_worker_gate_flag_off_noop(tmp_path, monkeypatch):
    """AICU_DELIVERY 未设（默认关）→ 字节级：COMPLETED 无 delivery_state。"""
    monkeypatch.delenv("AICU_DELIVERY", raising=False)
    monkeypatch.delenv("AICU_ARTIFACT_GATE", raising=False)
    ws = str(tmp_path)
    stub = _GateStubAICU(path_builder=lambda: _write_output(ws, {"main.py": "x = 1\n"}))
    node = _make_node(role="code")
    result, runtime = _run_worker(ws, stub, node)
    assert result == NodeStatus.COMPLETED
    _, _, updates = runtime.status_updates[-1]
    assert updates.get("delivery_state") is None
    assert updates.get("error") is None


def test_worker_gate_subflag_off_noop(tmp_path, monkeypatch):
    """AICU_DELIVERY=1 但 AICU_ARTIFACT_GATE=0 → Gate 真关闭：缺失路径仍 COMPLETED。"""
    monkeypatch.setenv("AICU_DELIVERY", "1")
    monkeypatch.setenv("AICU_ARTIFACT_GATE", "0")
    ws = str(tmp_path)
    missing = os.path.join(ws, "output", "ghost")
    stub = _GateStubAICU(path_builder=lambda: missing)
    node = _make_node(role="code")
    result, runtime = _run_worker(ws, stub, node)
    assert result == NodeStatus.COMPLETED, "AICU_ARTIFACT_GATE=0 应完全关闭 Gate"
    _, _, updates = runtime.status_updates[-1]
    assert updates.get("delivery_state") is None


# ============================================================================
# Scheduler 集成：Gate 失败 → 原地 auto-retry 至 max_iterations
# ============================================================================

class _MissingPathStub:
    """始终声明不存在的产物路径 → 稳定触发 Gate 失败。"""

    def __init__(self, workspace: str):
        self.workspace = workspace
        self.call_count = 0

    def ask(self, question: str, max_rounds: int = 3) -> dict:
        self.call_count += 1
        return {"final_output_path": os.path.join(self.workspace, "output", "ghost")}


class _GateFailWorker(Worker):
    """始终触发 Gate 失败（缺失产物）的 Worker 子类。"""

    def __init__(self, context, **kwargs):
        ws = context.runtime.get_node_workspace(context.node.id)
        super().__init__(context, mode="stub", stub=_MissingPathStub(ws))


class _MockDelegate:
    def __init__(self):
        self.status_changes = []
        self.completed_managers = []
        self._active_count = 0

    def on_manager_children_completed(self, manager_node_id: str) -> None:
        self.completed_managers.append(manager_node_id)

    def on_node_status_changed(self, node_id: str, status: NodeStatus) -> None:
        self.status_changes.append((node_id, status))

    def get_active_execution_count(self) -> int:
        return self._active_count


class _GraphRuntime:
    """Scheduler 测试用的 runtime gateway：记录 update_node_status + 提供 workspace。"""

    def __init__(self, graph: Graph, workspace: str):
        self.graph = graph
        self.workspace = workspace
        self.status_updates = []

    def get_node_workspace(self, node_id: str) -> str:
        return self.workspace

    def get_node(self, node_id: str):
        return self.graph.get_node(node_id)

    def get_dependencies(self, node_id: str) -> list:
        return []

    def get_children(self, node_id: str) -> list:
        return []

    def submit_plan(self, node_id: str, plan) -> None:
        pass

    def update_node_status(self, node_id: str, status: NodeStatus, **updates) -> None:
        self.status_updates.append((node_id, status, dict(updates)))

    def read_artifact(self, node_id: str, path: str) -> str:
        return ""

    def register_external_input_wait(self, node_id: str, input_schema=None) -> str:
        return "token"

    def deliver_external_input(self, wait_token: str, input_data) -> None:
        pass


def wait_until(condition_fn, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition_fn():
            return True
        time.sleep(interval)
    return False


def test_gate_failure_auto_retry_exhausts_max_iterations(tmp_path, monkeypatch):
    """Gate 硬门失败 → 原地 auto-retry 至 max_iterations，delivery_state=invalid 跨重试保持。"""
    from runtime.scheduler import Scheduler

    monkeypatch.setenv("AICU_DELIVERY", "1")
    ws = str(tmp_path)

    graph = Graph()
    planner = Planner(graph)
    registry = ExecutableRegistry()
    registry.register("worker", _GateFailWorker)

    node = Node(
        id="G1", exec_type="worker", role="dev",
        task="gate failure", config={},
        status=NodeStatus.READY, max_iterations=2,
    )
    graph.add_node(node)

    runtime = _GraphRuntime(graph, ws)
    delegate = _MockDelegate()
    scheduler = Scheduler(
        graph=graph, registry=registry, delegate=delegate,
        runtime_gateway=runtime, max_concurrency=1,
        planner=planner, retry_delay=0.05,
    )
    scheduler.start()
    try:
        def exhausted():
            n = graph.get_node("G1")
            return (n is not None and n.status == NodeStatus.FAILED
                    and scheduler._retry_attempts.get("G1", 0) == 2)
        assert wait_until(exhausted, timeout=10), \
            "G1 应耗尽 max_iterations=2（首跑 + 2 次重试）后保持 FAILED"

        # 稳定后不再重试
        time.sleep(0.3)
        assert graph.get_node("G1").status == NodeStatus.FAILED
        assert len(graph.nodes) == 1, "原地重试不得创建新节点"

        # 首跑 + 2 次重试共 3 次 FAILED，每次回填 delivery_state=invalid（跨重试保持）
        failed_updates = [u for u in runtime.status_updates if u[1] == NodeStatus.FAILED]
        assert len(failed_updates) == 3, f"应恰好 3 次 FAILED，got {len(failed_updates)}"
        for _, _, updates in failed_updates:
            assert updates.get("delivery_state") == "invalid"
            assert updates.get("error") is not None
            assert "MISSING_ARTIFACT" in updates["error"].cause
    finally:
        scheduler.stop()
