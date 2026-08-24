"""state.py — GroupGraphState 产出组图索引状态机。

第一版做薄：CodeGraph 自己维护自己的 index state，不并入 AICU State Manager。
状态：NOT_EXIST / READY / STALE / BUILDING / FAILED。
转移：NOT_EXIST → BUILDING → READY；READY → STALE → BUILDING → READY；异常 → FAILED。
写操作（init/index/sync）全局串行（锁）。
"""

import os
import threading
from enum import Enum

from .codegraph import _run_cli


class GraphState(str, Enum):
    NOT_EXIST = "NOT_EXIST"
    READY = "READY"
    STALE = "STALE"
    BUILDING = "BUILDING"
    FAILED = "FAILED"


class GroupGraphState:
    """每 shared_root 一个状态。"""

    _lock = threading.Lock()
    _states: dict = {}
    _build_lock = threading.Lock()

    @classmethod
    def get(cls, shared_root: str) -> GraphState:
        with cls._lock:
            return cls._states.get(shared_root, GraphState.NOT_EXIST)

    @classmethod
    def mark_stale(cls, shared_root: str) -> None:
        with cls._lock:
            cls._states[shared_root] = GraphState.STALE

    @classmethod
    def _set(cls, shared_root: str, state: GraphState) -> None:
        with cls._lock:
            cls._states[shared_root] = state

    @classmethod
    def ensure_ready(cls, shared_root: str) -> bool:
        """保证 shared_root 下的产出组图可用；失败返回 False（fail-silent）。

        ⚠️ 必须把 shared_root 作为显式 [path] 传给 codegraph init/sync：
        组目录位于项目根（已有 .codegraph 且 .gitignore 排除 workspaces/）之下，
        若只靠 cwd，codegraph 会 walk-up 命中项目根并**重索引整个项目**。
        """
        with cls._build_lock:  # 建索引全局串行
            state = cls.get(shared_root)
            if state == GraphState.READY:
                return True
            if state in (GraphState.BUILDING, GraphState.FAILED):
                return False
            cls._set(shared_root, GraphState.BUILDING)
            try:
                graph_dir = os.path.join(shared_root, ".codegraph")
                if not os.path.isdir(graph_dir):
                    # init 即构建初始索引（init + index 等价，只做一次）
                    _run_cli(["init", "--force", shared_root], shared_root, timeout=300)
                else:
                    _run_cli(["sync", shared_root], shared_root, timeout=120)
                cls._set(shared_root, GraphState.READY)
                return True
            except Exception:  # noqa: BLE001
                cls._set(shared_root, GraphState.FAILED)
                return False
