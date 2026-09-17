"""
tests/test_event_queue_unified.py — V3.1 P0#4 Event Queue 统一测试

每个 Node 都是 Producer + Consumer，各有自己的 event queue：
  Node A publish → Event Bus → Node B.event_queue → Node B 下一次执行时消费。

实现：不新增存储类，收敛既有 4 目录物理布局为统一契约 ``event_queue_dirs(node)``
（outbox / inbox / live），worker/manager prompt 构建器与投递侧共用；Manager 补
事件收件箱块（复用 Worker._build_event_inbox_instructions 的 empty-guard）。

覆盖:
  1. event_queue_dirs 三目录契约与 worker 发布块路径一致
  2. Manager expand prompt：有订阅 + inbox 有事件 → 注入「事件收件箱」块
  3. Manager expand prompt：config={} + 空 inbox → 不注入（字节回归守卫）
  4. Manager review prompt：有待处理事件 → 注入
  5. Manager review prompt：config={} + 空 inbox → 不注入
  6. executables.worker 不再有本地 _node_workspace 定义，块构建器走共享契约
  7. list_inbox 只读返回待处理事件，不删除文件
"""

import json
import os
from types import SimpleNamespace

import pytest

from executables.base import Executable, ExecutableContext
from executables.manager import Manager
from executables.worker import Worker
from runtime.agent_events import (
    _node_workspace,
    event_queue_dirs,
    list_inbox,
)
from runtime.models import ExecutionPlan, NodeStatus, Step
from runtime.runtime import Runtime


class _TestExecutable(Executable):
    """Minimal executable (auto-completes immediately)."""

    def __init__(self, context=None, **kwargs):
        super().__init__()

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.COMPLETED


def _step(step_id, config=None, **kw):
    kw.setdefault("exec_type", "worker")
    kw.setdefault("role", "default")
    kw.setdefault("task", f"task {step_id}")
    kw.setdefault("task_summary", f"summary {step_id}")
    kw.setdefault("depends_on", [])
    return Step(step_id=step_id, config=config or {}, **kw)


@pytest.fixture
def runtime(tmp_path):
    rt = Runtime(workspace_root=str(tmp_path))
    rt.registry.register("manager", _TestExecutable)
    rt.registry.register("worker", _TestExecutable)
    return rt


def _write_json_atomic(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _node_like(workspace_root: str, relpath: str, **kw) -> SimpleNamespace:
    config = {
        "_workspace_root": os.path.abspath(workspace_root),
        "_workspace_relpath": relpath,
        **(kw.pop("config", {}) or {}),
    }
    return SimpleNamespace(config=config, **kw)


# ============================================================================
# 1. event_queue_dirs 契约
# ============================================================================

class TestEventQueueDirsContract:
    def test_three_dirs_match_delivery_layout(self):
        node = _node_like("/tmp/ws", "nodes/n1")
        dirs = event_queue_dirs(node)
        ws = os.path.realpath("/tmp/ws/nodes/n1")
        assert dirs["outbox"] == os.path.join(ws, "events")
        assert dirs["inbox"] == os.path.join(ws, "event_inbox")
        assert dirs["live"] == os.path.join(ws, "events_inbox")

    def test_worker_publish_block_uses_outbox_contract(self):
        node = _node_like("/tmp/ws", "nodes/n1",
                          config={"events": {"publish": ["*"]}})
        text = Worker._build_event_publish_instructions(node)
        expected = event_queue_dirs(node)["outbox"]
        assert f"发布方式：在 `{expected}` 目录" in text

    def test_inbox_block_uses_contract_paths(self, tmp_path):
        ws = str(tmp_path)
        node = _node_like(ws, "nodes/n1",
                          config={"events": {"subscribe": ["sig"]}})
        dirs = event_queue_dirs(node)
        # 空守卫：inbox 无待处理事件 → 不注入块（live/inbox 路径均不出现）
        text = Worker._build_event_inbox_instructions(node)
        assert text == ""
        # 写入待处理事件 → 块注入，inbox 与 live 路径均出现
        os.makedirs(dirs["inbox"], exist_ok=True)
        _write_json_atomic(os.path.join(dirs["inbox"], "evt_a.json"),
                           {"event_id": "evt_a", "producer": "x",
                            "content": "sig 事件：/ws/shared/app/backend/api.yaml"})
        text2 = Worker._build_event_inbox_instructions(node)
        assert dirs["inbox"] in text2
        assert dirs["live"] in text2
        assert "evt_a" in text2
        assert "sig 事件" in text2


# ============================================================================
# 2-5. Manager 事件收件箱块
# ============================================================================

class TestManagerInboxBlock:
    def test_expand_prompt_includes_inbox_when_subscribed(self, runtime):
        root = runtime.submit_root_manager("build app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("mgr", {"events": {"subscribe": ["sig"]}},
                  exec_type="manager", role="architecture"),
        ]))
        mgr_node = nodes[0]
        _write_json_atomic(
            os.path.join(runtime.get_node_workspace(mgr_node.id),
                         "event_inbox", "evt_sig.json"),
            {"event_id": "evt_sig", "type": "sig",
             "producer": "other", "payload": {"msg": "hi"}},
        )
        mgr = Manager.__new__(Manager)
        ctx = ExecutableContext(node=mgr_node, runtime=runtime, config=mgr_node.config)
        prompt = mgr._build_expand_prompt(ctx)
        assert "事件收件箱" in prompt
        assert "evt_sig" in prompt

    def test_expand_prompt_omits_when_empty(self, runtime):
        root = runtime.submit_root_manager("build app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("mgr", {}, exec_type="manager", role="architecture"),
        ]))
        mgr_node = nodes[0]
        mgr = Manager.__new__(Manager)
        ctx = ExecutableContext(node=mgr_node, runtime=runtime, config=mgr_node.config)
        prompt = mgr._build_expand_prompt(ctx)
        # 字节回归守卫：无订阅 + 空 inbox → 不注入含 Runtime 内部词汇的块
        assert "事件收件箱" not in prompt
        for forbidden in ("event_inbox", "events_inbox"):
            assert forbidden not in prompt

    def test_review_prompt_includes_inbox_when_pending(self, runtime):
        root = runtime.submit_root_manager("build app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("mgr", {}, exec_type="manager", role="architecture"),
        ]))
        mgr_node = nodes[0]
        _write_json_atomic(
            os.path.join(runtime.get_node_workspace(mgr_node.id),
                         "event_inbox", "evt_sig.json"),
            {"event_id": "evt_sig", "type": "collaboration_request",
             "producer": "other", "payload": {"task": "review plan"}},
        )
        mgr = Manager.__new__(Manager)
        ctx = ExecutableContext(node=mgr_node, runtime=runtime, config=mgr_node.config)
        prompt = mgr._build_review_prompt(ctx, children_info=[])
        assert "事件收件箱" in prompt
        assert "evt_sig" in prompt

    def test_review_prompt_omits_when_empty(self, runtime):
        root = runtime.submit_root_manager("build app")
        nodes = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("mgr", {}, exec_type="manager", role="architecture"),
        ]))
        mgr_node = nodes[0]
        mgr = Manager.__new__(Manager)
        ctx = ExecutableContext(node=mgr_node, runtime=runtime, config=mgr_node.config)
        prompt = mgr._build_review_prompt(ctx, children_info=[])
        assert "事件收件箱" not in prompt
        for forbidden in ("event_inbox", "events_inbox"):
            assert forbidden not in prompt


# ============================================================================
# 6-7. _node_workspace 去重 / list_inbox 只读
# ============================================================================

class TestNodeWorkspaceDedupAndListInbox:
    def test_worker_no_local_node_workspace_definition(self):
        import executables.worker as worker_mod
        assert not hasattr(worker_mod.Worker, "_node_workspace")
        # 行为一致：worker 侧解析与 agent_events 权威实现等价
        node = _node_like("/tmp/ws", "nodes/n1")
        assert os.path.realpath("/tmp/ws/nodes/n1") == _node_workspace(node)

    def test_list_inbox_read_only(self, tmp_path):
        ws = str(tmp_path)
        node = _node_like(ws, "nodes/n1")
        inbox = os.path.join(_node_workspace(node), "event_inbox")
        os.makedirs(inbox, exist_ok=True)
        p1 = os.path.join(inbox, "evt_a.json")
        p2 = os.path.join(inbox, "evt_b.json")
        _write_json_atomic(p1, {"event_id": "evt_a", "type": "t1", "producer": "x"})
        _write_json_atomic(p2, {"event_id": "evt_b", "type": "t2", "producer": "y"})
        events = list_inbox(node)
        assert [e["event_id"] for e in events] == ["evt_a", "evt_b"]
        # 只读：不删除文件
        assert os.path.isfile(p1) and os.path.isfile(p2)

    def test_list_inbox_empty_when_no_dir(self, tmp_path):
        node = _node_like(str(tmp_path), "nodes/n1")
        assert list_inbox(node) == []
