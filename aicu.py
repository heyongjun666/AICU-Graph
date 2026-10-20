import json
import os
import re
import shutil
import signal
import subprocess
import yaml
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Dict, List, Sequence, Set, Tuple

from claude_code_agent import ClaudeCodeAgent, CCExecutionError, ModelImageInputError
from cli_adapters import skills_enabled_clis
from runtime.mcp_client import probe_servers_detailed
from runtime.mcp_manager import TRANSPORT_STDIO, resolve_servers
from runtime.repo_paths import REPO_ROOT, resolve_repo_relative
from runtime.roles_manager import build_available_roles_text


# sentinel for "调用方未传此参数"，与 None（可能是有效值，如 output_file=null）区分
_UNSET = object()

# Manager 角色的固定「组织构建者」人格骨架：所有 Manager 无条件前置注入，保证用户覆盖
# role_md 时不会丢失组织节点能力（Identity / Prime Directive / Dynamic Tree Growth /
# Workflow A-H）。role_md 字段现在只承载「领域角色」部分（如「项目组长，负责组织各成员工作」），
# 运行时由 AICU 拼接为：骨架 + 领域角色，再写入 CC 的 CLAUDE.md。
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
由系统 Router 统一路由; 需并发协作时让生产者直接发布事件(默认开启, 无需在计划里声明), 事件只描述事件本身;
G. 共享工作区 — 所有节点自动挂载同一 Session Shared Workspace; 为需要协作/融合的 worker 分配
不同 output_path 分区(默认给 Worker 分配具体分区, 空串=全局写高权限仅确实需要时用); 不再规划集成节点;
H. 输出纯 JSON ExecutionPlan.

你绝不亲自执行具体任务, 只负责产出结构正确的执行组织树."""


# ========== MCP 能力说明（只在该角色确实挂了 MCP server 时注入） ==========
# 为什么需要显式说明：工具本身是以真名 `mcp__<server>__<tool>` 注册进 CLI 工具表的，
# 模型每轮都看得见 —— 缺的从来不是「存在性」，而是**规范性**。没有这一段时，一个挂着
# github MCP 的代码角色遇到「提个 issue」，完全可能自己拼个 curl 绕过去，而且不解释为什么
# 不用。正式角色 mcp_probe 需要在 role_md 里手写「必须真的调用工具」；这里做统一兜底，
# 联调 lab 则只作为显式命名的测试夹具存在。
#
# 预检后的 active 列表只包含本次确实连通的 server；optional server 失败时记录在
# report.failures，并从 active 列表移除，但仍可在能力说明中告知模型。
# 成功但没有工具的 server 仍保留（tools 映射中存在该名字，工具列表为空）。
#
# 末尾的「不要为了用而用」不是客气话：只写「要优先用」会诱发为用而用 —— 硬凑一次调用
# 好显得用上了，那比不用更糟，因为它污染产物。
def _render_mcp_capability_note(
    servers: list,
    probed: Dict[str, List[str]],
    failures: Optional[Dict[str, str]] = None,
) -> str:
    """渲染「本次可用的 MCP 工具」块；没有 server 时返回 **空串**。

    调用方必须把空串当「不注入」处理（拼 prompt 时过滤掉、写 CLAUDE.md 时跳过），
    不要原样拼进去 —— 否则 `mcp: []` 的角色会凭空多出空行，改变既有产物的字节
    （绝大多数角色都是 `mcp: []`，它们的提示词必须逐字不变）。

    :param servers: 已配置并解析的 server 对象列表。
    :param probed:  active server 名 → 工具名列表。
    :param failures: optional server 名 → 本次预检错误；失败 server 不会下发给 CLI。
    """
    if not servers:
        return ""
    lines = [
        "# 可用工具通道 (MCP)",
        "",
        "本次配置声明了以下 MCP server；成功连通的工具会注册在你的工具表里"
        "（真名形如 mcp__<server>__<tool>），未连通的通道会明确标出：",
        "",
    ]
    for server in servers:
        where = "本地 stdio" if server.transport == TRANSPORT_STDIO else "远程 Streamable HTTP"
        tools = probed.get(server.name)
        failure = (failures or {}).get(server.name)
        if failure is not None or tools is None:
            detail = f"失败原因：{failure}。" if failure else ""
            lines.append(
                f"- {server.name}（{where}）：**本次未连通**，这些工具现在不在你的工具表里。"
                f"{detail}不要指望它们，更不要凭任务描述假装调用过；确实需要就走你手上现有的工具，"
                "并在产物里说明这条通道没通。"
            )
        elif not tools:
            lines.append(
                f"- {server.name}（{where}）：连上了，但对方没有暴露任何工具。"
            )
        else:
            lines.append(f"- {server.name}（{where}）：{' / '.join(tools)}")
    lines += [
        "",
        "动手之前先扫一眼上面这些工具能不能直接完成你要做的某一步：",
        "",
        "- 能用就用它们，不要自己用 OS 工具或命令行把同一件事再实现一遍；",
        "- 调用它们不需要额外确认，权限已经放行；",
        "- 真的调用了，就在产物里留下可核对的痕迹（原始返回的逐字片段），"
        "而不是「我调用成功了」这类转述 —— 转述是你说的，原始返回是它说的；",
        "- 上面没有哪个工具适合当前步骤时，照常用你自己的工具，**不要为了用而用**："
        "硬凑一次调用会污染产物，比不用更糟。",
    ]
    return "\n".join(lines)


# ========== Skill 优先说明（只在该角色确实挂了 skill 且该 CLI 接得上时注入） ==========
# 为什么需要：skill 清单只是会话开头的一段附带信息，其触发描述写给「用户说了什么」，而节点收到的
# 是框架拼好的任务提示词，两者对不上；实测（60e70127 会话 + 单节点复测）带 skill 的角色一次
# 都没主动调用过。所以由发送口明确要求「开工前先对照 skill」。
# 与 MCP 说明同挂发送口，且同样要求：没挂 skill 时返回空串，调用方必须过滤，既有提示词字节不变。
# 末尾「不要为了用而用」同 MCP 说明：硬凑一次调用比不用更糟。
# 正文不得出现半角花括号（与工作区边界块同理：避免污染输出契约）。
def _render_skills_note(names: List[str], skills_workspace_dir) -> str:
    """渲染「开工前先对照 skill」块；没配 skill 或该 CLI 接不上 skill 目录时返回 **空串**。"""
    if not names or not skills_workspace_dir:
        return ""
    skills_dir = "/".join(skills_workspace_dir)
    return (
        f"【Skill 优先】你配置了这些 skill：{'、'.join(names)}。"
        "开工前先判断任务里有没有哪一步属于其中某个 skill 的适用场景（以各 skill 的说明为准）：\n"
        "- 有：先用 Skill 工具调用它，再按它的流程做；"
        f"Skill 工具被拒绝或不可用时，直接 Read `{skills_dir}/<skill 名>/SKILL.md` 照做；\n"
        "- 没有：照常做，不要为了用而用，也不要在产物里声称用过实际没用的 skill。"
    )


class AICU:
    """
    AI Computing Unit — 单 CC 执行单元

    一次 ask() 只调用一个 CC（CC-0）生成结果并交付，没有节点内的审查 / 修订 / 融合 / 审计。
    结果的质量把关由上层 Manager 的 review 负责（见 executables/manager.py）。

    角色配置:
      通过 role 参数传入角色名称字符串（必须）。AICU 根据 role_type 参数确定角色目录：
      - role_type="manager" → 从 roles/Manager/ 目录加载
      - role_type="worker"  → 从 roles/Worker/ 目录加载
      先加载对应目录下的 default.yaml 作为基础配置，再加载 {role}.yaml 覆盖。
      新增角色只需在对应目录下放一个 {name}.yaml 文件，可以只写 role_md，
      其他字段自动从 default.yaml 继承。

      AICU 初始化参数（model/cli_name）也支持从 default.yaml 取默认值：
      调用方在 AICU(...) 中显式传参时用传入值，未传（None）则用合并后 yaml 中的对应字段。

      roles/default.yaml 与 roles/{role}.yaml 字段说明:

        【AICU 初始化参数】（均支持 None-sentinel：调用方传 None 则从 yaml 取）
        - model: CC 调用使用的模型名
        - cli_name: CLI 名称；null 表示使用系统默认
        - generate_instruction: 生成阶段 prompt 模板，{output_file} 替换为产出文件名。
                                显式设为 "" 时表示由 question 自带 prompt（调用方传
                                generate_instruction="" 用此机制）。

        【产出文件】
        - output_file: 产出文件名
        - final_file: 最终文件名
        - modular_output: 是否模块化多文件产出

        【AICU 人格】
        - role_md: AICU 角色人格文本，写入 CC 的 CLAUDE.md。
                   default.yaml 提供通用兜底人格，具体角色应在自己 yaml 中显式覆盖。

        【skills 配置】
        - skills: 该角色 CC 可用的 skill 名称列表（从 skills_dir 中选取）。
                  空列表表示不启用任何 skill。非空时，AICU 为 CC workspace 创建
                  .claude/skills/ 软链接到指定 skill，并通过 --setting-sources project
                  限定 CC 只加载这些 project-level skill，不加载用户级 skill。
        - skills_dir: skill 源目录。支持绝对路径、~ 家目录、或相对项目根的路径
                      （如 "skills" 表示仓库根下的 skills/ 目录，开源分发友好）。
                      默认 ~/.claude/skills，角色 yaml 可覆盖以使用自定义 skill 库。
                      仅当 skills 非空时此字段才生效。

        【mcp 配置】
        - mcp: 该角色可用的 MCP server 名字列表（在 config/mcp_servers.json 中定义）。
               空列表表示不启用。与 skill 同一思路：角色只引用名字，command/args 集中在
               注册表里 —— 角色 yaml 是可以在设置页上编辑的，内联可执行命令等于在页面上
               放一个「编辑一段将被执行的命令」的输入框。
               非空时 AICU 在建 CC 之前逐个预检（initialize + tools/list），连不上就抛错，
               节点随即 FAILED 并留下可读日志（MCP server 起不来时 CLI 只会打条 warning
               继续跑，不预检的话日志里不会有任何 MCP 相关的话）。
               配置按次注入、不碰用户的 CLI 配置文件，所以并发节点天然隔离。
               能落到哪些 CLI 上取决于 AICU 有没有为它接线（见各 adapter 的
               supports_mcp）：落到没接线的 CLI 上会在建 CC 时就报错，不会静默忽略。

    workspace 结构:
      cc_0/
        {output_file}        # CC-0 生成的产出（CC 自己写入）
      父目录/
        {final_file}         # 最终产出（从 CC-0 复制）
        aicu_result.json     # 结构化结果
    """

    def __init__(self,
                 role_type: str = "worker",
                 parent_workspace: Optional[str] = None,
                 model: Optional[str] = None,
                 session_model: Optional[str] = None,
                 cli_name: Optional[str] = None,
                 role: Optional[str] = None,
                 output_file=_UNSET,
                 final_file=_UNSET,
                 modular_output=_UNSET,
                 generate_instruction=_UNSET,
                 output_callback: Optional[Callable[[str, str], None]] = None,
                 api_base_url: Optional[str] = None,
                 api_key_env: Optional[str] = None,
                 provider_id: Optional[str] = None,
                 skills: Optional[List[str]] = None,
                 skills_dir: Optional[str] = None,
                 mcp: Optional[List[str]] = None,
                 skip_permissions: bool = True,
                 shared_target: Optional[str] = None,
                 shared_root: Optional[str] = None,
                 candidate_roles: Optional[Sequence[str]] = None):
        """
        初始化 AICU

        所有参数都可选（除 parent_workspace 和 role 必须提供）。未传的参数从
        对应角色目录的 default.yaml 取默认值，角色 yaml 中显式写的字段会覆盖 default。

        :param role_type: 角色类型（"manager" 或 "worker"），决定从 roles/Manager/ 还是
                     roles/Worker/ 目录加载 YAML。默认 "worker"（向后兼容）。
        :param parent_workspace: 工作目录（必须提供）
        :param model: 模型名称（节点/任务显式指定；未传则按 角色 yaml → 系统设置 解析，
                     全程无代码级兜底，均无则由目标 CLI 用自身默认模型）
        :param session_model: 系统设置（会话配置）注入的模型名，解析优先级最低，
                     仅当 显式 model 与 角色 yaml model 都未指定时使用
        :param cli_name: CLI 名称（默认从 default.yaml 的 cli_name 取）
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
                     CC 直写该分区。
        :param shared_root: 会话共享区根绝对路径（`{workspace_root}/shared`）。仅用于
                     **工作区域边界声明**：在每次 CC 调用前置入"你只能访问这些路径"的硬约束
                     （见 `_build_workspace_boundary_template`）。不传则边界块退化为只声明
                     本节点工作区（Router CC 等无共享区场景）。它不影响产物归属判定 ——
                     那是 shared_target 的职责。
        :param candidate_roles: 会话候选角色池（角色 id 列表）。非空时，
                     `{available_roles}` 面板**只列出池内角色**（严格模式，无例外，
                     连 default 也不出现在面板里）；不传/为空表示不限制，面板列出
                     roles/ 下全部角色（历史行为，逐字不变）。
                     注意这是**提示词候选范围**而非校验边界：PlanValidator 仍以全量角色
                     为允许集合（见 runtime/runtime.py 的 roles_dir），因此池外角色不会
                     被拒 —— 池的作用是收窄 Manager 的候选描述，不是阻断。
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
        _roles_parent = REPO_ROOT / "roles"  # roles/ 父目录
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

        # 会话候选角色池：None = 未绑定 → 角色面板列出 roles/ 下全部角色。
        # 空列表与 None 等价（一个角色都没有的池没有意义），必须在下面拼面板之前归一。
        self.candidate_roles: Optional[Set[str]] = (
            set(candidate_roles) if candidate_roles else None
        )

        # 4. 应用初始化参数：调用方未传（_UNSET）的从合并后的 yaml 取
        #    注意：model/cli_name 用 None-sentinel（None 不是有效值）
        #    output_file/final_file/modular_output/generate_instruction 用 _UNSET sentinel
        #    （因为这些字段 None/""/False 都可能是有效覆盖值，不能与"未传"混淆）
        # 模型解析链（不要代码兜底）：节点显式 model > 角色 yaml 显式 model >
        # 系统设置（session_model）> None（目标 CLI 使用自身默认模型）。
        # 每个环节都允许 None，绝不落入写死的默认模型名。
        if model is None:
            model = self.role_config.get("model")
        if model is None:
            model = session_model
        if cli_name is None:
            cli_name = self.role_config.get("cli_name")  # 可能仍是 None
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
        # （绑定了候选角色池时只列池内角色，见 runtime.roles_manager.build_available_roles_text）。
        self.available_roles_text = ""
        if "{available_roles}" in generate_instruction:
            available_text = build_available_roles_text(
                _roles_parent, self.candidate_roles
            )
            generate_instruction = generate_instruction.replace(
                "{available_roles}", available_text
            )
            self.available_roles_text = available_text
        # 目录模式下的主文档名（如 design.md / plan.json）与索引文件名
        main_doc = self.role_config.get("main_doc", None)
        index_file = self.role_config.get("index_file", "_INDEX.md")

        # V3-2.1 shared_target：canonical absolute path；CC 直写共享分区。
        if shared_target:
            shared_target = os.path.realpath(shared_target)
        self.shared_target = shared_target
        # 会话共享区根：只喂给"工作区域边界"声明，不参与产物归属判定（那是 shared_target 的事）。
        self.shared_root = os.path.realpath(shared_root) if shared_root else None
        self.workspace = Path(parent_workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.cli_name = cli_name

        self.output_file = output_file
        self.final_file = final_file
        self.modular_output = modular_output
        self.generate_instruction = generate_instruction
        # Stable role/decision context is installed once in CLAUDE.md.  The
        # output contract remains available as a small per-request suffix so
        # resumed conversations always receive the required response shape.
        (
            self._stable_generation_instruction,
            self._output_format_instruction,
        ) = self._split_generation_instruction(generate_instruction)
        self.main_doc = main_doc
        self.index_file = index_file
        # deliverable_dir: CC 交付物在 workspace 内的子目录，技能内部产物自然隔离
        # 默认为 None（workspace 根目录，向后兼容）
        self.deliverable_dir = self.role_config.get("deliverable_dir", None)

        # ---- skills 配置 ----
        # skills_dir: skill 源目录（默认 ~/.claude/skills，角色 yaml 可覆盖；
        # 相对路径按项目根解析，如 "skills"）
        # skills: 要启用的 skill 名称列表，从 skills_dir 中查找子目录，
        # 为 CC 在**它那个 CLI 的项目级 skill 目录**下创建软链接（claude 系是
        # .claude/skills/ 外加 --setting-sources project；qwen 是 .qwen/skills/，
        # 依此类推 —— 位置由 adapter.skills_workspace_dir 声明）。
        self.skills: List[str] = list(
            skills if skills is not None else self.role_config.get("skills") or []
        )
        skills_dir_cfg = skills_dir or self.role_config.get("skills_dir") or "~/.claude/skills"
        # 相对路径 → 相对项目根解析，便于开源分发：仓库自带 skills/ 时 yaml 可直接写
        # "skills"，clone 后无需改配置。规则与 MCP 注册表共用一份（runtime/repo_paths.py）。
        self._user_skills_dir = resolve_repo_relative(skills_dir_cfg)

        # ---- mcp 配置 ----
        # mcp: 要启用的 MCP server 名字列表，在 config/mcp_servers.json 中定义。
        # 这里只做「名字 → server 定义」的解析（名字写错立刻报错并列出可用的），
        # 真正的连通性预检在建 CC 之前（见下方 mcp 预检）。
        raw_mcp = mcp if mcp is not None else self.role_config.get("mcp")
        if raw_mcp is None:
            raw_mcp = []
        elif isinstance(raw_mcp, str):
            # YAML 里写成 mcp: echo-probe 时，它表示一个 server，不是字符序列。
            raw_mcp = [raw_mcp]
        elif not isinstance(raw_mcp, list):
            raise ValueError("mcp 配置必须是 server 名称字符串或名称列表")
        if any(not isinstance(name, str) for name in raw_mcp):
            raise ValueError("mcp 配置中的 server 名称必须是字符串")
        self.mcp: List[str] = list(raw_mcp)
        self._configured_mcp_servers = resolve_servers(self.mcp)
        self._mcp_servers = list(self._configured_mcp_servers)

        # 「skills 在这个 CLI 上接不上」的告警只发一次。
        self._skills_unwired_warned = False

        # 执行配置由节点/会话在边界传入；密钥只保存环境变量名，不读取或记录真实值。
        self.output_callback = output_callback
        self.api_base_url = api_base_url
        self.api_key_env = api_key_env
        self.provider_id = provider_id
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

        # ---- 将 role_md 写入 CC 的 CLAUDE.md ----
        # AICU 自己写 CLAUDE.md（不依赖 ClaudeCodeAgent 的 instruction_md_path），
        # 同时按 self.skills 为 CC 准备**它那个 CLI 的**项目级 skill 目录软链接，
        # 并由 enable_project_skills=True 给 claude 系补上 --setting-sources project
        # （其余 CLI 没有这个 argv 口子，只走目录）。
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

        # ---- mcp 预检 ----
        # 在建 CC 之前逐个连一次（initialize + tools/list）。位置与 skill 校验同理：
        # 让「环境没准备好」在启动阶段就以一条明确错误暴露出来。MCP server 起不来时
        # CLI 只会打一条 warning 继续跑，agent 发现工具不在会绕路或报一个与 MCP 无关的
        # 错 —— 不预检的话，日志里不会有任何 MCP 相关的话可查。
        # 失败时的路径和 skill 一样：AICU 构造抛异常 → Worker 捕获 → 节点 FAILED +
        # 可读日志 → 用户把 server 起来 → Continue 原地重跑（那时会重新构造 AICU，
        # 也就重新预检一次）。
        # 没挂 MCP 时 `_mcp_capability_note` 是空串，发送口与 CLAUDE.md 都据此跳过 ——
        # 绝大多数角色都是 `mcp: []`，它们的提示词必须逐字节不变。
        self._mcp_capability_note = ""
        self._skills_note = ""
        if self._mcp_servers:
            # 预检的返回值是 server 名 → 工具名，正好是能力说明要写的东西 —— 接住它，
            # 不额外多探一次。（预检失败的 server 不在返回值里，那正是「配了但没连通」。）
            probe_report = probe_servers_detailed(self._configured_mcp_servers)
            configured_mcp_servers = probe_report.configured
            self._mcp_servers = probe_report.active
            self._mcp_capability_note = _render_mcp_capability_note(
                configured_mcp_servers, probe_report.tools, probe_report.failures
            )
            # 预检过了才往下发。下发的是 server 对象本身（不是预先拼好的 JSON 串）：
            # 各 CLI 的配置形态不同，序列化是 adapter 的事（见 claude_code_agent.py 的
            # mcp_servers 参数说明）。落到没接线的 CLI 上会在建 CC 时直接报错。
            print(f"[AICU] mcp: {[server.name for server in self._mcp_servers]} (预检通过，内联下发)")

        cc_id = "CC-0"

        def cc_output(stream: str, message: str) -> None:
            if self.output_callback:
                self.output_callback(f"{cc_id}:{stream}", message)

        cc = ClaudeCodeAgent(
            workspace_path=str(self.workspace / "cc_0"),
            model=model,
            cli_name=cli_name,
            skip_permissions=self.skip_permissions,
            enable_project_skills=enable_skills,
            output_callback=cc_output,
            api_base_url=self.api_base_url,
            api_key_env=self.api_key_env,
            provider_id=self.provider_id,
            mcp_servers=self._mcp_servers,
        )
        # 写入一次性的角色与长期决策上下文；输出格式由每轮生成请求显式携带。
        role_context = self.role_md_content
        # MCP 能力说明夹在 role_md 与「长期生成规则」之间：它属于「我是谁、手上有什么」，
        # 而长期生成规则（Manager 那份 300 行）是输出契约 —— 排在它后面会被埋掉。
        if self._mcp_capability_note:
            role_context += "\n\n---\n\n" + self._mcp_capability_note
        if self._stable_generation_instruction:
            role_context += (
                "\n\n---\n\n# 长期生成规则 (Stable Generation Rules)\n\n"
                + self._stable_generation_instruction
            )
        if role_context:
            claude_md = cc.workspace / "CLAUDE.md"
            claude_md.write_text(role_context, encoding='utf-8')
        # 写入该 CLI 自己的项目级 skill 目录（仅当配置了 skills）
        if enable_skills:
            self._install_skills_to_cc(cc)
            if not self._skills_note:
                self._skills_note = _render_skills_note(
                    self.skills, cc.adapter.skills_workspace_dir
                )
        # 创建交付物子目录（deliverable_dir 指定时），技能内部产物自然隔离
        # V3-2.1：shared_target 模式下 CC 直写共享分区（产物唯一权威），
        # 不建私有 deliverables 死目录（避免 cc_0/deliverables 空壳）。
        if self.deliverable_dir and not self.shared_target:
            (cc.workspace / self.deliverable_dir).mkdir(parents=True, exist_ok=True)
        # 工作区域边界：构造期算一次挂在 CC 上（路径不随调用变化），
        # _cc_chat_with_retry 每次发送时读取。用动态属性而非 AICU 上的公共字段，
        # 是为了让 AICU.__new__ 形态的测试 CC（无此属性）自然退化为"不加边界块"。
        cc._workspace_boundary = self._build_workspace_boundary(cc)
        self.ccs = [cc]

        print(f"[AICU] 初始化完成: 1 个 CC，父目录: {self.workspace}")
        print(f"[AICU] 角色: output_file={self.output_file}")
        if self.role_md_content:
            print(f"[AICU] CLAUDE.md: 已写入 ({len(self.role_md_content)} 字符, 来源: {role_name_used})")
        if enable_skills:
            print(f"[AICU] skills: {self.skills} (project-level，已为 CC 软链接)")

    def _list_available_skills(self) -> List[str]:
        """列出 skills_dir 下所有可用 skill 名称"""
        if not self._user_skills_dir.exists():
            return []
        return sorted(p.name for p in self._user_skills_dir.iterdir() if p.is_dir())

    def _cc_deliverable_dir(self, cc_id: int) -> Path:
        """返回 CC 的交付物子目录，或 workspace 根目录（deliverable_dir 未设置时）。

        V3-2.1 shared_target 模式：CC 直写共享分区（产物唯一权威），
        返回分区本身。
        """
        if self.shared_target:
            return Path(self.shared_target)
        ws = self.ccs[cc_id].workspace
        if self.deliverable_dir:
            return ws / self.deliverable_dir
        return ws

    def _install_skills_to_cc(self, cc) -> None:
        """把 self.skills 送进这个 CC 的 CLI 能读到的项目级 skill 目录。

        目录由 adapter 声明（``skills_workspace_dir``），因为**每家读的位置不一样**：
        claude 读 ``.claude/skills``（另加 ``--setting-sources project``），
        codebuddy 读 ``.codebuddy/skills``，kimi / codex 读 ``.agents/skills``，
        qwen 读 ``.qwen/skills``，pi 读 ``.pi/skills``，gemini 读 ``.gemini/skills``，
        cursor 读 ``.cursor/skills``。

        修复前的形状是把 ``.claude/skills`` 写死、对**所有** CLI 照建 —— 非 claude 系上
        那是一堆死文件：建了，永远不生效，用户以为配好了。这正是 ``mcp_workspace_files``
        的 docstring 里记着的那个教训。

        接不上的 CLI（adapter 没声明目录）**只告警、照常跑**，不报错 —— 与 MCP 的硬门禁
        刻意不同。理由是两者的代价不对称：MCP 没接上时工具根本不存在，任务会以一种
        看不出原因的方式失败；skills 没接上只是少一份能力加成，任务本身照做。
        """
        adapter = cc.adapter
        if adapter.skills_workspace_dir is None:
            self._warn_skills_unwired(adapter)
            return
        adapter.skills_workspace_files(cc.workspace, self._user_skills_dir, self.skills)
        # 有的 CLI 光摆好目录不算数，还得先「授信」这个项目（现在只有 pi，见
        # PiAdapter.skills_workspace_trust）。默认实现什么都不做。
        adapter.skills_workspace_trust(cc.workspace)

    def _warn_skills_unwired(self, adapter) -> None:
        """角色配了 skills、但这个 CLI 接不上时，告警一次。"""
        if self._skills_unwired_warned:
            return
        self._skills_unwired_warned = True
        print(
            f"[警告] CLI '{adapter.id}' 的 project-level skill "
            f"{adapter.skills_unwired_reason()}，"
            f"本次运行声明的 skill {self.skills} 在它上面不会生效。"
            f"本次也可以改用已接入 skill 的 CLI"
            f"（{', '.join(skills_enabled_clis())}）。"
        )

    # ========== CC 调用容错 ==========

    CC_MAX_RETRIES = 2  # 遇到 error_during_execution 时最多重试次数

    # 模型不支持图片输入时的恢复指令：同会话 --resume 续接后，让 CC 跳过图片继续任务。
    _IMAGE_INPUT_RECOVERY_PROMPT = (
        "注意：上一步你读取了一张图片，但当前模型不支持图片输入，API 返回 400 失败，"
        "对话被中断。请忽略所有图片/截图内容，不要再读取任何图片文件"
        "（.png/.jpg/.jpeg/.gif/.webp/.bmp）。请基于已掌握的文本信息，改用"
        "不依赖视觉的方式（读取文本内容、脚本断言、关键数值核对等），继续完成你被分配的任务。"
    )

    # 粘性防读图硬约束：某个 CC 一旦触发过读图错误，后续每一次调用
    # （含重试）都携带本约束，防止任何后续轮次再次读图。
    _IMAGE_INPUT_BLOCKED_CONSTRAINT = (
        "【硬性约束】你此前曾因读取图片触发 API 400 报错（当前模型不支持图片输入）。"
        "从现在起，禁止用任何工具读取图片/PDF 文件"
        "（.png/.jpg/.jpeg/.gif/.webp/.bmp/.pdf；需要 PDF 内容时先转成文本再读），禁止截图目检。"
        "所有原本依赖视觉的核对，一律改用文本描述、脚本断言（如元素/样式断言）、关键数值断言。"
    )

    # 工作区域边界：**无条件**前置到每一次 CC 调用的硬约束（见 _cc_chat_with_retry）。
    # 与 _IMAGE_INPUT_BLOCKED_CONSTRAINT 的两点不同：
    #   1. 无条件生效（那个只在触发过读图错误后才带）；
    #   2. 必须随每次 cc.chat() 重新附加 —— 读图重试分支会整体替换 prompt，
    #      只在函数顶部加一次会在重试时静默丢失。
    def _build_workspace_boundary(self, cc: ClaudeCodeAgent) -> str:
        """为单个 CC 生成「你只能访问这些路径」的硬约束块。

        这是提示词层的前置声明：AICU 起子进程只给了工具白名单
        （--allowedTools Read Write Edit Bash），**不是路径白名单**，
        所以"哪些路径可以碰"只能靠这里讲清楚。

        注意：正文中**不得出现半角花括号**。prompt 末尾可能要求"只输出 JSON"，
        边界块里出现 { } 会与输出契约混淆。
        """
        lines = [
            "【工作区边界 · 硬性】",
            "你**只能**访问下列路径。除此之外的任何路径一律不得读取、写入、执行或遍历 ——",
            "包括宿主仓库源码、父级目录、其他会话的目录、用户主目录与系统目录。",
            "工具本身不做路径限制，但「能读」不构成许可。",
            "",
        ]
        if self.shared_root:
            lines.append(
                f"- 会话共享区: `{self.shared_root}`（可读可写，含兄弟分区，供协作复用）"
            )
        if self.shared_target:
            lines.append(
                f"- 你的专属分区: `{self.shared_target}`（你主要负责写入此处）"
            )
        cc_dir = getattr(cc, "workspace", None)
        if cc_dir:
            lines.append(
                f"- 你自己的私有目录: `{cc_dir}`（草稿/中间产物，不要写进共享区）"
            )
        lines += [
            "",
            "唯一例外: 仅当**用户自己的输入**明确点名某个外部地址或路径时，才可访问那一个",
            "被点名的目标，且不得扩大到它的父目录或同级路径。此外没有别的豁免。",
            "本任务输入中已经明确给出的路径（例如依赖产物路径）视为已授权，可直接使用。",
            "缺输入时如实说明缺什么，不要猜测、不要编造、也不要自行去范围外寻找。",
            "",
            "本约束不改变下方任务的产出目标与输出格式。",
        ]
        return "\n".join(lines)

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
        # 粘性防读图：一旦本 CC 触发过读图错误，后续所有调用（含重试）
        # 都携带【禁止读图】硬约束前缀，防止任何后续轮次再次读图。
        if getattr(cc, "_image_input_blocked", False):
            prompt = self._IMAGE_INPUT_BLOCKED_CONSTRAINT + "\n\n" + prompt
        original_prompt = prompt
        for attempt in range(1, self.CC_MAX_RETRIES + 2):  # 最多 CC_MAX_RETRIES+1 次（1次原始 + N次重试）
            try:
                # 工作区域边界：在发送点附加，而不是在函数顶部加一次 —— 下面的重试分支
                # 会整体替换 prompt（读图恢复指令 / 重置后的新 prompt），顶部加的会静默丢失。
                # 读取用 getattr 兜底：AICU.__new__ 形态的测试 CC 没有该属性 → 不加块。
                # MCP 能力说明与它同挂发送口，理由是同一个的两面：
                #   - 重试分支整体替换 prompt，挂在顶部的会丢；
                #   - CLAUDE.md 只有 Claude 系 CLI 会读（gemini 不读，见
                #     tests/test_workspace_boundary.py 的说明），而 MCP 工具是各家 CLI
                #     都能拿到的 —— 只写 CLAUDE.md 等于在一半 CLI 上静默失效。
                # 空串过滤后再 join：没挂 MCP 时产出的字节与加这段之前完全相同。
                _prefixes = [
                    p for p in (getattr(cc, "_workspace_boundary", ""),
                                getattr(self, "_mcp_capability_note", ""),
                                getattr(self, "_skills_note", ""))
                    if p
                ]
                _outgoing = "\n\n".join([*_prefixes, prompt])
                response = cc.chat(_outgoing, max_turns=max_turns, timeout=timeout)
                self._emit_cc_activity(cc, "completed", prompt, "finished")
                return response
            except ModelImageInputError as e:
                # 标记本 CC 触发过读图错误：此后所有调用均携带禁止读图硬约束
                cc._image_input_blocked = True
                # 模型不支持图片输入（读图触发 400 api_error）：模型能力限制而非
                # 会话损坏。claude_code_agent 已尽力保留 conversation_id：
                #   - 会话可续接 → 同会话 --resume 续接，给『跳过图片』恢复指令重试；
                #   - 会话不可续接 → 重置后带『禁读图』说明重试原任务。
                if attempt <= self.CC_MAX_RETRIES:
                    import time
                    print(
                        f"[AICU] CC 读取图片触发模型图片输入限制，第 {attempt} 次重试"
                        f" (共 {self.CC_MAX_RETRIES} 次): {e}"
                    )
                    time.sleep(5 * attempt)  # 递增等待: 5s, 10s, ...
                    if cc.conversation_id and attempt == 1:
                        # 同会话 --resume 续接，恢复指令携带具体报错文本，
                        # 让 CC 明确"为什么失败"，避免再次读图。
                        # 仅第 1 次重试走 resume：实测图片已写入会话历史时，resume 会立刻
                        # 再次 400；第 2 次起改为重置会话 + 原任务 + 禁读图说明。
                        prompt = (
                            self._IMAGE_INPUT_RECOVERY_PROMPT
                            + "\n\n[具体报错]\n"
                            + str(e)
                        )
                    else:
                        cc.reset_conversation()
                        prompt = (
                            original_prompt
                            + "\n\n---\n\n[注意] 上一步你尝试读取图片导致 API 报错："
                            "[具体报错]\n"
                            + str(e)
                            + "\n\n当前模型不支持图片输入。请忽略所有图片/截图内容，"
                            "不要再读取任何图片文件（.png/.jpg/.jpeg/.gif/.webp/.bmp），"
                            "改用文本、DOM/计算样式断言等不依赖视觉的方式继续完成任务。"
                        )
                else:
                    print(f"[AICU] CC 图片输入限制重试 {self.CC_MAX_RETRIES} 次后仍然失败，放弃")
                    self._emit_cc_activity(cc, "failed", prompt, "image_input_error")
                    raise
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
                    return raw.read_text(encoding="utf-8", errors="replace")
                return ""
            texts = (self._read_text_file(f) for f in files if f.exists())
            return "\n".join(t for t in texts if t is not None)
        path = self._cc_deliverable_dir(cc_id) / target
        if not path.exists():
            return ""
        if path.is_dir():
            return self._read_output_directory(path)
        return path.read_text(encoding='utf-8', errors='replace')

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
            parts = [index_path.read_text(encoding='utf-8', errors='replace')]
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
            parts.append(f.read_text(encoding='utf-8', errors='replace'))
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
        # 各 CLI 的项目级 skill 目录 —— AICU 往这些目录里建 skills 软链（见
        # _install_skills_to_cc）。不加进来的话，SKILL.md 会被当成 CC 写的代码：
        # 它是 .md（不在 NONCODE_EXTENSIONS 里）、也不在 NONCODE_FILENAMES 里，
        # 于是被复制进最优方案目录并出现在 _INDEX.md 的文件清单里。
        # 代价是「CC 处理的项目里恰好有同名目录时不收」—— 这几个本来就是各家 CLI
        # 自己的配置目录，排除掉更符合预期。（.claude 早就在这份名单里，同一条理由。）
        '.qwen', '.pi', '.gemini', '.cursor', '.opencode', '.codebuddy', '.agents',
        '.venv', 'venv', 'env', '.env', 'dist', 'build', '.next', '.nuxt',
        'target', 'bin', 'obj', '.idea', '.vscode', '.cache', 'coverage',
        '.tox', '.mypy_cache', '.ruff_cache', '__pypackages__',
    }
    # 编译产物/临时文件扩展名（不是源代码，是工具生成的）
    NONCODE_EXTENSIONS = {
        '.pyc', '.pyo', '.class', '.o', '.so', '.dll', '.exe',
        '.dat', '.db', '.sqlite', '.pickle',
        '.lock', '.cache', '.tmp', '.swp', '.bak', '.orig',
        '.egg-info', '.whl',
        '.log', '.out',  # 日志/命令输出（CC 测试产物），非源码
    }
    # 框架自身文件（AICU/CC 生成的管理文件，不是项目产出）
    NONCODE_FILENAMES = {
        'CLAUDE.md', 'AGENTS.md',
        'review.json', 'audit.json', 'aicu_result.json',
        'best_solution_summary.txt',
    }
    # 已知二进制后缀（拼接文本时直接跳过，省一次打开）；未列出的二进制由 _read_text_file 的 NUL 探测兜住
    BINARY_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico',
                       '.pdf', '.zip', '.gz', '.tar', '.bin', '.exe',
                       '.dll', '.so', '.class', '.pyc', '.pyo'}

    @classmethod
    def _read_text_file(cls, f: Path) -> Optional[str]:
        """读文本文件；二进制文件（已知后缀，或前 4 KB 含 NUL 字节）返回 None。"""
        if f.suffix.lower() in cls.BINARY_SUFFIXES:
            return None
        with open(f, 'rb') as fh:
            head = fh.read(4096)
            if b'\0' in head:
                return None
            data = head + fh.read()
        return data.decode('utf-8', errors='replace')

    def _is_code_file(self, f: Path, root: Path) -> bool:
        """判断 ``root`` 下的文件是否是代码文件（排除模式：非代码的才算非代码，其余都是代码）

        目录排除只看 ``f`` 相对 ``root`` 的路径：``root`` 自身及其祖先目录叫什么
        （例如工作区恰好位于名为 build / env 的目录下）与"这个文件是不是产出"无关。
        """
        if not f.is_file():
            return False
        # 排除非代码目录下的文件
        if any(part in self.NONCODE_DIRS for part in f.relative_to(root).parts):
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
        files = [f for f in directory.rglob("*") if self._is_code_file(f, directory)]
        return sorted(files)

    @staticmethod
    def _split_generation_instruction(instruction: str) -> Tuple[str, str]:
        """Split stable generation rules from the per-request output contract.

        Role identity and decision policy belong in ``CLAUDE.md``.  The output
        section is intentionally retained for every generation request because
        a resumed CLI conversation must still receive the current response
        shape explicitly.
        """
        if not instruction:
            return "", ""
        match = re.search(
            r"(?m)^##\s+(?:输出格式|JSON\s*格式要求|产出要求|产物组织方式)(?:\s|（|\(|:|：|$)",
            instruction,
        )
        if match is None:
            # Roles without a named output section have no separable stable
            # generation policy; keep their complete generation contract in
            # each request rather than silently dropping required instructions.
            return "", instruction.strip()
        return instruction[:match.start()].rstrip(), instruction[match.start():].strip()

    def _index_rules(self) -> str:
        """角色模板里 ``{index_rules}`` 的展开文本：_INDEX.md 只做导航的写法约束。

        单一来源：下游（依赖提示词、评审）只读索引的清单部分，各角色不各写一份。
        没有配置索引文件的角色展开为空串。
        """
        if not self.index_file:
            return ""
        idx = str(self.index_file)
        return (
            f"下游会先读 {idx}，再按需读取相关文件，避免无差别全读。{idx} 只做导航，"
            f"且下游只读它的清单部分，因此：\n"
            f"- 只写文件清单（每个文件一行：路径 — 一句话用途），主文档标明必读、附件标明按需；\n"
            f"- 版本沿革、变更记录、覆盖自检、验证读数等过程性叙述不要写进 {idx}，"
            f"也不要塞进主文档（下游会全读主文档）；确需留存就写进单独文件（如 CHANGELOG.md），清单里用一行指向它；\n"
            f"- 对下游有影响的决策、限制、未核实事项只在主文档里写一次，{idx} 里不重复；\n"
            f"- {idx} 随产出更新：文件有增删时改清单，只保留现状，不累积历史。"
        )

    def _render_placeholders(self, template: str) -> str:
        """替换 prompt 模板里的产物路径占位符。

        用 ``str.replace`` 而非 ``str.format``：模板里带 JSON 花括号，format 会
        把示例当成字段名并抛 KeyError。生成指令与输出契约共用同一张替换表，
        避免两处对 ``{deliverable_dir}`` 的取值口径各自漂移。
        """
        replacements = {
            # V3-2.1 shared_target：{deliverable_dir}→分区；{output_file}→""（杜绝分区下再套 output）
            "{output_file}": "" if self.shared_target else (
                str(self.output_file) if self.output_file is not None else ""
            ),
            "{index_rules}": self._index_rules(),
            "{main_doc}": str(self.main_doc) if self.main_doc else "",
            "{index_file}": str(self.index_file) if self.index_file else "",
            "{final_file}": str(self.final_file) if self.final_file else "",
            "{deliverable_dir}": str(self.shared_target) if self.shared_target else (
                str(self.deliverable_dir) if self.deliverable_dir else "."
            ),
        }
        for placeholder, value in replacements.items():
            template = template.replace(placeholder, value)
        return template

    def _format_output_instruction(self) -> str:
        """Return the formatted per-request output contract."""
        return self._render_placeholders(self._output_format_instruction)

    def _build_generation_prompt(
        self,
        question: str,
        incremental: bool = False,
        cc: Optional[ClaudeCodeAgent] = None,
    ) -> str:
        """Build a first-turn message or a resumed generation message."""
        # A normal ask() must also become incremental after this CC has a
        # conversation.  ``incremental=True`` remains an explicit override for
        # Manager Revise, while ``cc`` supplies the conversation state.
        is_continuation = incremental or bool(getattr(cc, "conversation_id", None))
        if is_continuation:
            output_contract = self._format_output_instruction()
            return (
                question if not output_contract else
                f"{question}\n\n{output_contract}"
            )
        instruction = self._format_instruction()
        return f"{instruction}\n\n问题: {question}" if instruction else question

    def ask_incremental(self, question: str) -> dict:
        """Continue an existing AICU conversation with only new requirements."""
        return self._ask_solo(question, incremental=True)

    def _format_instruction(self) -> str:
        """安全格式化 generate_instruction 模板（替换细节见 ``_render_placeholders``）。"""
        return self._render_placeholders(self.generate_instruction)

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

    def ask(self, question: str) -> dict:
        return self._ask_solo(question, incremental=False)

    def _ask_solo(self, question: str, incremental: bool = False) -> dict:
        """单 CC 独立完成：一次生成即交付，无审查 / 修订 / 融合 / 审计。"""
        print(f"\n{'='*60}")
        print(f"[AICU] 收到问题: {question[:80]}...")
        print(f"{'='*60}")

        cc = self.ccs[0]
        response = self._cc_chat_with_retry(
            cc, self._build_generation_prompt(question, incremental, cc), timeout=7200
        )
        # 保存 CC 响应到工作区文件，供 _read_cc_file / _finalize_result 读取
        cc_output_path = cc.workspace / "cc_output.md"
        cc_output_path.write_text(response, encoding='utf-8')
        if self.is_modular_output:
            files = self._list_code_files(0)
            print(f"  产出: {len(files)} 个文件")
        else:
            print(f"  产出: {len(self._read_cc_file(0))} 字符")

        return self._finalize_result(question)

    def _finalize_result(self, question: str) -> dict:
        """汇总结果并保存。"""
        # 保存最佳方案摘要
        if self.is_modular_output:
            # 跳过二进制文件，文本文件用 errors='replace' 防止 UnicodeDecodeError
            text_parts = []
            for f in self._list_code_files(0):
                try:
                    text = self._read_text_file(f)
                except OSError:
                    continue  # 跳过无法读取的文件
                if text is not None:
                    text_parts.append(text)
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
            "role_config": {
                "output_file": self.output_file,
                "main_doc": self.main_doc,
                "index_file": self.index_file,
            },
        }
        self._save_to_workspace("aicu_result.json",
            json.dumps(result, ensure_ascii=False, indent=2, default=str)
        )

        print(f"\n{'='*60}")
        print(f"[AICU] 最终产出: {final_dir}")
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
            if any(part in self.NONCODE_DIRS for part in f.relative_to(final_dir).parts):
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
