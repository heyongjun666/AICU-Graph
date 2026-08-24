"""tests/test_session_shared.py — Session Outputs 会话级「查看产出」文件树浏览器。

覆盖（V3-2.2 计划 test_session_shared.py 用例）：
- 空树：无 shared 目录 / 有 shared 但为空 → {"path": ".", "entries": [], "truncated": false}；
  响应不含 shared_root（前端视共享根为抽象资源）
- 顶层分区：注册分区行 kind=="artifact_partition" + producer{node_id/role/step_ref/artifact_type/key_files}
  + delivery{state}；未注册目录 kind=="directory" + producer/delivery=null（不虚构 producer）
- 框架目录隐藏：FRAMEWORK_DIR_NAMES 显式项（.artifacts/.codegraph/.git/cc_0 等）+ 任意 cc_* 前缀
- 子目录下钻：path="backend/src" → 普通清单，producer=null，规范化相对路径
- 同一分区多条 entry → producer 取 step_ref 字典序最大（Registry latest_per_partition 单一权威）
- 路径规范化：根→"."，forward slash，无 "./" 前缀
- read_session_shared_file：文本预览 / 二进制不可预览（content=null）/ 目录当文件读 →
  FileNotFoundError / 1MB 截断 truncated / 越界 ValueError / 缺失 FileNotFoundError
- symlink：顶层 symlink 跳过；symlink 中间路径 → 外部文件 → _resolve_workspace_path realpath
  containment → ValueError
- 列表 >1000 entries → truncated=true
- preview 纯函数：文本扩展名 / dotfile 特判 / mime
"""

import os
import tempfile

import pytest

from runtime.delivery.contracts import ArtifactContract
from runtime.delivery.preview import mime_type_for, previewable_path
from runtime.delivery.registry import FilesystemRegistry, build_artifact_entry
from runtime.models import ExecutionPlan, Step
from runtime.runtime import Runtime
from runtime.workspace_resolver import resolve_shared_workspace


# ============================================================================
# Helpers
# ============================================================================

def _step(step_id: str, config: dict, **kw) -> Step:
    kw.setdefault("exec_type", "worker")
    kw.setdefault("role", "dev")
    kw.setdefault("task", f"task {step_id}")
    kw.setdefault("task_summary", f"summary {step_id}")
    return Step(step_id=step_id, config=config, **kw)


def _mk_runtime(tmp_path) -> Runtime:
    """Real Runtime，无 scheduler（只做构造/注入，不自动执行）。"""
    return Runtime(workspace_root=str(tmp_path))


def _shared(rt: Runtime) -> str:
    return resolve_shared_workspace(rt.workspace_root)


def _register_partition(rt: Runtime, step_ref: str, role: str,
                        output_path: str, delivery_state: str | None,
                        files: dict) -> tuple:
    """建 root + 节点 + 写分区文件 + Registry 登记，返回 (node, partition 绝对路径)。"""
    root = rt.submit_root_manager("build app")
    nodes = rt.inject_plan(root, ExecutionPlan(steps=[
        _step(step_ref, {"output_path": output_path}, role=role),
    ]))
    node = nodes[0]
    node.delivery_state = delivery_state
    shared = _shared(rt)
    partition = os.path.join(shared, output_path)
    for rel, content in files.items():
        p = os.path.join(partition, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    reg = FilesystemRegistry(shared)
    reg.write(build_artifact_entry(
        node, partition, ArtifactContract(artifact_type="code"), shared, output_path,
    ))
    return node, partition


# ============================================================================
# 空树 / 不暴露 shared_root
# ============================================================================

def test_no_shared_dir_empty_tree(tmp_path):
    rt = _mk_runtime(tmp_path)
    result = rt.list_session_shared()
    assert result == {"path": ".", "entries": [], "truncated": False}
    assert "shared_root" not in result, "不得暴露真实 shared_root"


def test_empty_shared_dir_empty_tree(tmp_path):
    rt = _mk_runtime(tmp_path)
    os.makedirs(_shared(rt))
    result = rt.list_session_shared()
    assert result["entries"] == []
    assert result["truncated"] is False
    assert "shared_root" not in result


# ============================================================================
# 顶层分区 / 框架目录 / 未注册分区
# ============================================================================

def test_framework_dirs_hidden(tmp_path):
    rt = _mk_runtime(tmp_path)
    shared = _shared(rt)
    for d in (".artifacts", ".codegraph", ".git", "node_modules", "cc_0", "cc_7"):
        os.makedirs(os.path.join(shared, d), exist_ok=True)
    os.makedirs(os.path.join(shared, "backend"), exist_ok=True)
    names = {e["name"] for e in rt.list_session_shared()["entries"]}
    assert names == {"backend"}, f"框架目录应整体隐藏，got {names}"


def test_partition_with_producer(tmp_path):
    rt = _mk_runtime(tmp_path)
    node, _ = _register_partition(
        rt, "be", "backend", "backend", "validated",
        {"src/api.py": "def main(): pass\n"},
    )
    be_entry = next(e for e in rt.list_session_shared()["entries"] if e["name"] == "backend")
    assert be_entry["kind"] == "artifact_partition"
    assert be_entry["type"] == "directory"
    assert be_entry["producer"]["step_ref"] == "be"
    assert be_entry["producer"]["node_id"] == node.id
    assert be_entry["producer"]["role"] == "backend"
    assert be_entry["producer"]["artifact_type"] == "code"
    assert "src/api.py" in be_entry["producer"]["key_files"]
    assert be_entry["delivery"] == {"state": "validated"}
    assert be_entry["path"] == "backend"


def test_unregistered_partition_kind_directory(tmp_path):
    """Filesystem 存在但无 Registry 记录 → 不虚构 producer/delivery。"""
    rt = _mk_runtime(tmp_path)
    shared = _shared(rt)
    os.makedirs(os.path.join(shared, "random"))
    with open(os.path.join(shared, "random", "hello.txt"), "w") as f:
        f.write("hi")
    entry = next(e for e in rt.list_session_shared()["entries"] if e["name"] == "random")
    assert entry["kind"] == "directory"
    assert entry["producer"] is None
    assert entry["delivery"] is None


def test_top_level_file_kind_file(tmp_path):
    rt = _mk_runtime(tmp_path)
    shared = _shared(rt)
    os.makedirs(shared)
    with open(os.path.join(shared, "README.md"), "w") as f:
        f.write("# outputs")
    entry = next(e for e in rt.list_session_shared()["entries"] if e["name"] == "README.md")
    assert entry["kind"] == "file"
    assert entry["producer"] is None
    assert entry["delivery"] is None


# ============================================================================
# 下钻 / 路径规范化
# ============================================================================

def test_drill_down_subdir(tmp_path):
    rt = _mk_runtime(tmp_path)
    shared = _shared(rt)
    os.makedirs(os.path.join(shared, "backend", "src"))
    with open(os.path.join(shared, "backend", "src", "api.py"), "w") as f:
        f.write("x = 1\n")
    result = rt.list_session_shared(path="backend/src")
    assert result["path"] == "backend/src"
    entry = result["entries"][0]
    assert entry["name"] == "api.py"
    assert entry["kind"] == "file"
    assert entry["path"] == "backend/src/api.py"
    assert entry["producer"] is None
    assert entry["delivery"] is None


def test_path_normalization_root_and_sub(tmp_path):
    rt = _mk_runtime(tmp_path)
    shared = _shared(rt)
    os.makedirs(os.path.join(shared, "backend", "src"))
    root_result = rt.list_session_shared()
    assert root_result["path"] == "."
    sub_result = rt.list_session_shared(path="backend")
    assert sub_result["path"] == "backend"
    src = next(e for e in sub_result["entries"] if e["name"] == "src")
    assert src["path"] == "backend/src"
    assert not src["path"].startswith("./")
    assert "/" in src["path"]


def test_multiple_partitions_listed(tmp_path):
    rt = _mk_runtime(tmp_path)
    root = rt.submit_root_manager("build app")
    nodes = rt.inject_plan(root, ExecutionPlan(steps=[
        _step("be", {"output_path": "backend"}, role="backend"),
        _step("fe", {"output_path": "frontend"}, role="frontend"),
    ]))
    be, fe = nodes
    be.delivery_state = "validated"
    shared = _shared(rt)
    reg = FilesystemRegistry(shared)
    for node in (be, fe):
        partition = os.path.join(shared, node.config["_output_path"])
        os.makedirs(partition, exist_ok=True)
        with open(os.path.join(partition, "f.txt"), "w") as f:
            f.write("x")
        reg.write(build_artifact_entry(
            node, partition, ArtifactContract(artifact_type="code"),
            shared, node.config["_output_path"],
        ))
    names = {e["name"] for e in rt.list_session_shared()["entries"]}
    assert names == {"backend", "frontend"}


# ============================================================================
# 同一分区多条 entry → Registry latest_per_partition 规则
# ============================================================================

def test_latest_per_partition_selects_max_step_ref(tmp_path):
    rt = _mk_runtime(tmp_path)
    root = rt.submit_root_manager("build app")
    nodes = rt.inject_plan(root, ExecutionPlan(steps=[
        _step("be_v1", {"output_path": "backend", "allow_shared_output": True}, role="backend"),
        _step("be_v2", {"output_path": "backend", "allow_shared_output": True}, role="backend"),
    ]))
    be_v1, be_v2 = nodes
    be_v2.delivery_state = "validated"
    shared = _shared(rt)
    partition = os.path.join(shared, "backend")
    os.makedirs(partition, exist_ok=True)
    with open(os.path.join(partition, "api.py"), "w") as f:
        f.write("x = 1\n")
    reg = FilesystemRegistry(shared)
    for n in (be_v1, be_v2):
        reg.write(build_artifact_entry(
            n, partition, ArtifactContract(artifact_type="code"), shared, "backend",
        ))
    entry = next(e for e in rt.list_session_shared()["entries"] if e["name"] == "backend")
    # latest 判定收在 Registry（step_ref 字典序最大），UI/Runtime 不得另定规则
    assert entry["producer"]["step_ref"] == "be_v2"
    assert entry["delivery"] == {"state": "validated"}


# ============================================================================
# read_session_shared_file：预览 / 二进制 / 目录 / 截断 / 越界 / 缺失
# ============================================================================

def test_read_file_text_preview(tmp_path):
    rt = _mk_runtime(tmp_path)
    shared = _shared(rt)
    os.makedirs(os.path.join(shared, "backend"))
    with open(os.path.join(shared, "backend", "api.py"), "w") as f:
        f.write("def main(): pass\n")
    result = rt.read_session_shared_file("backend/api.py")
    assert result["path"] == "backend/api.py"
    assert result["size"] == 17
    assert result["previewable"] is True
    assert result["content"] == "def main(): pass\n"
    assert result["truncated"] is False


def test_read_file_binary_not_previewable(tmp_path):
    rt = _mk_runtime(tmp_path)
    shared = _shared(rt)
    os.makedirs(os.path.join(shared, "data"))
    with open(os.path.join(shared, "data", "blob.bin"), "wb") as f:
        f.write(b"\x00\x01\x02\xff")
    result = rt.read_session_shared_file("data/blob.bin")
    assert result["previewable"] is False
    assert result["content"] is None
    assert result["truncated"] is False


def test_read_dotfile_previewable(tmp_path):
    rt = _mk_runtime(tmp_path)
    shared = _shared(rt)
    os.makedirs(shared)
    with open(os.path.join(shared, ".env"), "w") as f:
        f.write("SECRET=x")
    result = rt.read_session_shared_file(".env")
    assert result["previewable"] is True
    assert result["content"] == "SECRET=x"


def test_read_directory_raises(tmp_path):
    """目录当文件读 → FileNotFoundError（禁止把目录当文件读）。"""
    rt = _mk_runtime(tmp_path)
    shared = _shared(rt)
    os.makedirs(os.path.join(shared, "backend"))
    with pytest.raises(FileNotFoundError):
        rt.read_session_shared_file("backend")


def test_read_missing_file_raises(tmp_path):
    rt = _mk_runtime(tmp_path)
    with pytest.raises(FileNotFoundError):
        rt.read_session_shared_file("ghost.py")


def test_read_large_file_truncated(tmp_path):
    rt = _mk_runtime(tmp_path)
    shared = _shared(rt)
    os.makedirs(os.path.join(shared, "big"))
    with open(os.path.join(shared, "big", "log.txt"), "w") as f:
        f.write("a" * 1_100_000)
    result = rt.read_session_shared_file("big/log.txt")
    assert result["truncated"] is True
    assert len(result["content"]) == 1_000_000  # 预览硬上限 = 1MB 产品安全边界


def test_out_of_bounds_raises(tmp_path):
    rt = _mk_runtime(tmp_path)
    os.makedirs(_shared(rt))
    with pytest.raises(ValueError):
        rt.list_session_shared(path="../outside")
    with pytest.raises(ValueError):
        rt.list_session_shared(path=str(tmp_path / "shared" / "backend"))  # 绝对路径
    with pytest.raises(ValueError):
        rt.read_session_shared_file("../../etc/passwd")


# ============================================================================
# symlink 安全
# ============================================================================

def test_symlink_top_level_skipped(tmp_path):
    rt = _mk_runtime(tmp_path)
    shared = _shared(rt)
    os.makedirs(shared)
    outside = tempfile.mkdtemp(prefix="aicu_outside_")
    os.symlink(outside, os.path.join(shared, "link_out"))
    names = {e["name"] for e in rt.list_session_shared()["entries"]}
    assert "link_out" not in names, "顶层 symlink 条目不得展示"


def test_symlink_midpath_escape_rejected(tmp_path):
    """symlink 中间路径 → 外部文件：_resolve_workspace_path realpath containment 必须拒绝。"""
    rt = _mk_runtime(tmp_path)
    shared = _shared(rt)
    os.makedirs(shared)
    outside = tempfile.mkdtemp(prefix="aicu_outside_")
    with open(os.path.join(outside, "outside.txt"), "w") as f:
        f.write("secret")
    os.symlink(outside, os.path.join(shared, "backend"))
    with pytest.raises(ValueError):
        rt.read_session_shared_file("backend/outside.txt")


# ============================================================================
# 列表截断 / Registry 扩展
# ============================================================================

def test_list_truncated_at_1000(tmp_path):
    rt = _mk_runtime(tmp_path)
    shared = _shared(rt)
    os.makedirs(shared)
    for i in range(1001):
        with open(os.path.join(shared, f"file_{i:04d}.txt"), "w") as f:
            f.write("x")
    result = rt.list_session_shared()
    assert result["truncated"] is True
    assert len(result["entries"]) == 1000


def test_registry_entries_and_latest_empty(tmp_path):
    shared = os.path.join(str(tmp_path), "shared")
    reg = FilesystemRegistry(shared)
    assert reg.entries() == []
    assert reg.latest_per_partition() == {}


# ============================================================================
# preview 纯函数
# ============================================================================

def test_previewable_path():
    assert previewable_path("a/b/api.py")
    assert previewable_path("report.md")
    assert previewable_path("data.json")
    assert previewable_path(".env")
    assert previewable_path(".gitignore")
    assert not previewable_path("a/b/blob.bin")
    assert not previewable_path("a/b/image.png")
    assert not previewable_path("a/b/NOEXT")


def test_mime_type_for():
    m = mime_type_for("backend/src/api.py")
    assert m is None or m.startswith("text/")
