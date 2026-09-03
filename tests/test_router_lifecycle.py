"""tests/test_router_lifecycle.py — Router 生命周期：与组织结构版本绑定

方案三定稿：
    「一个 Router AICU 服务一个 Organization Graph 版本。图不变则持续复用
    （快照缓存 + 会话复用）；图发生结构变化则切换 Router 上下文（重置会话）。
    运行状态变化（running/completed/failed）不切换。」

覆盖：
    - Graph.structure_version 只在结构变更时自增（状态变化不触发）
    - Router 快照缓存：图未变化时不重扫全量节点，状态变化时重建保持新鲜
    - Router 会话重置：结构变化 → reset 一次；状态变化 → 不 reset
"""

from runtime.graph import Graph
from runtime.models import Edge, EdgeType, EventMessage, Node, NodeStatus
from runtime.router import AICURouter


def _node(node_id, parent_id=None, exec_type="worker", role="dev",
          status=NodeStatus.WAITING, task="t", step_ref=None, task_summary=None):
    return Node(
        id=node_id,
        exec_type=exec_type,
        role=role,
        task=task,
        config={},
        status=status,
        parent_id=parent_id,
        step_ref=step_ref,
        task_summary=task_summary,
    )


def _event(producer="n1", objective="hello"):
    return EventMessage(event_id="e1", producer=producer, objective=objective)


class _FakeCC:
    def __init__(self):
        self.reset_count = 0

    def reset_conversation(self):
        self.reset_count += 1


class _FakeAICU:
    """real 模式注入的 AICU：带 ccs 列表（记录 reset）+ 固定路由决策。"""

    def __init__(self, targets=None):
        self.ccs = [_FakeCC()]
        self.targets = targets or []

    def ask(self, prompt):
        return {"targets": list(self.targets)}


def _real_router(fake, graph):
    router = AICURouter(aicu_instance=fake)
    router.attach_graph(graph)
    return router


def test_real_router_passes_system_config_to_aicu(monkeypatch, tmp_path):
    captured = {}

    class _ConfiguredAICU:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import aicu

    monkeypatch.setattr(aicu, "AICU", _ConfiguredAICU)
    router = AICURouter(
        workspace_root=str(tmp_path),
        config={
            "model": "system-model",
            "cli_name": "system-cli",
            "api_base_url": "https://system.example/v1",
            "api_key_env": "SYSTEM_API_KEY",
        },
    )

    router._create_real_aicu()

    assert captured["model"] == "system-model"
    assert captured["cli_name"] == "system-cli"
    assert captured["api_base_url"] == "https://system.example/v1"
    assert captured["api_key_env"] == "SYSTEM_API_KEY"


# ---------------------------------------------------------------------------
# Graph.structure_version 语义
# ---------------------------------------------------------------------------

def test_structure_version_bumps_only_on_structural_ops():
    g = Graph()
    assert g.structure_version == 0

    g.add_node(_node("n1"))
    assert g.structure_version == 1          # add_node

    g.add_node(_node("n2", parent_id="n1"))
    assert g.structure_version == 2

    g.update_node("n1", status=NodeStatus.COMPLETED)
    assert g.structure_version == 2          # 状态变化不触发

    g.update_node("n2", role="test")
    assert g.structure_version == 3          # role 是结构字段

    g.update_node("n2", parent_id=None)
    assert g.structure_version == 4          # 换 parent 是结构字段

    g.add_edge(Edge(id="e1", type=EdgeType.PARENT, from_node="n1", to_node="n2"))
    assert g.structure_version == 5          # add_edge

    g.remove_edge("e1")
    assert g.structure_version == 6          # remove_edge

    g.remove_node("n2")
    assert g.structure_version == 7          # remove_node


def test_structure_version_untouched_by_nonstructural_updates():
    g = Graph()
    g.add_node(_node("n1", parent_id=None, exec_type="manager", role="plan_mgr"))
    g.add_node(_node("n2", parent_id="n1"))
    g.update_node("n2", task="新任务")
    g.update_node("n2", config={"k": "v"})
    g.update_node("n2", pending_context={"event_content": "x"})
    assert g.structure_version == 2          # task/config/pending_context 均非结构


# ---------------------------------------------------------------------------
# Router 快照缓存：图没变不重扫全量节点
# ---------------------------------------------------------------------------

def test_snapshot_cache_reused_when_graph_unchanged():
    g = Graph()
    g.add_node(_node("n1", step_ref="a"))
    g.add_node(_node("n2", step_ref="b"))
    router = _real_router(_FakeAICU(), g)

    s1 = router._graph_snapshot("n1")
    s2 = router._graph_snapshot("n1")
    assert s1 is s2                          # 图未变化 → 同一缓存对象（未重扫）

    g.update_node("n1", status=NodeStatus.COMPLETED)
    s3 = router._graph_snapshot("n1")
    assert s3 is not s1                      # 状态变化 → 重建（status 保持新鲜）
    assert s3[0]["status"] == "Completed"

    assert router._graph_snapshot("n1") is s3  # 重建后再复用


def test_attach_graph_resets_snapshot_cache():
    g1 = Graph()
    g1.add_node(_node("n1"))
    g2 = Graph()
    g2.add_node(_node("m1"))
    router = _real_router(_FakeAICU(), g1)

    s1 = router._graph_snapshot("n1")
    router.attach_graph(g2)
    assert router._cached_snapshot is None   # 重挂后缓存清空
    s2 = router._graph_snapshot("m1")
    assert s2 is not s1
    assert s2[0]["id"] == "m1"


# ---------------------------------------------------------------------------
# Router 快照 / prompt 使用 task_summary 职责索引（方案：summary 替代 task 全文）
# ---------------------------------------------------------------------------

def test_snapshot_exposes_task_summary_field():
    g = Graph()
    g.add_node(_node("n1", step_ref="auth", task_summary="负责 auth 模块的 JWT 登录/注册接口"))
    router = _real_router(_FakeAICU(), g)

    snap = router._graph_snapshot("n1")
    assert snap[0]["task_summary"] == "负责 auth 模块的 JWT 登录/注册接口"


def test_snapshot_falls_back_to_truncated_task_when_summary_missing():
    g = Graph()
    g.add_node(_node("n1", task="实现后端功能", task_summary=None))
    router = _real_router(_FakeAICU(), g)

    snap = router._graph_snapshot("n1")
    assert snap[0]["task_summary"] == "实现后端功能"   # fallback = 截断后的 task


def test_build_prompt_shows_task_summary_not_full_task():
    long_task = "这是一个非常长的执行指令，" + "A" * 200 + "，包含上下文约束与协作锚点。"
    summary = "负责 auth 模块的 JWT 登录/注册接口"
    g = Graph()
    g.add_node(_node("n1", step_ref="auth", task=long_task, task_summary=summary))
    router = _real_router(_FakeAICU(), g)

    prompt = router._build_prompt(_event(), "n1", router._graph_snapshot("n1"))

    # 组织图节点清单行用 task_summary=（摘要），不再用 task= 全文
    node_line = next(l for l in prompt.splitlines() if l.startswith("- `auth`"))
    assert f"task_summary=`{summary}`" in node_line
    assert "task=`" not in node_line
    assert "这是一个非常长的执行指令" not in node_line
    # producer_ctx 仍保留 producer 自身 task= 兜底（仅一处，不在节点清单）
    assert prompt.count("task=`") == 1


# ---------------------------------------------------------------------------
# Router 会话重置：结构变化切换，状态变化不切换
# ---------------------------------------------------------------------------

def test_conversation_reset_on_structural_change_not_status():
    g = Graph()
    g.add_node(_node("n1", step_ref="a"))
    g.add_node(_node("n2", step_ref="b"))
    fake = _FakeAICU(targets=["n2"])
    router = _real_router(fake, g)
    cc = fake.ccs[0]

    router.route(_event(producer="n1"))          # 首次：初始化 epoch，不重置
    assert cc.reset_count == 0

    router.route(_event(producer="n1"))          # 图未变 → 复用，不重置
    assert cc.reset_count == 0

    g.update_node("n1", status=NodeStatus.COMPLETED)   # 状态变化
    router.route(_event(producer="n1"))
    assert cc.reset_count == 0                  # 状态变化不切换

    g.add_node(_node("n3", parent_id="n1"))     # 结构变化
    router.route(_event(producer="n1"))
    assert cc.reset_count == 1                  # 结构变化 → 重置一次

    g.add_node(_node("n4", parent_id="n1"))     # 再次结构变化
    router.route(_event(producer="n1"))
    assert cc.reset_count == 2


def test_stub_mode_structure_change_is_noop():
    g = Graph()
    g.add_node(_node("n1"))
    router = AICURouter(aicu_instance=_FakeAICU())
    router.attach_graph(g)

    router.route(_event())                      # 初始化 epoch
    g.add_node(_node("n2"))                     # 结构变化
    router.route(_event())                      # stub 无会话，不 crash
    assert router._structure_epoch == g.structure_version
