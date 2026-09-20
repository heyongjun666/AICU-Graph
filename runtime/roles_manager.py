"""
runtime/roles_manager.py — 角色配置管理（Manager/Worker 二级目录版）

扫描 roles/Manager/ 和 roles/Worker/ 两个子目录下的 YAML 文件，
提供角色列表查询和新增能力。

目录结构：
  roles/
    Manager/
      default.yaml    # Manager 角色默认配置
      (更多 Manager 角色)
    Worker/
      default.yaml    # Worker 角色默认配置
      (更多 Worker 角色)

新增角色只需在对应子目录下放置 {role_id}.yaml 文件（或通过 API 创建）。
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional, Set

import yaml

logger = logging.getLogger(__name__)

# role_id 合法字符：小写字母、数字、下划线
_ROLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# description 最大长度
_DESC_MAX_LEN = 80

# Manager/Worker 子目录名称
_ROLE_SUBDIRS = ("Manager", "Worker")

# 表单字段按用户配置流程排序；未列出的扩展字段追加在末尾。
_ROLE_FIELD_ORDER = (
    "summary", "description", "suitable_for", "not_for",
    "num_ccs", "model", "cli_name", "audit_rounds",
    "skills_dir", "skills", "mcp",
    "output_file", "final_file", "modular_output", "main_doc",
    "generate_instruction", "revise_instruction", "review_checklist",
    "adversarial_role", "audit_role", "audit_angles", "role_md",
)

# 高级配置字段：普通用户不需要理解、也不常改动的字段，前端默认收进「高级配置」折叠区。
# 基础字段（其余）= summary/description/suitable_for/not_for/skills/role_md 等。
_ROLE_ADVANCED_FIELDS = {
    "model", "num_ccs", "cli_name", "audit_rounds", "skills_dir",
    "output_file", "final_file", "modular_output", "main_doc",
    "generate_instruction", "revise_instruction", "review_checklist",
    "adversarial_role", "audit_role", "audit_angles",
    "deliverable_dir",
}

# 不展示在角色创建表单中的字段：这些字段有运行时兜底默认值（如 index_file 恒为
# _INDEX.md 且缺失时自动生成），暴露给用户只会增加困惑。它们仍可出现在角色 yaml 中，
# 校验照常放行，只是前端 schema 不返回、表单不渲染。
_HIDDEN_SCHEMA_FIELDS = frozenset({"index_file"})

# Manager 角色创建表单额外隐藏的「协议/样板」字段：这些字段（Decision Policy 生成指令、
# 审查/审计/产出/交付协议）由 default.yaml 统一定义并在所有 Manager 间复用，属于运行时
# 协议而非「该 Manager 在实际领域中扮演的角色」。表单只保留 summary/description/
# suitable_for/not_for/role_md 等身份字段 + num_ccs/model/cli_name/audit_rounds/skills
# 等简单执行配置。字段仍可出现在角色 yaml 中（校验照常放行），只是 schema 不返回。
_MANAGER_HIDDEN_SCHEMA_FIELDS = frozenset({
    "generate_instruction", "revise_instruction",
    "review_checklist", "adversarial_role", "audit_role", "audit_angles",
    "output_file", "final_file", "modular_output", "main_doc",
    "deliverable_dir",
})


# 绑定候选角色池时，在角色面板最前面插入的引导（未绑定时不插入，保证历史会话提示词逐字不变）。
# 两个作用：① 说明候选被限定，避免 Manager 去引用面板外的角色；② 池覆盖不足时的正确动作 ——
# roles/Manager/default.yaml 的通用建议是「建议新增专用角色」，有了池之后那句会把 Manager
# 往池外推，这里就地覆盖掉。
_ROLE_POOL_NOTICE = (
    "  【候选角色池】本会话的候选角色已限定在下方清单内，清单之外的角色不在本会话候选范围内。\n"
    "  若清单内无角色能覆盖某个 Capability，请勿强行套用最近邻，也不要使用清单外的角色名；\n"
    "  应在输出中如实标注候选缺失。"
)

# 池内角色全部不存在时（多半是后续被删）的失效告警。绝不静默回退全量角色 ——
# 那会让用户以为池在生效，实际它已退化成不限制。
_ROLE_POOL_INVALID_NOTICE = (
    "  ⚠ 本会话绑定的候选角色池已失效：池内角色均已不存在（可能已被删除）。\n"
    "  请勿改用池外的角色；请先在会话中重新绑定候选角色池，或如实说明候选角色缺失。"
)

# 面板里**出现带 mcp 的角色时**才插入的引导（一个都没有时不插入，未挂 MCP 的部署提示词逐字不变，
# 与 _ROLE_POOL_NOTICE 同一个纪律）。为什么非要有这一段：`mcp:` 这一行是角色可用的**外部系统**
# 通道（issue 追踪、知识库、监控……），而 suitable_for/not_for 是按领域语义写的、不承载通道。
# 于是「两个角色领域重叠、只有一个接得上目标系统」时，面板里的文字对 Manager 完全一样 ——
# 它没有任何依据做区分，只能挑一个领域最近的，然后那个角色手上根本没有对应工具。
# 这一段的落点必须是「选角色」这一步：等节点建出来再补救就已经选错了人。
_ROLE_MCP_NOTICE = (
    "  【外部工具通道】下方部分角色带 `mcp:` 行，列出该角色可用的**外部系统工具**"
    "（如 issue 追踪、知识库、监控平台）。\n"
    "  选角色时把「任务要对接的外部系统」也当作一项 Capability 来核对：任务明确要求读写某个"
    "外部系统时，必须选带该通道的角色，不要选一个领域最近、却只能靠命令行或 HTTP 自己绕过去的角色"
    "（那样做出来的产物对不上真系统，且从产物里看不出来它绕过）。\n"
    "  若清单内没有任何角色带所需通道，如实标注候选缺失，不要强行套用最近邻。\n"
    "  （审查阶段同样按这一条核对：只看 suitable_for 会放过「领域对口、却接不上目标系统」的选择。）"
)


def build_available_roles_text(
    roles_parent: Path, allowed_roles: Optional[Set[str]] = None
) -> str:
    """扫描 roles/Manager/ 和 roles/Worker/ 目录，动态生成可用角色列表文本。

    用于替换 generate_instruction 中的 {available_roles} 占位符。

    本函数与同模块的 ``_extract_description`` / ``RolesManager.list_roles`` 扫的是
    同一批目录，所以同处本模块 —— 子目录清单也只能有一份（``_ROLE_SUBDIRS``）：
    各写一遍的话，新增一类角色目录时，界面下拉里会出现它、Manager 的角色面板里
    却看不到。

    注意**不要**把这里的 ``SUMMARY_MAX_LEN``（140）与 ``_extract_description`` 的
    ``_DESC_MAX_LEN``（80）合并：前者是塞进 prompt 的面板行、要留住关键词，
    后者是列表页一行摘要、要防撑破布局，两个长度是各自场景定的。

    每个角色可选择性定义以下结构化字段(在角色yaml顶层):
      summary     : str  — 一句话精炼描述该角色的核心职责与擅长范围
      suitable_for: List[str] | str — 关键词清单, 表示本角色适合承接的任务特征
      not_for     : List[str] | str — 反向清单, 表示本角色不应被分配的任务类型

    若某字段缺失则按以下顺序回退:
      summary 缺失 → 从 role_md 第一行非纯单词标题内容截取前 N 字
      suitable_for / not_for 缺失 → 该行不输出

    Args:
        roles_parent: roles/ 父目录的 Path。
        allowed_roles: 会话候选角色池（角色 id 集合）。非 None 时**严格只列池内角色**
            —— 不并上 default，因为 default 不在 RolesManager 的可选角色里，让它出现
            会让 Manager 看到一个本不该被当作候选的条目。None 表示未绑定，列全部。

    Returns:
        格式化后的角色列表文本（含 Manager 角色和 Worker 角色两段）。
    """
    SUMMARY_MAX_LEN = 140   # 单条summary最大字符数 防止过长污染prompt

    def _normalize_list(val):
        """把可能是str/list的字段统一成干净字符串清单."""
        if val is None:
            return []
        if isinstance(val, str):
            # 允许用换行或分号分隔的多项写法
            raw_items = re.split(r"[\n;]+", val)
            out = []
            for it in raw_items:
                s = it.strip().lstrip("-").lstrip("*").lstrip("•").strip()
                if s and len(s) <= 100:
                    out.append(s[:100])
                elif s:
                    out.append(s[:100] + "…")
            return out
        if isinstance(val, (list, tuple)):
            out = []
            for it in val:
                s = str(it).strip()
                if s and len(s) <= 100:
                    out.append(s)
                elif s:
                    out.append(s[:100] + "…")
            return out
        return [str(val)[:100]]

    def _fallback_summary_from_role_md(role_md_text):
        """从 role_md 文本提取首句作为兜底摘要."""
        for line in (role_md_text or "").split("\n"):
            stripped = line.strip().lstrip("#").lstrip("-").lstrip("*").strip()
            if stripped and not re.match(r"^[A-Za-z\s]+$", stripped):
                return stripped[:SUMMARY_MAX_LEN]
        return ""

    lines = []
    matched = 0                 # 池内真实存在（可渲染）的角色数，用于 fail-loud 判定
    has_mcp_role = False        # 面板里有没有带外部工具通道的角色（决定插不插 _ROLE_MCP_NOTICE）
    for subdir_name in _ROLE_SUBDIRS:
        subdir = roles_parent / subdir_name
        if not subdir.is_dir():
            continue
        role_files = sorted(
            p.stem for p in subdir.glob("*.yaml")
            if not p.stem.startswith("_")
        )
        if allowed_roles is not None:
            # 严格模式：面板 == 池，无例外（default 也不列）
            role_files = [r for r in role_files if r in allowed_roles]
            matched += len(role_files)
        if not role_files:
            continue
        lines.append(f"  ### {subdir_name} 角色（可用于 exec_type=\"{subdir_name.lower()}\" 的 step）")
        lines.append(f"  格式说明:每条含summary/suitable_for/not_for三件套,"
                     f"选角色时须综合判断task关键词是否落在 suitable_for 范围且不被 not_for 否决.")
        for r in role_files:
            data = None
            try:
                with open(subdir / f"{r}.yaml", "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
            except Exception:
                pass
            if not isinstance(data, dict):
                data = {}

            # ---- 提取 summary ----
            summary_raw = data.get("summary", "")
            if isinstance(summary_raw, str) and summary_raw.strip():
                summary = summary_raw.strip()[:SUMMARY_MAX_LEN]
            elif isinstance(summary_raw, (list, tuple)) and summary_raw:
                joined = "; ".join(str(x).strip() for x in summary_raw if str(x).strip())
                summary = joined[:SUMMARY_MAX_LEN]
            else:
                summary = _fallback_summary_from_role_md(data.get("role_md"))

            # ---- 提取 suitable_for / not_for ----
            suit_list = _normalize_list(data.get("suitable_for"))
            nfor_list = _normalize_list(data.get("not_for"))

            # ---- 提取 mcp（外部系统通道）----
            # 这一行是 Manager 唯一能据以「按工具通道选角色」的依据，见 _ROLE_MCP_NOTICE。
            # 为空时**不输出**：绝大多数角色都是 mcp: []，它们的面板必须逐字节不变。
            mcp_list = _normalize_list(data.get("mcp"))
            if mcp_list:
                has_mcp_role = True

            if summary:
                lines.append(f"  - **{r}**:")
                indent = "      "
                lines.append(f"{indent}summary: {summary}")
                if suit_list:
                    lines.append(f"{indent}suitable_for: {', '.join(suit_list)}")
                if nfor_list:
                    lines.append(f"{indent}not_for: {', '.join(nfor_list)}")
                if mcp_list:
                    lines.append(f"{indent}mcp: {', '.join(mcp_list)}")
            elif mcp_list:
                # 没有 summary 但有通道：不能连通道一起吞掉 —— 那正是这里要修的
                # 「配了等于没配」。原来的兜底句让人去读 role_md，而 role_md 在
                # workspace 边界之外，Manager 读不到。
                lines.append(f"  - **{r}**:")
                indent = "      "
                lines.append(f"{indent}mcp: {', '.join(mcp_list)}")
            else:
                lines.append(f"  - **{r}**: （未提供描述信息，请直接阅读其 role_md 内容判断适用边界）")
        lines.append("")

    if allowed_roles is not None and matched == 0:
        # 池内角色全部不存在（多半是后续被删了）。绝不静默回退全量 ——
        # 那会让用户以为池在生效，实际它已退化成不限制。
        return _ROLE_POOL_INVALID_NOTICE

    if not lines:
        return "  （暂无可用角色）"

    # 前缀按需拼：未绑池时不插池引导；面板里没有带 mcp 的角色时不插通道引导。
    # 两条都属于「这段文字只在相关时才该出现」，写死会让绝大多数会话的提示词白白变长。
    prefix = []
    if allowed_roles is not None:
        prefix.append(_ROLE_POOL_NOTICE)
    if has_mcp_role:
        prefix.append(_ROLE_MCP_NOTICE)

    return "\n".join(prefix + lines)


def _id_to_display_name(role_id: str) -> str:
    """将 role_id 转成可读名称：dev_lead -> Dev Lead。"""
    return " ".join(word.capitalize() for word in role_id.split("_"))


def _extract_description(data: Any) -> str:
    """从 yaml 解析结果中提取 description。

    取 role_md 字段的首行有意义的文本（跳过 # 标题和单词标题）。
    无 role_md 或解析失败时返回空字符串。
    """
    if not isinstance(data, dict):
        return ""
    rm = data.get("role_md", "")
    if not rm or not isinstance(rm, str):
        return ""
    for line in rm.split("\n"):
        line = line.strip().lstrip("#").strip()
        if not line:
            continue
        # 跳过单词标题（Identity / Mission / Workflow 等）
        if re.match(r"^[A-Za-z]+$", line):
            continue
        return line[:_DESC_MAX_LEN]
    return ""


def _parse_role_yaml(file_path: str) -> dict:
    """解析单个角色 YAML 文件。

    Returns:
        dict 或 None（解析失败时）。
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        logger.warning("Failed to parse role file %s: %s", file_path, e)
        return None


class RolesManager:
    """角色配置管理器（Manager/Worker 二级目录版）。

    职责:
        - list_roles: 扫描 roles/Manager/ 和 roles/Worker/ 目录，
                      返回 [{id, display_name, description, type}]
        - add_role: 新增角色 yaml 文件（需指定 type="manager" 或 "worker"）
    """

    def __init__(self, roles_dir: str):
        """初始化 RolesManager。

        Args:
            roles_dir: roles/ 父目录的绝对路径（包含 Manager/ 和 Worker/ 子目录）。
        """
        self._roles_dir = roles_dir

    def list_roles(self) -> list[dict]:
        """扫描 roles/Manager/ 和 roles/Worker/ 下所有 .yaml 文件，返回角色列表。

        Returns:
            按 type + id 排序的 [{id, display_name, description, type}] 列表。
        """
        result: list[dict] = []

        if not os.path.isdir(self._roles_dir):
            logger.warning("Roles directory does not exist: %s", self._roles_dir)
            return result

        for subdir in _ROLE_SUBDIRS:
            subdir_path = os.path.join(self._roles_dir, subdir)
            if not os.path.isdir(subdir_path):
                logger.debug("Roles subdirectory does not exist: %s", subdir_path)
                continue

            role_type = subdir.lower()  # "Manager" → "manager", "Worker" → "worker"
            for name in os.listdir(subdir_path):
                if not name.endswith(".yaml"):
                    continue
                role_id = name[: -len(".yaml")]

                # 跳过 default.yaml（作为基类配置不暴露为可选角色）
                if role_id == "default":
                    continue

                # 文件名不是一个合法 role_id 的，跳过不列。这类文件**列出来也打不开**
                # （``_role_path`` 判它非法、接口返 400），却会被 ``iter_role_configs``
                # 拿去逐个读取 —— 于是它会先把「谁引用了这个 mcp / skill」这类遍历整轮
                # 的查询带崩，症状是完全无关的接口返 500。典型来源是 ``_`` 开头的临时
                # 角色文件（``build_available_roles_text`` 也是明确跳过它们的）。
                if not _ROLE_ID_PATTERN.fullmatch(role_id):
                    logger.warning(
                        "Skipping role file %s: '%s' is not a valid role_id",
                        os.path.join(subdir_path, name), role_id,
                    )
                    continue

                file_path = os.path.join(subdir_path, name)
                data = _parse_role_yaml(file_path)
                description = _extract_description(data) if data else ""

                result.append({
                    "id": role_id,
                    "display_name": _id_to_display_name(role_id),
                    "description": description,
                    "type": role_type,  # "manager" 或 "worker"
                })

        result.sort(key=lambda r: (r["type"], r["id"]))
        return result

    def _load_default_config(self, role_type: str) -> dict[str, Any]:
        default_path = self._role_path("default", role_type)
        if not os.path.isfile(default_path):
            return {}
        with open(default_path, "r", encoding="utf-8") as file:
            return yaml.safe_load(file) or {}

    def _effective_role_config(self, role_id: str, role_type: str) -> dict[str, Any]:
        """default.yaml 为基础、{role}.yaml 覆盖的合并配置（轻量，不走 schema）。"""
        defaults = self._load_default_config(role_type)
        file_path = self._role_path(role_id, role_type)
        if not os.path.isfile(file_path):
            raise KeyError(f"Role '{role_id}' not found")
        with open(file_path, "r", encoding="utf-8") as file:
            role_cfg = yaml.safe_load(file) or {}
        return {**defaults, **role_cfg}

    def iter_role_configs(self, *, include_defaults: bool = False):
        """逐个产出 ``(role_id, role_type, effective_config)``。

        给「谁引用了某个名字」这类反向查询用（如 MCP 注册表的 ``roles_referencing``）。

        返回的必须是**合并 default 之后**的配置：在 ``default.yaml`` 里声明 mcp、skills
        这类列表恰恰是最常见的用法，只看角色自己那份会漏掉全部引用者 —— 而漏报的后果是
        用户以为某个东西没人用，删掉了。默认情况下保持 MCP 查询的历史语义，不把 default
        本身列为可选角色；需要覆盖节点可选的 default 时显式传入 ``include_defaults``。

        不做成 HTTP 端点：它把每个角色的完整配置都读出来，而 ``list_roles()`` 返回的是给
        下拉框用的轻量列表。两者用途不同，别混成一条接口。
        """
        entries = []
        if include_defaults:
            entries.extend(
                {"id": "default", "type": subdir.lower()}
                for subdir in _ROLE_SUBDIRS
            )
        entries.extend(self.list_roles())
        for entry in entries:
            role_id, role_type = entry["id"], entry["type"]
            try:
                yield role_id, role_type, self._effective_role_config(role_id, role_type)
            except yaml.YAMLError as error:
                # 单个角色的 yaml 坏掉不该让整轮查询失败：调用方问的是「还有谁在引用」，
                # 一个坏文件不该把其余角色的答案一起吞掉。它本来也跑不起来，会另有报错。
                logger.warning("Skipping role %s: invalid YAML — %s", role_id, error)
            except KeyError:
                continue

    def roles_referencing_skill(self, skill_id: str) -> list[dict[str, str]]:
        """返回声明该 Skill 的角色，包含两类 default 及其有效配置继承者。"""
        hits: set[tuple[str, str]] = set()
        for role_id, role_type, config in self.iter_role_configs(include_defaults=True):
            skills = config.get("skills") if isinstance(config, dict) else None
            if isinstance(skills, str):
                names = [skills]
            elif isinstance(skills, list):
                names = [str(item) for item in skills]
            else:
                names = []
            if skill_id in names:
                hits.add((role_type, role_id))
        return [
            {"id": role_id, "type": role_type}
            for role_type, role_id in sorted(hits)
        ]

    def _get_field_catalog(
        self, role_type: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return defaults and every field already used by this role type."""
        defaults = self._load_default_config(role_type)
        samples = dict(defaults)
        subdir_path = os.path.join(self._roles_dir, role_type.capitalize())
        if os.path.isdir(subdir_path):
            for filename in sorted(os.listdir(subdir_path)):
                if filename == "default.yaml" or not filename.endswith(".yaml"):
                    continue
                data = _parse_role_yaml(os.path.join(subdir_path, filename))
                if not isinstance(data, dict):
                    continue
                for name, value in data.items():
                    if name.startswith("_"):
                        continue
                    if name not in samples or samples[name] is None:
                        samples[name] = value
        return defaults, samples

    def get_role_schema(self, role_type: str) -> dict[str, Any]:
        defaults, samples = self._get_field_catalog(role_type)
        labels_en = {
            "summary": "Summary", "description": "Description",
            "suitable_for": "Suitable for", "not_for": "Not for",
            "num_ccs": "Collaboration mode", "model": "Model", "cli_name": "CLI",
            "audit_rounds": "Audit rounds", "skills_dir": "Skills directory", "skills": "Skills",
            "mcp": "MCP servers",
            "output_file": "Intermediate output", "final_file": "Final output",
            "modular_output": "Modular output", "main_doc": "Main document",
            "generate_instruction": "Generate instruction", "revise_instruction": "Revise instruction",
            "review_checklist": "Review checklist", "adversarial_role": "Adversarial role",
            "audit_role": "Audit role", "audit_angles": "Audit angles", "role_md": "Role instructions",
            "deliverable_dir": "Deliverable directory",
        }
        labels_zh = {
            "summary": "摘要", "description": "描述",
            "suitable_for": "适用场景", "not_for": "不适用场景",
            "num_ccs": "协作模式", "model": "模型", "cli_name": "CLI",
            "audit_rounds": "审计轮数", "skills_dir": "Skills 目录", "skills": "Skills",
            "mcp": "MCP Server",
            "output_file": "中间产出", "final_file": "最终产出",
            "modular_output": "模块化产出", "main_doc": "主文档",
            "generate_instruction": "生成指令", "revise_instruction": "修订指令",
            "review_checklist": "审查清单", "adversarial_role": "对抗角色",
            "audit_role": "审计角色", "audit_angles": "审计角度", "role_md": "角色指令",
            "deliverable_dir": "交付目录",
        }
        descriptions_zh = {
            "summary": "一句话精炼描述该角色的核心职责与擅长范围，供 Manager 选角时参考。",
            "description": "详细说明角色的能力边界、适用场景和限制。",
            "suitable_for": "本角色适合承接的任务类型关键词清单，Manager 据此匹配子节点角色。",
            "not_for": "本角色不应被分配的任务类型关键词清单，用于反向排除。",
            "num_ccs": "协作模式（1-4，也对应任务重要性）：1=简单任务，单 Agent 独立完成（生成+自审）（默认）；2=普通任务，生成+审查；3=重要/关键路径任务，双 Agent 独立产出+评审融合；4=极高风险任务，双产出+评审+审计的完整闭环。",
            "model": "所有环节：Agent 调用的模型名。解析优先级：本处显式 > 角色 yaml 显式 > 系统设置 > CLI 自身默认（无代码兜底默认模型），留空表示跟随系统设置。",
            "cli_name": "所有环节：Agent 使用的 CLI 命令，留空表示用系统默认。",
            "audit_rounds": "审计阶段：对抗审查的最大轮数。每轮深度审计并自动修订，0 表示跳过审计。",
            "skills_dir": "所有环节：Skill 源目录。Agent 从中加载 skill，按所选 CLI 落到它自己的项目级 skill 目录（claude/internal 是 .claude/skills，codebuddy 是 .codebuddy/skills，kimi 是 .agents/skills，qwen 是 .qwen/skills，pi 是 .pi/skills，依此类推）。",
            "skills": "所有环节：启用的 skill 名称列表，执行时作为 project-level skill 加载。尚未接入该机制的 CLI（grok/opencode）会照常运行但打印一行警告，本次声明的 skill 不生效。",
            "mcp": "所有环节：启用的 MCP server 名称列表，名字定义在仓库根 mcp_servers.json 中。执行前会逐个预检连通性，连不上则该节点失败。",
            "output_file": "生成阶段：单文件模式下 Agent 写入结果的文件名（如 output）。modular_output=true 时通常设为 null。",
            "final_file": "交付阶段：AICU 从最优 Agent 复制来的最终产出文件名/目录名（如 plan_result）。",
            "modular_output": "生成阶段：是否启用模块化多文件产出。true 时 Agent 可生成多个独立文件；false 时写单个 output_file。",
            "main_doc": "交付阶段：下游节点优先读取的主文档名（如 design.md、test_report.md）。缺失时回退到索引文件 _INDEX.md。",
            "generate_instruction": "生成阶段：Agent 生成产出时的 prompt 模板，支持 {output_file}、{deliverable_dir} 等占位符。设为空字符串表示不追加生成指令。",
            "revise_instruction": "修订阶段：Agent 按审查意见修订产出时的 prompt 模板，{output_path} 会被替换为产出文件路径。",
            "review_checklist": "审查阶段：审查 Agent 逐项打分的检查清单，每项格式为 [检查点名称, 检查描述]。",
            "adversarial_role": "审查阶段：审查 Agent 的人格补充，以质疑视角找出方案缺陷。",
            "audit_role": "审计阶段：审计 Agent 的人格定义，专注发现隐性风险、技术债与安全漏洞。",
            "audit_angles": "审计阶段：审计 Agent 逐一排查的风险维度列表。",
            "role_md": "所有环节：Agent 的角色人格定义，描述身份、使命、工作流程与产出要求。",
            "deliverable_dir": "交付阶段：交付物在 workspace 内的子目录，技能内部产物隔离在根目录。默认 deliverables。",
        }
        descriptions_en = {
            "summary": "One-line description of the role's core responsibilities and strengths, shown to the Manager when matching roles.",
            "description": "Detailed explanation of the role's capabilities, applicable scenarios, and limitations.",
            "suitable_for": "Keywords for task types this role is suited for; the Manager matches child-node roles against these.",
            "not_for": "Keywords for task types this role should NOT be assigned, used for reverse exclusion.",
            "num_ccs": "Collaboration mode (1-4, also the task-importance tier): 1=simple task, single agent (generate+self-review) (default); 2=normal task, generate+review; 3=important/critical-path task, two agents produce + merge review; 4=extreme-risk task, full loop of produce+review+audit.",
            "model": "All phases: model name the agent calls. Resolution: explicit here > role YAML explicit > system settings > CLI default (no hardcoded fallback); leave empty to follow system settings.",
            "cli_name": "All phases: CLI command the agent uses; leave empty for the system default.",
            "audit_rounds": "Audit phase: max adversarial audit rounds. Each round deep-audits output and auto-revises; 0 skips auditing.",
            "skills_dir": "All phases: skill source directory. The agent loads skills from it into the selected CLI's own project-level skill directory (claude/internal: .claude/skills, codebuddy: .codebuddy/skills, kimi: .agents/skills, qwen: .qwen/skills, pi: .pi/skills, and so on).",
            "skills": "All phases: list of enabled skill names, loaded as project-level skills during execution. CLIs not yet wired for this (grok/opencode) still run, but print a warning that the declared skills have no effect.",
            "mcp": "All phases: list of enabled MCP server names, defined in mcp_servers.json at the repo root. Connectivity is pre-checked before execution; an unreachable server fails the node.",
            "output_file": "Generation phase: filename the agent writes its result to in single-file mode (e.g. output). Usually null when modular_output=true.",
            "final_file": "Delivery phase: final output filename/dir name copied from the best agent (e.g. plan_result).",
            "modular_output": "Generation phase: enable modular multi-file output. When true the agent can create multiple independent files; when false it writes a single output_file.",
            "main_doc": "Delivery phase: main document name downstream nodes read first (e.g. design.md, test_report.md), falling back to the _INDEX.md index file.",
            "generate_instruction": "Generation phase: prompt template for the agent's generation step. Supports {output_file}, {deliverable_dir} placeholders; empty string disables the appended instruction.",
            "revise_instruction": "Revision phase: prompt template used when the agent revises output per review; {output_path} is replaced with the output path.",
            "review_checklist": "Review phase: checklist the review agent scores against; each item is [checkpoint name, description].",
            "adversarial_role": "Review phase: persona added to the review agent, finding defects from a skeptical view.",
            "audit_role": "Audit phase: persona definition of the audit agent, focused on hidden risks, tech debt, and security holes.",
            "audit_angles": "Audit phase: list of risk dimensions the audit agent checks one by one.",
            "role_md": "All phases: role persona definition describing identity, mission, workflow, and output requirements.",
            "deliverable_dir": "Delivery phase: subdirectory for deliverables within the workspace; skill-internal outputs are isolated at the root. Default deliverables.",
        }
        # Manager 与 Worker 的 summary/suitable_for/not_for 语义不同：
        # Manager = 组织/规划/协调能力，Worker = 具体执行能力。示例需按角色类型区分，
        # 否则两个表单会显示相同示例，误导用户。
        manager_placeholders_zh = {
            "summary": "例如：将复合目标拆解为可并行推进的执行组织，并协调各执行角色",
            "suitable_for": "例如：多异构模块并行推进、跨模块接口契约定义、关键路径调度与并行化",
            "not_for": "例如：具体代码实现、UI 原型与交互设计、需求分析与文档编写",
        }
        manager_placeholders_en = {
            "summary": "e.g. decomposes a compound goal into a parallelizable execution organization and coordinates roles",
            "suitable_for": "e.g. multi-module engineering, cross-module interface contracts, critical-path scheduling & parallelization",
            "not_for": "e.g. concrete code implementation, UI prototyping & interaction design, requirements analysis & doc writing",
        }
        worker_placeholders_zh = {
            "summary": "例如：负责后端接口开发与单元测试，产出可交付的实现",
            "suitable_for": "例如：通用编程任务、文档编写、后端接口实现、单元测试",
            "not_for": "例如：（无特别排除项时留空，作为兜底角色应尽量通用）",
        }
        worker_placeholders_en = {
            "summary": "e.g. builds backend APIs with unit tests, producing a deliverable implementation",
            "suitable_for": "e.g. general programming, documentation, backend API implementation, unit testing",
            "not_for": "e.g. (leave empty if no exclusions; keep general as a fallback role)",
        }
        placeholders_zh = manager_placeholders_zh if role_type == "manager" else worker_placeholders_zh
        placeholders_en = manager_placeholders_en if role_type == "manager" else worker_placeholders_en
        ordered_names = [name for name in _ROLE_FIELD_ORDER if name in samples]
        ordered_names.extend(sorted(set(samples) - set(ordered_names)))
        hidden = _HIDDEN_SCHEMA_FIELDS
        if role_type == "manager":
            hidden = hidden | _MANAGER_HIDDEN_SCHEMA_FIELDS
        ordered_names = [name for name in ordered_names if name not in hidden]
        fields = []
        for name in ordered_names:
            value = samples[name]
            if isinstance(value, bool):
                field_type = "boolean"
            elif isinstance(value, int):
                field_type = "number"
            elif isinstance(value, list):
                field_type = "list"
            elif isinstance(value, str) and (
                "instruction" in name
                or name.endswith("_md")
                or name in {"review_checklist", "adversarial_role", "audit_role"}
            ):
                field_type = "multiline"
            else:
                field_type = "text"
            label = labels_zh.get(name, name.replace("_", " "))
            label_en = labels_en.get(name, name.replace("_", " ").title())
            description = descriptions_zh.get(name, "")
            description_en = descriptions_en.get(name, "")
            placeholder = placeholders_zh.get(name, "")
            placeholder_en = placeholders_en.get(name, "")
            if role_type == "manager" and name == "role_md":
                label = "领域角色"
                label_en = "Domain role"
                description = (
                    "该 Manager 在实际领域扮演的角色（如「项目组长，负责组织各成员工作」）。"
                    "通用的「组织构建者」协议（Identity / Prime Directive / Dynamic Tree Growth / "
                    "Workflow A-H）已由系统固定，运行时自动前置拼接，此处只需填写领域特化部分。"
                    "留空则使用纯通用组织构建者人格。"
                )
                description_en = (
                    "The domain role this manager plays in practice (e.g. 'project lead "
                    "organizing members' work'). The generic Organization Builder protocol "
                    "(Identity / Prime Directive / Dynamic Tree Growth / Workflow A-H) is fixed "
                    "by the system and prepended at runtime; fill in only the domain-specific "
                    "part here. Leave empty for the pure generic Organization Builder persona."
                )
                placeholder = "例如：项目组长，负责组织各成员的工作内容，协调进度与产出"
                placeholder_en = "e.g. Project lead — organize each member's work and coordinate progress & deliverables"
            fields.append({
                "name": name,
                "label": label,
                "label_en": label_en,
                "type": field_type,
                "default": defaults.get(name),
                "has_default": name in defaults,
                "nullable": name in defaults and defaults[name] is None,
                "advanced": name in _ROLE_ADVANCED_FIELDS,
                "description": description,
                "description_en": description_en,
                "placeholder": placeholder,
                "placeholder_en": placeholder_en,
            })
        return {"role_type": role_type, "defaults": defaults, "fields": fields}

    def get_role(self, role_id: str, role_type: str = "worker") -> dict:
        file_path = self._role_path(role_id, role_type)
        if not os.path.isfile(file_path):
            raise KeyError(f"Role '{role_id}' not found")
        with open(file_path, "r", encoding="utf-8") as file:
            content = file.read()
        config = yaml.safe_load(content) or {}
        defaults = self._load_default_config(role_type)
        return {
            "id": role_id,
            "display_name": _id_to_display_name(role_id),
            "description": _extract_description(config),
            "type": role_type,
            "content": content,
            "config": config,
            "defaults": defaults,
            "effective_config": {**defaults, **config},
            "schema": self.get_role_schema(role_type),
        }

    def _validate_config(self, config: Any, role_type: str) -> dict[str, Any]:
        if not isinstance(config, dict):
            raise ValueError("Role config must be a YAML mapping")
        defaults, samples = self._get_field_catalog(role_type)
        unknown = sorted(set(config) - set(samples))
        if unknown:
            raise ValueError(f"Unknown role config fields: {unknown}")
        for name, value in config.items():
            expected = samples[name]
            if value is None:
                if name in defaults and defaults[name] is None:
                    continue
                raise ValueError(f"Role config field '{name}' cannot be null")
            if expected is None:
                if not isinstance(value, str):
                    raise ValueError(f"Role config field '{name}' must be text or null")
            elif isinstance(expected, bool):
                if not isinstance(value, bool):
                    raise ValueError(f"Role config field '{name}' must be boolean")
            elif isinstance(expected, int):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"Role config field '{name}' must be a number")
            elif isinstance(expected, list):
                if not isinstance(value, list):
                    raise ValueError(f"Role config field '{name}' must be a list")
            elif isinstance(expected, str) and not isinstance(value, str):
                raise ValueError(f"Role config field '{name}' must be text")
        return config

    def _serialize_config(self, config: dict[str, Any], role_type: str) -> str:
        validated = self._validate_config(config, role_type)
        return yaml.safe_dump(validated, allow_unicode=True, sort_keys=False, default_flow_style=False)

    def update_role(self, role_id: str, content: Optional[str] = None, role_type: str = "worker", config: Optional[dict[str, Any]] = None) -> dict:
        file_path = self._role_path(role_id, role_type)
        if not os.path.isfile(file_path):
            raise KeyError(f"Role '{role_id}' not found")
        if config is not None:
            content = self._serialize_config(config, role_type)
        if content is None:
            raise ValueError("Role content or config is required")
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as error:
            raise ValueError(f"Invalid role YAML: {error}") from error
        with open(file_path, "w", encoding="utf-8") as file:
            file.write(content)
        return self.get_role(role_id, role_type)

    def delete_role(self, role_id: str, role_type: str = "worker") -> None:
        """删除角色 yaml 文件。

        default.yaml 是所有具体角色的继承基类，禁止删除。

        Raises:
            ValueError: role_id 为 default 时。
            KeyError: 角色不存在时。
        """
        if role_id == "default":
            raise ValueError("Cannot delete the default role")
        file_path = self._role_path(role_id, role_type)
        if not os.path.isfile(file_path):
            raise KeyError(f"Role '{role_id}' not found")
        os.remove(file_path)
        logger.info("Deleted role file: %s (type=%s)", file_path, role_type)

    def _role_path(self, role_id: str, role_type: str) -> str:
        if not role_id or not _ROLE_ID_PATTERN.fullmatch(role_id):
            raise ValueError(
                f"Invalid role_id '{role_id}': must match [a-z][a-z0-9_]*"
            )
        if role_type not in ("manager", "worker"):
            raise ValueError(
                f"Invalid role_type '{role_type}': must be 'manager' or 'worker'"
            )
        return os.path.join(
            self._roles_dir, role_type.capitalize(), f"{role_id}.yaml"
        )

    def add_role(self, role_id: str, content: Optional[str] = None, role_type: str = "worker", config: Optional[dict[str, Any]] = None) -> dict:
        """新增角色：在对应子目录下创建 {role_id}.yaml。

        Args:
            role_id: 角色标识，必须匹配 [a-z][a-z0-9_]*。
            content: yaml 文件内容。空串则写入空 role_md 模板。
            role_type: 角色类型（"manager" 或 "worker"），决定写入哪个子目录。
                       默认 "worker"。

        Returns:
            {id, display_name, description, type}

        Raises:
            ValueError: role_id 格式非法、role_type 非法、或角色已存在。
        """
        if not role_id or not _ROLE_ID_PATTERN.match(role_id):
            raise ValueError(
                f"Invalid role_id '{role_id}': must match [a-z][a-z0-9_]*"
            )

        if role_type not in ("manager", "worker"):
            raise ValueError(
                f"Invalid role_type '{role_type}': must be 'manager' or 'worker'"
            )

        # 确定子目录
        subdir_name = role_type.capitalize()  # "manager" → "Manager"
        subdir_path = os.path.join(self._roles_dir, subdir_name)
        os.makedirs(subdir_path, exist_ok=True)

        file_path = os.path.join(subdir_path, f"{role_id}.yaml")
        if os.path.exists(file_path):
            raise ValueError(
                f"Role '{role_id}' already exists in {subdir_name}/"
            )

        if config is not None:
            content = self._serialize_config(config, role_type)
        if content is None or not content.strip():
            content = "{}\n"
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as error:
            raise ValueError(f"Invalid role YAML: {error}") from error

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info("Created role file: %s (type=%s)", file_path, role_type)

        return self.get_role(role_id, role_type)
