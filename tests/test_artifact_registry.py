"""tests/test_artifact_registry.py — Phase 2 支柱 B：Artifact Registry（非权威 Snapshot）。

覆盖（V3-2 计划 test_artifact_registry.py 用例 + 补充）：
- entry 构建（step_ref/node_id/role/artifact_type/files/key_files）
- 扫描排除框架目录（.codegraph/.git 不进入 files）
- 写读回环 + 原子写（临时文件 os.replace）
- resolve 四种：@{step_ref}、@{step_ref}/path、@role:name、未知 → None
- Worker 集成：COMPLETED 后注册表落盘（含 Gate 通过路径）
- 二次覆盖 stale（repair/resume 同名 step_ref 改写为新内容）
- 写失败 → worker 仍 COMPLETED 且 delivery_state 不污染
- flag off 无注册表（字节级）
- 依赖块注入对比（Delivery 开启用 Registry 解析路径，关闭回退 node.artifact_path）
- Backend 抽象可替换（Protocol：FilesystemRegistry 与 mock DB 走同一调用方）
"""

import os
import shutil

import pytest

from executables.base import ExecutableContext
from executables.worker import Worker, _resolve_dep_artifact_path
from runtime.delivery.contracts import ArtifactContract
from runtime.delivery.registry import (
    FilesystemRegistry,
    build_artifact_entry,
    key_files_for,
    scan_artifact_files,
)
from runtime.models import Node, NodeStatus


# ============================================================================
# Stub / mock（沿用 test_artifact_gate 模式）
# ============================================================================

class _TmpRuntime:
    """指向真实临时 workspace 的 runtime mock。"""

    def __init__(self, workspace: str, deps: list | None = None):
        self.workspace = workspace
        self.deps = deps or []
        self.status_updates = []

    def get_node_workspace(self, node_id: str) -> str:
        return self.workspace

    def get_dependencies(self, node_id: str) -> list:
        return self.deps

    def get_node(self, node_id: str):
        return None

    def update_node_status(self, node_id: str, status: NodeStatus, **updates) -> None:
        self.status_updates.append((node_id, status, updates))


class _SharedWriteStub:
    """把产物直写共享分区（{shared}/{output_path}）的桩。"""

    def __init__(self, shared: str, output_path: str, files: dict):
        self.shared = shared
        self.output_path = output_path
        self.files = files
        self.call_count = 0

    def ask(self, question: str, max_rounds: int = 3) -> dict:
        self.call_count += 1
        partition = os.path.join(self.shared, self.output_path) if self.output_path else self.shared
        os.makedirs(partition, exist_ok=True)
        for name, content in self.files.items():
            with open(os.path.join(partition, name), "w", encoding="utf-8") as f:
                f.write(content)
        return {"final_output_path": partition}


def _make_node(role="code", config=None, step_ref="step_1_v1", **kw) -> Node:
    return Node(
        id="reg-node",
        exec_type="worker",
        role=role,
        task="test registry",
        config=config or {},
        status=NodeStatus.READY,
        step_ref=step_ref,
        **kw,
    )


def _make_context(runtime, node):
    return ExecutableContext(node=node, runtime=runtime, config={})


def _run_worker(workspace, stub, node):
    runtime = _TmpRuntime(workspace)
    ctx = _make_context(runtime, node)
    worker = Worker(ctx, aicu_instance=stub)
    result = worker.run(ctx)
    return result, runtime


# ============================================================================
# 纯 Registry 测试
# ============================================================================

def test_build_entry_fields(tmp_path):
    shared = os.path.join(str(tmp_path), "shared")
    art = os.path.join(shared, "app_ws")
    os.makedirs(art)
    with open(os.path.join(art, "main.py"), "w", encoding="utf-8") as f:
        f.write("def main(): pass\n")
    with open(os.path.join(art, "README.md"), "w", encoding="utf-8") as f:
        f.write("# app\n")

    node = _make_node(role="code", step_ref="step_1_v1", config={})
    entry = build_artifact_entry(
        node, art, ArtifactContract(artifact_type="code"), shared, "app_ws"
    )
    assert entry.step_ref == "step_1_v1"
    assert entry.node_id == "reg-node"
    assert entry.role == "code"
    assert entry.artifact_type == "code"
    assert entry.artifact_path == os.path.realpath(art)
    assert entry.partition == os.path.realpath(os.path.join(shared, "app_ws"))
    relpaths = [f["relpath"] for f in entry.files]
    assert "main.py" in relpaths and "README.md" in relpaths
    # key_files：code 契约只命中代码扩展名
    assert entry.key_files == ["main.py"]


def test_scan_excludes_framework_dirs(tmp_path):
    art = tmp_path / "ws"
    art.mkdir(parents=True)
    (art / "main.py").write_text("x = 1\n")
    # 框架目录：即使含文件也不进入 files
    (art / ".codegraph").mkdir()
    (art / ".codegraph" / "codegraph.db").write_text("db")
    (art / ".git").mkdir()
    (art / ".git" / "config").write_text("git")

    files = scan_artifact_files(str(art))
    relpaths = [f["relpath"] for f in files]
    assert "main.py" in relpaths
    assert ".codegraph" not in " ".join(relpaths)
    assert ".git" not in " ".join(relpaths)
    assert not any("codegraph.db" in r or r.endswith("config") for r in relpaths)


def test_write_read_roundtrip_atomic(tmp_path):
    shared = os.path.join(str(tmp_path), "shared")
    os.makedirs(shared)
    art = os.path.join(shared, "ws")
    os.makedirs(art)
    with open(os.path.join(art, "a.py"), "w", encoding="utf-8") as f:
        f.write("pass\n")

    node = _make_node(role="code", step_ref="step_9_v3", config={})
    entry = build_artifact_entry(node, art, ArtifactContract(artifact_type="code"), shared, "ws")
    reg = FilesystemRegistry(shared)
    reg.write(entry)

    # 临时文件已原子替换，无残留
    assert not [n for n in os.listdir(os.path.join(shared, ".artifacts")) if n.endswith(".tmp")]

    read = reg.read("step_9_v3")
    assert read is not None
    assert read.step_ref == "step_9_v3"
    assert read.role == "code"
    assert read.artifact_path == os.path.realpath(art)
    assert read.files == entry.files
    # 未知 step_ref → None
    assert reg.read("ghost") is None


def test_resolve_variants(tmp_path):
    shared = os.path.join(str(tmp_path), "shared")
    os.makedirs(shared)
    reg = FilesystemRegistry(shared)

    for step_ref, role, sub in [("step_1_v1", "code", "a"), ("step_2_v1", "code", "b"), ("step_3_v1", "test", "c")]:
        art = os.path.join(shared, f"ws_{sub}")
        os.makedirs(art)
        with open(os.path.join(art, f"{sub}.py"), "w", encoding="utf-8") as f:
            f.write("x\n")
        node = _make_node(role=role, step_ref=step_ref, config={})
        reg.write(build_artifact_entry(node, art, ArtifactContract(artifact_type="code"), shared, f"ws_{sub}"))

    # @step_ref → artifact_path
    assert reg.resolve("@step_2_v1") == os.path.realpath(os.path.join(shared, "ws_b"))
    # @step_ref/sub → 拼接子路径
    assert reg.resolve("@step_2_v1/b.py") == os.path.join(
        os.path.realpath(os.path.join(shared, "ws_b")), "b.py"
    )
    # @role:name → 该角色最新产出（step_ref 字典序最大 = step_2_v1）
    assert reg.resolve("@role:code") == os.path.realpath(os.path.join(shared, "ws_b"))
    assert reg.resolve("@role:test") == os.path.realpath(os.path.join(shared, "ws_c"))
    # 未知 → None；非法引用 → None
    assert reg.resolve("@nope") is None
    assert reg.resolve("@role:ghost") is None
    assert reg.resolve("not-a-ref") is None
    assert reg.resolve("") is None


def test_key_files_verdict_contract(tmp_path):
    art = tmp_path / "rep"
    art.mkdir()
    (art / "report.md").write_text(
        '<!-- VERDICT {"passed": true, "defects": []} -->\n'
    )
    (art / "notes.txt").write_text("x\n")
    files = scan_artifact_files(str(art))
    contract = ArtifactContract(
        artifact_type="document",
        validators=["exists", "non_empty", "verdict_block"],
    )
    keys = key_files_for(str(art), files, contract)
    assert keys == ["report.md"]


# ============================================================================
# Worker 集成：COMPLETED 落盘 / 覆盖 / 写失败 / flag off
# ============================================================================

def test_worker_publish_on_completed(tmp_path, monkeypatch):
    monkeypatch.setenv("AICU_DELIVERY", "1")
    shared = os.path.join(str(tmp_path), "shared")
    node = _make_node(
        role="code", step_ref="step_1_v1",
        config={"_shared_workspace": shared, "_output_path": "app_ws"},
    )
    stub = _SharedWriteStub(shared, "app_ws", {"main.py": "def main(): pass\n"})
    result, _runtime = _run_worker(str(tmp_path), stub, node)

    assert result == NodeStatus.COMPLETED
    entry = FilesystemRegistry(shared).read("step_1_v1")
    assert entry is not None, "COMPLETED 后应落盘 Registry 条目"
    assert entry.role == "code"
    assert entry.artifact_type == "code"
    assert "main.py" in [f["relpath"] for f in entry.files]


def test_republish_overwrites_stale(tmp_path, monkeypatch):
    """repair/resume 同名 step_ref 重建 → 覆盖旧内容（非追加）。"""
    monkeypatch.setenv("AICU_DELIVERY", "1")
    shared = os.path.join(str(tmp_path), "shared")

    # 第一版：只含 old.py
    stub1 = _SharedWriteStub(shared, "app_ws", {"old.py": "v1\n"})
    node = _make_node(role="code", step_ref="step_1_v1",
                      config={"_shared_workspace": shared, "_output_path": "app_ws"})
    assert _run_worker(str(tmp_path), stub1, node)[0] == NodeStatus.COMPLETED
    first = FilesystemRegistry(shared).read("step_1_v1")
    assert first is not None and "old.py" in [f["relpath"] for f in first.files]

    # 第二版（repair/resume 改写）：只含 new.py，同名覆盖
    shutil.rmtree(os.path.join(shared, "app_ws"))
    stub2 = _SharedWriteStub(shared, "app_ws", {"new.py": "v2\n"})
    assert _run_worker(str(tmp_path), stub2, node)[0] == NodeStatus.COMPLETED
    second = FilesystemRegistry(shared).read("step_1_v1")
    assert second is not None
    relpaths = [f["relpath"] for f in second.files]
    assert "new.py" in relpaths and "old.py" not in relpaths, \
        "同名 step_ref 应覆盖为最新内容"


def test_publish_failure_does_not_fail_worker(tmp_path, monkeypatch):
    """Registry 写失败 ≠ Worker 失败：仍 COMPLETED，delivery_state 不污染。"""
    monkeypatch.setenv("AICU_DELIVERY", "1")
    shared = os.path.join(str(tmp_path), "shared")

    def _boom(self, entry):
        raise RuntimeError("registry disk full")

    monkeypatch.setattr(
        "runtime.delivery.registry.FilesystemRegistry.write", _boom
    )

    node = _make_node(
        role="code", step_ref="step_1_v1",
        config={"_shared_workspace": shared, "_output_path": "app_ws"},
    )
    stub = _SharedWriteStub(shared, "app_ws", {"main.py": "x=1\n"})
    result, runtime = _run_worker(str(tmp_path), stub, node)

    assert result == NodeStatus.COMPLETED
    _, _, updates = runtime.status_updates[-1]
    # Gate 仍通过（validated），Registry 失败不污染 delivery_state
    assert updates.get("delivery_state") == "validated"


def test_flag_off_no_registry(tmp_path, monkeypatch):
    """AICU_DELIVERY 未设 → 字节级：不创建注册表目录。"""
    monkeypatch.delenv("AICU_DELIVERY", raising=False)
    monkeypatch.delenv("AICU_ARTIFACT_GATE", raising=False)
    shared = os.path.join(str(tmp_path), "shared")
    node = _make_node(
        role="code", step_ref="step_1_v1",
        config={"_shared_workspace": shared, "_output_path": "app_ws"},
    )
    stub = _SharedWriteStub(shared, "app_ws", {"main.py": "x=1\n"})
    result, runtime = _run_worker(str(tmp_path), stub, node)
    assert result == NodeStatus.COMPLETED
    assert not os.path.exists(os.path.join(shared, ".artifacts")), \
        "flag off 不得创建 Registry"
    _, _, updates = runtime.status_updates[-1]
    assert updates.get("delivery_state") is None


# ============================================================================
# 依赖块注入对比（Delivery on → Registry resolve；off → node.artifact_path）
# ============================================================================

class _UpNode:
    """模拟上游依赖节点：artifact_path 是旧路径，Registry 持有真实路径。"""

    def __init__(self, step_ref: str, artifact_path: str):
        self.step_ref = step_ref
        self.id = "upstream"
        self.role = "code"
        self.artifact_path = artifact_path


def test_dependency_block_registry_injection(tmp_path, monkeypatch):
    from executables.worker import _resolve_dep_artifact_path

    shared = os.path.join(str(tmp_path), "shared")
    art = os.path.join(shared, "dep_ws")
    os.makedirs(art)
    with open(os.path.join(art, "api.py"), "w", encoding="utf-8") as f:
        f.write("def api(): pass\n")

    up = _UpNode("dep_v1", "/private/old_path")
    FilesystemRegistry(shared).write(build_artifact_entry(
        up, art, ArtifactContract(artifact_type="code"), shared, "dep_ws"
    ))

    node = _make_node(role="code", config={"_shared_workspace": shared})
    ctx = _make_context(_TmpRuntime(str(tmp_path)), node)

    # Delivery 开启 → 解析为 Registry 持有的确定性绝对路径
    monkeypatch.setenv("AICU_DELIVERY", "1")
    assert _resolve_dep_artifact_path(ctx, up) == os.path.realpath(art)

    # Delivery 关闭 → 回退 node.artifact_path（现状直传）
    monkeypatch.delenv("AICU_DELIVERY", raising=False)
    assert _resolve_dep_artifact_path(ctx, up) == "/private/old_path"

    # Registry 无条目 + Delivery 开启 → 降级回退（不引入新错误面）
    ghost = _UpNode("ghost_v9", "/private/ghost")
    assert _resolve_dep_artifact_path(ctx, ghost) == "/private/ghost"


def test_dependency_block_prompt_injection(tmp_path, monkeypatch):
    """端到端：下游 Worker 的依赖读取块注入 Registry 解析路径。"""
    monkeypatch.setenv("AICU_DELIVERY", "1")
    shared = os.path.join(str(tmp_path), "shared")

    # 上游产物 + Registry 条目
    art = os.path.join(shared, "up_ws")
    os.makedirs(art)
    with open(os.path.join(art, "api.py"), "w", encoding="utf-8") as f:
        f.write("def api(): pass\n")
    up = _UpNode("up_v1", "/private/stale_path")
    FilesystemRegistry(shared).write(build_artifact_entry(
        up, art, ArtifactContract(artifact_type="code"), shared, "up_ws"
    ))

    # 上游节点（Completed）
    upstream = Node(
        id="up", exec_type="worker", role="code", task="up task",
        config={}, status=NodeStatus.COMPLETED,
        step_ref="up_v1", artifact_path="/private/stale_path",
    )

    # 下游 Worker：deps 返回上游
    class _DepRuntime(_TmpRuntime):
        def get_dependencies(self, node_id):
            return [upstream]

    node = _make_node(
        role="code", step_ref="down_v1",
        config={"_shared_workspace": shared, "_output_path": "down_ws"},
    )
    runtime = _DepRuntime(str(tmp_path), deps=[upstream])
    ctx = _make_context(runtime, node)

    stub = _SharedWriteStub(shared, "down_ws", {"main.py": "x=1\n"})
    stub._last_question = ""
    orig_ask = stub.ask

    def _capturing_ask(question, max_rounds=3):
        stub._last_question = question
        return orig_ask(question, max_rounds)

    stub.ask = _capturing_ask
    worker = Worker(ctx, aicu_instance=stub)
    result = worker.run(ctx)

    assert result == NodeStatus.COMPLETED
    assert "/private/stale_path" not in stub._last_question
    assert os.path.realpath(art) in stub._last_question


# ============================================================================
# Backend 抽象可替换（Protocol：同一调用方兼容 FilesystemRegistry 与 mock DB）
# ============================================================================

class _MockDBRegistry:
    """最小 Protocol 实现（内存版）——验证 Backend 可替换。"""

    def __init__(self):
        self._store = {}

    def write(self, entry):
        self._store[entry.step_ref] = entry

    def read(self, step_ref):
        return self._store.get(step_ref)

    def resolve(self, reference):
        if not reference or not reference.startswith("@"):
            return None
        entry = self._store.get(reference[1:])
        return entry.artifact_path if entry else None


def _consume_backend(backend, entry):
    """同一调用方：write → read 回环。"""
    backend.write(entry)
    read_back = backend.read(entry.step_ref)
    return read_back is not None and read_back.artifact_path == entry.artifact_path


def test_backend_replaceable(tmp_path):
    shared = os.path.join(str(tmp_path), "shared")
    os.makedirs(shared)
    art = os.path.join(shared, "ws")
    os.makedirs(art)
    with open(os.path.join(art, "a.py"), "w", encoding="utf-8") as f:
        f.write("pass\n")
    node = _make_node(role="code", step_ref="step_1_v1", config={})
    entry = build_artifact_entry(
        node, art, ArtifactContract(artifact_type="code"), shared, "ws"
    )

    fs_reg = FilesystemRegistry(shared)
    db_reg = _MockDBRegistry()
    # 同一调用方分别消费 FilesystemRegistry 与 mock DB，语义一致
    assert _consume_backend(fs_reg, entry) is True
    assert _consume_backend(db_reg, entry) is True
    # Protocol resolve 契约一致：已知引用返回 artifact_path，未知返回 None
    assert fs_reg.resolve("@step_1_v1") == os.path.realpath(art)
    assert db_reg.resolve("@step_1_v1") == os.path.realpath(art)
    assert fs_reg.resolve("@nope") is None
    assert db_reg.resolve("@nope") is None
