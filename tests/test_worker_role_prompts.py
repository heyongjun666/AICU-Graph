"""tests/test_worker_role_prompts.py — Worker 角色提示词边界与规模自适应回归。

对应计划《Worker 角色职责边界与任务自适应深度收敛》。本文件是**纯离线**的
静态/提示词级验证：解析 `roles/Worker/*.yaml` 并断言文本契约，或离线构造
`AICU`（monkeypatch 掉模型调用）后断言渲染出的提示词。

它证明的是「提示词内容与协议契约一致」，**不证明模型实际行为改变**。

分组：
  1. 可解析与协议一致性（含防漂移的协议逐字一致）
  2. 占位符卫生（运行时不支持的占位符会静默泄漏，必须静态拦截）
  3. 契约兼容回归（文件协议/交付契约与基线一致）
  4. 边界回归（各角色不越界）
  5. 技能卫生（无「人在环」skill 冲突）
  6. 具体收敛点
  7. 「任务适配协议」第 6 条（决策记录要求）
  8. grill 专项（自行裁决 + 不修订需求文档）
  9. grill → requirements 成对性
 10. requirements 专项（普通用户视角 + 主动具体化 + 来源三分）
 11. 审查位 N/A 适用性条款（且不改运行时协议）
"""

import hashlib
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from aicu import AICU, _CHECKLIST_APPLICABILITY_NOTE

ROOT = Path(__file__).resolve().parents[1]
WORKER_DIR = ROOT / "roles" / "Worker"
# 下划线开头的 MCP lab 角色是仓库内联调夹具，不是生产候选角色；它们必须与
# 正常角色的提示词契约隔离，尤其不能被 Manager 的 available_roles 面板暴露。
ALL_ROLES = sorted(
    p.stem for p in WORKER_DIR.glob("*.yaml") if not p.stem.startswith("_")
)

# pytest 收集期即保证目录存在且非空，避免后续断言静默通过
assert ALL_ROLES, f"未找到任何 Worker 角色 YAML: {WORKER_DIR}"


def _cfg(role: str) -> dict:
    return yaml.safe_load((WORKER_DIR / f"{role}.yaml").read_text(encoding="utf-8"))


def _text(role: str, *fields: str) -> str:
    cfg = _cfg(role)
    return "\n".join(str(cfg.get(f) or "") for f in fields)


def _flat(role: str, field: str) -> str:
    """把 list/dict 字段压成文本，便于做包含性断言。"""
    val = _cfg(role).get(field)
    return str(val)


# ============================================================================
# 1. 可解析与协议一致性
# ============================================================================


class TestParseAndProtocol:
    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_yaml_parses_and_fields_present(self, role):
        cfg = _cfg(role)
        assert isinstance(cfg, dict), f"{role}.yaml 解析结果不是 mapping"
        assert cfg.get("summary"), f"{role}.yaml 缺 summary"
        assert cfg.get("generate_instruction"), f"{role}.yaml 缺 generate_instruction"
        assert cfg.get("role_md"), f"{role}.yaml 缺 role_md"
        assert cfg.get("review_checklist"), f"{role}.yaml 缺 review_checklist"

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_role_md_contains_protocol(self, role):
        md = _cfg(role).get("role_md") or ""
        assert "## 任务适配协议" in md, f"{role}.yaml 的 role_md 缺少任务适配协议"

    def test_protocol_block_is_byte_identical_across_roles(self):
        """15 个角色的协议小节必须逐字一致（防后续单点漂移）。"""
        digests = {}
        for role in ALL_ROLES:
            md = _cfg(role)["role_md"]
            seg = md.split("## 任务适配协议", 1)[1].split("\n## ", 1)[0]
            digests.setdefault(hashlib.md5(seg.encode("utf-8")).hexdigest(), []).append(role)
        assert len(digests) == 1, f"协议块存在多个版本：{digests}"
        assert len(next(iter(digests.values()))) == len(ALL_ROLES)

    def test_protocol_has_ten_items(self):
        md = _cfg("code")["role_md"]
        seg = md.split("## 任务适配协议", 1)[1].split("\n## ", 1)[0]
        for i in range(1, 11):
            assert re.search(rf"^\s*{i}\.\s+\*\*", seg, re.M), f"协议缺第 {i} 条"


# ============================================================================
# 2. 占位符卫生
# ============================================================================

# _format_instruction 用 str.replace 处理固定占位符；未知占位符不报错、会原样
# 泄漏进提示词，因此必须静态拦截。{available_roles} 在 init 期一次性替换，
# {output_path} 仅用于 revise_instruction。
_KNOWN_PLACEHOLDERS = {
    "output_file", "main_doc", "index_file", "index_rules", "final_file",
    "deliverable_dir", "output_path", "available_roles",
}


class TestPlaceholderHygiene:
    @pytest.mark.parametrize("role", ALL_ROLES)
    @pytest.mark.parametrize("field", ["generate_instruction", "revise_instruction", "role_md"])
    def test_no_unknown_placeholders(self, role, field):
        txt = str(_cfg(role).get(field) or "")
        unknown = {
            m for m in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", txt)
            if m not in _KNOWN_PLACEHOLDERS
        }
        assert not unknown, f"{role}.{field} 含运行时不支持的占位符：{sorted(unknown)}"

    def test_helper_rejects_unknown_placeholder(self):
        """反向验证：本测试确实能抓到未知占位符（防止断言写空）。"""
        txt = "规模：{task_size}，风险：{risk}"
        unknown = {
            m for m in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", txt)
            if m not in _KNOWN_PLACEHOLDERS
        }
        assert unknown == {"task_size", "risk"}


# ============================================================================
# 3. 契约兼容回归
# ============================================================================

# 基线值取自本次改动前的角色配置；改动不得触碰文件协议。
_BASELINE_CONTRACT = {
    "acceptance": ("acceptance_report.md", "output", None, True, "_INDEX.md"),
    "architecture": ("design.md", "output", None, True, "_INDEX.md"),
    "code": (None, "output", "output", True, None),
    "data_analysis": (None, "output", "output", None, None),
    "default": (None, "output", "output", False, None),
    "deploy": ("deploy_guide.md", "output", None, True, "_INDEX.md"),
    "dev_lead": ("task_assignment.md", "output", None, True, "_INDEX.md"),
    "dev_member": (None, "output", None, True, None),
    "document": (None, "output", "output", None, None),
    "grill": (None, "output", "output", None, None),
    "incremental_plan": ("plan.json", "output", None, True, "_INDEX.md"),
    "prototype": ("prototype.md", "output", None, True, "_INDEX.md"),
    "requirements": ("requirements.md", "requirements.md", None, True, "_INDEX.md"),
    "test": ("test_report.md", "output", None, True, "_INDEX.md"),
    "visual_designer": ("design.md", "design.md", None, True, "_INDEX.md"),
}

# num_ccs 本次「完全不改分配」（决策 2），基线即当前值。
_BASELINE_NUM_CCS = {
    "acceptance": 2, "architecture": 4, "code": 2, "data_analysis": 2,
    "default": 2, "deploy": 2, "dev_lead": 3, "dev_member": 2,
    "document": 2, "grill": 3, "incremental_plan": 3, "prototype": 3,
    "requirements": 2, "test": 2, "visual_designer": 4,
}


class TestContractCompatibility:
    @pytest.mark.parametrize("role", sorted(_BASELINE_CONTRACT))
    def test_file_protocol_unchanged(self, role):
        cfg = _cfg(role)
        got = (cfg.get("main_doc"), cfg.get("final_file"), cfg.get("output_file"),
               cfg.get("modular_output"), cfg.get("index_file"))
        assert got == _BASELINE_CONTRACT[role], f"{role}.yaml 文件协议发生漂移"
        assert cfg.get("deliverable_dir") == "deliverables"

    @pytest.mark.parametrize("role", sorted(_BASELINE_NUM_CCS))
    def test_num_ccs_unchanged(self, role):
        assert _cfg(role).get("num_ccs") == _BASELINE_NUM_CCS[role]

    @pytest.mark.parametrize("role", ["test", "acceptance"])
    def test_verdict_block_protocol_preserved(self, role):
        txt = _text(role, "generate_instruction")
        assert "<!-- VERDICT " in txt, f"{role} 丢失 VERDICT 块要求"
        assert '"severity": "high|medium|low"' in txt
        assert "passed" in txt and "defects" in txt

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_generation_instruction_split_headings_unchanged(self, role):
        """_split_generation_instruction 按这些标题切分稳定/每次注入两段。

        新增小节不得使用这些标题，否则改变 CLAUDE.md 与每轮请求的内容划分。
        """
        txt = str(_cfg(role).get("generate_instruction") or "")
        allowed = {"## 产物组织方式", "## JSON 格式要求:"}
        # 与 aicu.py 的生产正则逐字一致（注意 \s 会吃掉换行，故此处不接 .*）
        matches = list(
            re.finditer(
                r"(?m)^##\s+(?:输出格式|JSON\s*格式要求|产出要求|产物组织方式)"
                r"(?:\s|（|\(|:|：|$)",
                txt,
            )
        )
        found = {m.group(0).strip() for m in matches}
        assert found <= allowed, f"{role} 出现了新的切分标题：{found - allowed}"

        # 交叉验证：真实切分函数确实在同一位置切开
        stable, output = AICU._split_generation_instruction(txt)
        if matches:
            assert stable == txt[: matches[0].start()].rstrip()
            assert output == txt[matches[0].start():].strip()
        else:
            assert stable == "" and output == txt.strip()


# ============================================================================
# 4. 边界回归（文本级）
# ============================================================================


class TestRoleBoundaries:
    def test_requirements_has_no_implementation_or_test_script_contract(self):
        txt = _text("requirements", "generate_instruction", "revise_instruction", "role_md")
        assert "不定义\"系统如何实现\"" in txt or "绝不定义" in txt
        assert "验收标准描述" in txt
        assert "不描述\"用什么工具测量\"" in txt

    def test_architecture_not_doing_task_split(self):
        txt = _text("architecture", "generate_instruction", "role_md")
        assert "不做任务级拆分" in txt
        assert "不写代码" in txt

    @pytest.mark.parametrize("role", ["code", "dev_member"])
    def test_code_roles_do_not_own_requirements_or_architecture(self, role):
        nf = _flat(role, "not_for")
        assert "需求分析" in nf, f"{role}.not_for 未排除需求分析"
        assert "架构" in nf, f"{role}.not_for 未排除架构设计"
        txt = _text(role, "generate_instruction", "role_md")
        assert any(p in txt for p in ("不做需求分析", "不负责需求分析", "禁止自行修改需求",
                                      "不修改需求", "需求分析与需求变更")), \
            f"{role} 缺少「不改需求」的边界表述"
        assert any(p in txt for p in ("不设计架构", "禁止自行修改需求、架构", "不自行修改架构",
                                      "禁止自行修改需求、架构和接口")), \
            f"{role} 缺少「不改架构」的边界表述"

    @pytest.mark.parametrize("role", ["test", "acceptance"])
    def test_verification_roles_report_only(self, role):
        txt = _text(role, "generate_instruction", "revise_instruction", "role_md")
        assert "不修复" in txt

    def test_prototype_defers_visual_and_code_details(self):
        txt = _text("prototype", "generate_instruction", "role_md")
        assert "不定义视觉风格" in txt
        assert "CSS/DOM/JS" in txt

    def test_visual_designer_defers_dom_css_js_and_flow(self):
        txt = _text("visual_designer", "generate_instruction", "role_md")
        assert "DOM" in txt and "CSS" in txt and "JavaScript" in txt
        assert "不做交互流程与页面跳转设计" in txt


# ============================================================================
# 5. 技能卫生
# ============================================================================


class TestSkillsHygiene:
    def test_dev_member_drops_tdd(self):
        assert "tdd" not in (_cfg("dev_member").get("skills") or [])

    def test_grill_drops_grilling(self):
        assert "grilling" not in (_cfg("grill").get("skills") or [])

    @pytest.mark.parametrize("role", ["architecture", "dev_lead"])
    def test_drops_improve_codebase_architecture(self, role):
        assert "improve-codebase-architecture" not in (_cfg(role).get("skills") or [])

    def test_kept_batch_safe_skills(self):
        assert _cfg("prototype").get("skills") == ["to-prd"]
        assert _cfg("dev_lead").get("skills") == ["to-issues"]
        assert _cfg("incremental_plan").get("skills") == ["to-issues"]

    def test_no_human_in_the_loop_skill_mounted(self):
        """任何角色都不得挂载要求人工交互/人工挑选的 skill。"""
        banned = {"grilling", "tdd", "improve-codebase-architecture"}
        for role in ALL_ROLES:
            mounted = set(_cfg(role).get("skills") or [])
            assert not (mounted & banned), f"{role} 挂载了人在环 skill：{mounted & banned}"


# ============================================================================
# 6. 具体收敛点
# ============================================================================


class TestConvergencePoints:
    def test_grill_has_no_fixed_round_count(self):
        txt = _text("grill", "generate_instruction", "role_md")
        assert "8-15" not in txt
        assert "轮次由任务复杂度与风险决定" in txt

    def test_prototype_prd_is_conditional(self):
        txt = _text("prototype", "generate_instruction")
        assert "仅当任务明确要求产出 PRD 时" in txt
        assert "PRD 不是默认产物" in txt

    def test_incremental_plan_allows_single_version(self):
        txt = _text("incremental_plan", "generate_instruction", "role_md")
        assert "单版本" in txt
        # plan.json 结构必须保留
        assert '"versions"' in _text("incremental_plan", "generate_instruction")

    def test_deploy_has_no_pending_marker_permission(self):
        txt = _text("deploy", "generate_instruction", "role_md")
        assert "需人工确认" in txt  # 仅作为「禁止项」出现
        assert "不要编造具体数值，也不要输出" in txt or "不臆造具体数值" in txt

    def test_acceptance_has_no_leading_space(self):
        raw = (WORKER_DIR / "acceptance.yaml").read_text(encoding="utf-8")
        assert not raw.startswith(" "), "acceptance.yaml 首行仍有前导空格"
        assert raw.startswith("#")


# ============================================================================
# 7. 协议第 6 条：当场拍板 + 高影响标注 + 影响范围 + 替代选项
# ============================================================================


class TestDecisionProtocol:
    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_item6_requires_on_the_spot_decision(self, role):
        md = _cfg(role)["role_md"]
        assert "当场拍板" in md

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_item6_requires_high_impact_flag_and_decision_record(self, role):
        md = _cfg(role)["role_md"]
        assert "高影响决策必须显式标注" in md
        assert "影响范围" in md
        assert "被排除的替代选项" in md

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_item6_forbids_pending_markers(self, role):
        md = _cfg(role)["role_md"]
        assert "待确认/TBD/OPEN" in md

    def test_no_role_asks_user_to_decide(self):
        """不得出现「等待/请示用户裁决」类表述（无人在环）。"""
        for role in ALL_ROLES:
            txt = _text(role, "generate_instruction", "revise_instruction", "role_md")
            for bad in ("请用户", "等待用户", "用户裁决", "请示", "交回上游裁决"):
                assert bad not in txt, f"{role} 含人在环表述：{bad}"

    def test_dev_lead_no_longer_marks_arch_ambiguity_pending(self):
        txt = _text("dev_lead", "generate_instruction", "revise_instruction", "role_md")
        assert "架构存在歧义时标注为待确认" not in txt
        assert "架构歧义是否已当场拍板" in txt or "当场拍板" in txt

    def test_dev_member_no_longer_defers_contract_conflicts(self):
        txt = _text("dev_member", "generate_instruction", "role_md")
        assert "契约冲突仲裁" not in txt
        assert "契约冲突当场决策" in txt or "契约冲突处理" in txt


# ============================================================================
# 8. grill 专项
# ============================================================================


class TestGrillRulings:
    def test_revise_instruction_is_self_ruled(self):
        txt = _text("grill", "revise_instruction")
        assert "自行裁决" in txt
        for bad in ("用户裁决", "用户给出的文本", "rulings", "请根据用户"):
            assert bad not in txt, f"grill.revise_instruction 残留人工裁决措辞：{bad}"

    def test_revise_instruction_outputs_ruling_list_shape(self):
        txt = _text("grill", "revise_instruction")
        assert "accept | reject" in txt
        assert "**finding**" in txt
        assert "**ruling**" in txt
        assert "**reason**" in txt
        assert "accept 时写明需求文档应如何改" in txt

    def test_revise_instruction_does_not_rewrite_requirements_doc(self):
        txt = _text("grill", "revise_instruction")
        assert "**不要修改上游需求文档**" in txt
        assert "修订由 requirements" in txt
        assert "将修订后的完整需求文档写入" not in txt
        assert "受控最小修订" not in txt

    def test_not_for_forbids_rewriting_requirements_doc(self):
        nf = _flat("grill", "not_for")
        assert "不直接修订需求文档" in nf
        assert "受控最小修订" not in nf

    def test_finding_format_preserved(self):
        txt = _text("grill", "generate_instruction")
        assert "### G<编号>" in txt
        for field in ("severity", "impact", "question", "recommendation", "rationale"):
            assert f"**{field}**" in txt, f"grill finding 缺字段 {field}"

    def test_checklist_covers_ruling_list_and_boundary(self):
        names = [item[0] for item in _cfg("grill")["review_checklist"]]
        assert "ruling_list" in names
        assert "no_doc_rewrite" in names


# ============================================================================
# 9. grill → requirements 成对性
# ============================================================================


class TestGrillRequirementsPairing:
    def test_requirements_consumes_ruling_list(self):
        txt = _text("requirements", "generate_instruction")
        assert "裁决清单" in txt
        assert "已拒绝质询" in txt
        # accept → 按 recommendation 修订；reject → 记入「已拒绝质询」
        assert "accept" in txt and "reject" in txt
        assert "reject 的 finding" in txt

    def test_requirements_checklist_has_intake_rulings(self):
        names = [item[0] for item in _cfg("requirements")["review_checklist"]]
        assert "intake_rulings" in names

    def test_pairing_is_two_sided(self):
        """两侧必须同时成立，防止只改一侧造成裁决悬空。"""
        grill_txt = _text("grill", "revise_instruction")
        req_txt = _text("requirements", "generate_instruction")
        assert "**不要修改上游需求文档**" in grill_txt
        assert "裁决清单" in req_txt and "已拒绝质询" in req_txt


# ============================================================================
# 10. requirements 专项
# ============================================================================


class TestRequirementsSpecial:
    def test_ordinary_user_perspective_in_both_places(self):
        gi = _text("requirements", "generate_instruction")
        md = _text("requirements", "role_md")
        for txt in (gi, md):
            assert "普通用户" in txt
            assert "可感知" in txt
        assert "## 组织全文的第一原则：普通用户视角" in gi
        assert "普通用户视角" in md

    def test_user_perspective_survives_scale_trimming(self):
        txt = _text("requirements", "generate_instruction", "role_md")
        assert "裁剪章节" in txt
        assert "不等于" in txt or "不降低" in txt

    def test_perceivability_test_kept(self):
        assert "用户能感知到什么" in _text("requirements", "generate_instruction")
        assert "用户能否感知到它" in _text("requirements", "role_md")

    def test_proactive_concretization_rule_exists(self):
        txt = _text("requirements", "generate_instruction", "role_md")
        assert "主动具体化" in txt
        assert "最小充分细节" in txt
        assert "系统要快" in txt  # 正例必须保留，防止规则空转

    def test_concretization_guards_both_boundaries(self):
        """两条边界必须同时存在：不扩范围、不落实现细节。"""
        txt = _text("requirements", "generate_instruction", "role_md")
        assert "不越界为范围扩张" in txt
        assert "不越界为实现细节" in txt
        assert "范围克制" in txt

    def test_assumption_cannot_be_scope_creep_entry(self):
        txt = _text("requirements", "role_md")
        assert "不得成为新增功能的入口" in txt

    def test_three_way_provenance(self):
        txt = _text("requirements", "generate_instruction", "role_md")
        assert "EXPLICIT" in txt and "DERIVED" in txt and "ASSUMPTION" in txt
        assert "计入决策记录" in txt
        assert "伪装成 EXPLICIT" in txt

    def test_assumption_must_reach_decision_record(self):
        gi = _text("requirements", "generate_instruction")
        assert "ASSUMPTION" in gi and "决策记录" in gi

    def test_no_pending_marker_permission_kept(self):
        txt = _text("requirements", "generate_instruction", "role_md")
        assert "不得出现" in txt and "待确认" in txt
        assert "具体化不产生未决项" in txt

    def test_checklist_covers_concretization_and_scope(self):
        names = [item[0] for item in _cfg("requirements")["review_checklist"]]
        for expected in ("provenance", "concretization", "scope_discipline",
                         "decisions_made", "intake_rulings"):
            assert expected in names, f"requirements 审查清单缺 {expected}"


# ============================================================================
# 10b. _INDEX.md 只做导航（{index_rules} 单一来源）
# ============================================================================

_INDEX_ROLES = [
    r for r in ALL_ROLES
    if "{index_rules}" in str(_cfg(r).get("generate_instruction") or "")
]


class TestIndexRules:
    def test_all_index_roles_use_shared_rules(self):
        """配置了 index_file 的角色都引用 {index_rules}，没有各写一份。"""
        with_index = [r for r in ALL_ROLES if _cfg(r).get("index_file")]
        assert with_index and set(with_index) == set(_INDEX_ROLES)
        for r in with_index:
            assert "下游会先读 {index_file}" not in str(_cfg(r).get("generate_instruction"))

    @pytest.mark.parametrize("role", _INDEX_ROLES)
    def test_rendered_rules_restrict_index_to_navigation(self, tmp_path, role):
        rendered = _make_aicu(tmp_path, role=role)._format_instruction()
        assert "{index_rules}" not in rendered and "{index_file}" not in rendered
        assert "只写文件清单" in rendered
        assert "版本沿革" in rendered and "不要写进 _INDEX.md" in rendered
        assert "不累积历史" in rendered

    def test_role_without_placeholder_renders_no_rules(self, tmp_path):
        """不引用 {index_rules} 的角色（code/dev_member）提示词不受影响。"""
        assert "只写文件清单" not in _make_aicu(tmp_path, role="code")._format_instruction()


# ============================================================================
# 11. 审查位 N/A 适用性条款
# ============================================================================


def _make_aicu(tmp_path, role="code", num_ccs=1):
    """离线构造 AICU（照搬 tests/test_shared_target.py 的做法）。"""
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


def _capture_prompts(monkeypatch, aicu):
    """monkeypatch 掉模型调用，返回收集到的 prompt 列表。"""
    captured = []

    def _fake(cc, prompt, timeout=7200, **kwargs):
        captured.append(prompt)
        return '{"approved": true, "defects": [], "summary": "ok"}'

    monkeypatch.setattr(aicu, "_cc_chat_with_retry", _fake)
    return captured


class TestReviewApplicabilityNote:
    def test_constant_text_is_what_plan_specified(self):
        assert "先判断每条检查项/审查角度是否适用于本次任务" in _CHECKLIST_APPLICABILITY_NOTE
        assert "N/A" in _CHECKLIST_APPLICABILITY_NOTE
        assert "approved=true" in _CHECKLIST_APPLICABILITY_NOTE
        assert "N/A 不得用于规避真实缺陷" in _CHECKLIST_APPLICABILITY_NOTE

    def test_pair_review_prompt_has_note(self, tmp_path, monkeypatch):
        aicu = _make_aicu(tmp_path, num_ccs=2)
        captured = _capture_prompts(monkeypatch, aicu)
        aicu._pair_review("问题", aicu.ccs[1])
        assert captured and _CHECKLIST_APPLICABILITY_NOTE in captured[0]
        assert "只有所有检查项都通过" not in captured[0]

    def test_triple_review_prompt_has_note(self, tmp_path, monkeypatch):
        aicu = _make_aicu(tmp_path, num_ccs=3)
        captured = _capture_prompts(monkeypatch, aicu)
        aicu._triple_review_only("问题")
        assert captured and _CHECKLIST_APPLICABILITY_NOTE in captured[0]

    def test_full_audit_prompt_has_note(self, tmp_path, monkeypatch):
        aicu = _make_aicu(tmp_path, num_ccs=4)
        captured = _capture_prompts(monkeypatch, aicu)
        aicu._full_audit("问题")
        assert captured and _CHECKLIST_APPLICABILITY_NOTE in captured[0]
        # 审计位 JSON 形状未变
        assert '"defects_found"' in captured[0]
        assert '"overall_assessment"' in captured[0]

    def test_source_no_longer_has_old_rule(self):
        src = (ROOT / "aicu.py").read_text(encoding="utf-8")
        assert "只有所有检查项都通过" not in src
        assert src.count("_CHECKLIST_APPLICABILITY_NOTE") >= 5  # 1 定义 + 4 渲染点(solo 已硬删自审)

    def test_runtime_protocol_unchanged(self, tmp_path, monkeypatch):
        """N/A 理由落在既有 summary 自由文本中：不新增 JSON 字段。"""
        aicu = _make_aicu(tmp_path, num_ccs=2)
        captured = _capture_prompts(monkeypatch, aicu)
        aicu._pair_review("问题", aicu.ccs[1])
        prompt = captured[0]
        assert '"approved": true/false' in prompt
        assert '"defects": [' in prompt
        assert '"check"' in prompt and '"description"' in prompt and '"severity"' in prompt
        assert '"summary": "总体评价"' in prompt
        # 未出现任何新增字段
        for forbidden in ('"applicable"', '"na_items"', '"skipped"'):
            assert forbidden not in prompt

    def test_severity_filtering_unchanged(self, tmp_path, monkeypatch):
        """high/medium 过滤逻辑不变：low 缺陷不进 defects。"""
        aicu = _make_aicu(tmp_path, num_ccs=2)

        def _fake(cc, prompt, timeout=7200, **kwargs):
            return (
                '{"approved": false, "defects": ['
                '{"check": "a", "description": "d1", "severity": "high"},'
                '{"check": "b", "description": "d2", "severity": "low"},'
                '{"check": "c", "description": "d3", "severity": "medium"}], '
                '"summary": "s"}'
            )

        monkeypatch.setattr(aicu, "_cc_chat_with_retry", _fake)
        defects = aicu._pair_review("问题", aicu.ccs[1])
        assert [d["severity"] for d in defects] == ["high", "medium"]

    def test_parse_failure_yields_none_not_empty_defects(self, tmp_path, monkeypatch):
        """解析失败返回 None（不是 []）—— 调用方据此判未通过，本次改动不得退回静默放行。"""
        aicu = _make_aicu(tmp_path, num_ccs=2)
        monkeypatch.setattr(
            aicu, "_cc_chat_with_retry",
            lambda cc, prompt, timeout=7200, **kw: "不是 JSON",
        )
        assert aicu._parse_adversarial_vote("不是 JSON") is None
        assert aicu._pair_review("问题", aicu.ccs[1]) is None


# ============================================================================
# 12. 整体视角（每个 CC 都是同角色克隆，独立的是上下文不是视角）
# ============================================================================
#
# 四个模式的评审位（CC-1/CC-2/CC-3）与生产位共享同一份 role_md 与同一份
# review_checklist，因此「独立审查」独立的是上下文而非视角。若清单只含零件级
# 条目，所有评审位就都只有零件级盲区——所以每个角色都必须自带一条整体项。


def _holistic_items(role: str):
    return [it for it in _cfg(role)["review_checklist"] if it[0].startswith("holistic_")]


class TestHolisticPerspective:
    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_every_role_has_exactly_one_holistic_item(self, role):
        items = _holistic_items(role)
        assert len(items) == 1, f"{role} 的整体视角项数量异常：{[i[0] for i in items]}"

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_holistic_item_is_last(self, role):
        """整体项排在最后：先逐条核对，最后退后一步看整体。"""
        assert _holistic_items(role)[0] == _cfg(role)["review_checklist"][-1]

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_holistic_item_is_not_na_able(self, role):
        """整体项不得写成可 N/A 的「如适用」条目——否则会被 N/A 条款绕过。"""
        desc = _holistic_items(role)[0][1]
        assert "如适用" not in desc, f"{role} 的整体项可被 N/A 绕过"
        assert "N/A" not in desc

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_holistic_item_is_judgeable(self, role):
        """必须写成可判定的：区分于逐条检查 + 明确什么算缺陷（severity）。"""
        desc = _holistic_items(role)[0][1]
        assert "报 high" in desc, f"{role} 的整体项没有明确的缺陷判定标准"
        assert re.search(r"不是逐[条个步页节项]|不是只看", desc), \
            f"{role} 的整体项没有与逐条检查区分开"

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_holistic_item_names_are_distinct(self, role):
        name = _holistic_items(role)[0][0]
        assert name != "holistic_overall" or role == "default"

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_personas_carry_holistic_clause(self, role):
        cfg = _cfg(role)
        assert "整体" in (cfg.get("adversarial_role") or ""), f"{role}.adversarial_role 缺整体口径"
        assert "整体" in (cfg.get("audit_role") or ""), f"{role}.audit_role 缺整体口径"

    def test_all_holistic_names_unique(self):
        names = [_holistic_items(r)[0][0] for r in ALL_ROLES]
        assert len(set(names)) == len(names), f"整体项命名重复：{names}"


class TestHolisticRenderedInAllModes:
    """确认整体项确实进入各模式的评审/审计提示词（而不只是躺在 YAML 里）。"""

    @staticmethod
    def _prompt_for(monkeypatch, tmp_path, role, run):
        aicu = _make_aicu(tmp_path, role=role, num_ccs=_BASELINE_NUM_CCS[role])
        captured = _capture_prompts(monkeypatch, aicu)
        run(aicu)
        assert captured, f"{role} 未产生提示词"
        return captured[0]

    def test_pair_mode(self, tmp_path, monkeypatch):
        p = self._prompt_for(monkeypatch, tmp_path, "acceptance",
                             lambda a: a._pair_review("做一个鹈鹕骑车的 SVG 动画", a.ccs[1]))
        assert "**holistic_experience**" in p
        assert "产物整体是否满足用户最初提出的那个请求" in p
        assert '"severity": "high/medium/low"' in p

    def test_triple_mode(self, tmp_path, monkeypatch):
        p = self._prompt_for(monkeypatch, tmp_path, "prototype",
                             lambda a: a._triple_review_only("做一个鹈鹕骑车的 SVG 动画"))
        assert "**holistic_walkthrough**" in p

    def test_full_audit_mode_merges_checklist_and_angles(self, tmp_path, monkeypatch):
        p = self._prompt_for(monkeypatch, tmp_path, "architecture",
                             lambda a: a._full_audit("做一个鹈鹕骑车的 SVG 动画"))
        assert "**holistic_coherence**" in p
        # 审计位编号在清单之后顺延，整体项仍属清单段
        assert "按以下角度深入审查:" in p
        assert '"defects_found"' in p and '"overall_assessment"' in p

    def test_holistic_label_matches_role_not_shared(self, tmp_path, monkeypatch):
        """不同角色渲染出的整体项不同——证明它是角色化清单而非统一话术。"""
        a = self._prompt_for(monkeypatch, tmp_path, "acceptance",
                             lambda x: x._pair_review("q", x.ccs[1]))
        b = self._prompt_for(monkeypatch, tmp_path, "visual_designer",
                             lambda x: x._pair_review("q", x.ccs[1]))
        assert "holistic_experience" in a and "holistic_visual" in b
        assert "holistic_visual" not in a and "holistic_experience" not in b


# ============================================================================
# 12. 会话候选角色池对 {available_roles} 面板的过滤
#
# 池是**提示词候选范围**（软约束），不是 PlanValidator 边界；本组只钉面板文本。
# 最关键的一条是向后兼容：未绑池时必须与历史行为逐字节一致。
# ============================================================================

import tempfile  # noqa: E402

from runtime.roles_manager import (  # noqa: E402
    _ROLE_POOL_INVALID_NOTICE,
    _ROLE_POOL_NOTICE,
    build_available_roles_text,
)


def _panel(allowed_roles=None) -> str:
    return build_available_roles_text(ROOT / "roles", allowed_roles)


class TestCandidateRolePoolPanel:
    def test_unbound_panel_lists_every_role_and_default(self):
        """向后兼容回归钉：未绑池 = 历史行为，且不得出现任何池引导行。"""
        panel = _panel(None)

        for role_id in ALL_ROLES:
            assert f"  - **{role_id}**:" in panel, role_id
        assert "  - **default**:" in panel
        assert _ROLE_POOL_NOTICE not in panel
        assert _ROLE_POOL_INVALID_NOTICE not in panel

    def test_unbound_panel_is_byte_identical_to_the_two_arg_default(self):
        assert _panel() == _panel(None)

    def test_filtered_panel_is_exactly_the_pool(self):
        """严格模式：面板 == 池，无例外（连 default 也不列）。"""
        pool = {"code", "engineering_coord"}
        panel = _panel(pool)

        assert "  - **code**:" in panel
        assert "  - **engineering_coord**:" in panel
        assert "  - **test**:" not in panel
        assert "  - **default**:" not in panel
        assert _ROLE_POOL_NOTICE in panel

    def test_pool_of_one_manager_role_drops_the_worker_section(self):
        panel = _panel({"engineering_coord"})

        assert "  ### Manager 角色" in panel
        assert "  ### Worker 角色" not in panel
        assert "  - **engineering_coord**:" in panel

    def test_partially_stale_pool_renders_the_survivors_without_an_alarm(self):
        """只失效一部分时照常渲染剩余角色，不告警 —— 告警只留给全失效。"""
        panel = _panel({"code", "deleted_role"})

        assert "  - **code**:" in panel
        assert "deleted_role" not in panel
        assert _ROLE_POOL_INVALID_NOTICE not in panel

    def test_fully_stale_pool_fails_loud_instead_of_falling_back(self):
        """防「静默回退全量」的钉：池全失效时只输出告警，一个角色名都不出现。"""
        panel = _panel({"ghost", "phantom"})

        assert panel == _ROLE_POOL_INVALID_NOTICE
        assert "  - **code**:" not in panel
        assert "  - **default**:" not in panel
        assert _ROLE_POOL_NOTICE not in panel

    def test_pool_ids_are_not_matched_across_role_type(self, tmp_path):
        """同名角色在 Manager/ 与 Worker/ 下会被 id 一起放行（池存的是扁平 id 列表）。"""
        (tmp_path / "Manager").mkdir()
        (tmp_path / "Worker").mkdir()
        (tmp_path / "Manager" / "shared.yaml").write_text("summary: m\n", encoding="utf-8")
        (tmp_path / "Worker" / "shared.yaml").write_text("summary: w\n", encoding="utf-8")
        (tmp_path / "Worker" / "other.yaml").write_text("summary: o\n", encoding="utf-8")

        panel = build_available_roles_text(tmp_path, {"shared"})

        assert panel.count("  - **shared**:") == 2
        assert "  - **other**:" not in panel

    def test_missing_roles_dir_still_reports_invalidation(self, tmp_path):
        panel = build_available_roles_text(tmp_path / "nope", {"code"})

        assert panel == _ROLE_POOL_INVALID_NOTICE


# ============================================================================
# 13. 面板暴露角色的外部工具通道（mcp）
#
# Manager 选角色时能看到的只有这个面板（summary / suitable_for / not_for / mcp），
# 而 suitable_for 是按领域语义写的、**不承载工具通道**。于是「两个角色领域重叠、
# 只有一个接得上目标系统」时，面板文字对 Manager 完全一样 —— 它只能挑领域最近的，
# 然后那个角色手上根本没有对应工具。本组钉住的是「通道这一事实必须出现在面板里」。
#
# 与之配套的语义解释（`mcp:` 是什么、该怎么据它选角色）在 _ROLE_MCP_NOTICE 里，
# 同组一并钉；它**只在面板里真出现带通道的角色时才插入**。
# ============================================================================

from runtime.roles_manager import _ROLE_MCP_NOTICE  # noqa: E402


def _roles_dir(tmp_path, **by_subdir):
    """按 {子目录: {角色名: yaml 文本}} 建一棵临时 roles/ 树。"""
    for subdir, roles in by_subdir.items():
        (tmp_path / subdir).mkdir(exist_ok=True)
        for role_id, body in roles.items():
            (tmp_path / subdir / f"{role_id}.yaml").write_text(body, encoding="utf-8")
    return tmp_path


class TestRolePanelExposesMcpChannels:
    def test_unbound_panel_is_byte_identical_to_the_two_arg_default(self, tmp_path):
        """向后兼容回归钉：未绑池 + 无通道角色的面板必须与历史行为逐字一致。"""
        root = _roles_dir(
            tmp_path,
            Worker={"plain": "summary: 普通角色\nsuitable_for:\n- 写代码\nmcp: []\n"},
        )
        panel = build_available_roles_text(root, None)

        assert panel == (
            "  ### Worker 角色（可用于 exec_type=\"worker\" 的 step）\n"
            "  格式说明:每条含summary/suitable_for/not_for三件套,"
            "选角色时须综合判断task关键词是否落在 suitable_for 范围且不被 not_for 否决.\n"
            "  - **plain**:\n"
            "      summary: 普通角色\n"
            "      suitable_for: 写代码\n"
        )
        assert _ROLE_MCP_NOTICE not in panel

    def test_role_with_channel_renders_the_line(self, tmp_path):
        root = _roles_dir(tmp_path, Worker={
            "coded": "summary: 带通道的角色\nmcp: [github, jira]\n",
        })

        panel = build_available_roles_text(root, None)

        assert "      mcp: github, jira\n" in panel

    def test_channel_line_sits_under_its_own_role(self, tmp_path):
        """两个角色一个带通道一个不带：mcp 行不能串到隔壁。"""
        root = _roles_dir(tmp_path, Worker={
            "with_ch": "summary: 有通道\nmcp: [github]\n",
            "without": "summary: 没通道\nmcp: []\n",
        })

        panel = build_available_roles_text(root, None)
        with_block = panel.split("  - **with_ch**:")[1].split("  - **without**:")[0]
        without_block = panel.split("  - **without**:")[1]

        assert "mcp: github" in with_block
        assert "mcp:" not in without_block

    def test_notice_is_inserted_only_when_some_role_has_a_channel(self, tmp_path):
        """没有带通道的角色时**不插入**引导 —— 未挂 MCP 的部署提示词不该白白变长。"""
        root = _roles_dir(tmp_path, Worker={"plain": "summary: 普通\nmcp: []\n"})

        assert _ROLE_MCP_NOTICE not in build_available_roles_text(root, None)

        (root / "Worker" / "wired.yaml").write_text(
            "summary: 接了通道\nmcp: [github]\n", encoding="utf-8"
        )
        assert _ROLE_MCP_NOTICE in build_available_roles_text(root, None)

    def test_notice_follows_the_pool_notice(self, tmp_path):
        """两条引导都在时，池引导在前（先说候选范围，再说怎么读候选）。"""
        root = _roles_dir(
            tmp_path,
            Manager={"default": "summary: 基类\nmcp: []\n"},
            Worker={"wired": "summary: 接了通道\nmcp: [github]\n"},
        )

        panel = build_available_roles_text(root, {"wired"})

        assert panel.index(_ROLE_POOL_NOTICE) < panel.index(_ROLE_MCP_NOTICE)

    def test_role_with_channel_but_no_summary_still_lists_the_channel(self, tmp_path):
        """没有 summary 但有通道：不能连通道一起吞掉（那正是「配了等于没配」）。

        注意这里走进的是**另一条分支** —— 原兜底句让人去读 role_md，而 role_md 在
        workspace 边界之外，Manager 读不到。所以有通道时必须自己把通道列出来。
        """
        root = _roles_dir(tmp_path, Worker={"bare": "mcp: [monitoring]\n"})

        panel = build_available_roles_text(root, None)

        assert "      mcp: monitoring\n" in panel
        assert "请直接阅读其 role_md" not in panel

    def test_real_repo_panel_lists_the_probe_channels(self):
        """真实 roles/ 目录：唯一带通道的 mcp_probe 必须把它列出来。"""
        panel = _panel(None)

        assert "  - **mcp_probe**:" in panel
        assert "      mcp: echo-probe\n" in panel
        assert _ROLE_MCP_NOTICE in panel

    def test_stale_pool_alarm_still_wins_over_the_notice(self, tmp_path):
        """池全失效时只输出告警 —— 通道引导不能挤进来。"""
        root = _roles_dir(tmp_path, Worker={"wired": "summary: 接了通道\nmcp: [github]\n"})

        panel = build_available_roles_text(root, {"ghost"})

        assert panel == _ROLE_POOL_INVALID_NOTICE

    def test_notice_contract_explains_external_capability_selection(self, tmp_path):
        root = _roles_dir(
            tmp_path,
            Worker={"wired": "summary: 接了通道\nmcp: [github]\n"},
        )
        panel = build_available_roles_text(root, None)

        assert _ROLE_MCP_NOTICE in panel
        for phrase in (
            "外部工具通道",
            "任务要对接的外部系统",
            "必须选带该通道的角色",
            "候选缺失",
        ):
            assert phrase in _ROLE_MCP_NOTICE


class TestRealRepositoryRoleAndMcpContracts:
    def test_probe_role_is_visible_but_private_lab_roles_are_not(self):
        panel = _panel(None)

        assert "  - **mcp_probe**:" in panel
        assert "      mcp: echo-probe\n" in panel
        assert "_mcp_lab_local" not in panel
        assert "_mcp_lab_remote" not in panel
        assert "_mcp_lab_synth" not in panel

    def test_repository_registry_contains_only_the_valid_probe_server(self):
        registry_path = ROOT / "mcp_servers.json"
        raw = json.loads(registry_path.read_text(encoding="utf-8"))

        assert set(raw["servers"]) == {"echo-probe"}
        echo = raw["servers"]["echo-probe"]
        assert echo["command"] == "python3"
        assert echo["args"] == ["tests/mcp_echo_server.py"]
        assert echo["required"] is True


# End of real-role/MCP contract tests.
