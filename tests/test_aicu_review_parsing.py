"""tests/test_aicu_review_parsing.py — 单 CC AICU 的结果形状 + Manager 对新旧结果的兼容。

背景：AICU 已移除全部多 CC 协作模式（pair / triple / full 的节点内自审、修订、
融合、审计）。现在 `AICU` 始终只建一个 CC（`cc_0`），`ask()` / `ask_incremental()`
只走 `_ask_solo` → 一次 `_cc_chat_with_retry`，质量把关全部交给上层 Manager 的
review。

本文件钉住三件事：
1. 单 CC 契约：`len(ccs)==1`、一次 ask 只调用一次 CC、不再自审；
2. `aicu_result.json` 不再包含 audit_passed / audit_defects / scores / avg_scores /
   best_id / consensus_reached / vote_rounds 等多 CC 协作产物；
3. Manager 侧（`_degrade_review` / `_write_review_output`）对**旧版**遗留的
   aicu_result.json（带 audit_* / scores / best_id）与**新版**结果都能容忍。
   旧形状 fixture 是手写的，刻意保持旧形状。
"""

import json
import os
from types import SimpleNamespace

import pytest

from aicu import AICU


# ============================================================================
# Helpers
# ============================================================================

def _make_aicu(tmp_path, role="code"):
    """离线构造真实 AICU（照搬 tests/test_worker_role_prompts.py 的做法）。"""
    ws = str(tmp_path / "ws")
    os.makedirs(ws, exist_ok=True)
    return AICU(
        role_type="worker",
        role=role,
        parent_workspace=ws,
        skills=[],          # 跳过 skills_dir 校验
        modular_output=True,
        output_file=None,
    )


# 旧版 AICU 在多 CC 模式下落盘的 aicu_result.json 形状（手写，刻意保持旧形状）。
_OLD_SHAPED_CLEAN = {
    "final_output_path": "/tmp/final",
    "content": "产出",
    "scores": {"0": {"0": 8.0}},
    "avg_scores": {"0": 8.0},
    "best_id": 0,
    "consensus_reached": True,
    "vote_rounds": 1,
    "audit_passed": True,
    "audit_defects": [],
}
_OLD_SHAPED_WITH_DEFECTS = {
    **_OLD_SHAPED_CLEAN,
    "consensus_reached": False,
    "audit_passed": False,
    "audit_defects": [
        {"aspect": "风险", "description": "缺少回滚方案", "severity": "medium"}
    ],
}

# 多 CC 协作产物键：新版 aicu_result.json 一个都不该有
_REMOVED_RESULT_KEYS = (
    "audit_passed", "audit_defects", "scores", "avg_scores",
    "best_id", "consensus_reached", "vote_rounds",
)

_CHILDREN = [{"step_ref": "s1", "role": "code", "task": "t"}]


# ============================================================================
# 1. 单 CC 契约
# ============================================================================

class TestSoloNoSelfReview:
    def test_solo_skips_self_review(self, tmp_path, monkeypatch):
        """单 CC 只生成一次即交付，不再有自审/修订轮。"""
        a = _make_aicu(tmp_path)
        monkeypatch.setattr(a, "_list_code_files", lambda i: [])
        monkeypatch.setattr(a, "_read_cc_file", lambda i: "")

        calls = []
        a._cc_chat_with_retry = (
            lambda cc, prompt, timeout=7200, **kw: calls.append(prompt) or "产出"
        )

        a.ask("原始问题")

        assert len(calls) == 1, "应只调用一次 CC（仅生成），不得再自审"


def test_ask_builds_exactly_one_cc_and_writes_slim_result(tmp_path, monkeypatch):
    """code 角色 ask()：恰好一个 CC、一次 CC 调用，落盘结果无多 CC 协作产物键。"""
    a = _make_aicu(tmp_path, role="code")
    assert len(a.ccs) == 1
    assert a.ccs[0].workspace.name == "cc_0"
    monkeypatch.setattr(a, "_list_code_files", lambda i: [])
    monkeypatch.setattr(a, "_read_cc_file", lambda i: "")

    calls = []
    a._cc_chat_with_retry = (
        lambda cc, prompt, timeout=7200, **kw: calls.append((cc, prompt)) or "产出"
    )

    result = a.ask("原始问题")

    assert len(a.ccs) == 1
    assert len(calls) == 1, "一次 ask 只应触发一次 _cc_chat_with_retry"
    assert calls[0][0] is a.ccs[0]
    for key in _REMOVED_RESULT_KEYS:
        assert key not in result, f"返回值不应再含 {key}"

    snapshot = json.loads((a.workspace / "aicu_result.json").read_text(encoding="utf-8"))
    for key in _REMOVED_RESULT_KEYS:
        assert key not in snapshot, f"aicu_result.json 不应再含 {key}"
    assert {"final_output_path", "content", "role_config"} <= set(snapshot)


def test_ask_incremental_also_uses_single_cc_call(tmp_path, monkeypatch):
    a = _make_aicu(tmp_path)
    monkeypatch.setattr(a, "_list_code_files", lambda i: [])
    monkeypatch.setattr(a, "_read_cc_file", lambda i: "")
    calls = []
    a._cc_chat_with_retry = (
        lambda cc, prompt, timeout=7200, **kw: calls.append(prompt) or "产出"
    )

    a.ask_incremental("追加需求")

    assert len(a.ccs) == 1 and len(calls) == 1


# ============================================================================
# 2. Manager 侧：同时容忍旧形状与新形状的 aicu_result.json
# ============================================================================

def test_manager_degrades_to_legacy_when_no_audit_signal(tmp_path, monkeypatch):
    """新版结果不带 audit 键 → _degrade_review 走 legacy「全量失败」，不是降级通过。

    Manager 用 `"audit_passed" in raw_output` 判定「审计跑过且通过」；单 CC AICU
    不产生审计，格式失败的评审不能因此被误降级成全量通过（fail-open）。
    """
    a = _make_aicu(tmp_path)
    monkeypatch.setattr(a, "_list_code_files", lambda i: [])
    monkeypatch.setattr(a, "_read_cc_file", lambda i: "")
    a._cc_chat_with_retry = lambda cc, prompt, timeout=7200, **kw: "产出"

    a.ask("q")
    new_shaped = json.loads((a.workspace / "aicu_result.json").read_text(encoding="utf-8"))

    from executables.manager import Manager

    assessments, status = Manager._degrade_review(new_shaped, _CHILDREN)

    assert status == "parsed", f"应走 legacy 迭代修复路径，实际 {status}"
    assert all(not item["passed"] for item in assessments), "全量失败而非全量通过"


@pytest.mark.parametrize(
    "legacy, expected_status, expected_passed",
    [
        (_OLD_SHAPED_CLEAN, "degraded_pass", True),
        (_OLD_SHAPED_WITH_DEFECTS, "unparseable_fail", False),
    ],
    ids=["old-shaped-clean", "old-shaped-with-defects"],
)
def test_manager_degrade_tolerates_old_shaped_result(
    tmp_path, legacy, expected_status, expected_passed
):
    """旧版遗留的 aicu_result.json（audit_* / scores / best_id）仍被 Manager 正确解读。"""
    path = tmp_path / "aicu_result.json"
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    raw = json.loads(path.read_text(encoding="utf-8"))

    from executables.manager import Manager

    assessments, status = Manager._degrade_review(raw, _CHILDREN)

    assert status == expected_status
    assert all(item["passed"] is expected_passed for item in assessments)


def test_manager_reads_both_old_and_new_shaped_results(tmp_path):
    """Manager 的 review 代码（降级决策 + review_v*.md 落盘）对新旧两种形状都不崩。"""
    from executables.manager import Manager

    new_shaped = {
        "final_output_path": "/tmp/final",
        "content": "产出",
        "content_parsed": None,
        "role_config": {"output_file": None, "main_doc": None, "index_file": None},
    }
    ctx = SimpleNamespace(
        node=SimpleNamespace(id="n1"),
        config={},
        runtime=SimpleNamespace(get_node_workspace=lambda node_id: str(tmp_path / node_id)),
    )

    for tag, raw in (("old", _OLD_SHAPED_WITH_DEFECTS), ("new", new_shaped)):
        assessments, status = Manager._degrade_review(raw, _CHILDREN)
        assert assessments and status in {"degraded_pass", "unparseable_fail", "parsed"}

        # _write_review_output 不使用 self 状态，借 None 直接调用
        review_path = Manager._write_review_output(
            None, ctx, raw, {"s1": "artifact"}, 1 if tag == "old" else 2
        )
        text = open(review_path, encoding="utf-8").read()
        assert "# Review Report" in text and "**Audit Passed**" in text

    # 旧形状的审计结论被如实反映；新形状没有审计信号时不伪造失败
    assert "**Audit Passed**: False" in (tmp_path / "n1" / "review_v1.md").read_text(encoding="utf-8")
    assert "**Audit Passed**: True" in (tmp_path / "n1" / "review_v2.md").read_text(encoding="utf-8")
