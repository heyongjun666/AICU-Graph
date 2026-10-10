"""
runtime/handoff.py — 新会话交接（Fresh-session handoff）

Worker 的 CC 会话（``--resume``）会随激活轮次不断变长，每一轮都要重放整段历史。
对「被事件唤醒、只需处理一批反馈」的节点，更便宜的做法是：开一个**新会话**，
把上一轮的状态以**检查点**的形式交代清楚——

  检查点 = 原任务 + 上一版产出位置（指针）+ 上一轮工作笔记（Agent 自己写的）

本模块是这套机制里**与提示词无关的部分**的唯一事实来源：
  - ``fresh_session_fields``：写进 ``pending_context`` 的检查点字段（Scheduler / Runtime 共用）；
  - ``notes_path`` / ``read_notes``：工作笔记的落点与读取。

提示词文案（检查点怎么渲染、笔记怎么要求 Agent 写）在 ``executables/handoff_prompts.py``。

笔记放在节点私有 workspace 的**根目录**（与 ``events/``、``event_inbox/`` 同级），
不在 ``cc_{i}/`` 也不在共享分区里：交付物扫描只看后两者，笔记不会混进产物。
"""

import os
from typing import Any

from runtime.agent_events import _node_workspace

SESSION_FRESH = "fresh"
NOTES_FILENAME = "handoff_notes.md"


def fresh_session_fields(node: Any) -> dict:
    """下一次执行要以新会话启动时，写进 ``pending_context`` 的检查点字段。

    ``prior_artifacts`` 是相对节点 workspace 的路径（指针，不是内容）：
    上一版产出就在工作区里，由 Agent 按需读取。
    """
    artifact = getattr(node, "artifact_path", None)
    return {
        "session_mode": SESSION_FRESH,
        "original_task": node.task,
        "prior_artifacts": [artifact] if artifact else [],
    }


def notes_path(node: Any) -> str:
    """该节点工作笔记的绝对路径。"""
    return os.path.join(_node_workspace(node), NOTES_FILENAME)


def read_notes(node: Any) -> str:
    """读取上一轮工作笔记；不存在或读取失败返回空串。

    笔记只是增强：Agent 没写、写不全都不能阻塞执行，检查点此时退化为无笔记版本。
    """
    try:
        with open(notes_path(node), "r", encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""
