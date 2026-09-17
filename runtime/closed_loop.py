"""runtime/closed_loop.py — 闭环协作台账（A2）业务产物。

Manager review 逐轮把「缺陷 → 修复 → 验证」累积到节点 `collaboration_ledger`，
全部通过（COMPLETED）时，将完整闭环写成只读业务产物 closed_loop.json，供交付审计
与最终产出完整性核对。此模块不承载 Runtime 状态 —— 状态累积在 Manager 节点，
此处的报告是落盘的业务交付物（A2 原则：business artifact, not runtime state）。
"""

import json
import os
from datetime import datetime
from typing import Any, Optional

# VERDICT 契约（A2 扩展）：测试/验收角色在报告中写入结构化闭环判定块
# `<!-- VERDICT {...} -->`，供失败证据扫描（_scan_failure_evidence）与闭环台账消费。
VERDICT_DOC = """<!-- VERDICT {...} --> 契约（测试/验收角色产出）：
{
  "passed": true/false,
  "defects": [
    {
      "file": "缺陷文件（相对路径或共享分区路径）",
      "line": "行号或范围（可选）",
      "desc": "缺陷描述",
      "expected_action": "期望的正确行为",
      "produced_by": "产出该缺陷的 step_ref（可选，闭环追溯）",
      "fixed_by": "修复该缺陷的 step_ref（可选，闭环追溯）"
    }
  ],
  "cycle": 当前闭环轮次（可选）
}"""


def _status(node: Any) -> str:
    """节点状态转稳定字符串，供台账与清单跨结构比较。

    str Enum 取 ``.name``（如 "COMPLETED"）而非值：两个产物的字段含义相同，
    状态口径必须一致，否则同一节点在 closed_loop.json 与 deliverable_manifest.json
    里会显示成两种写法。缺 status 时返回字符串 "None"（而非 None），保持字段恒为串。
    """
    s = getattr(node, "status", None)
    if s is None:
        return "None"
    return s.name if hasattr(s, "name") else str(s)


def _write_json_into_workspace(
    report: dict,
    workspace: str,
    filename: str,
    output_dir: str,
) -> str:
    """把产物原子写入 {workspace}/{output_dir}/{filename}，返回绝对路径。

    两个交付产物的落盘方式逐字相同，只有文件名不同。写入用 tmp + os.replace
    保证原子（避免读者看到半截 JSON）；此处**有意不 fsync**，与合并前一致 ——
    这两个产物是审计用的业务交付物，不是 Runtime 状态，不需要掉电持久化保证。
    """
    out_dir = os.path.join(workspace, output_dir)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)  # 原子写，避免半截文件
    return path


def build_closed_loop_report(
    manager: Any,
    children: list[Any],
    ledger: Optional[list[dict]] = None,
    escalations: Optional[list[dict]] = None,
) -> dict:
    """构造只读闭环报告（纯函数，不写盘）。

    Args:
        manager: Manager Node（含 collaboration_ledger / iteration_count）。
        children: 最终子节点列表。
        ledger: collaboration_ledger；缺省从 manager 节点读取。
        escalations: C2 升级台账（不可收敛的协作路由）；缺省空列表。

    Returns:
        可 JSON 序列化的闭环报告 dict。
    """
    if ledger is None:
        ledger = list(getattr(manager, "collaboration_ledger", []) or [])
    if escalations is None:
        escalations = list(getattr(manager, "_escalations", []) or [])
    now = datetime.now().isoformat()

    overall_passed = bool(children) and all(
        _status(c) == "COMPLETED" for c in children
    )
    # C2 收敛判定：全部通过且无升级记录 → 完整闭环；否则标记为未完全收敛。
    converged = overall_passed and not escalations
    return {
        "type": "closed_loop",
        "version": 2,
        "generated_at": now,
        "manager": {
            "id": getattr(manager, "id", ""),
            "role": getattr(manager, "role", ""),
            "task": getattr(manager, "task", ""),
            "status": _status(manager),
            "iterations": getattr(manager, "iteration_count", 0),
        },
        "collaboration_ledger": ledger,
        "escalations": escalations,
        "children": [
            {
                "step_ref": c.step_ref or c.id,
                "node_id": getattr(c, "id", ""),
                "role": getattr(c, "role", ""),
                "task": getattr(c, "task", ""),
                "status": _status(c),
                "artifact_path": getattr(c, "artifact_path", None),
                "activation_count": getattr(c, "activation_count", 0),
            }
            for c in children
        ],
        "verdict": {
            "overall_passed": overall_passed,
            "children_total": len(children),
            "converged": converged,
            "escalation_count": len(escalations),
        },
    }


def write_closed_loop_report(
    report: dict,
    workspace: str,
    output_dir: str = "output",
) -> str:
    """把闭环报告原子写入 {workspace}/output/closed_loop.json。

    Args:
        report: build_closed_loop_report 的返回值。
        workspace: Manager 节点 workspace 根目录。
        output_dir: 交付子目录（默认 output，与 worker 产物目录一致）。

    Returns:
        写入的绝对路径。
    """
    return _write_json_into_workspace(report, workspace, "closed_loop.json", output_dir)


def build_deliverable_manifest(
    manager: Any,
    children: list[Any],
    escalations: Optional[list[dict]] = None,
    verify_existence: bool = False,
) -> dict:
    """构造交付物清单（C3：PASSED 时只读业务产物，纯函数不写盘）。

    记录最终子节点产物的完整性：每个子节点必须带非空 artifact_path，
    delivery_complete 表示本 Manager 子树全部产出已落盘。

    Args:
        manager: Manager Node。
        children: 最终子节点列表。
        escalations: C2 升级台账（可选，用于标注未闭合协作）。
        verify_existence: 默认 False 字节级一致（test_closed_loop 宽松语义：
            相对路径/未真实存在仍计 complete）。Assembly 开启时传 True，
            complete/delivery_complete 判真实存在。

    Returns:
        可 JSON 序列化的交付物清单 dict。
    """
    if escalations is None:
        escalations = list(getattr(manager, "_escalations", []) or [])
    now = datetime.now().isoformat()

    items = []
    for c in children:
        ap = getattr(c, "artifact_path", None)
        if verify_existence:
            complete = ap is not None and os.path.exists(ap)
        else:
            complete = ap is not None
        items.append({
            "step_ref": c.step_ref or c.id,
            "node_id": getattr(c, "id", ""),
            "role": getattr(c, "role", ""),
            "exec_type": getattr(c, "exec_type", ""),
            "status": _status(c),
            "artifact_path": ap,
            "shared_workspace": (getattr(c, "config", None) or {}).get(
                "_shared_workspace"
            ),
            "complete": complete,
        })
    return {
        "type": "deliverable_manifest",
        "version": 1,
        "generated_at": now,
        "manager": {
            "id": getattr(manager, "id", ""),
            "role": getattr(manager, "role", ""),
            "task": getattr(manager, "task", ""),
            "status": _status(manager),
        },
        "deliverables": items,
        "escalations": escalations,
        "summary": {
            "total": len(items),
            "complete": sum(1 for i in items if i["complete"]),
            "incomplete": sum(1 for i in items if not i["complete"]),
            "delivery_complete": bool(items) and all(i["complete"] for i in items),
        },
    }


def write_deliverable_manifest(
    report: dict,
    workspace: str,
    output_dir: str = "output",
) -> str:
    """把交付物清单原子写入 {workspace}/output/deliverable_manifest.json。"""
    return _write_json_into_workspace(report, workspace, "deliverable_manifest.json", output_dir)
