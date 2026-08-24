"""runtime/delivery — Delivery Reliability Layer（V3-2）。

原则：
- 确定性、无 LLM；语义交给 LLM，事实交给 Runtime。
- Shared Workspace 是 Source of Truth；Registry 只是 Snapshot（非权威）。
- ``runtime/delivery/`` 只读不写 Node：不调用 update_node_status / 不 mutate
  Node 状态，只返回结构化结果（GateResult / AssemblyResult），状态写回由
  Manager（executables 层）决定并经由 Runtime 完成。
- 全部由 ``AICU_DELIVERY=1`` 伞形门控（默认关，字节级兼容旧会话/旧单测）；
  Phase 1 子门 ``AICU_ARTIFACT_GATE=0`` 单独关闭 Artifact Gate。
"""

import logging
import os

logger = logging.getLogger(__name__)


def delivery_enabled() -> bool:
    """伞形开关：AICU_DELIVERY=1 才启用整套 Delivery 层。"""
    return os.environ.get("AICU_DELIVERY") == "1"


def gate_enabled() -> bool:
    """Artifact Gate 开关：AICU_DELIVERY=1 且 AICU_ARTIFACT_GATE != "0"。"""
    if not delivery_enabled():
        return False
    return os.environ.get("AICU_ARTIFACT_GATE", "1") != "0"


__all__ = ["delivery_enabled", "gate_enabled"]
