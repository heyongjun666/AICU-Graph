"""tests/test_role_prompt_contract.py — 角色提示词的结构与纪律契约。

与 test_worker_role_prompts.py 的分工：
  - 那个文件钉住**业务边界与文件协议**（谁不做什么、产物字段、VERDICT 块）；
  - 本文件钉住**提示词本身的质量契约**（统一骨架、交付状态、证据要求、
    数字纪律、CLI 无关性、Manager 任务契约、跨角色一致性）。

这些断言是静态的：它们证明提示词文本符合契约，不证明模型行为改变。
"""

import os
import re
from pathlib import Path

import pytest
import yaml

from aicu import AICU

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "roles"
WORKER_DIR = ROLES / "Worker"
MANAGER_DIR = ROLES / "Manager"

WORKERS = sorted(p.stem for p in WORKER_DIR.glob("*.yaml") if not p.stem.startswith("_"))
MANAGERS = sorted(p.stem for p in MANAGER_DIR.glob("*.yaml") if not p.stem.startswith("_"))
DOMAIN_MANAGERS = ["backend_manager", "frontend_manager", "engineering_coord", "release_planner"]

# code/dev_member 没有 index_file，mcp_probe 是联调夹具：它们同样遵守新骨架。
assert WORKERS and MANAGERS


def _cfg(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _worker(role: str) -> dict:
    return _cfg(WORKER_DIR / f"{role}.yaml")


def _manager(role: str) -> dict:
    return _cfg(MANAGER_DIR / f"{role}.yaml")


def _md(role: str) -> str:
    return str(_worker(role).get("role_md") or "")


def _gi(role: str) -> str:
    return str(_worker(role).get("generate_instruction") or "")


# ============================================================================
# 1. 统一骨架
# ============================================================================

SKELETON = ["## 任务适配协议", "## 职责边界", "## 硬规则", "## 交付契约", "## 自检闸门"]
LEGACY_HEADINGS = ["## 铁律", "## 违规红线", "## 完成前自检", "## 核心原则", "## 核心职责"]


class TestWorkerSkeleton:
    @pytest.mark.parametrize("role", WORKERS)
    def test_has_role_title_and_exact_heading_order(self, role):
        md = _md(role)
        assert re.search(r"^# 角色：\S", md, re.M), f"{role} 缺 `# 角色：...` 标题"
        positions = []
        for heading in SKELETON:
            assert md.count(heading) == 1, f"{role} 的 {heading} 出现 {md.count(heading)} 次"
            positions.append(md.index(heading))
        assert positions == sorted(positions), f"{role} 的章节顺序不符合统一骨架"
        assert md.index("# 角色：") < positions[0]

    @pytest.mark.parametrize("role", WORKERS)
    def test_no_legacy_sections(self, role):
        md = _md(role)
        for legacy in LEGACY_HEADINGS:
            assert not re.search(rf"^{re.escape(legacy)}\s*$", md, re.M), \
                f"{role} 仍保留旧章节 {legacy}"

    @pytest.mark.parametrize("role", WORKERS)
    def test_delivery_status_vocabulary(self, role):
        md = _md(role)
        for status in ("DONE", "DONE_WITH_CONCERNS", "NEEDS_CONTEXT", "BLOCKED"):
            assert status in md, f"{role} 缺交付状态 {status}"

    @pytest.mark.parametrize("role", WORKERS)
    def test_delivery_contract_mentions_evidence_and_numbers(self, role):
        md = _md(role)
        assert "证据" in md, f"{role} 的交付契约没有证据要求"
        assert "数字" in md, f"{role} 的交付契约没有数字纪律"

    @pytest.mark.parametrize("role", WORKERS)
    def test_role_md_has_no_placeholders(self, role):
        unknown = re.findall(r"\{[A-Za-z_][A-Za-z0-9_]*\}", _md(role))
        assert not unknown, f"{role}.role_md 不应含占位符：{unknown}"


# ============================================================================
# 2. 可移植性与无人在环
# ============================================================================

_BANNED_TOOL_PROSE = [
    r"\b(Read|Write|Edit|Bash|Grep|Glob)\s*工具",
    r"\bSkill\s*工具",
    r"mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+",
]

_HUMAN_IN_LOOP = ["请用户", "等待用户", "用户裁决", "请示", "交回上游裁决", "需人工确认后"]


class TestPortabilityAndAutonomy:
    @pytest.mark.parametrize("role", WORKERS)
    @pytest.mark.parametrize("field", ["role_md", "generate_instruction"])
    def test_no_cli_specific_tool_names(self, role, field):
        txt = str(_worker(role).get(field) or "")
        for pattern in _BANNED_TOOL_PROSE:
            hit = re.search(pattern, txt)
            assert not hit, f"{role}.{field} 点名了具体工具：{hit.group(0) if hit else ''}"

    @pytest.mark.parametrize("role", WORKERS)
    def test_no_human_in_the_loop(self, role):
        txt = _md(role) + "\n" + _gi(role)
        for bad in _HUMAN_IN_LOOP:
            assert bad not in txt, f"{role} 含人在环表述：{bad}"


def _positive_pending_line(line: str) -> bool:
    if not re.search(r"待确认|TBD|OPEN", line):
        return False
    # 允许作为禁止项出现
    return not re.search(r"不得|禁止|不要|不输出|不出现|不产生|避免|不允许", line)


class TestNoPendingMarkers:
    @pytest.mark.parametrize("path", sorted(ROLES.glob("Worker/*.yaml")) + sorted(ROLES.glob("Manager/*.yaml")))
    def test_no_positive_pending_marker(self, path):
        raw = path.read_text(encoding="utf-8")
        body = "\n".join(
            line for line in raw.splitlines() if not line.lstrip().startswith("#")
        )
        offenders = [line.strip() for line in body.splitlines() if _positive_pending_line(line)]
        assert not offenders, f"{path.name} 存在正向未决标记：{offenders}"


# ============================================================================
# 3. 数字纪律
# ============================================================================

_METRIC_CLAIM = re.compile(
    r"(覆盖率|覆盖率达到|通过率|满意度|吞吐|延迟|耗时|QPS|P95|P99)"
    r"[^\n]{0,16}?\d+(?:\.\d+)?\s*(?:%|ms|毫秒|次|倍|s)?",
    re.I,
)
_METRIC_ALLOW = re.compile(
    r"实测|测得|测量|本轮|目标|假设|预期|要求|建议|推荐|示例|正例|反例|上游|标注|"
    r"禁止|不得|不要|不编造|不臆造|未测量",
)


class TestNumericDiscipline:
    @pytest.mark.parametrize("role", WORKERS)
    def test_metric_claims_are_sourced_or_flagged(self, role):
        txt = _md(role) + "\n" + _gi(role)
        for line in txt.splitlines():
            if _METRIC_CLAIM.search(line) and not _METRIC_ALLOW.search(line):
                pytest.fail(f"{role} 出现无来源指标：{line.strip()}")


# ============================================================================
# 4. 一条规则只有一个家
# ============================================================================

def _sentences(text: str) -> set:
    """切出 >=20 字的规范句（忽略空白），用于跨字段重复检测。"""
    normalized = re.sub(r"\s+", "", text)
    parts = re.split(r"[。！？；\n]", normalized)
    return {p for p in parts if len(p) >= 20}


# requirements 的测试钉住了同一规则在两侧出现，属白名单
_DUP_ALLOWLIST = {
    "requirements": ["普通用户", "可感知", "主动具体化", "EXPLICIT", "验收标准"],
}


class TestNoDuplicatedRules:
    @pytest.mark.parametrize("role", WORKERS)
    def test_role_md_and_instruction_do_not_repeat(self, role):
        overlap = _sentences(_md(role)) & _sentences(_gi(role))
        allow = _DUP_ALLOWLIST.get(role, [])
        overlap = {s for s in overlap if not any(a in s for a in allow)}
        assert not overlap, f"{role} 在 role_md 与 generate_instruction 间重复了规则：{sorted(overlap)}"


# ============================================================================
# 5. Manager 契约
# ============================================================================

class TestManagerContract:
    def test_default_declares_step_task_contract(self):
        gi = str(_manager("default").get("generate_instruction") or "")
        for field in ("Outcome", "Write scope", "Read scope", "Acceptance", "Non-goals", "Failure contract"):
            assert field in gi, f"Manager/default 缺 Step 任务契约字段：{field}"

    def test_default_declares_ownership_and_dependencies(self):
        gi = str(_manager("default").get("generate_instruction") or "")
        for term in ("output_path", "唯一", "depends_on", "循环依赖", "并行"):
            assert term in gi, f"Manager/default 缺组织规则：{term}"

    @pytest.mark.parametrize("role", DOMAIN_MANAGERS)
    def test_domain_fragments_are_domain_deltas_only(self, role):
        md = str(_manager(role).get("role_md") or "")
        for inherited in ("# Identity", "# Workflow", "# Success Criteria", "T1-T4", "决策 1"):
            assert inherited not in md, f"{role} 仍在重复继承内容：{inherited}"
        for section in ("## 领域定位与成功判据", "## 领域拆分轴", "## 领域契约与证据", "## 领域失败模式"):
            assert section in md, f"{role} 缺领域章节：{section}"

    def test_release_planner_drops_removed_protocol_terms(self):
        md = str(_manager("release_planner").get("role_md") or "")
        for stale in ("反条件", "豁免规则", "exemption A"):
            assert stale not in md, f"release_planner 仍引用已移除概念：{stale}"


# ============================================================================
# 6. 跨角色一致性
# ============================================================================

class TestCrossRoleContracts:
    def test_grill_verdict_is_in_schema_and_consumed(self):
        grill_gi = _gi("grill")
        for field in ("verdict", "reason", "evidence"):
            assert f"**{field}**" in grill_gi, f"grill finding 缺字段 {field}"
        assert "accept" in grill_gi and "reject" in grill_gi

        req = _gi("requirements") + _md("requirements")
        for term in ("裁决清单", "已拒绝质询", "accept", "reject"):
            assert term in req, f"requirements 未消费 grill 裁决：{term}"

    @pytest.mark.parametrize("role", ["test", "acceptance"])
    def test_verdict_gate_is_strict_and_identical(self, role):
        txt = _gi(role)
        assert "passed = (defects == [])" in txt, f"{role} 未采用统一判定口径"
        assert "severity" in txt

    def test_no_unenforced_acceptance_promise(self):
        for role in WORKERS:
            assert "验收时会核对" not in (_gi(role) + _md(role)), \
                f"{role} 仍在承诺没有节点执行的验收"


# ============================================================================
# 7. 渲染烟测
# ============================================================================

def _make_aicu(tmp_path, role, role_type="worker"):
    ws = tmp_path / f"ws-{role_type}-{role}"
    ws.mkdir(parents=True, exist_ok=True)
    return AICU(
        role_type=role_type,
        role=role,
        parent_workspace=str(ws),
        skills=[],
        modular_output=True,
        output_file=None,
    )


class TestRenderSmoke:
    @pytest.mark.parametrize("role", WORKERS)
    def test_worker_renders_without_unresolved_placeholders(self, tmp_path, role):
        aicu = _make_aicu(tmp_path, role)
        assert SKELETON[0] in aicu.role_md_content
        rendered = aicu._format_instruction()
        leftovers = set(re.findall(r"\{[A-Za-z_][A-Za-z0-9_]*\}", rendered))
        assert not leftovers, f"{role} 渲染后仍有占位符：{leftovers}"

    @pytest.mark.parametrize("role", MANAGERS)
    def test_manager_renders_without_unresolved_placeholders(self, tmp_path, role):
        aicu = _make_aicu(tmp_path, role, role_type="manager")
        rendered = aicu._format_instruction()
        leftovers = set(re.findall(r"\{[A-Za-z_][A-Za-z0-9_]*\}", rendered))
        assert not leftovers, f"Manager/{role} 渲染后仍有占位符：{leftovers}"


# ============================================================================
# 8. 长度预算（防止提示词只增不减地膨胀）
# ============================================================================

# 预算按"当前规模 + 约 15% 余量"设定。想加规则时先删/合并旧规则，
# 而不是无上限追加 —— 这正是本组断言存在的意义。
WORKER_MD_MAX_LINES, WORKER_MD_MAX_BYTES = 70, 7_500
WORKER_GI_MAX_LINES, WORKER_GI_MAX_BYTES = 65, 5_000
DOMAIN_MANAGER_MD_MAX_LINES, DOMAIN_MANAGER_MD_MAX_BYTES = 45, 4_000
DEFAULT_MANAGER_GI_MAX_LINES, DEFAULT_MANAGER_GI_MAX_BYTES = 380, 30_000


class TestLengthBudget:
    @pytest.mark.parametrize("role", WORKERS)
    def test_worker_role_md_within_budget(self, role):
        md = _md(role)
        assert len(md.splitlines()) <= WORKER_MD_MAX_LINES, f"{role} role_md 超行数预算"
        assert len(md.encode()) <= WORKER_MD_MAX_BYTES, f"{role} role_md 超字节预算"

    @pytest.mark.parametrize("role", WORKERS)
    def test_worker_gi_within_budget(self, role):
        gi = _gi(role)
        assert len(gi.splitlines()) <= WORKER_GI_MAX_LINES, f"{role} generate_instruction 超行数预算"
        assert len(gi.encode()) <= WORKER_GI_MAX_BYTES, f"{role} generate_instruction 超字节预算"

    @pytest.mark.parametrize("role", DOMAIN_MANAGERS)
    def test_domain_manager_role_md_within_budget(self, role):
        md = str(_manager(role).get("role_md") or "")
        assert len(md.splitlines()) <= DOMAIN_MANAGER_MD_MAX_LINES, f"{role} role_md 超行数预算"
        assert len(md.encode()) <= DOMAIN_MANAGER_MD_MAX_BYTES, f"{role} role_md 超字节预算"

    def test_default_manager_gi_within_budget(self):
        gi = str(_manager("default").get("generate_instruction") or "")
        assert len(gi.splitlines()) <= DEFAULT_MANAGER_GI_MAX_LINES, "Manager/default 超行数预算"
        assert len(gi.encode()) <= DEFAULT_MANAGER_GI_MAX_BYTES, "Manager/default 超字节预算"
