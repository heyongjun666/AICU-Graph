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
from typing import Any, Optional

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
    "skills_dir", "skills",
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
    "deliverable_dir", "artifact_contract",
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
    "deliverable_dir", "artifact_contract",
})


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

    def get_artifact_contract(self, role_id: str, role_type: str = "worker"):
        """读取角色 yaml 的 ``artifact_contract:``（default + role 覆盖合并）。

        Returns:
            runtime.delivery.contracts.ArtifactContract；角色缺失/解析失败回退默认
            契约（{validators: [exists, non_empty]}，向后兼容老角色）。
        """
        from runtime.delivery.contracts import ArtifactContract, parse_contract

        try:
            config = self._effective_role_config(role_id, role_type)
        except (KeyError, OSError):
            return ArtifactContract()
        return parse_contract(config.get("artifact_contract"))

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
            "output_file": "Intermediate output", "final_file": "Final output",
            "modular_output": "Modular output", "main_doc": "Main document",
            "generate_instruction": "Generate instruction", "revise_instruction": "Revise instruction",
            "review_checklist": "Review checklist", "adversarial_role": "Adversarial role",
            "audit_role": "Audit role", "audit_angles": "Audit angles", "role_md": "Role instructions",
            "deliverable_dir": "Deliverable directory", "artifact_contract": "Artifact contract",
        }
        labels_zh = {
            "summary": "摘要", "description": "描述",
            "suitable_for": "适用场景", "not_for": "不适用场景",
            "num_ccs": "协作模式", "model": "模型", "cli_name": "CLI",
            "audit_rounds": "审计轮数", "skills_dir": "Skills 目录", "skills": "Skills",
            "output_file": "中间产出", "final_file": "最终产出",
            "modular_output": "模块化产出", "main_doc": "主文档",
            "generate_instruction": "生成指令", "revise_instruction": "修订指令",
            "review_checklist": "审查清单", "adversarial_role": "对抗角色",
            "audit_role": "审计角色", "audit_angles": "审计角度", "role_md": "角色指令",
            "deliverable_dir": "交付目录", "artifact_contract": "产物契约",
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
            "skills_dir": "所有环节：Skill 源目录的绝对路径，Agent 从中加载 skill。",
            "skills": "所有环节：启用的 skill 名称列表，执行时作为 project-level skill 加载。",
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
            "artifact_contract": "交付阶段：产物契约，声明产物的机械特征（内容类型 + 校验器），用于交付门禁的确定性校验。",
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
            "skills_dir": "All phases: absolute path to the skill source directory the agent loads skills from.",
            "skills": "All phases: list of enabled skill names, loaded as project-level skills during execution.",
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
            "artifact_contract": "Delivery phase: artifact contract declaring mechanical traits (content type + validators) for deterministic checks at the delivery gate.",
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
