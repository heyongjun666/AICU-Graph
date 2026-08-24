"""metrics.py — Resolver 打点，JSONL 落盘（A/B 验证的数据源）。

字段：
- injected_bytes / resolver_ms / group_queries / project_queries / graph_builds：Resolver 内确定性测量
- estimated_baseline_dependency_tokens：未开 codegraph 时依赖文本注入的估算 token（对比参考，
  基线本有分层读取/按需 Read，故不宣称"实际节省 N token"）
"""

import json
import os
import time
from typing import Optional


def _workspace_root(node) -> Optional[str]:
    try:
        return (node.config or {}).get("_workspace_root") or os.getcwd()
    except Exception:  # noqa: BLE001
        return None


def record(node, role: str, *, injected_bytes: int = 0,
           resolver_ms: int = 0, group_queries: int = 0,
           project_queries: int = 0, graph_builds: int = 0,
           estimated_baseline_dependency_tokens: int = 0) -> None:
    """追加一行 JSON 到 {workspace_root}/codegraph_metrics.jsonl。失败静默。"""
    try:
        root = _workspace_root(node)
        if not root:
            return
        entry = {
            "node_id": getattr(node, "id", None),
            "role": role,
            "enabled": True,
            "ts": time.time(),
            "injected_bytes": injected_bytes,
            "resolver_ms": resolver_ms,
            "group_queries": group_queries,
            "project_queries": project_queries,
            "graph_builds": graph_builds,
            "estimated_baseline_dependency_tokens": estimated_baseline_dependency_tokens,
        }
        path = os.path.join(root, "codegraph_metrics.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
