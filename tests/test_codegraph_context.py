"""
tests/test_codegraph_context.py — CodeGraph Context Provider 测试

覆盖：
  1. 守卫 no-op：AICU_CODEGRAPH 关 / stub|mock 模式 → build_codegraph_context /
     build_review_context 返回 ""（承重契约：默认关字节级不变，Resolver 在 stub 下 no-op）
  2. Resolver 确定性提取：只命中 file/symbol 引用，不做自然语言语义判断
  3. 预算裁剪：≤4KB 注入预算，超出从尾部截断保留头部
  4. GroupGraphState 状态机：NOT_EXIST→READY（init+index）、READY→STALE→READY（sync）、
     异常→FAILED、BUILDING 中不可重入
  5. notify_artifact_updated 上下文失效边界：flag 关 no-op，flag 开 mark_stale
  6. build_codegraph_context 真实注入：依赖共享组产出清单 + 项目图角色查询
"""

import os
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from runtime.context import (
    GroupGraphState,
    build_codegraph_context,
    build_review_context,
    notify_artifact_updated,
)
from runtime.context.codegraph import query_group_inventory
from runtime.context.resolver import DEFAULT_BUDGET, assemble, extract_candidates, truncate
from runtime.context.state import GraphState
from runtime.models import Node, NodeStatus


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def _node(node_id="n1", role="code", task="", shared=None, workspace_root=None):
    config = {}
    if shared:
        config["_shared_workspace"] = shared
    if workspace_root:
        config["_workspace_root"] = workspace_root
    return Node(
        id=node_id,
        parent_id=None,
        exec_type="worker",
        role=role,
        task=task,
        config=config,
        status=NodeStatus.READY,
    )


def _dep(shared):
    return _node(node_id=f"dep_{os.path.basename(shared)}", shared=shared)


class _FakeRuntime:
    def __init__(self, deps=None):
        self._deps = deps or []

    def get_dependencies(self, node_id):
        return self._deps


def _ctx(node, mode="real", deps=None):
    return SimpleNamespace(
        config={"mode": mode},
        node=node,
        runtime=_FakeRuntime(deps or []),
    )


@pytest.fixture(autouse=True)
def _reset_graph_state():
    GroupGraphState._states.clear()
    yield
    GroupGraphState._states.clear()


# ---------------------------------------------------------------------------
# 1. 守卫 no-op（承重契约）
# ---------------------------------------------------------------------------


def test_flag_off_noop(monkeypatch):
    monkeypatch.delenv("AICU_CODEGRAPH", raising=False)
    node = _node(task="fix create_article in src/app.py")
    assert build_codegraph_context(node, _ctx(node)) == ""


def test_flag_on_but_stub_noop(monkeypatch):
    monkeypatch.setenv("AICU_CODEGRAPH", "1")
    node = _node(task="fix create_article in src/app.py")
    assert build_codegraph_context(node, _ctx(node, mode="stub")) == ""


def test_flag_on_but_mock_noop(monkeypatch):
    monkeypatch.setenv("AICU_CODEGRAPH", "1")
    node = _node(task="fix create_article in src/app.py")
    assert build_codegraph_context(node, _ctx(node, mode="mock")) == ""


def test_no_extractable_candidates_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("AICU_CODEGRAPH", "1")
    node = _node(task="请帮我分页优化性能", workspace_root=str(tmp_path))
    assert build_codegraph_context(node, _ctx(node)) == ""


# ---------------------------------------------------------------------------
# 2. Resolver 确定性提取
# ---------------------------------------------------------------------------


def test_extract_candidates_files_and_symbols():
    files, symbols = extract_candidates("handle create_article in src/app.py and ArticleService")
    assert "src/app.py" in files
    assert "create_article" in symbols
    assert "ArticleService" in symbols


def test_extract_candidates_ignores_natural_language():
    files, symbols = extract_candidates("请帮我实现用户分页与性能优化")
    assert files == []
    assert symbols == []


_TRUNC_MARKER = "\n... [truncated]"


def test_truncate_respects_budget():
    big = "x" * (DEFAULT_BUDGET + 5000)
    out = truncate(big)
    assert len(out) <= DEFAULT_BUDGET + len(_TRUNC_MARKER)
    assert out.startswith("x" * 100)


def test_assemble_dedup_and_budget():
    blocks = ["### 块A\n" + "a" * 3000, "### 块B\n" + "b" * 3000]
    out = assemble("H", "D", blocks, DEFAULT_BUDGET)
    assert len(out) <= DEFAULT_BUDGET + len(_TRUNC_MARKER)
    assert out.startswith("H")


# ---------------------------------------------------------------------------
# 3. build_codegraph_context 依赖共享组 + 项目图注入
# ---------------------------------------------------------------------------


def test_dependency_group_inventory_injected(monkeypatch, tmp_path):
    monkeypatch.setenv("AICU_CODEGRAPH", "1")
    shared = str(tmp_path / "shared" / "group")
    node = _node(task="fix create_article", shared=None, workspace_root=str(tmp_path))
    dep = _dep(shared)
    with patch.object(GroupGraphState, "ensure_ready", return_value=True), \
         patch("runtime.context.codegraph._run_cli", return_value="- a.py\n- b.py\n"), \
         patch("runtime.context.codegraph._project_root", return_value="/tmp"):
        block = build_codegraph_context(node, _ctx(node, deps=[dep]))
    assert block
    assert "## 🔍 代码图谱上下文" in block
    assert "共享组代码产出" in block


def test_project_role_query(monkeypatch, tmp_path):
    monkeypatch.setenv("AICU_CODEGRAPH", "1")
    node = _node(task="fix ArticleService handle()", workspace_root=str(tmp_path))
    with patch("runtime.context.codegraph._run_cli", return_value="called:\n- x.py\n"), \
         patch("runtime.context.codegraph._project_root", return_value="/tmp"):
        block = build_codegraph_context(node, _ctx(node))
    assert block
    assert "ArticleService" in block
    assert "(callees)" in block


def test_skip_project_roles(monkeypatch, tmp_path):
    monkeypatch.setenv("AICU_CODEGRAPH", "1")
    node = _node(role="requirements", task="fix ArticleService", workspace_root=str(tmp_path))
    with patch("runtime.context.codegraph._run_cli", return_value="x"), \
         patch("runtime.context.codegraph._project_root", return_value="/tmp"):
        block = build_codegraph_context(node, _ctx(node))
    # requirements 角色跳过项目查询；无依赖 → 无任何块
    assert block == ""


# ---------------------------------------------------------------------------
# 4. build_review_context
# ---------------------------------------------------------------------------


def test_review_context_noop_when_off(monkeypatch, tmp_path):
    monkeypatch.delenv("AICU_CODEGRAPH", raising=False)
    node = _node(role="dev_lead", workspace_root=str(tmp_path))
    children = [{"shared_workspace": str(tmp_path / "shared" / "g")}]
    assert build_review_context(_ctx(node), children) == ""


def test_review_context_group_inventory(monkeypatch, tmp_path):
    monkeypatch.setenv("AICU_CODEGRAPH", "1")
    node = _node(role="dev_lead", workspace_root=str(tmp_path))
    shared = str(tmp_path / "shared" / "g")
    children = [{"shared_workspace": shared}]
    with patch.object(GroupGraphState, "ensure_ready", return_value=True), \
         patch("runtime.context.codegraph._run_cli", return_value="- c.py\n- d.py\n"):
        block = build_review_context(_ctx(node), children)
    assert block
    assert "共享组代码产出" in block
    assert "g" in block


# ---------------------------------------------------------------------------
# 5. GroupGraphState 状态机
# ---------------------------------------------------------------------------


def test_state_machine_not_exist_to_ready(tmp_path):
    shared = str(tmp_path / "shared" / "g")
    with patch("runtime.context.state._run_cli", return_value="") as cli:
        assert GroupGraphState.ensure_ready(shared) is True
    assert GroupGraphState.get(shared) == GraphState.READY
    calls = [c.args[0] for c in cli.call_args_list]
    # NOT_EXIST → init（显式 [path]，避免 walk-up 命中项目根重索引全项目）
    assert ["init", "--force", shared] in calls


def test_state_machine_stale_to_ready(tmp_path):
    shared = str(tmp_path / "shared" / "g")
    os.makedirs(os.path.join(shared, ".codegraph"), exist_ok=True)
    GroupGraphState.mark_stale(shared)
    assert GroupGraphState.get(shared) == GraphState.STALE
    with patch("runtime.context.state._run_cli", return_value="") as cli:
        assert GroupGraphState.ensure_ready(shared) is True
    assert GroupGraphState.get(shared) == GraphState.READY
    calls = [c.args[0] for c in cli.call_args_list]
    assert ["sync", shared] in calls


def test_state_machine_failed(tmp_path):
    shared = str(tmp_path / "shared" / "g")
    with patch("runtime.context.state._run_cli", side_effect=RuntimeError("boom")):
        assert GroupGraphState.ensure_ready(shared) is False
    assert GroupGraphState.get(shared) == GraphState.FAILED
    # FAILED 不再重试
    assert GroupGraphState.ensure_ready(shared) is False


def test_state_machine_building_returns_false(tmp_path):
    shared = str(tmp_path / "shared" / "g")
    GroupGraphState._states[shared] = GraphState.BUILDING
    assert GroupGraphState.ensure_ready(shared) is False


# ---------------------------------------------------------------------------
# 6. notify_artifact_updated 失效边界
# ---------------------------------------------------------------------------


def test_notify_artifact_updated_flag_off_noop(monkeypatch, tmp_path):
    monkeypatch.delenv("AICU_CODEGRAPH", raising=False)
    shared = str(tmp_path / "shared" / "g")
    notify_artifact_updated(shared)
    assert GroupGraphState.get(shared) == GraphState.NOT_EXIST


def test_notify_artifact_updated_flag_on_marks_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("AICU_CODEGRAPH", "1")
    shared = str(tmp_path / "shared" / "g")
    notify_artifact_updated(shared)
    assert GroupGraphState.get(shared) == GraphState.STALE


# ---------------------------------------------------------------------------
# 7. 真实 CLI 回归：组图显式 [path] 隔离（防 walk-up 重索引项目根）
# ---------------------------------------------------------------------------


def test_real_group_graph_isolation(tmp_path):
    """组目录位于祖先项目（有 .codegraph 且 .gitignore 排除父级）之下时，
    ensure_ready 必须显式 [path] 在组目录建独立图，files 命中组清单而非祖先项目清单。"""
    cli = shutil.which("codegraph")
    if not cli:
        pytest.skip("codegraph CLI not available")

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".gitignore").write_text("shared/\n")
    (proj / "parent.py").write_text("def parent_fn():\n    pass\n")
    subprocess.run([cli, "init", "--force", str(proj)], check=True, capture_output=True)

    shared = str(proj / "shared" / "grp")
    os.makedirs(shared)
    with open(os.path.join(shared, "calc.py"), "w") as f:
        f.write("def multiply(a, b):\n    return a * b\n")

    assert GroupGraphState.ensure_ready(shared) is True
    # 独立组图建在组目录内，而非祖先项目根
    assert os.path.isdir(os.path.join(shared, ".codegraph"))
    raw = query_group_inventory(shared)
    assert "calc.py" in raw
    assert "parent.py" not in raw  # 命中组图而非祖先项目图
