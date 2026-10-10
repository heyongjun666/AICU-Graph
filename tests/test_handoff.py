"""新会话交接（runtime/handoff.py + executables/handoff_prompts.py）测试。

覆盖：检查点字段、笔记落点与读取、两段提示词文案、Scheduler 对 Worker 事件重激活的
fresh 标记、Worker.run 的会话策略（fresh 弹缓存 / 普通激活仍续接）。
"""

import os
from typing import Any

from executables.base import ExecutableContext
from executables.handoff_prompts import render_checkpoint, render_notes_instructions
from executables.registry import ExecutableRegistry
from executables.worker import Worker
from runtime.graph import Graph
from runtime.handoff import (
    NOTES_FILENAME,
    SESSION_FRESH,
    fresh_session_fields,
    notes_path,
    read_notes,
)
from runtime.models import Node, NodeStatus
from runtime.scheduler import Scheduler
from tests.test_event_reactivation_bridge import (
    MockDelegate,
    MockGateway,
    _feedback_event,
    _make_node,
    _write_event,
)


def _worker_node(tmp_path, **kwargs) -> Node:
    return _make_node("W", str(tmp_path), **kwargs)


class TestHandoffData:

    def test_fresh_fields_carry_task_and_artifact(self, tmp_path):
        node = _worker_node(tmp_path)
        node.artifact_path = "cc_0/out"
        assert fresh_session_fields(node) == {
            "session_mode": SESSION_FRESH,
            "original_task": "Task W",
            "prior_artifacts": ["cc_0/out"],
        }

    def test_fresh_fields_without_artifact(self, tmp_path):
        node = _worker_node(tmp_path)
        node.artifact_path = None
        assert fresh_session_fields(node)["prior_artifacts"] == []

    def test_notes_live_in_node_workspace_root(self, tmp_path):
        node = _worker_node(tmp_path)
        path = notes_path(node)
        ws = os.path.realpath(os.path.join(str(tmp_path), "nodes", "W"))
        assert path == os.path.join(ws, NOTES_FILENAME)
        # 不在 cc_{i}/ 下：交付物扫描看不到它
        assert os.path.dirname(path) == ws

    def test_read_notes_missing_is_empty(self, tmp_path):
        assert read_notes(_worker_node(tmp_path)) == ""

    def test_read_notes_returns_stripped_content(self, tmp_path):
        node = _worker_node(tmp_path)
        os.makedirs(os.path.dirname(notes_path(node)), exist_ok=True)
        with open(notes_path(node), "w", encoding="utf-8") as f:
            f.write("\n## 已确认的结论\n- X\n\n")
        assert read_notes(node) == "## 已确认的结论\n- X"


class TestHandoffPrompts:

    def test_notes_instructions_contain_path_and_sections(self):
        text = render_notes_instructions("/ws/W/handoff_notes.md")
        assert "/ws/W/handoff_notes.md" in text
        for section in (
            "已确认的结论", "已做的决定和理由", "已试过但不行的做法",
            "未决问题", "必须遵守的约束", "最近一次验证",
        ):
            assert section in text
        assert "[已验证]" in text and "[推测]" in text
        assert "依据位置" in text
        assert "增量更新" in text
        assert "不是交付物" in text

    def test_checkpoint_with_notes(self):
        text = render_checkpoint(
            {"original_task": "ORIG", "prior_artifacts": ["cc_0/out"]},
            "NOTES_BODY",
        )
        assert "ORIG" in text and "cc_0/out" in text
        assert "上一轮工作笔记" in text and "NOTES_BODY" in text
        assert "以实际情况为准" in text
        # 提示怎么用笔记 + 收尾时要更新
        assert "[已验证]" in text and "[推测]" in text
        assert "更新" in text

    def test_checkpoint_without_notes_has_no_notes_section(self):
        text = render_checkpoint({"original_task": "ORIG"}, "")
        assert "ORIG" in text
        assert "上一轮工作笔记" not in text


class TestSchedulerMarksWorkerEventsFresh:

    def _scheduler(self, tmp_path, *nodes):
        graph = Graph()
        for n in nodes:
            graph.add_node(n)
        return Scheduler(
            graph, ExecutableRegistry(), MockDelegate(), MockGateway(),
            event_batch_threshold=1,
        )

    def test_worker_event_activation_is_fresh(self, tmp_path):
        worker = _worker_node(tmp_path)
        worker.artifact_path = "cc_0/out"
        scheduler = self._scheduler(tmp_path, worker)
        _write_event(str(tmp_path), "W", _feedback_event("P"))
        scheduler._process_reactivation_events()

        ctx = worker.pending_context
        assert ctx["session_mode"] == SESSION_FRESH
        assert ctx["original_task"] == "Task W"
        assert ctx["prior_artifacts"] == ["cc_0/out"]
        assert ctx["event_batch"]

    def test_manager_event_activation_is_not_marked(self, tmp_path):
        manager = _make_node("M", str(tmp_path), exec_type="manager",
                             role="engineering_coord")
        scheduler = self._scheduler(tmp_path, manager)
        _write_event(str(tmp_path), "M", _feedback_event("P"))
        scheduler._process_reactivation_events()

        assert "session_mode" not in (manager.pending_context or {})


class _RecordingAsk:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.prompts: list[str] = []

    def ask(self, prompt: str) -> dict:
        self.prompts.append(prompt)
        return {"final_output_path": os.path.join(self.workspace, "result.md")}


class _Runtime:
    def __init__(self, root: str):
        self.root = root

    def update_node_status(self, node_id: str, status: NodeStatus, **updates: Any) -> None:
        return

    def get_node_workspace(self, node_id: str) -> str:
        return self.root

    def get_node(self, node_id: str):
        return None

    def flush_node_events(self, node_id: str) -> None:
        return

    def append_node_log(self, *args: Any, **kwargs: Any) -> None:
        return

    def update_cc_activity(self, *args: Any, **kwargs: Any) -> None:
        return


class TestWorkerSessionPolicy:

    def _run(self, tmp_path, pending: dict, notes: str = ""):
        # activation_count>0：模拟「已被激活过」的节点，finally 不会清缓存
        node = _worker_node(tmp_path, status=NodeStatus.WAITING, activation_count=1)
        node.pending_context = pending
        if notes:
            os.makedirs(os.path.dirname(notes_path(node)), exist_ok=True)
            with open(notes_path(node), "w", encoding="utf-8") as f:
                f.write(notes)
        ctx = ExecutableContext(node=node, runtime=_Runtime(str(tmp_path)))
        ask = _RecordingAsk(str(tmp_path))
        sentinel = object()
        with Worker._aicu_cache_lock:
            Worker._aicu_cache[node.id] = sentinel
        try:
            Worker(context=ctx, aicu_instance=ask).run(ctx)
            with Worker._aicu_cache_lock:
                evicted = Worker._aicu_cache.get(node.id) is not sentinel
        finally:
            with Worker._aicu_cache_lock:
                Worker._aicu_cache.pop(node.id, None)
        return ask.prompts[0], evicted

    def test_fresh_event_activation_drops_session_and_injects_checkpoint(self, tmp_path):
        pending = {
            **fresh_session_fields(_worker_node(tmp_path)),
            "event_batch": [{"event_id": "e1", "producer": "P",
                             "objective": "EVENT_OBJECTIVE"}],
            "activated_by": "P",
        }
        prompt, evicted = self._run(tmp_path, pending, notes="PRIOR_NOTES")
        assert evicted
        assert "新会话检查点" in prompt
        assert "PRIOR_NOTES" in prompt
        assert "EVENT_OBJECTIVE" in prompt
        assert "会话续接" not in prompt
        # 检查点字段不会被通用反馈段重复倾倒
        assert "**session_mode**" not in prompt
        assert "**original_task**" not in prompt
        # 新会话同样要求写笔记
        assert NOTES_FILENAME in prompt

    def test_plain_activation_still_resumes(self, tmp_path):
        pending = {"activated_by": "P", "event_content": "fix it"}
        prompt, evicted = self._run(tmp_path, pending)
        assert not evicted
        assert "会话续接" in prompt
        assert "新会话检查点" not in prompt
        assert NOTES_FILENAME in prompt
