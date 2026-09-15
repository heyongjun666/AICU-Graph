"""tests/test_aicu_review_parsing.py — 评审/审计 JSON 解析的 fail-closed 契约。

背景（真实会话回归）：审查者的回复里先引用了一段示例负载
（``{"message": "login success"}``），真正的结论在其后。旧 `_extract_json` 从
``text.find('{')`` 抓**第一个**大括号平衡块，于是取到示例、结论被丢弃；再叠加
「空 defects == 自审通过」，两个 Worker 的自审结果都被解析错却都记为通过。

本文件钉住三件事：
1. `_extract_json` 取的是结论而不是散文里的示例；
2. 「无法解析」是一等信号（None），与「解析成功且无缺陷」（[]）严格区分；
3. 该信号一路传到策略出口 —— solo/pair/triple 的 consensus_reached、
   full 的 audit_passed，以及 `_finalize_result` 不再为无审计阶段写伪造结论。
"""

import json
import os

import pytest

import aicu as aicu_module
from aicu import AICU


# ============================================================================
# Helpers
# ============================================================================

def _new_aicu():
    """不跑 __init__（不拉起 CC / 不碰 workspace）的裸实例，用于纯解析断言。"""
    return aicu_module.AICU.__new__(aicu_module.AICU)


def _make_aicu(tmp_path, num_ccs=1, role="code"):
    """离线构造真实 AICU（照搬 tests/test_worker_role_prompts.py 的做法）。"""
    ws = str(tmp_path / "ws")
    os.makedirs(ws, exist_ok=True)
    return AICU(
        role_type="worker",
        role=role,
        parent_workspace=ws,
        num_ccs=num_ccs,
        skills=[],          # 跳过 skills_dir 校验
        modular_output=True,
        output_file=None,
    )


def _stub_reply(aicu, reply):
    """把所有 CC 对话替换为固定回复。"""
    aicu._cc_chat_with_retry = lambda cc, prompt, timeout=7200, **kw: reply


# 真实会话里把结论带偏的那段形状：示例负载在前，结论在后
_PROSE_WITH_EXAMPLE_THEN_VERDICT = (
    "我已通读产出。登录接口成功时返回 "
    '{"message": "login success"}，前端据此跳转首页。\n'
    "按检查清单逐项审查后，结论如下：\n"
    '{"approved": false, "defects": ['
    '{"check": "鉴权", "description": "未校验 token 有效期", "severity": "high"}], '
    '"summary": "存在一处高危缺陷"}'
)

_AUDIT_VERDICT = (
    "审计完成。\n"
    '{"defects_found": [{"aspect": "风险", "description": "缺少回滚方案", '
    '"severity": "medium", "location": "deploy.md"}], '
    '"overall_assessment": "存在中等风险"}'
)


# ============================================================================
# 1. _extract_json：取结论，不取散文里的示例
# ============================================================================

class TestExtractJson:
    def test_example_payload_in_prose_does_not_hijack_verdict(self):
        """回归主用例：首个平衡块是示例负载，必须跳过它取真正的结论。"""
        a = _new_aicu()
        got = a._extract_json(
            _PROSE_WITH_EXAMPLE_THEN_VERDICT, required_keys=("approved", "defects")
        )
        assert got is not None
        assert got["approved"] is False
        assert got["defects"][0]["severity"] == "high"

    def test_without_required_keys_keeps_first_object(self):
        """未给判据键时行为不变：仍取第一个可解析对象。"""
        a = _new_aicu()
        got = a._extract_json('前言 {"message": "login success"} 后记')
        assert got == {"message": "login success"}

    def test_required_keys_absent_returns_none(self):
        """给了判据键但全文无匹配 → None，不退化成 {}。"""
        a = _new_aicu()
        assert a._extract_json("没有 JSON 的纯文本回复", required_keys=("approved",)) is None
        assert a._extract_json(
            '{"message": "ok"}', required_keys=("approved", "defects")
        ) is None

    def test_last_match_wins_when_multiple_conclusions(self):
        """多段匹配取最后一个 —— 结论在示例之后是常态。"""
        a = _new_aicu()
        text = (
            '{"approved": true, "defects": []}\n'
            "……复核后修正结论：\n"
            '{"approved": false, "defects": [{"severity": "high"}]}'
        )
        got = a._extract_json(text, required_keys=("approved", "defects"))
        assert got["approved"] is False

    def test_nested_object_does_not_break_outer(self):
        """嵌套对象不能截断外层匹配。"""
        a = _new_aicu()
        got = a._extract_json(
            '{"approved": false, "defects": [], "meta": {"inner": {"deep": 1}}}',
            required_keys=("approved", "defects"),
        )
        assert got["meta"]["inner"]["deep"] == 1

    def test_unbalanced_prose_quotes_recover(self):
        """散文里不成对的引号不得让后续结论块全部失配。"""
        a = _new_aicu()
        text = '他说 " 这句引号没闭合\n' + _PROSE_WITH_EXAMPLE_THEN_VERDICT
        got = a._extract_json(text, required_keys=("approved", "defects"))
        assert got is not None and got["approved"] is False


# ============================================================================
# 2. 解析器：无法解析 → None（不是空的干净结果）
# ============================================================================

class TestParsersDistinguishFailureFromCleanPass:
    def test_clean_pass_is_empty_list_not_none(self):
        a = _new_aicu()
        p = a._parse_adversarial_vote('{"approved": true, "defects": [], "summary": ""}')
        assert p is not None and p["defects"] == []
        assert a._parse_audit(
            '{"defects_found": [], "overall_assessment": "无问题"}'
        )["defects_found"] == []

    def test_unparseable_is_none(self):
        a = _new_aicu()
        assert a._parse_adversarial_vote("不是 JSON") is None
        assert a._parse_audit("不是 JSON") is None

    def test_missing_required_keys_is_none(self):
        a = _new_aicu()
        assert a._parse_adversarial_vote('{"summary": "已全部通过"}') is None
        assert a._parse_audit('{"overall_assessment": "良好"}') is None


# ============================================================================
# 3. 端到端：解析失败不得被记为通过
# ============================================================================

class TestSoloAndPairFailClosed:
    @pytest.mark.parametrize("num_ccs", [1, 2])
    def test_review_parse_failure_blocks_consensus(self, tmp_path, monkeypatch, num_ccs):
        a = _make_aicu(tmp_path, num_ccs=num_ccs)
        _stub_reply(a, "好的，我已审查完毕，没有问题。")
        monkeypatch.setattr(a, "_list_code_files", lambda i: [])
        monkeypatch.setattr(a, "_read_cc_file", lambda i: "")

        run = a._ask_peer_solo if num_ccs == 1 else a._ask_peer_pair
        result = run("原始问题")

        assert result["consensus_reached"] is False, "解析失败必须判未通过"
        assert result["vote_rounds"] == 1, "无缺陷清单，不应进入修订循环"

    def test_review_example_payload_does_not_flip_verdict(self, tmp_path, monkeypatch):
        """回归主用例端到端：回复里带示例负载时，仍要读出 high 缺陷并进入修订。"""
        a = _make_aicu(tmp_path, num_ccs=2)
        _stub_reply(a, _PROSE_WITH_EXAMPLE_THEN_VERDICT)
        monkeypatch.setattr(a, "_list_code_files", lambda i: [])
        monkeypatch.setattr(a, "_read_cc_file", lambda i: "")

        seen = []
        monkeypatch.setattr(
            a, "_revise_with_defect_list",
            lambda question, cc, defects: seen.append(defects),
        )
        result = a._ask_peer_pair("原始问题", max_rounds=1)

        # 结论被示例负载劫持的话 defects 会是空，修订根本不会被调用
        assert seen and seen[0][0]["check"] == "鉴权"
        assert result["consensus_reached"] is False

    def test_clean_review_still_passes(self, tmp_path, monkeypatch):
        """对照组：真正解析出「无缺陷」时照常通过，修复不得把正常路径也判死。"""
        a = _make_aicu(tmp_path, num_ccs=1)
        _stub_reply(a, '{"approved": true, "defects": [], "summary": "无问题"}')
        monkeypatch.setattr(a, "_list_code_files", lambda i: [])
        monkeypatch.setattr(a, "_read_cc_file", lambda i: "")

        assert a._ask_peer_solo("原始问题")["consensus_reached"] is True


class TestFullAuditFailsClosed:
    def test_audit_parse_failure_blocks_audit_passed(self, tmp_path, monkeypatch):
        a = _make_aicu(tmp_path, num_ccs=4)
        _stub_reply(a, "审计结束，我没有发现任何问题。")
        monkeypatch.setattr(a, "_list_code_files", lambda i: [])
        monkeypatch.setattr(a, "_read_cc_file", lambda i: "")
        monkeypatch.setattr(a, "_copy_best_to_cc0", lambda best_id: None)

        result = a._ask_peer_full("原始问题", max_rounds=1)

        assert result["consensus_reached"] is False
        assert result["audit_passed"] is False

    def test_clean_audit_still_passes(self, tmp_path, monkeypatch):
        a = _make_aicu(tmp_path, num_ccs=4)
        # 按提示词内容分派：生成两位 / CC-2 融合 / CC-3 审计各给对应形状的回复
        def _per_call(cc, prompt, timeout=7200, **kw):
            if "比较两个独立方案" in prompt:
                return ('{"scores": {"A": 8, "B": 7}, "best": "A", '
                        '"approved": true, "defects": [], "summary": ""}')
            if "专门寻找方案中的漏洞" in prompt:
                return '{"defects_found": [], "overall_assessment": "无问题"}'
            return "生成阶段产出（不参与解析）"

        a._cc_chat_with_retry = _per_call
        monkeypatch.setattr(a, "_list_code_files", lambda i: [])
        monkeypatch.setattr(a, "_read_cc_file", lambda i: "")
        monkeypatch.setattr(a, "_copy_best_to_cc0", lambda best_id: None)

        result = a._ask_peer_full("原始问题", max_rounds=1)

        assert result["audit_passed"] is True
        assert result["consensus_reached"] is True


# ============================================================================
# 4. _finalize_result：无审计阶段不得伪造审计结论
# ============================================================================

class TestFinalizeResultAuditKeys:
    @pytest.mark.parametrize("num_ccs", [1, 2, 3])
    def test_no_audit_keys_for_strategies_without_audit(self, tmp_path, monkeypatch, num_ccs):
        """CC<4 没有审计阶段 → 结果 dict 不含 audit_passed / audit_defects。"""
        a = _make_aicu(tmp_path, num_ccs=num_ccs)
        monkeypatch.setattr(a, "_list_code_files", lambda i: [])
        monkeypatch.setattr(a, "_read_cc_file", lambda i: "")
        a._save_to_workspace = lambda *args, **kw: None

        result = a._finalize_result(
            question="q", scores={0: {0: 8.0}}, best_id=0,
            consensus_reached=True, vote_rounds=1,
        )

        assert "audit_passed" not in result
        assert "audit_defects" not in result

    def test_audit_strategy_writes_real_verdict(self, tmp_path, monkeypatch):
        a = _make_aicu(tmp_path, num_ccs=4)
        monkeypatch.setattr(a, "_list_code_files", lambda i: [])
        monkeypatch.setattr(a, "_read_cc_file", lambda i: "")
        a._save_to_workspace = lambda *args, **kw: None

        result = a._finalize_result(
            question="q", scores={0: {0: 8.0}}, best_id=0,
            consensus_reached=False, vote_rounds=1,
            audit_defects=None, audit_passed=False,
        )

        assert result["audit_passed"] is False
        assert result["audit_defects"] == []
        assert result["consensus_reached"] is False


# ============================================================================
# 5. 跨层钉：Manager 的降级决策不再被伪造的审计信号命中
# ============================================================================

def test_manager_degrades_to_legacy_when_no_audit_signal(tmp_path, monkeypatch):
    """CC<4 结果不带 audit 键 → _degrade_review 走 legacy「全量失败」，不是降级通过。

    这是本次行为变更的落点：旧实现里 solo/pair 的 audit_passed 恒为 True，
    Manager 用 `"audit_passed" in raw_output` 判定「审计跑过且通过」，于是格式失败
    的评审被降级成全量通过（fail-open）。现在没有审计就没有这个键。
    """
    a = _make_aicu(tmp_path, num_ccs=1)
    monkeypatch.setattr(a, "_list_code_files", lambda i: [])
    monkeypatch.setattr(a, "_read_cc_file", lambda i: "")
    a._save_to_workspace = lambda *args, **kw: None

    aicu_result = a._finalize_result(
        question="q", scores={0: {0: 8.0}}, best_id=0,
        consensus_reached=False, vote_rounds=1,
    )
    # 落盘前断言：这两个键就是 Manager 侧判据的全部来源
    assert not ({"audit_passed", "audit_defects"} & set(aicu_result))

    from executables.manager import Manager

    children = [{"step_ref": "s1", "role": "code", "task": "t"}]
    assessments, status = Manager._degrade_review(aicu_result, children)

    assert status == "parsed", f"应走 legacy 迭代修复路径，实际 {status}"
    assert all(not item["passed"] for item in assessments), "全量失败而非全量通过"


def test_manager_still_degrades_pass_on_real_audit_evidence():
    """对照组：真带 audit_passed=True 且无缺陷时，降级通过分支仍然有效。"""
    from executables.manager import Manager

    children = [{"step_ref": "s1", "role": "code", "task": "t"}]
    _assessments, status = Manager._degrade_review(
        {"audit_passed": True, "audit_defects": []}, children
    )
    assert status == "degraded_pass"


def test_finalize_result_writes_json_snapshot(tmp_path, monkeypatch):
    """落盘快照里同样不得出现伪造键（端到端产物级证据）。"""
    a = _make_aicu(tmp_path, num_ccs=1)
    monkeypatch.setattr(a, "_list_code_files", lambda i: [])
    monkeypatch.setattr(a, "_read_cc_file", lambda i: "")

    a._finalize_result(
        question="q", scores={0: {0: 8.0}}, best_id=0,
        consensus_reached=True, vote_rounds=1,
    )
    snapshot = json.loads((a.workspace / "aicu_result.json").read_text(encoding="utf-8"))
    assert "audit_passed" not in snapshot
    assert "audit_defects" not in snapshot
