"""
tests/test_goal_context.py — 两级目标的注入：团队目标（根节点 task 全文）+ 个人当前目标

每个节点的 prompt 里必须同时有两级目标：

  - **团队目标** —— 整棵树的顶层目标，即根节点**当前**的 task；
  - **个人当前目标** —— 本节点这一刻要执行的任务，即 `node.task`。

两者原先都被截断过，而且截断处**没有任何标记**说明后面还有内容：

  | 位置 | 旧行为 |
  |---|---|
  | Worker 的「## 项目目标」块（`_build_worker_context`） | `root_task[:300]` |
  | Manager 快照里的团队目标（只有祖先链第一行） | 130 字 |
  | Manager 快照里的本节点任务 | 130 字 |
  | Manager 增量续跑块里的「原任务」 | `original[:300]` |

砍在半句上的目标比不给目标更坏：节点会把残缺的那半句当成目标本身，而且它无从察觉。
所以下面断言的是**尾部**——截断恰好发生在后半段，`_TAIL` 出现在文本里就说明没被砍。

同时钉住**标签**：两级目标必须各自点明自己是哪一级，节点才分得清。

  | 级别 | Worker | Manager |
  |---|---|---|
  | 团队 | `## 项目目标（团队目标）` | `### 〇、团队目标(根节点任务全文)` |
  | 个人 | `## 你的任务（个人当前目标）` | 快照 `个人当前目标:` + prompt 末尾 `## 你的任务(个人当前目标)` |

Manager 末尾那个标题原本叫「(本节点原始 Task 描述)」——那是个错误：`node.task` 是
**可变**的「现在该执行什么」槽位，经一次 Revise 后它就是增量任务，而真正的原任务由同一条
prompt 上方的「## 增量续跑任务」块标为「原任务」给出。旧标题会和那个「原任务」互相打脸。

有意保留截断的地方：祖先链 / 兄弟列表里**别人**的 task（130/120/100 字）。那是索引，
不是正文；有了独立的团队目标段之后，它不再承担传递目标的职责。见
`TestManagerSnapshot::test_team_goal_appears_exactly_once`。

不覆盖：续接轮（CC `--resume` 保留完整对话历史）不重建目标块，见
`Worker._build_resume_task_body` 的 docstring。
"""

import pytest

from executables.base import Executable, ExecutableContext
from executables.manager import Manager
from executables.worker import Worker
from runtime.models import ExecutionPlan, NodeStatus, Step
from runtime.runtime import Runtime


class _TestExecutable(Executable):
    """Minimal executable for scheduler (auto-completes immediately)."""

    def __init__(self, context=None, **kwargs):
        super().__init__()

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.COMPLETED


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


# 尾部哨兵 + 填充：目标要比旧的 300 字上限长得多，让截断一定发生在哨兵之前。
# 断言盯哨兵而不是盯整串，才能把「悄悄砍掉」和「没砍」区分开。
_FILLER = "本段用于把目标撑到远超旧的 300 字上限。"
GOAL_TAIL = "【目标最后一句：交付物须包含 README 与可复现的验收脚本】"
LONG_GOAL = "实现 aicu-hub —— AICU 扩展资产的开放平台。\n" + _FILLER * 20 + "\n" + GOAL_TAIL

TASK_TAIL = "【本节点任务的最后一句：产出模块清单与接口签名表】"
LONG_TASK = "设计后端分层与接口。\n" + _FILLER * 15 + "\n" + TASK_TAIL


# ============================================================================
# 1. Worker：团队目标 + 个人当前目标
# ============================================================================

class TestWorkerGoalContext:
    def _worker_ctx(self, runtime, node):
        return ExecutableContext(node=node, runtime=runtime, config=node.config)

    def test_team_goal_is_injected_in_full(self, runtime):
        """根任务全文进「## 项目目标（团队目标）」，尾部不丢（旧的 [:300] 会丢掉）。"""
        root = runtime.submit_root_manager(LONG_GOAL)
        node = runtime.inject_plan(root, ExecutionPlan(steps=[_step("be", {})]))[0]
        text = Worker._build_worker_context(self._worker_ctx(runtime, node))

        assert "## 项目目标（团队目标）" in text
        assert LONG_GOAL in text
        assert GOAL_TAIL in text

    def test_topmost_ancestor_is_the_one_used(self, runtime):
        """三层：最底层 Worker 拿到的是**根**任务的全文，不是它父 Manager 的 task。"""
        root = runtime.submit_root_manager(LONG_GOAL)
        sub = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("sub", {}, exec_type="manager", task=LONG_TASK),
        ]))[0]
        leaf = runtime.inject_plan(sub.id, ExecutionPlan(steps=[_step("leaf", {})]))[0]

        text = Worker._build_worker_context(self._worker_ctx(runtime, leaf))

        assert GOAL_TAIL in text
        assert LONG_TASK not in text  # 父 Manager 的任务不得顶替团队目标

    def test_compose_task_body_carries_both_levels(self, runtime):
        """最终喂给 CC 的任务体：团队目标（前缀）与个人当前目标（末尾）都在，都是全文。"""
        root = runtime.submit_root_manager(LONG_GOAL)
        node = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("be", {}, task=LONG_TASK),
        ]))[0]
        ctx = self._worker_ctx(runtime, node)

        body = Worker._compose_task_body(
            [Worker._build_worker_context(ctx)], ctx.node.task
        )
        lines = body.splitlines()

        # 按**整行**匹配，不用子串：目标块末尾那句交叉引用里也含「## 你的任务（个人
        # 当前目标）」，子串断言会被它满足 —— 标题即使被删掉测试照样过（变异检验抓到过）。
        assert "## 项目目标（团队目标）" in lines
        assert "## 你的任务（个人当前目标）" in lines
        assert GOAL_TAIL in body
        assert LONG_TASK in body       # 个人任务原样追加，一字不差
        assert TASK_TAIL in body
        # 目标块末尾那句话必须把两级目标的**名字**都点出来，节点才知道谁是谁
        assert "以上是**团队目标**" in body

    def test_compose_task_body_without_context_returns_task_only(self):
        assert Worker._compose_task_body([], "只有任务") == "只有任务"


# ============================================================================
# 2. Manager：快照里的团队目标段 + 本节点 task 全文
# ============================================================================

class TestManagerSnapshot:
    def _sub_manager(self, runtime):
        """root(长任务) → sub manager(长任务)，返回 (root_id, sub_node)。"""
        root = runtime.submit_root_manager(LONG_GOAL)
        sub = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("sub", {}, exec_type="manager", task=LONG_TASK),
        ]))[0]
        return root, sub

    def _snapshot(self, runtime, node):
        mgr = Manager.__new__(Manager)
        ctx = ExecutableContext(node=node, runtime=runtime, config=node.config)
        return mgr._build_execution_context_snapshot(ctx)

    def test_team_goal_has_its_own_full_text_section(self, runtime):
        _, sub = self._sub_manager(runtime)
        snap = self._snapshot(runtime, sub)

        # headline 按整行匹配，避免被别处的提及混过去（同 TestWorkerGoalContext 的说明）
        assert "### 〇、团队目标(根节点任务全文)" in snap.splitlines()
        assert LONG_GOAL in snap
        assert GOAL_TAIL in snap

    def test_personal_goal_is_labelled_and_full(self, runtime):
        """本节点自己的 task 不再砍到 130 字，且标签点明这是「个人当前目标」
        （逐行缩进，所以断言盯尾行）。"""
        _, sub = self._sub_manager(runtime)
        snap = self._snapshot(runtime, sub)

        assert "个人当前目标:" in [line.strip() for line in snap.splitlines()]
        assert TASK_TAIL in snap
        # 与 Part0 的「团队目标」成对出现
        assert "团队目标" in snap

    def test_team_goal_appears_exactly_once(self, runtime):
        """祖先链里**别人**的 task 仍然是被截断的索引 —— 全文只在 Part0 出现一次。"""
        root = runtime.submit_root_manager(LONG_GOAL)
        sub = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("sub", {}, exec_type="manager", task=LONG_TASK),
        ]))[0]
        leaf = runtime.inject_plan(sub.id, ExecutionPlan(steps=[
            _step("leaf", {}, exec_type="manager"),
        ]))[0]
        snap = self._snapshot(runtime, leaf)

        assert GOAL_TAIL in snap               # Part0 给的是全文
        assert snap.count(LONG_GOAL) == 1      # 链上那一行是截断后的索引，不是第二份全文

    def test_root_manager_skips_the_section(self, runtime):
        """根 Manager 自己就是团队目标的出处，不重复给一遍：
        它的 task 由 `_build_expand_prompt` 拼在 prompt 末尾的「## 你的任务」里。"""
        root = runtime.submit_root_manager(LONG_GOAL)
        snap = self._snapshot(runtime, runtime.get_node(root))

        assert "### 〇、团队目标" not in snap
        # 祖先链为空 → 也不会有「个人当前目标」行
        assert "(无 —— 本节点为根 Manager)" in snap


# ============================================================================
# 3. Manager 增量续跑块里的「原任务」
# ============================================================================

class TestManagerOriginalTask:
    def test_original_task_is_shown_in_full(self, runtime):
        """原任务与增量任务是一对：要让 Manager 判断哪些既有子节点能覆盖增量，
        被砍掉后半段的原任务是没法比的。旧代码是 original[:300]。"""
        root = runtime.submit_root_manager(LONG_GOAL)
        sub = runtime.inject_plan(root, ExecutionPlan(steps=[
            _step("sub", {}, exec_type="manager", task="短任务"),
        ]))[0]
        # revise 块只在「本节点有子节点」或 pending_revise 时出现
        runtime.inject_plan(sub.id, ExecutionPlan(steps=[_step("leaf", {})]))

        node = runtime.get_node(sub.id)
        node.pending_revise = True
        node.pending_context = {
            "original_task": LONG_TASK,
            "incremental_task": "只加一个字段",
        }
        mgr = Manager.__new__(Manager)
        ctx = ExecutableContext(node=node, runtime=runtime, config=node.config)

        prompt = mgr._build_expand_prompt(ctx)

        assert "## 增量续跑任务" in prompt
        assert LONG_TASK in prompt
        assert TASK_TAIL in prompt
