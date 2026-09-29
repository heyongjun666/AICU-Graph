"""Tests for 节点任务链（Node.task_history）— 记住 task 被谁、为什么顶掉。

背景：`task` 不是「这个节点当初被派了什么活」，而是「现在该执行什么」的可变槽位。
Revise / 增量复用 / 评审修复 / 界面改任务 都会把它整个盖掉，而盖掉之后原任务在整个
系统里再无留痕 —— `pending_context` 开跑即清空、`LogEntry.task` 生产路径从不填、
修复路径还会顺手 log=[]。于是概览页上一个被改过三次的节点，只能看到最后一次的增量
任务，看不出它本来是干什么的。

覆盖：
1.  连续两次 Revise：链条第一条是**最初**那条（不是第一次的增量）
2.  _reactivate_reused_worker 的 reason 标记（reuse / revise / retry）
3.  submit_fix_plan：fix task 与子节点相同时**不记**（计划常原样回显），不同时记 repair
4.  update_manual_node 只改 config、task 原样重发 → 不记（界面编辑框每次保存都重发）
5.  链长封顶 20，丢最旧
6.  落盘往返后链条完整
7.  get_graph_snapshot 带 task_history
8.  旧 graph.json（无该字段）恢复成 []，且不报 unknown-field 警告
9.  单条按 MAX_TASK_LENGTH 截断
10. update_node_status 拒收 task（防将来有人从透传口绕过任务链）
"""

import json
import warnings

import pytest

from runtime.models import (
    MAX_TASK_LENGTH,
    Edge,
    EdgeType,
    Node,
    NodeStatus,
    Step,
    superseded_task_history,
)
from runtime.persistence import Persistence
from runtime.runtime import Runtime


# ============================================================================
# Helpers
# ============================================================================

def _make_runtime(tmp_path) -> Runtime:
    rt = Runtime(
        workspace_root=str(tmp_path),
        persistence=Persistence(file_path=str(tmp_path / "state.json")),
    )
    # 关闭后台 tick 线程，使断言确定性（revise 相关方法内部调用 scheduler.start()）
    rt.scheduler.start = lambda: None  # type: ignore[method-assign]
    return rt


def _completed_root(rt: Runtime, task: str = "Build feature") -> str:
    mgr_id = rt.submit_root_manager(task=task, role="default")
    rt.graph.update_node(mgr_id, status=NodeStatus.COMPLETED)
    return mgr_id


def _worker_node(node_id: str, parent_id, task: str = "Task W1",
                 status: NodeStatus = NodeStatus.COMPLETED,
                 artifact_path=None) -> Node:
    return Node(
        id=node_id,
        parent_id=parent_id,
        exec_type="worker",
        role="dev",
        task=task,
        task_summary=f"summary of {node_id}",
        config={},
        status=status,
        step_ref=node_id,
        artifact_path=artifact_path,
    )


def _step(task: str, role: str = "dev") -> Step:
    return Step(step_id="s1", exec_type="worker", role=role, task=task)


# ============================================================================
# 1. 报的这个 bug 本体：Revise 之后看不到原任务
# ============================================================================

class TestReviseRecordsSupersededTask:

    def test_two_revises_keep_the_true_original(self, tmp_path):
        """连续两次 Revise 后，链条第一条必须是**最初**那条。

        这是整件事的出发点：用户在概览页看到的是第二次的增量任务，
        他要知道「这个节点本来是干什么的」。
        """
        rt = _make_runtime(tmp_path)
        mgr_id = _completed_root(rt, task="Build feature")

        rt.revise_manager_node(mgr_id, "Add pagination")
        rt.graph.update_node(mgr_id, status=NodeStatus.COMPLETED)
        rt.revise_manager_node(mgr_id, "Add dark mode")

        node = rt.graph.get_node(mgr_id)
        assert node.task == "Add dark mode"
        assert [e["task"] for e in node.task_history] == [
            "Build feature", "Add pagination",
        ]
        assert [e["reason"] for e in node.task_history] == ["revise", "revise"]
        # 时间正序：最早的在前，最新的在最后
        assert node.task_history[0]["at"] <= node.task_history[1]["at"]

    def test_pending_context_original_task_is_untouched(self, tmp_path):
        """任务链是**旁路**，不取代 pending_context["original_task"]。

        那条是喂给 Manager 提示词的（executables/manager.py 的「原任务」行），
        有自己的既有语义与测试，两者并存。
        """
        rt = _make_runtime(tmp_path)
        mgr_id = _completed_root(rt, task="Build feature")
        rt.revise_manager_node(mgr_id, "Add pagination")
        node = rt.graph.get_node(mgr_id)
        assert node.pending_context["original_task"] == "Build feature"
        assert node.task_history[0]["task"] == "Build feature"


# ============================================================================
# 2. _reactivate_reused_worker：三条调用链在这里合流，reason 在源头分开
# ============================================================================

class TestReactivateReusedWorkerReason:

    def _harness(self, tmp_path):
        rt = _make_runtime(tmp_path)
        rt.graph.add_node(Node(
            id="mgr", exec_type="manager", role="default", task="root",
            config={}, status=NodeStatus.WAITING, step_ref="mgr",
        ))
        child = _worker_node("W1", "mgr")
        rt.graph.add_node(child)
        rt.graph.add_edge(Edge(
            id="e1", type=EdgeType.PARENT, from_node="mgr", to_node="W1",
        ))
        return rt, child

    def _prepared(self, rt, node_id):
        return rt._prepare_node_config(node_id, {}, "mgr")

    def test_default_reason_is_reuse(self, tmp_path):
        rt, child = self._harness(tmp_path)
        rt._reactivate_reused_worker(
            child, _step("incremental"), self._prepared(rt, child.id)
        )
        node = rt.graph.get_node(child.id)
        assert [e["reason"] for e in node.task_history] == ["reuse"]
        assert node.task_history[0]["task"] == "Task W1"

    def test_reason_can_be_overridden_per_caller(self, tmp_path):
        """revise_worker_node 传 revise、自动重试传 retry —— 同一段代码，标记不同。"""
        for reason in ("revise", "retry"):
            rt, child = self._harness(tmp_path)
            rt._reactivate_reused_worker(
                child, _step("incremental"), self._prepared(rt, child.id),
                reason=reason,
            )
            node = rt.graph.get_node(child.id)
            assert [e["reason"] for e in node.task_history] == [reason]

    def test_revise_worker_node_defaults_to_revise(self, tmp_path):
        rt, child = self._harness(tmp_path)
        rt.revise_worker_node(child.id, "incremental")
        node = rt.graph.get_node(child.id)
        assert [e["reason"] for e in node.task_history] == ["revise"]


# ============================================================================
# 3. 评审修复：计划常原样回显子节点 task，那种不算改动
# ============================================================================

class TestSubmitFixPlanRecordsRepair:

    def _harness(self, tmp_path):
        rt = _make_runtime(tmp_path)
        rt.graph.add_node(Node(
            id="root", exec_type="manager", role="engineering_coord",
            task="root task", config={}, status=NodeStatus.WAITING,
            manager_phase="waiting", step_ref="root",
        ))
        rt.graph.add_node(_worker_node(
            "calc", "root", task="calc", status=NodeStatus.FAILED,
        ))
        rt.graph.add_edge(Edge(
            id="e1", type=EdgeType.PARENT, from_node="root", to_node="calc",
        ))
        return rt

    def test_differing_fix_task_is_recorded(self, tmp_path):
        rt = self._harness(tmp_path)
        rt.submit_fix_plan(
            "root",
            [{"step_ref": "calc", "reason": "calc bug",
              "fix": {"role": "dev", "task": "fix the calc"}}],
            1,
        )
        node = rt.graph.get_node("calc")
        assert node.task == "fix the calc"
        assert node.task_history == [{
            "task": "calc", "at": node.task_history[0]["at"], "reason": "repair",
        }]

    def test_echoed_fix_task_records_nothing(self, tmp_path):
        """fix.task 与子节点当前 task 逐字相同时不记 —— 否则"历史"里会出现一条
        与当前任务一模一样的条目，等于什么都没说。"""
        rt = self._harness(tmp_path)
        rt.submit_fix_plan(
            "root",
            [{"step_ref": "calc", "reason": "calc bug",
              "fix": {"role": "dev", "task": "calc"}}],
            1,
        )
        assert rt.graph.get_node("calc").task_history == []


# ============================================================================
# 4. 界面改任务：编辑框每次保存都把 task 一起重发
# ============================================================================

class TestManualEdit:

    def _node(self, rt):
        rt.graph.add_node(_worker_node(
            "W1", None, task="original", status=NodeStatus.WAITING,
        ))

    def test_config_only_edit_records_nothing(self, tmp_path):
        """只改模型名也会把原样的 task 发上来。没有这道闸门，改一次配置就多一条
        与当前任务逐字相同的"历史"。"""
        rt = _make_runtime(tmp_path)
        self._node(rt)
        rt.update_manual_node("W1", task="original", config={"model": "x"})
        node = rt.graph.get_node("W1")
        assert node.config["model"] == "x"
        assert node.task_history == []

    def test_changed_task_is_recorded(self, tmp_path):
        rt = _make_runtime(tmp_path)
        self._node(rt)
        rt.update_manual_node("W1", task="rewritten", config={})
        node = rt.graph.get_node("W1")
        assert node.task == "rewritten"
        assert node.task_history == [{
            "task": "original", "at": node.task_history[0]["at"], "reason": "manual",
        }]

    def test_manager_with_children_cannot_downgrade_to_worker(self, tmp_path):
        """有子节点的 manager 禁止改 exec_type 为 worker（子树会悬空、评审卡死）。"""
        rt = _make_runtime(tmp_path)
        mgr = Node(
            id="M1", exec_type="manager", role="engineering_coord",
            task="manage", task_summary="s", config={},
            status=NodeStatus.WAITING, step_ref="M1",
        )
        rt.graph.add_node(mgr)
        rt.graph.add_node(_worker_node("W1", "M1", status=NodeStatus.COMPLETED))
        rt.graph.add_edge(
            Edge(id="e1", type=EdgeType.PARENT, from_node="M1", to_node="W1")
        )

        with pytest.raises(RuntimeError):
            rt.update_manual_node("M1", exec_type="worker", role="code")

        # 不误伤合法修改：改 task / 改同类角色仍可进行
        rt.update_manual_node("M1", task="rewritten", config={})
        assert rt.graph.get_node("M1").task == "rewritten"


# ============================================================================
# 5-9. 模型层面的性质
# ============================================================================

class TestTaskHistoryModel:

    def test_capped_at_max_keeping_newest(self, tmp_path):
        rt = _make_runtime(tmp_path)
        mgr_id = _completed_root(rt, task="gen-0")
        for i in range(1, 26):
            rt.graph.update_node(mgr_id, status=NodeStatus.COMPLETED)
            rt.revise_manager_node(mgr_id, f"gen-{i}")

        node = rt.graph.get_node(mgr_id)
        assert len(node.task_history) == 20
        # 丢的是最旧的，留下的是最近的 20 条
        assert [e["task"] for e in node.task_history] == [
            f"gen-{i}" for i in range(5, 25)
        ]
        assert node.task == "gen-25"

    def test_survives_persistence_roundtrip(self, tmp_path):
        rt = _make_runtime(tmp_path)
        mgr_id = _completed_root(rt, task="Build feature")
        rt.revise_manager_node(mgr_id, "Add pagination")
        before = rt.graph.get_node(mgr_id).task_history

        path = str(tmp_path / "state.json")
        Persistence(file_path=path).save(rt.graph)

        restored = Persistence(file_path=path).load()
        node = restored.nodes[mgr_id]
        assert node.task == "Add pagination"
        assert node.task_history == before

    def test_snapshot_exposes_task_history(self, tmp_path):
        """get_graph_snapshot 是**手写 key 字面量**，不加这一行新字段就到不了前端。"""
        rt = _make_runtime(tmp_path)
        mgr_id = _completed_root(rt, task="Build feature")
        rt.revise_manager_node(mgr_id, "Add pagination")

        snapshot = rt.get_graph_snapshot()
        entry = next(n for n in snapshot["nodes"] if n["id"] == mgr_id)
        assert entry["task_history"] == rt.graph.get_node(mgr_id).task_history

    def test_legacy_payload_without_field_loads_empty(self):
        """旧 graph.json 没有这个字段 → 恢复成 []，且**不**报警告。

        也钉住「创建时不播种」：旧节点不能让一条已经是增量任务的 task 冒充原任务。
        """
        payload = {
            "id": "legacy", "exec_type": "worker", "role": "dev",
            "task": "已经是增量任务了", "config": {}, "status": "Completed",
        }
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            node = Node.from_json(json.dumps(payload))
        assert node.task_history == []
        assert node.task == "已经是增量任务了"

    def test_entry_truncated_to_max_task_length(self):
        """单条按 MAX_TASK_LENGTH 截断。

        UpdateNodeRequest.task 放到 100000，20 条不截断能顶到 2MB/节点，
        而 5000 已经是全系统对 task 的既有上限（Step 那一侧就在截）。
        """
        long_task = "x" * (MAX_TASK_LENGTH + 500)
        entry = superseded_task_history([], long_task, "new", "revise")[0]
        assert entry["task"] == "x" * MAX_TASK_LENGTH
        assert entry["reason"] == "revise"

    def test_unchanged_task_returns_none_not_empty_list(self):
        """必须是 None：写空列表会把已有历史整个清掉。"""
        assert superseded_task_history([], "same", "same", "revise") is None
        assert superseded_task_history([], None, None, "revise") is None
        assert superseded_task_history([], None, "", "revise") is None


# ============================================================================
# 10. 通用透传口的守卫
# ============================================================================

class TestUpdateNodeStatusRejectsTask:

    def test_task_via_status_passthrough_is_rejected(self, tmp_path):
        """update_node_status 是**通用透传**，不是改 task 的入口。

        今天没有调用方传 task，将来有人传就会绕过任务链，让一条任务悄无声息地
        消失。堵在这里，比等它发生再回头查便宜得多。
        """
        rt = _make_runtime(tmp_path)
        rt.graph.add_node(_worker_node(
            "W1", None, task="original", status=NodeStatus.WAITING,
        ))
        with pytest.raises(ValueError, match="does not accept 'task'"):
            rt.update_node_status("W1", NodeStatus.READY, task="sneaky")
