import json
import os
import re
import shutil
import signal
import subprocess
import yaml
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Dict, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from claude_code_agent import ClaudeCodeAgent, CCExecutionError


# sentinel for "调用方未传此参数"，与 None（可能是有效值，如 output_file=null）区分
_UNSET = object()

# description 提取最大长度（与 roles_manager.py 保持一致）
_DESC_MAX_LEN = 80

# Manager 角色的固定「组织构建者」人格骨架：所有 Manager 无条件前置注入，保证用户覆盖
# role_md 时不会丢失组织节点能力（Identity / Prime Directive / Dynamic Tree Growth /
# Workflow A-H）。role_md 字段现在只承载「领域角色」部分（如「项目组长，负责组织各成员工作」），
# 运行时由 AICU 拼接为：骨架 + 领域角色，再写入每个 CC 的 CLAUDE.md。
_MANAGER_ROLE_MD_SKELETON = """# Identity
你是一名 AI 组织构建者(Organization Builder).
你不预测整棵树, 你只在当前信息下构建下一层组织 —— 树随着执行动态生长.

# Prime Directive
ExecutionPlan 的目标是建立最合理的执行组织, 而非最少或最多 Step.

# Dynamic Tree Growth
执行树是动态生长的, 不是一次性静态蓝图.
你只负责本层的直接下属节点; 子 Manager 执行后产生的新信息会驱动树的下一轮生长.
当前不确定的细节, 交给下层在拥有更多信息时再决策 —— 不要替它们预测未来.

# Workflow
每次收到目标后严格按以下步骤思考:
A. 理解目标与成功判据;
B. 提取必需 Capability, 映射到 suitable_for 覆盖它的角色, 以角色为骨架设计阶段;
C. 将阶段映射为节点并初判 M/W 标签;
D. 反问门 — 对每个拟设为 manager 的节点验证其在下展后是否会沦为单-worker 冗余;
E. 应用 T1–T4 进一步校验每个 worker 标签确实触底;
F. 设计 depends_on 强数据流关系, 为协作节点指定共享锚点确保接口对齐;
F2. 设计事件协作（决策 6）——需要反馈闭环的节点对（如产出↔质检、审核↔产出）不必声明通信关系,
由系统 Router 统一路由; 需并发协作时给生产者配置 events.publish, 事件 content 只描述事件本身;
G. 共享工作区 — 所有节点自动挂载同一 Session Shared Workspace; 为需要协作/融合的 worker 分配
不同 output_path 分区(默认给 Worker 分配具体分区, 空串=全局写高权限仅确实需要时用); 不再规划集成节点;
H. 输出纯 JSON ExecutionPlan.

你绝不亲自执行具体任务, 只负责产出结构正确的执行组织树."""


class AICU:
    """
    AI Computing Unit — 多 Agent 并行生成 + 交叉评审 + 对抗审查 + 深度审计

    工作模式 (peer): N 个 CC 平级协作，做同一任务，互相评审。按 num_ccs 自动选择策略:
      - CC=1 (solo):   单 CC 生成 + 自审
      - CC=2 (pair):   CC-0 生产 + CC-1 审查
      - CC=3 (triple): CC-0/1 并行产出 + CC-2 比较融合
      - CC≥4 (full):   CC-0/1 产出 + CC-2 评审融合 + CC-3 审计（多轮闭环）

    peer 模式流程:
      1. 并行生成：每个 CC 将结果写入自己 workspace 的 output_file
      2. 交叉评审：CC 读取其他 CC 的 output_file 并打分
      3. 对抗审查：CC 读取候选 output_file，按检查清单审计
      4. 深度审计：CC 读取 output_file，按审计角度找隐含缺陷
      5. 返回最终答案

    角色配置:
      通过 role 参数传入角色名称字符串（必须）。AICU 根据 role_type 参数确定角色目录：
      - role_type="manager" → 从 roles/Manager/ 目录加载
      - role_type="worker"  → 从 roles/Worker/ 目录加载
      先加载对应目录下的 default.yaml 作为基础配置，再加载 {role}.yaml 覆盖。
      新增角色只需在对应目录下放一个 {name}.yaml 文件，可以只写 role_md，
      其他字段自动从 default.yaml 继承。

      AICU 初始化参数（num_ccs/model/cli_name/audit_rounds）也支持从 default.yaml 取默认值：
      调用方在 AICU(...) 中显式传参时用传入值，未传（None）则用合并后 yaml 中的对应字段。

      roles/default.yaml 与 roles/{role}.yaml 字段说明:

        【AICU 初始化参数】（均支持 None-sentinel：调用方传 None 则从 yaml 取）
        - num_ccs: CC 数量，决定协作策略（1=solo, 2=pair, 3=triple, ≥4=full）
        - model: CC 调用使用的模型名
        - cli_name: CLI 名称；null 表示使用系统默认
        - audit_rounds: 对抗审查最大轮数
        - output_file: CC 产出文件名（modular_output=True 时通常设为 None）
        - final_file: AICU 最终产出文件名
        - modular_output: 是否模块化多文件产出
        - generate_instruction: 生成阶段 prompt 模板，{output_file} 替换为产出文件名。
                                显式设为 "" 时表示由 question 自带 prompt（调用方传
                                generate_instruction="" 用此机制）。

        【修订位 CC-0】
        - revise_instruction: 修订阶段 prompt 模板，{output_path} 替换为产出文件路径。

        【审查/融合/审计位 CC-1/CC-2/CC-3】
        - review_checklist: 评审检查清单 [[name, desc], ...]，CC 按此逐项打分/找缺陷。

        【审查位 CC-1/CC-2】
        - adversarial_role: 审查位的人格补充，注入 prompt 开头（"你是一个{adversarial_role}"）。

        【审计位 CC-3】
        - audit_role: 审计位的人格补充，注入 prompt 开头（"你是一个{audit_role}"）。
        - audit_angles: 审计角度列表，追加到 review_checklist 之后供 CC-3 深入审查。

        【产出文件】
        - output_file: 产出文件名
        - final_file: 最终文件名
        - modular_output: 是否模块化多文件产出

        【AICU 人格】
        - role_md: AICU 角色人格文本，写入每个 CC 的 CLAUDE.md，所有 CC 共享。
                   default.yaml 提供通用兜底人格，具体角色应在自己 yaml 中显式覆盖。

        【skills 配置】
        - skills: 该角色 CC 可用的 skill 名称列表（从 skills_dir 中选取）。
                  空列表表示不启用任何 skill。非空时，AICU 为每个 CC workspace 创建
                  .claude/skills/ 软链接到指定 skill，并通过 --setting-sources project
                  限定 CC 只加载这些 project-level skill，不加载用户级 skill。
        - skills_dir: skill 源目录（绝对路径，支持 ~ 表示家目录）。
                      默认 ~/.claude/skills，角色 yaml 可覆盖以使用自定义 skill 库。
                      仅当 skills 非空时此字段才生效。

    workspace 结构 (peer 模式):
      cc_N/
        {output_file}        # CC-N 生成的产出（CC 自己写入）
        review.json          # CC-N 的评审打分
        vote_round_M.json    # CC-N 第 M 轮对抗审查结果
        audit.json           # CC-N 的深度审计结果
      cc_0/
        {output_file}        # 修订后的产出（CC-0 自己修改）
      父目录/
        {final_file}         # 最终产出（从最优 CC 复制）
        aicu_result.json     # 完整结构化结果
    """

    def __init__(self,
                 role_type: str = "worker",
                 num_ccs: Optional[int] = None,
                 parent_workspace: Optional[str] = None,
                 model: Optional[str] = None,
                 cli_name: Optional[str] = None,
                 audit_rounds: Optional[int] = None,
                 role: Optional[str] = None,
                 output_file=_UNSET,
                 final_file=_UNSET,
                 modular_output=_UNSET,
                 generate_instruction=_UNSET,
                 output_callback: Optional[Callable[[str, str], None]] = None,
                 api_base_url: Optional[str] = None,
                 api_key_env: Optional[str] = None,
                 skills: Optional[List[str]] = None,
                 skills_dir: Optional[str] = None,
                 skip_permissions: bool = True,
                 shared_target: Optional[str] = None):
        """
        初始化 AICU

        所有参数都可选（除 parent_workspace 和 role 必须提供）。未传的参数从
        对应角色目录的 default.yaml 取默认值，角色 yaml 中显式写的字段会覆盖 default。

        :param role_type: 角色类型（"manager" 或 "worker"），决定从 roles/Manager/ 还是
                     roles/Worker/ 目录加载 YAML。默认 "worker"（向后兼容）。
        :param num_ccs: CC 数量（默认从 default.yaml 的 num_ccs 取）
        :param parent_workspace: 工作目录（必须提供）
        :param model: 模型名称（默认从 default.yaml 的 model 取）
        :param cli_name: CLI 名称（默认从 default.yaml 的 cli_name 取）
        :param audit_rounds: 对抗审查最大轮数（默认从 default.yaml 的 audit_rounds 取）
        :param role: 角色名称（字符串，必须）。AICU 根据 role_type 加载对应子目录下的
                     default.yaml 作为基础配置，再用 {role}.yaml 覆盖。
                     新增角色只需在对应子目录下放一个 {name}.yaml 文件。
        :param output_file: CC 产出文件名。不传（默认）从 yaml 取；传显式值（含 None）
                     覆盖 yaml。modular_output=True 时通常设为 None。
        :param final_file: AICU 最终产出文件名（不传则从 yaml 取）。
        :param modular_output: 是否模块化多文件产出（不传则从 yaml 取）。
        :param generate_instruction: 生成阶段 prompt 模板（不传则从 yaml 取）。
                     传 '' 表示 question 已自带 prompt，AICU 不追加生成指令。
        :param shared_target: 共享分区绝对路径（V3-2.1）。非 None 时 CC 直写该分区，
                     产物权威 = 分区本身（不再产生 nodes/{id}/output 副本）。
                     所有 CC（含多 CC 编排）统一直写同一分区。
        """
        # ---- 加载角色配置（数据驱动：default.yaml 为基础，{role}.yaml 覆盖） ----
        if role is None:
            raise ValueError(
                "AICU 需要显式 role 名称（字符串）。请在 roles/ 目录下放置 {name}.yaml "
                "配置文件，并传入 role=\"{name}\"。"
            )
        if not isinstance(role, str):
            raise TypeError(
                f"role 参数必须是 str（角色名，对应 roles/{{role}}.yaml 文件），"
                f"实际类型: {type(role).__name__}"
            )
        if parent_workspace is None:
            raise ValueError("AICU 需要 parent_workspace（工作目录）")

        _role_type = (role_type or "worker")
        _roles_parent = Path(__file__).parent / "roles"  # roles/ 父目录
        # system_role 目录保持原名（框架内部角色）；其余统一首字母大写
        # （"manager" → "Manager", "worker" → "Worker"）。
        _roles_dir_name = "system_role" if _role_type == "system_role" else _role_type.capitalize()
        _roles_dir = _roles_parent / _roles_dir_name

        # 1. 加载 default.yaml 作为基础配置
        default_yaml = _roles_dir / "default.yaml"
        if not default_yaml.exists():
            raise FileNotFoundError(
                f"默认角色配置文件缺失: {default_yaml}"
            )
        with open(default_yaml, encoding='utf-8') as f:
            base_config = yaml.safe_load(f) or {}

        # 2. 加载角色专属 yaml（缺失则报错并列出可用角色，过滤 _ 开头和 default）
        role_yaml = _roles_dir / f"{role}.yaml"
        if not role_yaml.exists():
            available = sorted(p.stem for p in _roles_dir.glob("*.yaml")
                              if not p.stem.startswith("_") and p.stem != "default")
            raise FileNotFoundError(
                f"未找到角色配置文件: {role_yaml}。可用角色: {available}"
            )
        with open(role_yaml, encoding='utf-8') as f:
            role_overrides = yaml.safe_load(f) or {}

        # 3. 合并：角色 yaml 中显式写的字段覆盖 default.yaml
        #    （显式写空字符串/空列表也算覆盖，如 generate_instruction: "" 表示
        #    question 已自带 prompt，AICU 不再追加生成指令）
        self.role_config = {**base_config, **role_overrides}
        self.role_name = role
        self.role_type = role_type

        # 4. 应用初始化参数：调用方未传（_UNSET）的从合并后的 yaml 取
        #    注意：num_ccs/model/cli_name/audit_rounds 用 None-sentinel（None 不是有效值）
        #    output_file/final_file/modular_output/generate_instruction 用 _UNSET sentinel
        #    （因为这些字段 None/""/False 都可能是有效覆盖值，不能与"未传"混淆）
        if num_ccs is None:
            num_ccs = self.role_config.get("num_ccs", 1)
        if model is None:
            model = self.role_config.get("model", "DeepSeek-V4-Flash")
        if cli_name is None:
            cli_name = self.role_config.get("cli_name")  # 可能仍是 None
        if audit_rounds is None:
            audit_rounds = self.role_config.get("audit_rounds", 2)
        if output_file is _UNSET:
            output_file = self.role_config.get("output_file", None)
        if final_file is _UNSET:
            final_file = self.role_config.get("final_file", "final_output")
        if modular_output is _UNSET:
            modular_output = self.role_config.get("modular_output", False)
        if generate_instruction is _UNSET:
            generate_instruction = self.role_config.get("generate_instruction", "")

        # ---- 动态注入可用角色列表到 generate_instruction ----
        # 如果 generate_instruction 中包含 {available_roles} 占位符，
        # 扫描 roles/Manager/ 和 roles/Worker/ 目录动态生成角色列表文本并替换
        self.available_roles_text = ""
        if "{available_roles}" in generate_instruction:
            available_text = self._build_available_roles_text(_roles_parent)
            generate_instruction = generate_instruction.replace(
                "{available_roles}", available_text
            )
            self.available_roles_text = available_text
        # 目录模式下的主文档名（如 design.md / plan.json）与索引文件名
        main_doc = self.role_config.get("main_doc", None)
        index_file = self.role_config.get("index_file", "_INDEX.md")

        self.num_ccs = num_ccs
        # V3-2.1 shared_target：canonical absolute path；所有 CC（含多 CC 编排）直写共享分区。
        if shared_target:
            shared_target = os.path.realpath(shared_target)
        self.shared_target = shared_target
        self.workspace = Path(parent_workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.cli_name = cli_name
        self.audit_rounds = audit_rounds

        self.output_file = output_file
        self.final_file = final_file
        self.modular_output = modular_output
        self.generate_instruction = generate_instruction
        self.main_doc = main_doc
        self.index_file = index_file
        self.review_checklist = self.role_config.get("review_checklist", [])
        self.adversarial_role = self.role_config.get("adversarial_role", "")
        self.audit_role = self.role_config.get("audit_role", "")
        self.audit_angles = self.role_config.get("audit_angles", [])
        self.revise_instruction = self.role_config.get("revise_instruction", "")
        # deliverable_dir: CC 交付物在 workspace 内的子目录，技能内部产物自然隔离
        # 默认为 None（workspace 根目录，向后兼容）
        self.deliverable_dir = self.role_config.get("deliverable_dir", None)

        # ---- skills 配置 ----
        # skills_dir: skill 源目录（默认 ~/.claude/skills，角色 yaml 可覆盖）
        # skills: 要启用的 skill 名称列表，从 skills_dir 中查找子目录，
        # 为每个 CC workspace 创建 .claude/skills/ 软链接，并通过
        # --setting-sources project 限定 CC 只加载这些 project-level skill。
        self.skills: List[str] = list(
            skills if skills is not None else self.role_config.get("skills") or []
        )
        skills_dir_cfg = skills_dir or self.role_config.get("skills_dir") or "~/.claude/skills"
        self._user_skills_dir = Path(skills_dir_cfg).expanduser()

        # 执行配置由节点/会话在边界传入；密钥只保存环境变量名，不读取或记录真实值。
        self.output_callback = output_callback
        self.api_base_url = api_base_url
        self.api_key_env = api_key_env
        self.skip_permissions = skip_permissions

        # ---- AICU 角色人格 ----
        # Manager 的 role_md 只承载「领域角色」部分；通用的「组织构建者」骨架由系统固定前置，
        # 避免用户覆盖 role_md 时丢失组织节点能力。Worker 的 role_md 仍是完整人格，原样使用。
        domain_role_md = (self.role_config.get("role_md") or "").strip()
        if self.role_type == "manager":
            self.role_md_content = _MANAGER_ROLE_MD_SKELETON
            if domain_role_md:
                self.role_md_content += (
                    "\n\n---\n\n# 领域角色 (Domain Role)\n\n" + domain_role_md
                )
        else:
            self.role_md_content = domain_role_md
        if not self.role_md_content:
            raise ValueError(
                f"角色配置合并后仍无 role_md 字段（default.yaml 和 {role_yaml} 都未提供）"
            )
        role_name_used = f"roles/{role}.yaml (+default)"

        # ---- 将 role_md 写入每个 CC 的 CLAUDE.md ----
        # AICU 自己写 CLAUDE.md（不依赖 ClaudeCodeAgent 的 instruction_md_path），
        # 因为多个 CC 共享同一个 role_md
        # 同时按 self.skills 为每个 CC 准备 .claude/skills/ 软链接，
        # 并通过 enable_project_skills=True 限定 CC 只加载这些 skill
        enable_skills = bool(self.skills)
        if enable_skills:
            # 校验 skills_dir 存在
            if not self._user_skills_dir.exists():
                raise FileNotFoundError(
                    f"skills_dir 不存在: {self._user_skills_dir} "
                    f"(来自 yaml skills_dir 配置)。"
                    f"请在 yaml 中设置正确的 skills_dir，或安装 skill 到该目录。"
                )
            # 校验：所有 skill 必须在 skills_dir 中存在
            missing = [s for s in self.skills
                       if not (self._user_skills_dir / s).exists()]
            if missing:
                raise FileNotFoundError(
                    f"角色 yaml 中配置的 skill 在 skills_dir "
                    f"({self._user_skills_dir}) 未找到: {missing}。"
                    f"可用 skill: {self._list_available_skills()}"
                )

        self.ccs: List[ClaudeCodeAgent] = []
        for i in range(num_ccs):
            cc_id = f"CC-{i}"

            def cc_output(stream: str, message: str, _cc_id: str = cc_id) -> None:
                if self.output_callback:
                    self.output_callback(f"{_cc_id}:{stream}", message)

            cc = ClaudeCodeAgent(
                workspace_path=str(self.workspace / f"cc_{i}"),
                model=model,
                cli_name=cli_name,
                skip_permissions=self.skip_permissions,
                enable_project_skills=enable_skills,
                output_callback=cc_output,
                api_base_url=self.api_base_url,
                api_key_env=self.api_key_env,
            )
            # 写入 CLAUDE.md
            if self.role_md_content:
                claude_md = cc.workspace / "CLAUDE.md"
                claude_md.write_text(self.role_md_content, encoding='utf-8')
            # 写入 .claude/skills/ 软链接（仅当配置了 skills）
            if enable_skills:
                self._install_skills_to_cc(cc.workspace)
            # 创建交付物子目录（deliverable_dir 指定时），技能内部产物自然隔离
            # V3-2.1：shared_target 模式下 CC 直写共享分区（产物唯一权威），
            # 不建私有 deliverables 死目录（避免 cc_{i}/deliverables 空壳）。
            if self.deliverable_dir and not self.shared_target:
                (cc.workspace / self.deliverable_dir).mkdir(parents=True, exist_ok=True)
            self.ccs.append(cc)

        print(f"[AICU] 初始化完成: {num_ccs} 个 CC，父目录: {self.workspace}")
        print(f"[AICU] 角色: output_file={self.output_file}, adversarial={self.adversarial_role[:40]}...")
        if self.role_md_content:
            print(f"[AICU] CLAUDE.md: 已写入 ({len(self.role_md_content)} 字符, 来源: {role_name_used})")
        if enable_skills:
            print(f"[AICU] skills: {self.skills} (project-level，已为每个 CC 软链接)")

    def _list_available_skills(self) -> List[str]:
        """列出 ~/.claude/skills/ 下所有可用 skill 名称"""
        if not self._user_skills_dir.exists():
            return []
        return sorted(p.name for p in self._user_skills_dir.iterdir() if p.is_dir())

    @staticmethod
    def _build_available_roles_text(roles_parent: Path) -> str:
        """扫描 roles/Manager/ 和 roles/Worker/ 目录，动态生成可用角色列表文本。

        用于替换 generate_instruction 中的 {available_roles} 占位符。

        每个角色可选择性定义以下结构化字段(在角色yaml顶层):
          summary     : str  — 一句话精炼描述该角色的核心职责与擅长范围
          suitable_for: List[str] | str — 关键词清单, 表示本角色适合承接的任务特征
          not_for     : List[str] | str — 反向清单, 表示本角色不应被分配的任务类型

        若某字段缺失则按以下顺序回退:
          summary 缺失 → 从 role_md 第一行非纯单词标题内容截取前 N 字
          suitable_for / not_for 缺失 → 该行不输出

        Args:
            roles_parent: roles/ 父目录的 Path。

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
        for subdir_name, label in [("Manager", "Manager"), ("Worker", "Worker")]:
            subdir = roles_parent / subdir_name
            if not subdir.is_dir():
                continue
            role_files = sorted(
                p.stem for p in subdir.glob("*.yaml")
                if not p.stem.startswith("_")
            )
            if not role_files:
                continue
            lines.append(f"  ### {label} 角色（可用于 exec_type=\"{label.lower()}\" 的 step）")
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

                if summary:
                    lines.append(f"  - **{r}**:")
                    indent = "      "
                    lines.append(f"{indent}summary: {summary}")
                    if suit_list:
                        lines.append(f"{indent}suitable_for: {', '.join(suit_list)}")
                    if nfor_list:
                        lines.append(f"{indent}not_for: {', '.join(nfor_list)}")
                else:
                    lines.append(f"  - **{r}**: （未提供描述信息，请直接阅读其 role_md 内容判断适用边界）")
            lines.append("")

        if not lines:
            return "  （暂无可用角色）"

        return "\n".join(lines)

    def _cc_deliverable_dir(self, cc_id: int) -> Path:
        """返回 CC 的交付物子目录，或 workspace 根目录（deliverable_dir 未设置时）。

        V3-2.1 shared_target 模式：所有 CC 直写共享分区（产物唯一权威），
        返回分区本身（solo 模式下即唯一 CC 的写入点）。
        """
        if self.shared_target:
            return Path(self.shared_target)
        ws = self.ccs[cc_id].workspace
        if self.deliverable_dir:
            return ws / self.deliverable_dir
        return ws

    def _install_skills_to_cc(self, cc_workspace: Path) -> None:
        """
        在 CC workspace 下创建 .claude/skills/ 目录，
        为 self.skills 中每个 skill 创建一个软链接指向 ~/.claude/skills/{name}/。
        已存在的链接/目录会被清空重建，确保 idempotent。
        """
        target_dir = cc_workspace / ".claude" / "skills"
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        for skill_name in self.skills:
            src = self._user_skills_dir / skill_name
            dst = target_dir / skill_name
            # 用软链接，避免拷贝整份 skill 文件；CC 会跟随链接读取 SKILL.md
            dst.symlink_to(src.resolve())

    # ========== CC 调用容错 ==========

    CC_MAX_RETRIES = 2  # 遇到 error_during_execution 时最多重试次数

    def _emit_cc_activity(self, cc: ClaudeCodeAgent, status: str, prompt: str = "", event: str = "") -> None:
        if not self.output_callback:
            return
        try:
            cc_id = f"CC-{self.ccs.index(cc)}"
        except ValueError:
            cc_id = "CC-unknown"
        payload = json.dumps({
            "cc_id": cc_id,
            "status": status,
            "event": event,
            "phase": "executing" if status == "running" else status,
            "task": prompt.replace("\n", " ").strip()[:160],
        }, ensure_ascii=False)
        self.output_callback("activity", payload)

    def _cc_chat_with_retry(self, cc: ClaudeCodeAgent, prompt: str,
                             timeout: int = 7200, max_turns: Optional[int] = None) -> str:
        """
        带容错的 CC 调用：遇到 CCExecutionError 或 TimeoutExpired 时重置对话并重试

        :param cc: ClaudeCodeAgent 实例
        :param prompt: 对话内容
        :param timeout: 超时时间（秒），默认 7200（2 小时）
        :param max_turns: 最大执行轮数
        :return: CC 的回复文本
        """
        self._emit_cc_activity(cc, "running", prompt, "started")
        for attempt in range(1, self.CC_MAX_RETRIES + 2):  # 最多 CC_MAX_RETRIES+1 次（1次原始 + N次重试）
            try:
                response = cc.chat(prompt, max_turns=max_turns, timeout=timeout)
                self._emit_cc_activity(cc, "completed", prompt, "finished")
                return response
            except CCExecutionError as e:
                if attempt <= self.CC_MAX_RETRIES:
                    print(f"[AICU] CC 遇到 error_during_execution，第 {attempt} 次重试 (共 {self.CC_MAX_RETRIES} 次): {e}")
                    # CCExecutionError 已在 claude_code_agent.py 中重置了 conversation_id
                    # 等待一小段时间后重试
                    import time
                    time.sleep(5 * attempt)  # 递增等待: 5s, 10s, ...
                else:
                    print(f"[AICU] CC 重试 {self.CC_MAX_RETRIES} 次后仍然失败，放弃")
                    self._emit_cc_activity(cc, "failed", prompt, "failed")
                    raise
            except subprocess.TimeoutExpired as e:
                # CC 卡死超时：chat() 内部已 kill 整个进程组，这里重置对话后重试
                if attempt <= self.CC_MAX_RETRIES:
                    print(f"[AICU] CC 超时（>{timeout}s），第 {attempt} 次重试 (共 {self.CC_MAX_RETRIES} 次)")
                    cc.reset_conversation()
                    import time
                    time.sleep(5 * attempt)
                else:
                    print(f"[AICU] CC 超时重试 {self.CC_MAX_RETRIES} 次后仍然失败，放弃")
                    self._emit_cc_activity(cc, "failed", prompt, "timeout")
                    raise

    # ========== 文件辅助 ==========

    def _cc_output_path(self, cc_id: int) -> Path:
        """CC deliverable_dir 中产出文件的路径（模块化输出时返回 deliverable_dir 本身）

        V3-2.1 shared_target 模式：分区即产物根，modular/非 modular 统一返回分区，
        杜绝 {partition}/output 嵌套副本。
        """
        base = self._cc_deliverable_dir(cc_id)
        if self.shared_target:
            return base
        if self.output_file is None:
            return base
        return base / self.output_file

    def _read_cc_file(self, cc_id: int, filename: Optional[str] = None) -> str:
        """读取 CC deliverable_dir 中的产出文件。

        当 output_file 指向目录时（CC agent 将产出组织为多文件目录），
        自动探索目录内容：优先读 _INDEX.md，否则拼接所有 .md 文件。

        V3-2.1 shared_target 模式：分区即产物根，统一按目录模式读取分区
        （_list_code_files 拼代码 + cc_output.md 兜底），忽略 output_file。
        """
        target = filename or self.output_file
        # shared_target：分区即产物根，统一目录模式（防 {partition}/output 嵌套被当产出）
        if self.shared_target:
            target = None
        if target is None:
            # 模块化输出（output_file=None）：拼接所有代码文件内容
            files = self._list_code_files(cc_id)
            if not files:
                # 兜底：deliverable_dir 无代码文件时，回退读取 CC 的原始响应文件
                # （cc_output.md）。覆盖 Manager 等 output_file=None、CC 直接把
                # 产出（如纯 JSON ExecutionPlan）写在回复文本里的角色——否则
                # final_content 恒为空，plan_result 落盘为空文件，下游解析失败。
                raw = self.ccs[cc_id].workspace / "cc_output.md"
                if raw.exists():
                    return raw.read_text(encoding="utf-8")
                return ""
            return "\n".join(f.read_text(encoding='utf-8') for f in files if f.exists())
        path = self._cc_deliverable_dir(cc_id) / target
        if not path.exists():
            return ""
        if path.is_dir():
            return self._read_output_directory(path)
        return path.read_text(encoding='utf-8')

    @staticmethod
    def _read_output_directory(dir_path: Path) -> str:
        """读取 CC 产出目录的内容。

        优先读取 _INDEX.md（作为导航索引），
        若不存在则拼接目录下所有 .md 文件（跳过框架文件）。
        """
        # 框架/管理文件，不作为 CC 业务产出
        _FRAMEWORK_FILES = {'CLAUDE.md', 'cc_output.md', 'aicu_result.json'}
        index_path = dir_path / "_INDEX.md"
        if index_path.exists():
            # _INDEX.md 作为主入口，附加文件清单供下游按需读取
            parts = [index_path.read_text(encoding='utf-8')]
            parts.append("\n\n---\n## 产出目录文件清单\n")
            for f in sorted(dir_path.rglob("*")):
                if not f.is_file():
                    continue
                if f.name in _FRAMEWORK_FILES:
                    continue
                rel = f.relative_to(dir_path)
                parts.append(f"- `{rel}` ({f.stat().st_size} bytes)")
            return "\n".join(parts)
        # 无 _INDEX.md: 拼接所有 .md 文件
        md_files = sorted(
            f for f in dir_path.rglob("*.md")
            if f.is_file() and f.name not in _FRAMEWORK_FILES
        )
        if not md_files:
            return ""
        parts = []
        for f in md_files:
            rel = f.relative_to(dir_path)
            parts.append(f"\n\n==> {rel}\n")
            parts.append(f.read_text(encoding='utf-8'))
        return "\n".join(parts)

    def _save_to_workspace(self, filename: str, content: str):
        """将内容写入 AICU 父级 workspace（覆盖已存在的同名目录）"""
        path = self.workspace / filename
        # 防御：前次运行可能已将产出组织为目录，先清理
        if path.is_dir():
            import shutil
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
    @property
    def is_modular_output(self) -> bool:
        """是否为模块化输出（多文件）"""
        return self.modular_output

    # 非代码目录（缓存/编译产物/环境目录，不是 CC 写的产出）
    NONCODE_DIRS = {
        '__pycache__', '.pytest_cache', 'node_modules', '.git', '.claude',
        '.codegraph', '.venv', 'venv', 'env', '.env', 'dist', 'build', '.next', '.nuxt',
        'target', 'bin', 'obj', '.idea', '.vscode', '.cache', 'coverage',
        '.tox', '.mypy_cache', '.ruff_cache', '__pypackages__',
    }
    # 编译产物/临时文件扩展名（不是源代码，是工具生成的）
    NONCODE_EXTENSIONS = {
        '.pyc', '.pyo', '.class', '.o', '.so', '.dll', '.exe',
        '.dat', '.db', '.sqlite', '.pickle',
        '.lock', '.cache', '.tmp', '.swp', '.bak', '.orig',
        '.egg-info', '.whl',
    }
    # 框架自身文件（AICU/CC 生成的管理文件，不是项目产出）
    NONCODE_FILENAMES = {
        'CLAUDE.md', 'AGENTS.md',
        'review.json', 'audit.json', 'aicu_result.json',
        'best_solution_summary.txt',
    }
    def _is_code_file(self, f: Path) -> bool:
        """判断一个文件是否是代码文件（排除模式：非代码的才算非代码，其余都是代码）"""
        if not f.is_file():
            return False
        # 排除非代码目录下的文件
        if any(part in self.NONCODE_DIRS for part in f.parts):
            return False
        # 排除非代码文件名
        if f.name in self.NONCODE_FILENAMES:
            return False
        # 排除非代码扩展名
        if f.suffix.lower() in self.NONCODE_EXTENSIONS:
            return False
        # 无扩展名的文件（如 Makefile, Dockerfile）已在 NONCODE_FILENAMES 里排除
        # 其余无扩展名文件视为代码（如可执行脚本）
        return True

    def _list_code_files(self, cc_id: int) -> List[Path]:
        """列出 CC deliverable_dir 下所有代码文件"""
        return self._list_code_files_in_dir(self._cc_deliverable_dir(cc_id))

    def _list_code_files_in_dir(self, directory: Path) -> List[Path]:
        """列出目录下所有代码文件（排除模式，支持任意语言）"""
        if not directory.exists() or not directory.is_dir():
            return []
        files = [f for f in directory.rglob("*") if self._is_code_file(f)]
        return sorted(files)

    def _files_to_prompt_lines(self, files: List[Path]) -> str:
        """将文件路径列表格式化为 prompt 中的行"""
        return "\n".join(f"  - {f}" for f in files)

    def _format_instruction(self) -> str:
        """安全格式化 generate_instruction 模板。

        使用 str.replace() 代替 str.format()，避免模板中含有 JSON 示例代码
        中的 { 和 } 时引发 KeyError。
        """
        result = self.generate_instruction
        replacements = {
            # V3-2.1 shared_target：{deliverable_dir}→分区；{output_file}→""（杜绝分区下再套 output）
            "{output_file}": (
                "" if self.shared_target
                else (str(self.output_file) if self.output_file is not None else "")
            ),
            "{main_doc}": str(self.main_doc) if self.main_doc else "",
            "{index_file}": str(self.index_file) if self.index_file else "",
            "{final_file}": str(self.final_file) if self.final_file else "",
            "{deliverable_dir}": (
                str(self.shared_target) if self.shared_target
                else (str(self.deliverable_dir) if self.deliverable_dir else ".")
            ),
        }
        for placeholder, value in replacements.items():
            result = result.replace(placeholder, value)
        return result

    def _extract_json(self, text: str) -> Optional[dict]:
        """从 CC 回复中提取 JSON（优先使用平衡大括号匹配，避免贪婪匹配跨 JSON 对象）"""
        start = text.find('{')
        if start >= 0:
            depth = 0
            in_string = False
            escape_next = False
            for i in range(start, len(text)):
                ch = text[i]
                if escape_next:
                    escape_next = False
                    continue
                if ch == '\\' and in_string:
                    escape_next = True
                    continue
                if ch == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        try:
                            return json.loads(candidate)
                        except (json.JSONDecodeError, ValueError):
                            break
        # Fallback: 从第一个 { 开始逐步扩展，找到第一个合法 JSON
        if start >= 0:
            for end in range(start + 1, len(text)):
                if text[end] == '}':
                    candidate = text[start:end + 1]
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, ValueError):
                        continue
        return None

    # ========== 进程管理 ==========

    def kill_processes(self) -> None:
        """杀死所有 CC agent 正在运行的子进程。

        用于 pause 场景：通过 kill 进程组来中断阻塞的 CC.chat() 调用，
        使 Worker.run() 的线程得以返回，同时保留 AICU 实例及其 conversation_id。

        关键：杀进程前设置 CC 的 _preserve_conversation 标志，
        阻止 chat() 在 returncode≠0 时重置 conversation_id。
        标志在 _create_real_aicu() 返回缓存实例时清除。
        """
        for cc in self.ccs:
            cc._preserve_conversation.set()
            pid = getattr(cc, '_last_process_pid', None)
            if pid is not None:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass

    # ========== 主入口 ==========

    def ask(self, question: str, max_rounds: int = 3) -> dict:
        return self._ask_peer(question, max_rounds)

    # ========== peer 模式：按 num_ccs 选择策略 ==========

    def _ask_peer(self, question: str, max_rounds: int = 3) -> dict:
        """
        peer 模式入口：根据 CC 数量选择不同协作策略。

        策略说明:
          CC=1 (solo):    单 CC 独立完成。速度最快、成本最低。
                          适合：简单代码修改、小功能开发、快速验证想法。

          CC=2 (pair):    CC-0 生产 + CC-1 审查修订。
                          适合：日常开发工作。

          CC=3 (triple):  CC-0/CC-1 独立产出 + CC-2 比较融合。
                          适合：需求分析、PRD、架构设计、任务拆分等需要多种思路的工作。

          CC≥4 (full):    CC-0/CC-1 产出 + CC-2 评审融合 + CC-3 审计。
                          形成「生成 → 评审 → 审计 → 修复 → 交付」完整闭环。
                          适合：架构设计、QA、复杂开发和关键决策场景。
        """
        print(f"\n{'='*60}")
        print(f"[AICU] 收到问题: {question[:80]}...")
        print(f"[AICU] 策略: {self._peer_strategy_name()}")
        print(f"{'='*60}")

        if self.num_ccs == 1:
            return self._ask_peer_solo(question, max_rounds)
        elif self.num_ccs == 2:
            return self._ask_peer_pair(question, max_rounds)
        elif self.num_ccs == 3:
            return self._ask_peer_triple(question, max_rounds)
        else:
            return self._ask_peer_full(question, max_rounds)

    def _peer_strategy_name(self) -> str:
        names = {1: "solo (1CC: 生成+自审)", 2: "pair (2CC: 生产+审查)",
                 3: "triple (3CC: 双生产+评审融合)", 4: "full (4CC+: 双生产+评审+审计)"}
        return names.get(self.num_ccs, f"full ({self.num_ccs}CC: 双生产+评审+审计)")

    # ==================== CC=1: solo 模式 ====================
    # 单 CC 独立完成任务。速度最快、成本最低。
    # 适合：简单代码修改、小功能开发、快速验证想法。
    # 流程：生成 → 自审 → 修订 → 复审 → 交付

    def _ask_peer_solo(self, question: str, max_rounds: int = 3) -> dict:
        cc = self.ccs[0]

        # ---- 生成 ----
        print(f"\n[AICU-solo] 阶段1: 生成")
        gen_instr = self._format_instruction()
        response = self._cc_chat_with_retry(cc, f"{gen_instr}\n\n问题: {question}", timeout=7200)
        # 保存 CC 响应到工作区文件，供 _read_cc_file / _finalize_result 读取
        cc_output_path = cc.workspace / "cc_output.md"
        cc_output_path.write_text(response, encoding='utf-8')
        if self.is_modular_output:
            files = self._list_code_files(0)
            print(f"  产出: {len(files)} 个文件")
        else:
            print(f"  产出: {len(self._read_cc_file(0))} 字符")

        # ---- 自审+修订循环 ----
        consensus_reached = False
        round_num = 0
        all_defects = []

        for round_num in range(1, max_rounds + 1):
            print(f"\n[AICU-solo] 阶段2: 自审 (第 {round_num} 轮)")
            defects = self._solo_review(question, cc)
            if not defects:
                consensus_reached = True
                print(f"  自审通过")
                break
            print(f"  发现 {len(defects)} 个缺陷")
            for d in defects:
                print(f"    [{d.get('severity')}] {d.get('description', '')[:80]}")
            all_defects = defects

            print(f"  → 修订...")
            self._revise_with_defect_list(question, cc, defects)

        if not consensus_reached:
            print(f"\n[AICU-solo] 自审未通过 (已 {max_rounds} 轮修订)")

        # ---- 汇总结果 ----
        return self._finalize_result(
            question=question,
            scores={0: {0: 8.0}},  # solo 无对比，给默认分
            best_id=0,
            consensus_reached=consensus_reached,
            vote_rounds=round_num,
            audit_defects=[],
        )

    def _solo_review(self, question: str, cc: ClaudeCodeAgent) -> List[dict]:
        """单 CC 自审：读取自己的产出，找出 high/medium 缺陷"""
        read_instruction = self._build_read_instruction_text(0)

        checklist_text = "\n".join(
            f"  {i+1}. **{name}**: {desc}"
            for i, (name, desc) in enumerate(self.review_checklist)
        )

        prompt = (
            f"你是一个{self.adversarial_role or '严格的审查专家'}。请审查你刚刚编写的方案。\n\n"
            f"原始问题: {question}\n\n"
            f"{read_instruction}\n\n"
            f"按以下检查清单逐项审查:\n"
            f"{checklist_text}\n\n"
            f"严格按以下 JSON 格式返回，不要加任何其他文字:\n"
            f'{{"approved": true/false, "defects": ['
            f'{{"check": "检查项", "description": "缺陷描述", "severity": "high/medium/low"}}], '
            f'"summary": "总体评价"}}\n\n'
            f"规则: 只有所有检查项都通过（defects 为空）时才 approved=true。\n"
            f"只报告 high/medium 缺陷，忽略 low 级别问题。"
        )
        prompt = self._inject_roles_context(prompt)
        resp = self._cc_chat_with_retry(cc, prompt, timeout=7200)
        parsed = self._parse_adversarial_vote(resp)

        # 保存结果
        review_data = {"round": 1, "raw_response": resp, "result": parsed}
        review_path = cc.workspace / "review.json"
        review_path.write_text(json.dumps(review_data, ensure_ascii=False, indent=2), encoding='utf-8')

        return [d for d in parsed.get("defects", [])
                if d.get("severity") in ("high", "medium")]

    # ==================== CC=2: pair 模式 ====================
    # CC-0 负责完成任务，CC-1 负责 Review 并给出修改建议，CC-0 修正后输出。
    # 适合：日常开发工作。
    # 流程：CC-0 生成 → CC-1 审查 → CC-0 修订 → CC-1 复审 → 交付

    def _ask_peer_pair(self, question: str, max_rounds: int = 3) -> dict:
        producer = self.ccs[0]
        reviewer = self.ccs[1]

        # ---- 生成 ----
        print(f"\n[AICU-pair] 阶段1: CC-0 生成")
        gen_instr = self._format_instruction()
        response = self._cc_chat_with_retry(producer, f"{gen_instr}\n\n问题: {question}", timeout=7200)
        cc_output_path = self.ccs[0].workspace / "cc_output.md"
        cc_output_path.write_text(response, encoding='utf-8')
        if self.is_modular_output:
            print(f"  产出: {len(self._list_code_files(0))} 个文件")
        else:
            print(f"  产出: {len(self._read_cc_file(0))} 字符")

        # ---- CC-1 审查 + CC-0 修订循环 ----
        consensus_reached = False
        round_num = 0

        for round_num in range(1, max_rounds + 1):
            print(f"\n[AICU-pair] 阶段2: CC-1 审查 (第 {round_num} 轮)")
            defects = self._pair_review(question, reviewer)
            if not defects:
                consensus_reached = True
                print(f"  CC-1 审查通过")
                break
            print(f"  CC-1 发现 {len(defects)} 个缺陷")
            for d in defects:
                print(f"    [{d.get('severity')}] {d.get('description', '')[:80]}")

            print(f"  → CC-0 修订...")
            self._revise_with_defect_list(question, producer, defects)

        if not consensus_reached:
            print(f"\n[AICU-pair] 审查未通过 (已 {max_rounds} 轮修订)")

        return self._finalize_result(
            question=question,
            scores={0: {0: 8.0}, 1: {0: 7.0}},
            best_id=0,
            consensus_reached=consensus_reached,
            vote_rounds=round_num,
            audit_defects=[],
        )

    def _pair_review(self, question: str, reviewer: ClaudeCodeAgent) -> List[dict]:
        """CC-1 审查 CC-0 的产出"""
        read_instruction = self._build_read_instruction_text(0)

        checklist_text = "\n".join(
            f"  {i+1}. **{name}**: {desc}"
            for i, (name, desc) in enumerate(self.review_checklist)
        )

        prompt = (
            f"你是一个{self.adversarial_role}。\n\n"
            f"原始问题: {question}\n\n"
            f"{read_instruction}\n\n"
            f"按以下检查清单逐项审查:\n"
            f"{checklist_text}\n\n"
            f"严格按以下 JSON 格式返回，不要加任何其他文字:\n"
            f'{{"approved": true/false, "defects": ['
            f'{{"check": "检查项", "description": "缺陷描述", "severity": "high/medium/low"}}], '
            f'"summary": "总体评价"}}\n\n'
            f"规则: 只有所有检查项都通过（defects 为空）时才 approved=true。\n"
            f"只报告 high/medium 缺陷，忽略 low 级别问题。"
        )
        prompt = self._inject_roles_context(prompt)
        resp = self._cc_chat_with_retry(reviewer, prompt, timeout=7200)
        parsed = self._parse_adversarial_vote(resp)

        review_data = {"round": 1, "raw_response": resp, "result": parsed}
        review_path = reviewer.workspace / "review.json"
        review_path.write_text(json.dumps(review_data, ensure_ascii=False, indent=2), encoding='utf-8')

        return [d for d in parsed.get("defects", [])
                if d.get("severity") in ("high", "medium")]

    # ==================== CC=3: triple 模式 ====================
    # CC-0 和 CC-1 独立产出方案（彼此看不到对方结果），
    # CC-2 作为 Reviewer 比较两个方案、评分、融合最佳部分形成最终结果。
    # 适合：需求分析、PRD、架构设计、任务拆分等需要多种思路的工作。
    # 流程：CC-0/CC-1 并行生成 → CC-2 比较融合 → 修订 → 交付

    def _ask_peer_triple(self, question: str, max_rounds: int = 3) -> dict:
        # ---- 双 CC 并行生成 ----
        print(f"\n[AICU-triple] 阶段1: CC-0/CC-1 并行生成")
        gen_instr = self._format_instruction()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self._cc_chat_with_retry, self.ccs[0],
                            f"{gen_instr}\n\n问题: {question}", 86400),
                pool.submit(self._cc_chat_with_retry, self.ccs[1],
                            f"{gen_instr}\n\n问题: {question}", 86400),
            ]
            responses = []
            for i, f in enumerate(futures):
                try:
                    resp = f.result()
                    responses.append(resp)
                    cc_output_path = self.ccs[i].workspace / "cc_output.md"
                    cc_output_path.write_text(resp, encoding='utf-8')
                except Exception as e:
                    responses.append(None)
                    print(f"  [AICU-triple] CC-{i} 生成失败: {e}")
        for i in range(2):
            if self.is_modular_output:
                print(f"  CC-{i}: {len(self._list_code_files(i))} 个文件")
            else:
                print(f"  CC-{i}: {len(self._read_cc_file(i))} 字符")

        # ---- CC-2 比较融合 ----
        print(f"\n[AICU-triple] 阶段2: CC-2 比较融合")
        merge_result = self._triple_merge(question)

        best_id = merge_result["best_id"]
        print(f"  CC-2 选择: CC-{best_id} (评分: {merge_result['scores']})")
        if merge_result["defects"]:
            print(f"  发现 {len(merge_result['defects'])} 个缺陷")

        # 把最优方案复制到 CC-0
        self._copy_best_to_cc0(best_id)

        # ---- 修订循环 ----
        consensus_reached = not merge_result["defects"]
        round_num = 0
        all_defects = merge_result["defects"]

        for round_num in range(1, max_rounds + 1):
            if not all_defects:
                break
            print(f"\n[AICU-triple] 阶段3: 修订 (第 {round_num} 轮)")
            self._revise_with_defect_list(question, self.ccs[0], all_defects)

            # CC-2 复查
            print(f"  CC-2 复查...")
            recheck = self._triple_review_only(question)
            all_defects = recheck["defects"]
            if not all_defects:
                consensus_reached = True
                print(f"  CC-2 复查通过")
                break
            print(f"  CC-2 复查: 仍有 {len(all_defects)} 个缺陷")

        if not consensus_reached:
            print(f"\n[AICU-triple] 审查未通过 (已 {max_rounds} 轮修订)")

        return self._finalize_result(
            question=question,
            scores=merge_result["raw_scores"],
            best_id=best_id,
            consensus_reached=consensus_reached,
            vote_rounds=round_num,
            audit_defects=[],
        )

    def _triple_merge(self, question: str) -> dict:
        """CC-2 比较两个方案、评分、融合，返回最优方案和缺陷"""
        reviewer = self.ccs[2]
        read_0 = self._build_read_instruction_text(0)
        read_1 = self._build_read_instruction_text(1)

        checklist_text = "\n".join(
            f"  {i+1}. **{name}**: {desc}"
            for i, (name, desc) in enumerate(self.review_checklist)
        )

        prompt = (
            f"你是一个评审专家，需要比较两个独立方案并选出最优方案。\n\n"
            f"原始问题: {question}\n\n"
            f"方案 A (CC-0) 的文件:\n{read_0}\n\n"
            f"方案 B (CC-1) 的文件:\n{read_1}\n\n"
            f"请用 Read 工具读取两个方案的所有文件，然后:\n"
            f"1. 对每个方案按以下标准评分(1-10):\n{checklist_text}\n"
            f"2. 选择得分更高的方案作为基础\n"
            f"3. 如果另一个方案有更好的部分，融合进来\n"
            f"4. 检查融合后方案的缺陷\n\n"
            f"严格按以下 JSON 格式返回:\n"
            f'{{"scores": {{"A": 分数, "B": 分数}}, "best": "A"/"B", '
            f'"merge_notes": "融合说明", '
            f'"approved": true/false, '
            f'"defects": ['
            f'{{"check": "检查项", "description": "缺陷描述", "severity": "high/medium/low"}}], '
            f'"summary": "总体评价"}}\n\n'
            f"只报告 high/medium 缺陷。"
        )
        prompt = self._inject_roles_context(prompt)
        resp = self._cc_chat_with_retry(reviewer, prompt, timeout=7200)
        parsed = self._extract_json(resp) or {}

        # 解析评分
        raw_scores = parsed.get("scores", {})
        def _to_float(v, default=7.0):
            """确保分数是 float；LLM 有时返回嵌套 dict 如 {"quality":8,"impl":7}"""
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, dict):
                vals = [x for x in v.values() if isinstance(x, (int, float))]
                return sum(vals) / len(vals) if vals else default
            try:
                return float(v)
            except (ValueError, TypeError):
                return default

        scores_a = _to_float(raw_scores.get("A", 7.0))
        scores_b = _to_float(raw_scores.get("B", 7.0))
        best = parsed.get("best", "A")
        best_id = 0 if best == "A" else 1

        # 如果融合了另一方案的部分，CC-2 需要直接编辑文件
        # 这里只记录 merge_notes，实际融合由修订阶段处理

        defects = [d for d in parsed.get("defects", [])
                   if d.get("severity") in ("high", "medium")]

        # 保存结果
        merge_data = {"raw_response": resp, "result": parsed}
        merge_path = reviewer.workspace / "merge.json"
        merge_path.write_text(json.dumps(merge_data, ensure_ascii=False, indent=2), encoding='utf-8')

        return {
            "best_id": best_id,
            "scores": f"A={scores_a}, B={scores_b}",
            "defects": defects,
            "raw_scores": {0: {0: scores_a, 1: scores_b}, 2: {0: scores_a, 1: scores_b}},
        }

    def _triple_review_only(self, question: str) -> dict:
        """CC-2 只审查 CC-0（已融合后的最优方案）"""
        reviewer = self.ccs[2]
        read_instruction = self._build_read_instruction_text(0)

        checklist_text = "\n".join(
            f"  {i+1}. **{name}**: {desc}"
            for i, (name, desc) in enumerate(self.review_checklist)
        )

        prompt = (
            f"你是一个{self.adversarial_role}，请复查修订后的方案。\n\n"
            f"原始问题: {question}\n\n"
            f"{read_instruction}\n\n"
            f"按以下清单审查:\n{checklist_text}\n\n"
            f'返回 JSON: {{"approved": true/false, "defects": ['
            f'{{"check": "检查项", "description": "缺陷描述", "severity": "high/medium/low"}}], '
            f'"summary": "总体评价"}}\n\n'
            f"只报告 high/medium 缺陷。"
        )
        prompt = self._inject_roles_context(prompt)
        resp = self._cc_chat_with_retry(reviewer, prompt, timeout=7200)
        parsed = self._parse_adversarial_vote(resp)
        defects = [d for d in parsed.get("defects", [])
                   if d.get("severity") in ("high", "medium")]
        return {"defects": defects}

    # ==================== CC≥4: full 模式 ====================
    # CC-0/CC-1 独立产出方案，CC-2 评审融合，CC-3 审计。
    # CC-2 根据审计结果修复，最终交付。
    # 形成「生成 → 评审 → 审计 → 修复 → 交付」完整闭环。
    # 适合：架构设计、QA、复杂开发和关键决策场景。

    def _ask_peer_full(self, question: str, max_rounds: int = 3) -> dict:
        # ---- 双 CC 并行生成 ----
        print(f"\n[AICU-full] 阶段1: CC-0/CC-1 并行生成")
        gen_instr = self._format_instruction()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self._cc_chat_with_retry, self.ccs[0],
                            f"{gen_instr}\n\n问题: {question}", 86400),
                pool.submit(self._cc_chat_with_retry, self.ccs[1],
                            f"{gen_instr}\n\n问题: {question}", 86400),
            ]
            responses = []
            for i, f in enumerate(futures):
                try:
                    resp = f.result()
                    responses.append(resp)
                    cc_output_path = self.ccs[i].workspace / "cc_output.md"
                    cc_output_path.write_text(resp, encoding='utf-8')
                except Exception as e:
                    responses.append(None)
                    print(f"  [AICU-full] CC-{i} 生成失败: {e}")
        for i in range(2):
            if self.is_modular_output:
                print(f"  CC-{i}: {len(self._list_code_files(i))} 个文件")
            else:
                print(f"  CC-{i}: {len(self._read_cc_file(i))} 字符")

        # ---- CC-2 评审融合 ----
        print(f"\n[AICU-full] 阶段2: CC-2 评审融合")
        merge_result = self._triple_merge(question)
        best_id = merge_result["best_id"]
        print(f"  CC-2 选择: CC-{best_id} (评分: {merge_result['scores']})")

        # 把最优方案复制到 CC-0
        self._copy_best_to_cc0(best_id)

        # 修订评审发现的缺陷
        if merge_result["defects"]:
            print(f"  → CC-0 修订评审缺陷...")
            self._revise_with_defect_list(question, self.ccs[0], merge_result["defects"])

        # ---- CC-3 审计 + CC-0 修复 迭代循环 ----
        # 原单趟审计改为多轮闭环：审计 → 修复 → 重审，直到通过或达到 max_rounds
        print(f"\n[AICU-full] 阶段3: CC-3 深度审计 (最多 {max_rounds} 轮)")
        audit_passed = False
        round_num = 0
        last_audit_defects: List[dict] = []

        for round_num in range(1, max_rounds + 1):
            print(f"\n  --- 审计第 {round_num} 轮 ---")
            audit_defects = self._full_audit(question)
            last_audit_defects = audit_defects

            if not audit_defects:
                audit_passed = True
                print(f"  CC-3 审计通过")
                break

            print(f"  CC-3 发现 {len(audit_defects)} 个缺陷")
            for d in audit_defects:
                print(f"    [{d.get('severity')}] {d.get('description', '')[:80]}")

            # CC-0 根据审计结果修复，下一轮由 CC-3 重新审计验证
            print(f"  → CC-0 修复审计缺陷...")
            self._revise_with_defect_list(question, self.ccs[0], audit_defects)

        if not audit_passed:
            print(f"\n[AICU-full] 审计未通过 (已 {max_rounds} 轮修复)")

        # ---- 汇总 ----
        # consensus 条件：评审无缺陷 且 审计最终通过
        consensus_reached = (not merge_result["defects"]) and audit_passed
        return self._finalize_result(
            question=question,
            scores=merge_result["raw_scores"],
            best_id=best_id,
            consensus_reached=consensus_reached,
            vote_rounds=round_num,
            audit_defects=last_audit_defects,
        )

    def _full_audit(self, question: str) -> List[dict]:
        """CC-3 专属审计：寻找漏洞、风险和遗漏"""
        auditor = self.ccs[3]

        read_instruction = self._build_read_instruction_text(0)

        # 合并 review checklist + audit angles
        checklist_text = "\n".join(
            f"  {i+1}. **{name}**: {desc}"
            for i, (name, desc) in enumerate(self.review_checklist)
        )
        angles_text = "\n".join(
            f"  {len(self.review_checklist)+i+1}. **{angle}**"
            for i, angle in enumerate(self.audit_angles)
        )

        prompt = (
            f"你是一个{self.audit_role}，专门寻找方案中的漏洞、风险和遗漏。\n\n"
            f"原始问题: {question}\n\n"
            f"{read_instruction}\n\n"
            f"按以下角度深入审查:\n"
            f"{checklist_text}\n"
            f"{angles_text}\n\n"
            f"严格按以下 JSON 格式返回:\n"
            f'{{"defects_found": ['
            f'{{"aspect": "审查角度", "description": "缺陷描述", '
            f'"severity": "high/medium/low", "location": "位置"}}], '
            f'"overall_assessment": "总体评价"}}\n\n'
            f"只报告 high/medium 缺陷，忽略 low 级别问题。\n"
            f"宁可漏报 low 也不要误报 high。"
        )
        prompt = self._inject_roles_context(prompt)
        resp = self._cc_chat_with_retry(auditor, prompt, timeout=7200)
        parsed = self._parse_audit(resp)

        audit_data = {"raw_response": resp, "result": parsed}
        audit_path = auditor.workspace / "audit.json"
        audit_path.write_text(json.dumps(audit_data, ensure_ascii=False, indent=2), encoding='utf-8')

        return [d for d in parsed.get("defects_found", [])
                if d.get("severity") in ("high", "medium")]

    # ==================== 公共工具方法 ====================

    def _build_read_instruction_text(self, cc_id: int) -> str:
        """构建文件读取指令文本"""
        if self.is_modular_output:
            cc_dd = self._cc_deliverable_dir(cc_id)
            idx_path = cc_dd / self.index_file if self.index_file else None
            if idx_path and idx_path.exists():
                # 索引存在：先读索引，按需读其他
                main_line = f"，主文档为 {self.main_doc}" if self.main_doc else ""
                return (
                    f"请先用 Read 工具读取索引文件: {idx_path}\n"
                    f"索引中列出了所有产物文件及其作用{main_line}。\n"
                    f"根据审查需要，用 Read 工具按需读取相关文件（主文档必读，附件按需）。"
                )
            # 无索引：回退到列出所有文件
            files = self._list_code_files(cc_id)
            lines = self._files_to_prompt_lines(files)
            return f"请用 Read 工具读取以下所有文件:\n{lines}"
        else:
            # output_file=None 的角色（如 Manager）：产出写在 CC 回复文本里，
            # 直接指向 cc_output.md，避免让审查者去 Glob 空 deliverables 目录。
            if self.output_file is None:
                raw = self.ccs[cc_id].workspace / "cc_output.md"
                if raw.exists():
                    return f"请用 Read 工具读取文件: {raw}"
            output_path = self._cc_output_path(cc_id)
            if output_path.is_dir():
                return self._build_dir_read_instruction(output_path)
            return f"请用 Read 工具读取文件: {output_path}"

    @staticmethod
    def _build_dir_read_instruction(dir_path: Path) -> str:
        """构建目录型产出的读取指令"""
        idx_path = dir_path / "_INDEX.md"
        if idx_path.exists():
            return (
                f"产出为目录，请先用 Read 工具读取索引文件: {idx_path}\n"
                f"索引中列出了所有产物文件及其作用。\n"
                f"根据审查需要，用 Read 工具按需读取目录 {dir_path} 下的相关文件。"
            )
        # 无索引：列出目录下所有 .md 文件
        md_files = sorted(
            f for f in dir_path.rglob("*.md")
            if f.is_file() and f.name != 'CLAUDE.md'
        )
        if md_files:
            lines = "\n".join(f"  - {f.relative_to(dir_path)}" for f in md_files)
            return (
                f"产出为目录 {dir_path}，请用 Read 工具读取以下文件:\n{lines}"
            )
        return f"请用 Glob 工具探索目录: {dir_path}"

    def _inject_roles_context(self, prompt: str) -> str:
        """向审查 prompt 追加可用角色参考信息（仅 Manager 角色有效）"""
        if self.available_roles_text:
            prompt += (
                f"\n\n## 角色分配参考\n"
                f"以下是本方案中可用的角色列表及其能力边界。审查时请特别关注:\n"
                f"- 所选角色是否落在其 suitable_for 范围内且不被 not_for 否决?\n"
                f"{self.available_roles_text}\n"
            )
        return prompt

    def _copy_best_to_cc0(self, best_id: int):
        """把最优方案的交付物复制到 CC-0 的 deliverable_dir"""
        if best_id == 0:
            return
        if self.is_modular_output:
            best_dd = self._cc_deliverable_dir(best_id)
            cc0_dd = self._cc_deliverable_dir(0)
            # V3-2.1 shared_target 防御：所有 CC 共享同一分区 → 无需复制
            if best_dd == cc0_dd:
                return
            # 删除 CC-0 旧交付物
            for old_file in self._list_code_files_in_dir(cc0_dd):
                old_file.unlink()
            # 复制最优方案的交付物到 CC-0
            for src_file in self._list_code_files_in_dir(best_dd):
                rel = src_file.relative_to(best_dd)
                dst = cc0_dd / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst)
        else:
            best_path = self._cc_output_path(best_id)
            cc0_output = self._cc_output_path(0)
            # V3-2.1 shared_target 防御：同一分区 → 无需复制
            if best_path == cc0_output:
                return
            if best_path.is_dir():
                # 目录型产出：清理 CC-0 旧产出，递归复制目录树
                if cc0_output.is_dir():
                    shutil.rmtree(cc0_output)
                elif cc0_output.exists():
                    cc0_output.unlink()
                shutil.copytree(best_path, cc0_output)
            else:
                best_content = self._read_cc_file(best_id)
                # 防御：CC-0 可能已将产出组织为目录，先清理
                if cc0_output.is_dir():
                    shutil.rmtree(cc0_output)
                elif cc0_output.exists():
                    cc0_output.unlink()
                cc0_output.write_text(best_content, encoding='utf-8')

    def _revise_with_defect_list(self, question: str, cc: ClaudeCodeAgent,
                                  defects: List[dict]):
        """让指定 CC 根据缺陷列表修订产出"""
        defect_lines = []
        for d in defects:
            defect_lines.append(
                f"- [{d.get('severity', '?')}] {d.get('check', d.get('aspect', '?'))}: "
                f"{d.get('description', '')}"
            )
        defect_text = "\n".join(defect_lines)

        read_instruction = self._build_read_instruction_text(0)

        if self.is_modular_output:
            revise_instr = self.revise_instruction
        else:
            output_path = str(self._cc_output_path(0))
            revise_instr = self.revise_instruction.format(output_path=output_path)

        prompt = (
            f"原始问题: {question}\n\n"
            f"{read_instruction}\n\n"
            f"审查中发现了以下缺陷:\n{defect_text}\n\n"
            f"{revise_instr}"
        )
        self._cc_chat_with_retry(cc, prompt, timeout=7200)

    def _finalize_result(self, question: str, scores: dict, best_id: int,
                          consensus_reached: bool, vote_rounds: int,
                          audit_defects: List[dict]) -> dict:
        """汇总结果并保存"""
        avg_scores = self._aggregate_scores(scores) if scores else {0: 8.0}

        # 二进制后缀（best_solution_summary 拼接时跳过）
        BINARY_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico',
                           '.pdf', '.zip', '.gz', '.tar', '.bin', '.exe',
                           '.dll', '.so', '.class', '.pyc', '.pyo'}

        # 保存最佳方案摘要
        if self.is_modular_output:
            # 跳过二进制文件，文本文件用 errors='replace' 防止 UnicodeDecodeError
            text_parts = []
            for f in self._list_code_files(0):
                if f.suffix.lower() in BINARY_SUFFIXES:
                    continue
                try:
                    text_parts.append(f.read_text(encoding='utf-8', errors='replace'))
                except Exception:
                    pass  # 跳过无法读取的文件
            self._save_to_workspace("best_solution_summary.txt", "\n".join(text_parts))

        # 复制到 final 目录
        if self.is_modular_output:
            if self.shared_target:
                # V3-2.1：产物已直写共享分区（Source of Truth），不再复制到
                # workspace/final_file；仅在分区内校验 _INDEX.md 与 main_doc。
                final_dir = Path(self.shared_target)
                self._ensure_index_file(final_dir)
                if self.main_doc:
                    main_path = final_dir / self.main_doc
                    if not main_path.exists():
                        print(f"[AICU] WARNING: 主文档 {self.main_doc} 未在产出中找到，下游解析可能兜底")
            else:
                final_dir = self.workspace / self.final_file
                final_dir.mkdir(parents=True, exist_ok=True)
                cc0_dd = self._cc_deliverable_dir(0)
                for src_file in self._list_code_files(0):
                    rel = src_file.relative_to(cc0_dd)
                    dst = final_dir / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst)
                # 校验 _INDEX.md：缺失则自动生成基础索引
                self._ensure_index_file(final_dir)
                # 校验 main_doc：缺失记 WARNING（不阻塞）
                if self.main_doc:
                    main_path = final_dir / self.main_doc
                    if not main_path.exists():
                        print(f"[AICU] WARNING: 主文档 {self.main_doc} 未在产出中找到，下游解析可能兜底")
        else:
            final_content = self._read_cc_file(0)
            if self.shared_target:
                # V3-2.1：产物已直写分区，不落 workspace/final_file 副本。
                final_dir = Path(self.shared_target)
            else:
                self._save_to_workspace(self.final_file, final_content)
                final_dir = self.workspace / self.final_file

        # content: 最终产出内容成为 ask() 返回值的一等字段，消费者无需再反向解析 final_output_path
        if self.is_modular_output:
            summary_path = self.workspace / "best_solution_summary.txt"
            content = (summary_path.read_text(encoding='utf-8', errors='replace')
                       if summary_path.exists() else "")
        else:
            content = final_content

        # content_parsed: 内容本身是 JSON 时给出解析结果（stub 直传 dict 场景 content 即为 dict）
        content_parsed = None
        if isinstance(content, dict):
            content_parsed = content
        elif isinstance(content, str):
            stripped = content.strip()
            if stripped.startswith('{') or stripped.startswith('['):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, dict):
                        content_parsed = parsed
                except (json.JSONDecodeError, ValueError):
                    content_parsed = None

        result = {
            # V3-2.1 shared_target：final_output_path = 共享分区本身（产物根），
            # 不再是 workspace/final_file 副本。
            "final_output_path": str(final_dir),
            "content": content,
            "content_parsed": content_parsed,
            "scores": scores,
            "avg_scores": avg_scores,
            "best_id": best_id,
            "consensus_reached": consensus_reached,
            "vote_rounds": vote_rounds,
            "audit_defects": audit_defects,
            "audit_passed": len(audit_defects) == 0,
            "strategy": self._peer_strategy_name(),
            "role_config": {
                "output_file": self.output_file,
                "main_doc": self.main_doc,
                "index_file": self.index_file,
                "adversarial_role": self.adversarial_role,
                "audit_role": self.audit_role,
            },
        }
        self._save_to_workspace("aicu_result.json",
            json.dumps(result, ensure_ascii=False, indent=2, default=str)
        )

        print(f"\n{'='*60}")
        print(f"[AICU] 最终产出: {final_dir}")
        print(f"[AICU] 审查: {'通过' if consensus_reached else '未通过'}, "
              f"审计: {'通过' if result['audit_passed'] else '已修订'}")
        print(f"{'='*60}")

        return result

    def _ensure_index_file(self, final_dir: Path):
        """目录模式下确保 _INDEX.md 存在；缺失则自动生成基础索引"""
        idx_path = final_dir / self.index_file
        if idx_path.exists():
            return
        # 列出目录下所有文件（相对路径），生成基础索引
        files = []
        for f in sorted(final_dir.rglob("*")):
            if not f.is_file():
                continue
            if f.name == self.index_file:
                continue
            if any(part in self.NONCODE_DIRS for part in f.parts):
                continue
            rel = f.relative_to(final_dir).as_posix()
            files.append((rel, f.suffix.lower()))
        if not files:
            return
        lines = ["# 阶段产物索引（自动生成）", "",
                 "CC 未产出 _INDEX.md，以下为框架自动列出的文件清单。", ""]
        # 主文档优先
        main_doc = self.main_doc
        main_listed = False
        if main_doc:
            for rel, _ in files:
                if rel == main_doc:
                    lines.append("## 主文档（必读）")
                    lines.append(f"- {rel} — 主文档（自动标注）")
                    lines.append("")
                    main_listed = True
                    break
        lines.append("## 附件（按需读取）")
        for rel, _ in files:
            if main_listed and rel == main_doc:
                continue
            lines.append(f"- {rel}")
        idx_path.write_text("\n".join(lines) + "\n", encoding='utf-8')
        print(f"[AICU] 自动生成索引: {idx_path}")

    def _aggregate_scores(self, scores: Dict[int, Dict[int, float]]) -> Dict[int, float]:
        answer_scores: Dict[int, List[float]] = {}
        for reviewer_id, score_dict in scores.items():
            for answer_id, score in score_dict.items():
                # 防御：确保 score 是 float（LLM 有时返回嵌套 dict）
                if isinstance(score, (int, float)):
                    score = float(score)
                elif isinstance(score, dict):
                    vals = [x for x in score.values() if isinstance(x, (int, float))]
                    score = sum(vals) / len(vals) if vals else 5.0
                else:
                    try:
                        score = float(score)
                    except (ValueError, TypeError):
                        score = 5.0
                answer_scores.setdefault(answer_id, []).append(score)
        return {aid: sum(ss) / len(ss) for aid, ss in answer_scores.items()}

    def _parse_adversarial_vote(self, text: str) -> dict:
        raw = self._extract_json(text)
        if raw:
            approved = raw.get("approved", False)
            if isinstance(approved, str):
                approved = approved.lower() == "true"
            defects = raw.get("defects", [])
            if not isinstance(defects, list):
                defects = []
            return {"approved": bool(approved), "defects": defects, "summary": raw.get("summary", "")}

        # 兜底：无法解析结构化 JSON 时，不依赖关键词推断状态（旧实现
        # re.search('defect|缺陷|问题|错误|bug') 会把「无缺陷，已全部修复」等通过语态
        # 误判为拒绝）。结构化缺陷只由 JSON 路径的 defects 数组承载；自由文本只作摘要记录。
        # approved 兜底默认 False —— 与 JSON 路径 raw.get("approved", False) 一致：
        # 「缺少显式结构化批准即视为未批准」，绝不在无结构化证据时臆造通过。
        return {"approved": False, "defects": [], "summary": text[:500]}

    def _parse_audit(self, text: str) -> dict:
        raw = self._extract_json(text)
        if raw:
            defects = raw.get("defects_found", [])
            if not isinstance(defects, list):
                defects = []
            return {"defects_found": defects, "overall_assessment": raw.get("overall_assessment", "")}
        return {"defects_found": [], "overall_assessment": text[:500]}


