"""多终端 Agent CLI 适配层。

把 Claude Code 协议与其它终端 Agent CLI（codex / kimi / qwen /
antigravity / grok）的协议差异抽象为 CliAdapter 策略，供
claude_code_agent.ClaudeCodeAgent 编排逻辑调用。

每个 adapter 负责以下协议细节：
  - 探测：is_installed()（shutil.which）
  - 建命令：build_base_command() / print_flags() / resume_flag() / max_turns_flag()
  - 传 prompt：prompt_args()（返回 (追加参数, 是否走 stdin)）
  - 建环境变量：build_env()
  - 解析：format_stream_event() / parse_result()
    （parse_result 结果统一归一化为 {result, session_id, subtype, errors}）
  - MCP 下发：mcp_flags() / mcp_workspace_files() / build_env()，四个钩子里选用得上的
    （见 CliAdapter.supports_mcp 那一段的说明）

新增一个 CLI 只需新增一个 adapter（或一条 GenericExecAdapter spec），
不触碰 ClaudeCodeAgent 的编排逻辑（子进程/流式/超时/续接/重试）。

各 CLI 的已知模型清单（供前端下拉候选）集中维护在 cli_models.json。

flags 依据（截至 2026-08；MCP 那一侧的核实情况另见各 adapter 的 docstring）：
  - claude：Claude Code 协议
  - kimi：官方命令参考 https://moonshotai.github.io/kimi-cli/en/reference/kimi-command
  - qwen：官方文档 https://qwenlm.github.io/qwen-code-docs（headless 页）已核实
  - opencode / pi / codebuddy / gemini / cursor：官方文档已核实（见各 adapter docstring）
  - codex：最佳猜测（未实测）
  - antigravity：登录被 eligibility 拒（账号/地区级），已从下拉隐藏（见 AntigravityAdapter）
  - grok：headless 那套已对 1.0.5 逐条核实（``-p`` / ``--output-format`` / 输出形状，
    见 GrokAdapter）；MCP 的实现与 CLI 侧（配置认不认、握手通不通）也核实过，但**模型侧
    没验过**（本机没有 XAI_API_KEY），所以 ``supports_mcp`` 当前是 False —— 以 GrokAdapter
    的 docstring 为准，别照这一行判断
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from runtime.credentials import (
    api_key_env_for_vendor,
    vendor_of_model,
)
# 只取这一个常量：传输的名字由 mcp_manager 定义，adapter 这边比较时用同一个名字，
# 免得两处各写一份 "http" 字面量。
from runtime.mcp_manager import TRANSPORT_HTTP

# 这个模块只留 adapter 策略本身：厂商/端点在 cli_providers，输出解析在 cli_results，
# MCP 配置落盘在 mcp_codecs。下面只导入本文件真正用到的名字 —— 其余调用方直接从
# 拥有它的模块导入，避免再造一层「看起来在这、其实在别处」的再导出。
from cli_providers import (
    KNOWN_MODELS,
    _ANTHROPIC_AUTH_TOKEN_ENV,
    _CLI_NATIVE,
    _CUSTOM_PROVIDER_ID,
    _opencode_config_content,
    _provision_codex_config,
    _provision_pi_config,
    _provision_qwen_config,
    _release_codex_config,
    _release_pi_config,
    _release_qwen_config,
    list_provider_presets,
    model_supported_by_cli,
)
from cli_results import (
    _claude_style_result,
    _coerce_errors,
    _extract_event_text,
    _extract_session_id,
    _format_json_event,
    _render_openai_content,
    _result_events,
)
from mcp_codecs import (
    _cc_mcp_json,
    _grok_mcp_toml,
    _mcp_capability,
    _mcp_servers_project_json,
    _mcp_servers_settings_json,
    _probe_pi_mcp,
    _write_kimi_trust,
    _write_pi_trust,
)


class CliAdapter:
    """单个终端 Agent CLI 的协议抽象基类。"""

    id: str = ""
    display_name: str = ""
    command: str = ""

    @property
    def known_models(self) -> list[str]:
        """已知模型清单（供前端下拉候选）。

        对 claude / codex / kimi 过滤掉「该 CLI 的协议没有对应原生端点」的模型
        （如 claude 选不了 gemini），让下拉里出现的都是真能连上的。
        """
        return [
            model
            for model in KNOWN_MODELS.get("all", [])
            if model_supported_by_cli(self.id, model)
        ]

    # 可选同名冲突甄别：(探测 argv, 必须含子串, 必须排除子串)。None 表示只用 which。
    # 命令名可能被无关工具占用时，设此属性以甄别。
    install_probe: Optional[tuple[list[str], list[str], list[str]]] = None

    def is_installed(self) -> bool:
        if shutil.which(self.command) is None:
            return False
        if self.install_probe is None:
            return True
        argv, must_contain, must_not_contain = self.install_probe
        try:
            result = subprocess.run(
                [self.command, *argv], capture_output=True, text=True, timeout=5,
            )
            output = f"{result.stdout or ''} {result.stderr or ''}".lower()
        except (OSError, subprocess.TimeoutExpired):
            return True  # 无法探测时保守放行，交由运行时报错
        return (
            any(marker in output for marker in must_contain)
            and not any(marker in output for marker in must_not_contain)
        )

    # ---- 命令构建 ----

    def model_arg(self, model: Optional[str], *, api_base_url: Optional[str]) -> Optional[str]:
        """交给 CLI 的 ``--model`` 取值（默认即 AICU 的模型名）。

        有的 CLI 对模型名有自己的一套写法：opencode 与 pi 要求 ``<provider>/<model>``，
        而第三方端点是由 AICU 以 ``aicu`` 这个 provider id 注册进去的，所以走改道时
        必须补上前缀。取值为 None 表示不加 ``--model``。

        ``api_base_url`` 非空 = 本次确实指向了第三方原生端点（见 native_base_url），
        只有这时前缀才成立 —— 没改道时 ``aicu`` 这个 provider 根本不存在。
        """
        return model

    def build_base_command(
        self,
        *,
        model: Optional[str],
        skip_permissions: bool,
        enable_project_skills: bool,
        mcp_servers: Optional[list] = None,
    ) -> list:
        raise NotImplementedError

    def print_flags(self, streaming: bool) -> list:
        raise NotImplementedError

    def resume_flag(self, session_id: str) -> list:
        return []

    def max_turns_flag(self, n: int) -> list:
        # 默认不加：codex / kimi / opencode / pi / gemini / cursor 的文档都没有
        # max-turns 类轮次 flag（kimi 的 --max-steps-per-turn 语义不同，不能混用）。
        return []

    def prompt_args(self, prompt: str) -> tuple[list, bool]:
        """返回 (追加到 cmd 的 prompt 参数, 是否仍需通过 stdin 传 prompt)。默认位置参数。"""
        return ([prompt], False)

    # ---- MCP ----

    # 本 CLI 能不能接收 AICU 下发的 MCP server 配置。
    #   False（默认）—— **AICU 还没为这个 CLI 接入 MCP**。注意这不等于「该 CLI 不支持
    #     MCP」：agy 自己就有 MCP，只是它只认全局配置文件、注入不了，所以至今未接线
    #     （实测过程见 AntigravityAdapter）。两者混为一谈会让用户在界面和报错里读到
    #     一句关于 CLI 的假话，然后跑去换 CLI。
    #     角色如果声明了 mcp 而落到这类 CLI 上，AICU 会直接报错而不是静默忽略 ——
    #     skill 曾经吃过这个亏（软链对所有 CLI 照建，非 claude 系上是死文件，建了但
    #     永远不生效），现已按 CLI 分开接线，见下面的 skills_workspace_dir。
    #   子类可以把它改成 @property：pi 的答案是「看本机装没装那个扩展」，没法用一个
    #     类属性写死（见 _mcp_capability）。
    supports_mcp = False

    # 项目级 MCP 配置文件的路径，**相对 CC 的 workspace 逐段给出**。
    #   None（默认）—— 这个 CLI 不走「落盘项目文件」这条路。
    #   (".gemini", "settings.json") 这样 —— 基类的 mcp_workspace_files 照此写盘。
    # 只描述路径；内容由 _mcp_workspace_payload 决定。需要往一处写**多个**文件、
    # 或路径不由 workspace 直接决定的 CLI，自己覆盖 mcp_workspace_files 即可。
    mcp_workspace_config: Optional[tuple] = None

    # 落盘配置里「远程地址」那个键叫什么。默认 ``url``。
    #
    # 参数化的唯一原因是 gemini/qwen 这一系：这两家**没有 type 字段，传输由键名决定**
    # —— ``url`` 表示 SSE（已被 Streamable HTTP 取代的旧传输），``httpUrl`` 才是
    # Streamable HTTP。写成 ``url`` 不报错，只静默连到一条我们并不支持的传输上。
    # 只有 GeminiAdapter 覆盖它（qwen 连路径容器都不一样、自己整个覆盖了函数）。
    # 与 ``mcp_workspace_config`` 是同一个手法：差异收敛成一个类属性，而不是抄一份。
    mcp_url_key = "url"

    # 项目级 skills 目录，**相对 CC 的 workspace 逐段给出**。
    #   None（默认）—— 这个 CLI 拿不到 AICU 下发的 skill。注意这不等于「该 CLI 不支持
    #     skill」：codex / opencode / agy 各家都有自己的项目级 skill 机制，只是入口各不
    #     相同、AICU 还没接线。两者混为一谈会让用户在日志里读到一句关于 CLI 的假话。
    #   (".qwen", "skills") 这样 —— 基类的 skills_workspace_files 照此建链接。
    #
    # 这里**只用一个属性**，不像 MCP 那样「能力布尔 + 路径」两个。因为 MCP 那两个是
    # 正交的（pi 的 supports_mcp 是运行时探测：路径写死了，本机装没装那个扩展要看
    # 子进程的输出），而 skills 没有这个维度 —— 「认不认项目级目录」是每家一个常量，
    # 不随本机装的东西变。路径的有无**就是**能力本身；再加一个 supports_skills 只能是
    # 它的派生副本，而派生量迟早对不上，那正是 AICU 反复要消灭的 bug 类。
    #
    # 真正消灭「声明了却没实现」的是测试不变量（TestSkillsSupportFlag），不是字段个数。
    skills_workspace_dir: Optional[tuple] = None

    def mcp_flags(self, mcp_servers: list) -> list:
        """argv 形式的 MCP 注入，追加在命令末尾（默认不注入，返回空）。

        传的是**已解析好的 McpServer 列表**而不是配置串：每个节点各拼一份内联传给
        自己的 CLI 子进程，不落盘、不入库，天然按节点隔离，也不会覆盖用户自己的
        MCP 配置。序列化由各 adapter 自己做 —— 三家的线上形态根本不一样
        （claude 系是 ``--mcp-config=<json>``，opencode 走环境变量，codex 是重复的 ``-c``）。

        位置有讲究：这里返回的参数会被追加到 ``build_base_command`` + ``print_flags``
        之后。claude 系全是 flag，放末尾无所谓；codex 的 ``-c`` 是全局 flag、必须排在
        ``exec`` 子命令**之前**，所以它不用这个钩子，改在 ``build_base_command`` 里注入。
        """
        return []

    def mcp_workspace_files(self, workspace, mcp_servers: Optional[list]) -> list:
        """把 MCP 配置写成「工作目录下的项目级文件」，返回落盘的文件路径（默认不落盘）。

        前两个钩子（``mcp_flags`` 走 argv、``build_env`` 走环境变量）都能做到不碰盘，
        但有的 CLI 两样都不吃：qwen 只认 ``<cwd>/.qwen/settings.json`` 这个项目级配置
        文件，没有任何 argv/env 口子。这条路避不开落盘。

        落盘的代价要用生命周期补回来：**返回的路径由调用方在 CLI 进程结束后删除**。
        留着会被 AICU 的产物收集当成交付物 —— ``.qwen`` 不在 ``NONCODE_DIRS`` 里，
        ``.json`` 也不在 ``NONCODE_EXTENSIONS`` 里，会被当成 CC 写的代码文件复制进
        最优方案目录、并出现在 ``_INDEX.md`` 的文件清单里。文件名与内容是 adapter 的
        事，何时写、何时删是调用方的事（见 claude_code_agent.py 的 ``_drop_transient``）。

        也考虑过把这些目录加进 ``NONCODE_DIRS`` 再加一道防线，**结论是不加**：
        收集发生在同一个进程、``chat()`` 返回之后，而 ``_drop_transient`` 挂在
        ``finally`` 上 —— 能让它不执行的场景（AICU 自己被 SIGKILL）同样不会走到收集，
        这道防线挡不住任何真实情况。反过来，往 ``NONCODE_DIRS`` 里加 ``.cursor`` /
        ``.gemini`` 会顺手改变「用户项目里恰好有同名目录时收不收」这个已有行为。

        之所以能按节点隔离：每个 CC 有独立 workspace（``workspace/cc_<i>``），而 CLI
        子进程的 cwd 就是它，所以项目级文件天然一 CC 一份，不会互相覆盖，也不碰用户
        自己的全局配置。

        实现：声明了 ``mcp_workspace_config`` 的 adapter 走这版通用实现（写一份
        ``_mcp_workspace_payload``，路径按那个元组拼）。gemini / cursor / pi / kimi
        四家的落盘内容同形，差异只有路径 —— 与其抄四遍，不如把差异收敛成一个类属性。
        grok 走这条路但换 TOML（覆盖 payload），qwen 连路径容器都不一样、自己整个覆盖。
        没声明的一律不落盘。
        """
        # ``supports_mcp`` 这一条不能省：像 kimi 那样「配置路径写死、但本机装的那个
        # 版本到底认不认」要由能力门禁决定的 CLI，少了它就会在未声明支持的情况下偷偷
        # 落盘 —— TestMcpSupportFlag.test_undeclared_support_never_injects 正是守这个。
        if not mcp_servers or not self.mcp_workspace_config or not self.supports_mcp:
            return []
        path = Path(workspace).joinpath(*self.mcp_workspace_config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._mcp_workspace_payload(mcp_servers), encoding="utf-8")
        return [path]

    def _mcp_workspace_payload(self, mcp_servers: list) -> str:
        """写进 ``mcp_workspace_config`` 那个文件的字节内容。

        默认 = 四家共用的 ``mcpServers`` JSON。grok 覆盖它返回 TOML（见 GrokAdapter）。
        远程地址的键名经 ``mcp_url_key`` 传下去 —— gemini 要 ``httpUrl``。
        """
        return _mcp_servers_project_json(mcp_servers, url_key=self.mcp_url_key)

    def mcp_unwired_reason(self) -> str:
        """``supports_mcp`` 为假时，一个能直接嵌进 ``"CLI 'x' 的 MCP ___，但……"``
        这句话里的**原因短语**。

        默认是「尚未接入 AICU」—— 绝大多数情况下这是实话。留着这个钩子是因为「为什么没接」
        只有 adapter 知道：有的 CLI 自己就有 MCP，只是注入方式对不上，那是关于 CLI 的事实，
        守卫里写死一句会把它说错，还会把用户指向一个他做不了的动作。

        当前覆盖它的是 pi：它的门禁问的是本机装没装 ``pi-mcp-adapter`` 扩展，为假时该做的
        动作是装扩展 —— 沿用默认句会把一个三十秒能自己解决的事说成「等我们接」。kimi 也
        覆盖过，成因是本机版本没有 MCP 入口；那条 2026-09-18 已证实是误判，kimi 现在
        ``supports_mcp`` 恒真、不再走这个分支。

        测试仍守着「守卫问的是 adapter 而不是写死一句」这条契约 —— 样本换来换去，
        契约没变，用例该守的是契约。
        """
        return "尚未接入 AICU"

    # ---- 环境变量 ----

    # ---- 项目级 skill ----

    def skills_workspace_files(self, workspace, skills_dir, skill_names: list) -> list:
        """把 ``skill_names`` 链接进 ``<workspace>/<skills_workspace_dir>/``，返回落盘路径。

        ``skills_workspace_dir`` 为 None 时返回空（= 这个 CLI 拿不到 AICU 下发的 skill，
        调用方据此告警）。默认实现建**软链**：skill 是多文件目录（含脚本与引用），拷一份
        不值当；pi 的 ``canonicalizePath`` 明确解软链查重，说明官方就是按链接设计的。

        覆盖点是留给 opencode 那种「塞目录不够」的 CLI —— 它的项目级 skill 要走配置键
        ``skills.paths``，光有目录是假接线。与 ``mcp_workspace_files`` 同一手法。

        与 ``mcp_workspace_files`` 的**生命周期不同**：MCP 的文件是每次运行前写、跑完就删
        （``_drop_transient``）；skill 是 AICU 构造期建一次、长期留存，所以调用方是
        ``AICU._install_skills_to_cc`` 而不是 ``ClaudeCodeAgent.chat``。
        """
        if not skills_dir or not skill_names or not self.skills_workspace_dir:
            return []
        target_dir = Path(workspace).joinpath(*self.skills_workspace_dir)
        # 幂等：清空重建，与 AICU 一直以来的语义一致（重复构造同一个 workspace 不叠加）
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for name in skill_names:
            dst = target_dir / name
            # 用软链，避免拷贝整份 skill 文件；CLI 会跟随链接读取 SKILL.md
            dst.symlink_to((Path(skills_dir) / name).resolve())
            written.append(dst)
        return written

    def skills_workspace_trust(self, workspace) -> Optional[Path]:
        """落盘 skill 之外，这个 CLI 还要不要一份**项目信任记录**？默认不要。

        与 ``mcp_workspace_files`` 的「顺带补一条信任记录」是同一件事，但这里的默认是
        「不要」而不是「要」：目前只有 pi 有这道门禁，其余各家的项目级目录是先信任后读
        （kimi 的信任门禁只卡 MCP，不卡 skill —— 2026-09-20 去掉信任记录后 A/B 仍然 PASS）。

        返回「本次新写出来的那条记录」（或 None）。**与 MCP 那条的差别在生命周期**：
        MCP 的信任记录跟着一次性配置一起被 ``_drop_transient`` 删掉；skill 是长期留存的，
        所以这条记录也留着 —— 删了下次运行还得再写一遍，而它必须**在 CLI 启动前**就在。
        """
        return None

    def skills_unwired_reason(self) -> str:
        """``skills_workspace_dir`` 为 None 时，一个能直接嵌进
        ``"CLI 'x' 的 project-level skill ___"`` 这句话里的**原因短语**。

        默认是「尚未接入 AICU」—— 绝大多数情况下这是实话。与 ``mcp_unwired_reason``
        同一条纪律：说「AICU 还没接」，不说「该 CLI 不支持」。后者是关于 CLI 的假话
        （各家都有自己的项目级 skill 机制），用户读了会跑去换 CLI。
        """
        return "尚未接入 AICU"

    def _inject_api_key(
        self,
        env: dict,
        key_env: Optional[str],
        target_env: Optional[str] = None,
    ) -> None:
        """把 ``key_env`` 指向的密钥值写进子进程环境。

        ``api_key_env`` 是「AICU 从哪个变量取 key」，CLI 真正读的变量名可能不同
        （例如 codex 固定读 ``OPENAI_API_KEY``），所以目标变量单独传。缺失时
        什么都不写 —— 保持 CLI 自己的登录态，而不是塞一个空字符串把它顶掉。
        """
        if not key_env:
            return
        api_key = os.environ.get(key_env)
        if api_key:
            env[target_env or self.api_key_env] = api_key

    def _env_with_api_key(self, base_env: dict, key_env: Optional[str]) -> dict:
        """复制一份环境并注入本次 API key。

        给「只换 key、不改端点」的 adapter 共用：qwen 的端点由 settings.json 决定，
        gemini 直连官方、没有统一的 base URL 变量 —— 两边都不需要通过环境变量改道，
        但都要把本次的 key 交给 CLI。
        """
        env = dict(base_env)
        self._inject_api_key(env, key_env or self.api_key_env)
        return env

    def build_env(
        self,
        base_env: dict,
        *,
        api_base_url: Optional[str],
        api_key_env: Optional[str],
        model: Optional[str] = None,
        mcp_servers: Optional[list] = None,
    ) -> dict:
        """给子进程的环境变量（默认原样传下去）。

        ``model`` 是本次运行的模型名：绝大多数 CLI 用命令行参数拿模型，不需要它；
        但 kimi 只有一个环境变量通道（``KIMI_MODEL_NAME``），模型名必须和环境一起给
        （见 KimiAdapter.build_env）。

        ``mcp_servers`` 是本次运行要下发的 MCP server（``list[McpServer]``，已解析）。
        只有走环境变量传配置的 CLI 需要它 —— 目前是 opencode，它把 provider 与 mcp
        放在**同一个** ``OPENCODE_CONFIG_CONTENT`` JSON 里，所以两者必须在同一处拼
        （见 OpenCodeAdapter.build_env）。
        """
        return base_env

    def api_key_env_for_model(self, model: Optional[str]) -> Optional[str]:
        """注入 sk 时使用的认证环境变量名（默认该 CLI 的 ``api_key_env``）。

        多厂商 CLI（如 opencode）按模型厂商覆盖，返回对应厂商的标准环境变量名。
        """
        return getattr(self, "api_key_env", None)

    # ---- 第三方模型供给（provision） ----

    # 本 CLI 的第三方端点是否落在「AICU 托管的配置文件槽」里（见 provider_slot_base_url）。
    #   False（默认）—— 端点走环境变量（claude / kimi / codebuddy）或每次运行现拼的内联
    #     配置（opencode），盘上不留 AICU 的痕迹，也就没有「上次的旧值」可言。
    #   True —— 端点和 key 变量名写在 CLI 自己的配置文件里（codex / qwen / pi）。
    #     这类适配器必须覆盖 provision，且**每次运行都要被调用**、不允许在入参为空时
    #     早退：入参为空恰恰是「撤掉自己上次写的」的信号。这条对应关系有测试守着
    #     （test_managed_slot_flag_matches_provision_override）。
    managed_slot = False

    def provision(
        self, *, api_base_url: Optional[str], api_key_env: Optional[str], model: Optional[str]
    ) -> None:
        """把本次运行该由 AICU 供的端点写进该 CLI 的配置（默认 no-op）。

        ``api_base_url`` 取 ``provider_slot_base_url(...)`` 的返回值，也就是「AICU 托管槽
        本次该持有的端点」；**为 None 表示撤掉自己上次写进配置的内容**，而不是「什么都不做」。

        覆盖此方法的是有托管槽的 CLI（codex：``~/.codex/config.toml``；qwen：
        ``~/.qwen/settings.json``；pi：``~/.pi/agent/models.json``），它们靠预写配置让
        运行时的 ``--model <id>`` 能命中；其余 CLI 走环境变量或内联配置，无需写盘。
        """
        return None

    # ---- 解析 ----

    def format_stream_event(self, line: str) -> str:
        return line

    def parse_result(self, raw: str) -> Optional[dict]:
        return None




class ClaudeCodeAdapter(CliAdapter):
    """Claude Code CLI 协议适配器。"""

    api_key_env = "ANTHROPIC_API_KEY"

    def __init__(self, cli_id: str, display_name: str, command: str):
        self.id = cli_id
        self.display_name = display_name
        self.command = command

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool,
        mcp_servers: Optional[list] = None,
    ) -> list:
        cmd = [self.command]
        if model:
            cmd.extend(["--model", model])
        if skip_permissions:
            # root 用户下 --dangerously-skip-permissions 被禁止，
            # 用 --allowedTools 只开放必要的文件操作工具
            allowed_tools = ["Read", "Write", "Edit", "Bash"]
            # 声明了 MCP server 时必须把它的工具一并写进白名单。--allowedTools 是
            # 白名单而非「先问再放行」：dontAsk 模式下不在名单里的工具一律直接拒绝。
            # 实测漏掉这一步的表现是 permission_denials 里出现 mcp__<server>__<tool>、
            # agent 拿不到结果后绕路或报一个与 MCP 毫无关系的错 —— 正是预检要消除的那类
            # 失败（见 runtime/mcp_client.py 的模块说明）。
            # `mcp__<server>` 是 server 级通配，一条即放行该 server 的全部工具，不必逐工具枚举。
            # 名字取自同一个 server 列表（与 mcp_flags 下发的配置同源），不可能对不上。
            allowed_tools.extend(f"mcp__{server.name}" for server in (mcp_servers or []))
            cmd.extend(self.allowed_tools_flag(allowed_tools))
            cmd.extend(["--permission-mode", "dontAsk"])
        if enable_project_skills:
            # 限定只加载项目级 skill —— 落到哪儿由子类的 skills_workspace_dir 决定
            # （codebuddy 虽共用本类，目录是 .codebuddy/skills；argv 这一段它照样要）
            cmd.extend(["--setting-sources", "project"])
        return cmd

    def allowed_tools_flag(self, tool_names: list) -> list:
        """把工具名列表拼成 ``--allowedTools`` 那一段 argv。

        默认「空格连接成单个参数」。这是 claude CLI 自己文档里的形态：``--allowedTools
        <tools...>`` 写着 "Comma or space-separated list of tool names to allow"，
        实测也确实认（AICU 的 claude 节点一直这么跑，产物正常）。

        **CodeBuddyAdapter 覆盖了它**，因为 codebuddy 虽号称 Claude Code 协议克隆，
        这一处解析却不同：它不拆串，整串会被当成**一个**工具名。后果不是「MCP 用不了」
        而是「白名单整个失效」—— 名字对不上任何工具，于是 Read / Write / Edit / Bash
        在 dontAsk 下被逐条拒绝，与有没有声明 MCP 无关（两组对照都实测过）。
        改法与证据见 CodeBuddyAdapter.allowed_tools_flag。
        """
        return ["--allowedTools", " ".join(tool_names)]

    def print_flags(self, streaming: bool) -> list:
        if streaming:
            return ["--print", "--verbose", "--output-format", "stream-json"]
        return ["--print", "--output-format", "json"]

    def resume_flag(self, session_id: str) -> list:
        return ["--resume", session_id]

    def max_turns_flag(self, n: int) -> list:
        return ["--max-turns", str(n)]

    def prompt_args(self, prompt: str) -> tuple[list, bool]:
        # 通过 stdin 传 prompt，避免深层递归上下文快照超过 ARG_MAX
        return ([], True)

    supports_mcp = True

    # claude 的项目级 skill 目录（**codebuddy 覆盖成 ``.codebuddy/skills``**，
    # 见 CodeBuddyAdapter —— 两个 CLI 共用这个类，目录不同）。
    #
    # 这一族也是唯一走 argv 通道的：目录 + ``--setting-sources project``
    # （见 build_base_command）。其余 CLI 都只走目录，因为它们的 argv 有逐字节断言
    # 盯着，不许加 flag。
    skills_workspace_dir = (".claude", "skills")

    def mcp_flags(self, mcp_servers: list) -> list:
        if not mcp_servers:
            return []
        # 必须用 `--flag=<json>` 的等号形式：`--mcp-config` 声明为可变参数
        # (`<configs...>`)，用空格形式会把后面追加的 `--resume` / prompt 一起吞掉，
        # 当成配置文件路径去读（实测报 "MCP config file not found: .../mcp"）。
        # 等号形式只取一个值，不消费后续 argv，因此放在命令的哪个位置都安全。
        #
        # --strict-mcp-config：只认这份内联配置，忽略用户自己的 MCP 设置。节点声明的
        # mcp 列表就是它该看到的那一份，结果可预期 —— 与 skill 用 --setting-sources
        # project 把用户级 skill 挡在外面是同一个取舍。
        return [f"--mcp-config={_cc_mcp_json(mcp_servers)}", "--strict-mcp-config"]

    def build_env(
        self,
        base_env: dict,
        *,
        api_base_url: Optional[str],
        api_key_env: Optional[str],
        model: Optional[str] = None,
        mcp_servers: Optional[list] = None,
    ) -> dict:
        env = dict(base_env)
        if api_base_url:
            env["ANTHROPIC_BASE_URL"] = api_base_url
        # 认证值由 _build_process_env 按 api_key_env 注入（sk 不在这一层）。
        # 这里只负责一件事：改道到第三方端点时清掉父环境里可能残留的 Anthropic key ——
        # 留着它会被 CLI 一起读走，等于把一个 Anthropic 的 key 发到别家端点。
        if api_base_url and api_key_env == _ANTHROPIC_AUTH_TOKEN_ENV:
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    def api_key_env_for_model(self, model: Optional[str]) -> Optional[str]:
        """claude CLI 的认证变量名：同族用 ``ANTHROPIC_API_KEY``，跨厂商改道改用
        ``ANTHROPIC_AUTH_TOKEN``。

        实测（deepseek 的 /anthropic 端点）：只给 ``ANTHROPIC_API_KEY`` 时 claude CLI
        直接判成「Not logged in · Please run /login」并以 rc=1 退出，连请求都没发；
        换成 ``ANTHROPIC_AUTH_TOKEN`` 即通。这与 deepseek / 智谱 / 阿里云文档里一律
        配 ``ANTHROPIC_AUTH_TOKEN`` 一致 —— 第三方端点认的是 Authorization: Bearer。

        同族（Anthropic 自己的模型）保持原变量名，本机既有登录态行为不变。

        只对 claude 本身生效：CodeBuddyAdapter 继承了本类，但它有自己的认证体系
        （CODEBUDDY_API_KEY，实测可直连第三方端点），不做这个 Bearer 替换。
        """
        if self.id != "claude":
            return self.api_key_env
        vendor = vendor_of_model(model or "")
        if not vendor or vendor == _CLI_NATIVE["claude"][0]:
            return self.api_key_env
        return _ANTHROPIC_AUTH_TOKEN_ENV

    def format_stream_event(self, line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
        if not isinstance(event, dict):
            return ""
        event_type = event.get("type")
        if event_type == "assistant":
            content = event.get("message", {}).get("content", [])
            parts = []
            for item in content if isinstance(content, list) else []:
                if item.get("type") == "text" and item.get("text"):
                    parts.append(item["text"])
                elif item.get("type") == "tool_use":
                    parts.append(f"Tool: {item.get('name', 'unknown')}")
            return "\n".join(parts)
        if event_type == "result":
            return str(event.get("result") or "")
        if event_type == "tool_result":
            return str(event.get("content") or "")
        return ""

    def parse_result(self, raw: str) -> Optional[dict]:
        return _claude_style_result(raw)


class CodeBuddyAdapter(ClaudeCodeAdapter):
    """CodeBuddy Code CLI（``codebuddy``，@tencent-ai/codebuddy-code）。

    腾讯的 Claude Code 协议克隆，flags 与 Claude Code 几乎一致（--print /
    --output-format text|json|stream-json / --max-turns / --resume /
    --allowedTools / --setting-sources 等），复用 ClaudeCodeAdapter 的协议实现，
    仅覆盖认证环境变量。

    认证 env（官方文档核实）：
      - CODEBUDDY_API_KEY：API 密钥，用于模型接口调用，非交互模式 (-p) 下始终使用
      - CODEBUDDY_AUTH_TOKEN：平台认证令牌，用于所有平台接口调用

    **MCP 能跑通**（2026-09-18 端到端实测，判据是探针 server 侧的旁路调用日志 ——
    只有 server 进程写得进去，模型伪造不了）。走到这一步需要两处 codebuddy 专属适配，
    都在本类里，父类与其它适配器一个字都没动：

      1. ``build_env`` 注入 ``CODEBUDDY_DEFER_TOOL_LOADING=0``（仅在声明了 MCP 时）。
         codebuddy 默认开着 MCP 工具的「延迟加载」：schema 不进上下文，模型要用得先
         ToolSearch、再经它自己的 ``DeferExecuteTool`` 按名字转发。审批发生在**那层壳**
         上，而父类下发的 ``mcp__<server>`` 是 server 级通配、覆盖不到壳 —— 非交互模式
         下没有审批提示可用，于是必被拒。关掉延迟加载后工具以真名直接注册，父类的通配
         就够了。**一个权限都没放宽**，也没写用户 settings。
      2. ``allowed_tools_flag`` 覆盖成「一个名字一个 argv 项」。

    ★ 第 2 条与本适配器先前那版结论有关，必须说明白。旧版这里写的是「MCP 默认走不通、
    除非用户改 settings」，理由有三条，其中两条**站不住，已撤回**：

      - 「把 ``DeferExecuteTool`` 补进 ``--allowedTools`` 无效」—— 那是在**整串**形态下
        测的，而整串在 codebuddy 里根本没被解析成名单，补什么都无效。这条不是关于
        ``DeferExecuteTool`` 的证据。
      - 「中途那次『产物对』是通过伪造的」—— 那次用的是 ``--allowedTools "$@"``，
        shell 展开成的是**独立 argv 项**；后来推翻它的那次用的是**整串**。两者不是同一个
        形状，属于混淆对照。该说法一并撤回，真相不明。

    仍然成立的是：在父类原来的 argv 形态下，``tools/call`` 确实一次都没发生（旁路日志
    实测）。所以「旧形态跑不通」是对的，**错的是原因**。

    关于 ``--allowedTools`` 的形态：codebuddy 不拆串，整串会被当成**一个**工具名，
    于是白名单整个失效 —— 不只是 MCP，Read / Write / Edit / Bash 一起被拒。这与有没有
    声明 MCP **无关**，两组对照都跑过。也就是说这条修复对**每次** codebuddy 运行生效。
    claude 不受影响：它的文档写明认空格分隔的整串，真机节点用整串产物正常。

    综上，``supports_mcp = True``（继承自父类）现在名副其实 —— 链路是真通的，不是
    「理论上通、实际要用户先改配置」。

    仍然刻意不加的东西：``-y`` / ``--permission-mode bypassPermissions``。那会把审批
    整个关掉，让这个适配器变成比 claude 危险一档的形态，而上面的做法不需要它。
    """

    api_key_env = "CODEBUDDY_API_KEY"

    def __init__(self):
        super().__init__("codebuddy", "CodeBuddy Code", "codebuddy")

    # codebuddy 的项目级 skill 目录是 ``.codebuddy/skills``，**不是**父类的
    # ``.claude/skills`` —— 尽管它自称 Claude Code 协议克隆、也照收 ``--setting-sources``。
    #
    # 2026-09-20 真机 A/B 对照（同一份软链，只换目录；各 8 轮，因为**单轮的 FAIL 不可信**
    # —— 模型会时不时无视清单直接答 NONE，见探针模块说明）：
    #   ``.claude/skills``    → 8 轮全无金丝雀，且有几轮**列出了非空清单**（对照组在，
    #                           所以这是有效 FAIL，不是测量失效）
    #   ``.codebuddy/skills`` → 第 2 轮答出金丝雀，B 组没有 ⇒ PASS
    # 它自己的包里也把这一条钉死了：``.codebuddy/skills`` 出现 16 次、
    # ``.claude/skills`` **0 次**（``.claude/agents``/``.claude/commands`` 倒是有，
    # 那是兼容旧路径的读入，不是技能目录）。包内字面量是确定性证据，A/B 是佐证。
    #
    # 这一处正是「复用父类会静默失效」的样板：三个 CLI 共用 ClaudeCodeAdapter，
    # 目录却只有两个相同。所以它必须是子类的覆盖，而不是父类的默认值。
    skills_workspace_dir = (".codebuddy", "skills")

    def build_env(
        self,
        base_env: dict,
        *,
        api_base_url: Optional[str],
        api_key_env: Optional[str],
        model: Optional[str] = None,
        mcp_servers: Optional[list] = None,
    ) -> dict:
        env = dict(base_env)
        # 官方文档未列出 base URL env，但二进制里读 CODEBUDDY_BASE_URL 且原样使用
        # （不额外拼 /v2）；本机实测：指向 https://api.deepseek.com/v1 并配
        # CODEBUDDY_API_KEY 可正常出结果。
        if api_base_url:
            env["CODEBUDDY_BASE_URL"] = api_base_url
        key_env = api_key_env or self.api_key_env
        if key_env:
            api_key = os.environ.get(key_env)
            if api_key:
                env[self.api_key_env] = api_key
        if mcp_servers:
            # 关掉 MCP 工具的「延迟加载」。默认开着时 MCP 工具的 schema 不进上下文，
            # 模型要用得先 ToolSearch、再经 DeferExecuteTool 按名字转发 —— 而审批发生在
            # 那层壳上，非交互模式没有审批提示可用，于是调用必被拒（旁路调用日志 0 次）。
            # 关掉后工具以 mcp__<server>__<tool> 的真名直接注册，父类下发的
            # mcp__<server> 通配即可放行 —— **不需要放宽任何权限**，也不碰用户的 settings。
            #
            # 只在声明了 MCP server 时才设：不涉及 MCP 的运行保持 codebuddy 的默认行为，
            # 免得白付「所有工具 schema 都常驻上下文」那笔代价。
            # 2.137.1 的解析链（dist/codebuddy.js）：env 优先，其次 settings 的
            # deferToolLoading，再次 productFeatures.DeferToolLoading（默认 true）。
            env["CODEBUDDY_DEFER_TOOL_LOADING"] = "0"
        return env

    def allowed_tools_flag(self, tool_names: list) -> list:
        """覆盖父类的「空格连接成单个参数」形态 —— codebuddy 不拆串。

        codebuddy 虽号称 Claude Code 协议克隆，这一处解析不同：整串会被当成**一个**
        工具名。后果不是「MCP 用不了」，而是**白名单整个失效** —— 每个名字都对不上任何
        工具，dontAsk 下 Read / Write / Edit / Bash 被逐条拒绝。

        对照实测（唯一变量就是这一处，其余同模型、同端点、同 --mcp-config、同 dontAsk、
        同 skip_permissions=True）：

          - 整串 → ``Permission to use Write has been denied because this tool requires
            approval but permission prompts are not available in non-interactive
            mode``，Bash 同样被拒，产物为空；
          - 拆开 → Write 成功、产物落地，MCP 工具也照常被真调（旁路日志 1 次）。

        **与有没有声明 MCP 无关**：声明与不声明两组都跑过，结果一致。所以这条修复是对
        每次运行生效的，不是只修 MCP 那条路。

        claude 不需要这个覆盖：它的 ``--allowedTools`` 文档写明认 "comma or
        space-separated list"，且真机 claude 节点用整串形态产物正常。
        """
        return ["--allowedTools", *tool_names]


# codex exec --json 的事件信封：正文在 ``item`` 里，``item.type`` 说明这条是什么
# （agent_message / reasoning / command_execution / file_change / error …）。
# 只有 agent_message 是给用户看的回复；其余是过程事件，不能混进最终答案，
# 否则命令输出会被当成回复。
# 实测形态：{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"可用"}}
_CODEX_MESSAGE_ITEM_TYPES = ("agent_message", "assistant_message", "message")


def _codex_item_text(event: dict) -> str:
    """从 codex 的 item 信封里取回复文本；不是回复事件则返回空串。"""
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") not in _CODEX_MESSAGE_ITEM_TYPES:
        return ""
    for key in ("text", "content", "message"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list):
            rendered = _render_openai_content(value)
            if rendered:
                return rendered
    return ""


def _codex_mcp_args(servers: Optional[list]) -> list:
    """把 MCP server 转成 codex 的重复 ``-c key=value`` 参数。

    codex 没有 ``--mcp-config`` 这类「整份配置」的 flag，但有一个通用的配置覆盖通道
    ``-c <key=value>``，键路径用点号（与 ``~/.codex/config.toml`` 的节名一一对应）。
    这样注入的是**本次进程的命令行参数**，不写 config.toml、不碰用户自己的 MCP 配置、
    并发节点之间也不会互相覆盖 —— 与 claude 系内联 ``--mcp-config`` 是同一个取舍。

    值统一用 ``json.dumps`` 序列化：TOML 的字符串与数组字面量和 JSON 恰好同形
    （``"x"`` / ``["a","b"]``），而手写引号极易在含空格或引号的路径上翻车。
    本机实测 ``-c 'mcp_servers.x.args=["-c","pass"]'`` 被正确解析成两个 argv。

    远程条目走 ``.url=`` + ``.http_headers=``，**不发** command/args/env。三者是
    codex 配置里两条并列的连接方式，同时给等于让 codex 自己挑一条 —— 那时它到底
    起不起子进程就不再由我们决定了。头是 ``http_headers``（字面量映射）而不是
    ``env_http_headers``：后者的值是**环境变量名**，写错等于把 token 换成它的名字发出去。
    """
    if not servers:
        return []
    args: list = []
    for server in servers:
        prefix = f"mcp_servers.{server.name}"
        if server.transport == TRANSPORT_HTTP:
            args.extend(["-c", f"{prefix}.url={json.dumps(server.url)}"])
            if server.headers:
                args.extend(["-c", f"{prefix}.http_headers={json.dumps(dict(server.headers))}"])
            continue
        args.extend(["-c", f"{prefix}.command={json.dumps(server.command)}"])
        if server.args:
            args.extend(["-c", f"{prefix}.args={json.dumps(list(server.args))}"])
        for key, value in server.env.items():
            args.extend(["-c", f"{prefix}.env.{key}={json.dumps(value)}"])
    return args


class CodexAdapter(CliAdapter):
    """OpenAI Codex CLI（``codex exec``）。

    flags 依据官方 developer-commands 参考（learn.chatgpt.com/docs/developer-commands）
    核实；本环境无法访问该站（connection refused），内容由用户提供。

    关键约定：
      - ``codex exec``（简写 ``codex e``）为无头/CI 模式子命令
      - ``--json`` 输出 newline-delimited JSON 事件（每个状态变化一行）
      - 续接是 exec 的子命令：``codex exec resume <session_id>``，非 --resume flag
      - 免审批：``--dangerously-bypass-approvals-and-sandbox``（文档建议仅在专用沙箱 VM 内用）
      - 模型：命令行传 --model（文档可见表格未列出，需实测）；亦可经
        ``~/.codex/config.toml`` 的 model 字段或 ``-c model=...`` 覆盖
      - 认证：``codex login``（ChatGPT 账号 / API key / access token）；API key 走
        ``OPENAI_API_KEY`` 环境变量，需额外 setup
    """

    id = "codex"
    display_name = "OpenAI Codex CLI"
    command = "codex"
    api_key_env = "OPENAI_API_KEY"
    # 同名的无关工具也叫 codex：--help 里有 exec 才是 OpenAI Codex。
    install_probe = (["--help"], ["exec"], [])

    supports_mcp = True

    # 项目级 skill：``.agents/skills``（2026-09-20 真机 A/B 实测 PASS）。
    #
    # 这段注释改过三次，过程本身比结论值钱，所以留着：
    #   1. 最初写的是「codex 无 project-level skill 概念」—— 错的，它在二进制里有
    #      ``repo skills root`` 与 ``.agents/skills``。
    #   2. 然后单轮 A/B 两轮都答 NONE，于是判成「试过了，这么接是错的」，写成了 None。
    #      那个 FAIL 是**测量失效**：``codex exec --json`` 那一轮里模型直接回了一句
    #      NONE，A、B 都是 —— 对照组是空的，什么都证明不了。
    #   3. 加了重复轮次（探针的 --repeat）之后，A 组在同一份 ``.agents/skills/`` 上
    #      原样答出金丝雀，B 组没有 ⇒ PASS。
    #
    # 教训：**单轮的 FAIL 不可信，单轮的 PASS 可信** —— 金丝雀是个生造串，没被加载的
    # CLI 不可能凭空答出来，但「加载了而模型懒得列」随时会发生（见探针模块说明）。
    #
    # 另外 codex 的技能还有一条 **Agent Plugin** 通道
    # （``<repo-root>/.agents/plugins/marketplace.json``、``$CODEX_HOME/skills``），
    # 与本目录是两条并行的路；我们只走后者，因为它与 AICU 的「往工作区落软链」同构。
    skills_workspace_dir = (".agents", "skills")

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool,
        mcp_servers: Optional[list] = None,
    ) -> list:
        # -c 是**全局** flag，必须排在 exec 子命令之前（`codex -c ... exec ...`）。
        # 这正是 codex 不用 mcp_flags 钩子的原因：那个钩子产出的是追加在命令末尾的
        # 参数，会落到 exec 之后 —— 位置不对，codex 不会认。
        cmd = [self.command, *_codex_mcp_args(mcp_servers)]
        cmd.append("exec")
        if model:
            cmd.extend(["--model", model])
        if skip_permissions:
            # 等价于 Claude Code 的 skip_permissions；文档警示仅在专用沙箱 VM 内使用
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        # ``enable_project_skills`` 是 claude 系的 argv 通道（`--setting-sources project`），
        # codex 不走它 —— 它的 skill 由 AICU 落到 <cwd>/.agents/skills/ 生效。
        # 曾在这里写过「codex 无 project-level skill 概念」，是错的：它在二进制里有
        # `repo skills root` 与 `.agents/skills` 这个概念。
        return cmd

    def print_flags(self, streaming: bool) -> list:
        # --json 输出 NDJSON 事件流，最终回复在最后一条事件里，parse_result 直接提取。
        # 不再用 --output-last-message：该 flag 需要 <FILE> 值（写文件而非打印 stdout），
        # 且会吞掉其后的位置参数，导致 prompt 丢失。
        return ["--json"]

    def resume_flag(self, session_id: str) -> list:
        # resume 是 exec 的子命令（codex exec resume <id>），非 --resume flag
        return ["resume", session_id]

    def prompt_args(self, prompt: str) -> tuple[list, bool]:
        # codex exec 支持位置参数与 stdin 两种传 prompt 方式；走 stdin 更稳
        # （避免长 prompt 超 ARG_MAX，与 claude/kimi 一致）。
        return ([], True)

    def build_env(
        self,
        base_env: dict,
        *,
        api_base_url: Optional[str],
        api_key_env: Optional[str],
        model: Optional[str] = None,
        mcp_servers: Optional[list] = None,
    ) -> dict:
        env = dict(base_env)
        if api_base_url:
            env["OPENAI_BASE_URL"] = api_base_url
        # codex 只认 OPENAI_API_KEY，不认 api_key_env 本身的名字
        self._inject_api_key(env, api_key_env, "OPENAI_API_KEY")
        return env

    def api_key_env_for_model(self, model: Optional[str]) -> Optional[str]:
        # Codex 的自定义 provider 通过 config.toml 的 env_key 读取对应厂商 Key。
        return api_key_env_for_vendor(vendor_of_model(model))

    managed_slot = True

    def provision(
        self, *, api_base_url: Optional[str], api_key_env: Optional[str], model: Optional[str]
    ) -> None:
        # api_base_url 为 None = 本次不由 AICU 供端点（同族且策略是撤槽、或模型判不出
        # 厂商），必须把上次写进 config.toml 的东西撤掉 —— 早退就会把过期端点当成
        # 这一次的事实。见 provider_slot_base_url / _release_codex_config。
        if not api_base_url:
            _release_codex_config()
            return
        # config.toml 的 env_key 必须与 _build_process_env 注入 sk 用的变量名逐字一致，
        # 否则 codex 报「Missing environment variable」。显式 provider route 的
        # api_key_env（例如 OpenRouter）优先；旧的无 provider 配置才按模型厂商回退。
        env_key = api_key_env or api_key_env_for_vendor(vendor_of_model(model))
        _provision_codex_config(api_base_url, env_key, model)

    def format_stream_event(self, line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
        if not isinstance(event, dict):
            return ""
        item_text = _codex_item_text(event)
        if item_text:
            return item_text
        for key in ("content", "text", "message", "output"):
            if isinstance(event.get(key), str) and event[key]:
                return event[key]
        return ""

    def parse_result(self, raw: str) -> Optional[dict]:
        # --json 输出 NDJSON（每行一个状态事件），兼容单对象 JSON 与纯文本
        text = raw.strip()
        if not text:
            return None
        result = None
        session_id = None
        subtype = None
        errors: list = []
        parsed_any = False
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            parsed_any = True
            # 先认 item 信封（codex 现行输出），它比下面的通用键扫描更权威。
            item_text = _codex_item_text(value)
            if item_text:
                result = item_text
            for key in ("result", "output", "text", "answer", "response"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    result = candidate
                    break
            if result is None:
                msg = value.get("message")
                if isinstance(msg, str) and msg:
                    result = msg
                elif isinstance(msg, dict):
                    rendered = _render_openai_content(msg.get("content"))
                    if rendered:
                        result = rendered
            # codex 的续接 id 叫 thread_id（thread.started 事件里给出），
            # 不采它则 codex 的多轮对话实际不会 --resume，每轮都是新会话。
            sid = (
                value.get("session_id")
                or value.get("conversation_id")
                or value.get("thread_id")
            )
            if sid:
                session_id = sid
            if value.get("subtype"):
                subtype = value["subtype"]
            if value.get("errors") or value.get("error"):
                errors = _coerce_errors(value.get("errors") or value.get("error"))
        if not parsed_any:
            return None
        return {
            "result": result if result is not None else text,
            "session_id": session_id,
            "subtype": subtype,
            "errors": errors,
        }


class KimiAdapter(CliAdapter):
    """Moonshot Kimi Code CLI（``kimi``）。

    依据当前 Kimi Code CLI 帮助：
      - 无头模式：--prompt/-p <prompt>
      - 输出：--output-format text|stream-json（prompt 模式）
      - 续接：--session/-S <session_id>；无 --max-turns
    stream-json 为 OpenAI 风格 JSONL（{"role":"assistant","content":...}）。

    **模型与凭据走 ``KIMI_MODEL_*`` 这一族环境变量**（0.43.1 实测）：

      - 老文档里的 ``KIMI_BASE_URL`` / ``KIMI_API_KEY`` 作为 shell 变量**是死的** ——
        凭据只从 config.toml 的 ``[providers.*]`` 读（官方 docs/en/configuration/
        env-vars.md）。只给这两个变量的实测结果是「Model "X" is not configured in
        config.toml」，所以本 adapter 不再写它们。
      - ``KIMI_MODEL_NAME`` 是这一族的开关：连同 ``KIMI_MODEL_API_KEY`` /
        ``KIMI_MODEL_BASE_URL`` / ``KIMI_MODEL_PROVIDER_TYPE``，kimi 在**内存里**合成一个
        临时 provider，不写回配置文件 —— 因此 kimi 属于「没有托管槽」的一类
        （managed_slot = False），盘上不留痕迹，也就没有「上次运行留下的旧值」。
      - 代价是 ``-m <alias>`` 的优先级高于这一族：它只认 config.toml 里的别名，
        所以走环境通道时绝不能传 ``--model``（见 model_arg）。
      - 这一族两版都吃：本机 PATH 上的 0.38.0 与 ``~/.kimi-code/bin/kimi`` 的 0.43.1
        都能被它驱动。差别只在 ``--model`` —— 0.38.0 在 prompt 模式下带 ``--model``
        会直接 Node 崩溃（栈底是 AgentActivityView.dispose），0.43.1 只是干净地报
        「Model "X" is not configured in config.toml」。改道时不传 ``--model``
        正好绕开 0.38.0 那个崩溃（2026-09-16 两份二进制各跑一次实测）。

    **MCP：托管智能体那条路是刻意不适配的**（2026-09-17 核过官方文档
    ``platform.kimi.com/docs/hosted-agents/mcp``）。Kimi 平台侧确实有 MCP，但那属于
    「托管智能体」（hosted agents），跟本 CLI 是两个产品 —— 那边 MCP 是
    ``POST /v1/agents`` 请求体里的 ``mcp_servers``，全篇没有任何配置文件路径，也没有
    ``kimi mcp`` 这类子命令。三处对不上，所以不接：

      - **形状**：每项只支持 ``type: "url"``（远程 MCP），没有 ``command`` / ``args`` /
        ``env`` 的位置；而 AICU 下发的是 stdio（``_mcp_servers_project_json``）。
      - **位置**：托管智能体跑在 Kimi 云端（请求体里的 ``environment_id`` / ``vault_id``），
        够不到本机 localhost 的 server —— 而端到端判据恰恰就是本机 echo server 吐的
        ``probe_echo: AICU-MCP-PROBE-7f3a``。
      - **形态**：接它等于新增一类 HTTP 后端，与 AICU「起子进程 + 往 workspace 写项目级
        配置文件」的 adapter 模型不是一回事，不是改一行的事。

    本 CLI 自己那条 MCP 路径见 ``mcp_workspace_files``：往 project 层
    ``<cwd>/.kimi-code/mcp.json`` 落配置，**外加一条 workspace 信任记录** —— 少了后者，
    kimi 的 trust 门禁会把这份配置静默丢掉（机制与实测见 ``_write_kimi_trust`` 上方那段）。
    """

    id = "kimi"
    display_name = "Kimi Code"
    command = "kimi"
    # 用户在设置页给 kimi 填的 key 对应的变量名（对外展示用）；真正注入子进程的那个
    # 名字由 api_key_env_for_model 决定 —— kimi 读的是 KIMI_MODEL_API_KEY。
    api_key_env = "KIMI_API_KEY"

    # kimi 把「上下文窗口」当 max_tokens 发给厂商：拦包实测
    # ``max_tokens = min(KIMI_MODEL_MAX_CONTEXT_SIZE, 131072)``（默认 262144 → 发 131072）。
    # 厂商对输出 token 有硬上限，超了直接 400，所以这里记实测撞过的天花板；没记的厂商
    # 用 kimi 自己的 131072。键名与 runtime.credentials.vendor_of_model 的返回值逐字一致。
    #
    # 上限是**按模型**的（阿里云实测：qwen3.8-max 收 131072，qwen3-coder-plus 卡 65536），
    # 而 AICU 的模型清单只有名字、没有 token 元数据，所以这里只能取「该厂商各模型都收得下
    # 的下限」—— 保守但不报错，代价是大窗口模型在 kimi 眼里只剩这么多上下文。
    #   2026-09-16 实测：aliyun 65536 是实测到的下限（qwen3-coder-plus 报
    #   「Range of max_tokens should be [1, 65536]」）；deepseek / zhipu / moonshot
    #   收 131072 都是 200，故没有它们的行。
    _CONTEXT_LIMIT_BY_VENDOR = {"aliyun": 65536}

    # kimi 的项目级 MCP 配置文件，位置从 0.43.1 与 2.0.0 两份 bundle 逐字核实：
    # ``resolveMcpJsonPaths`` 里的 project 一路就是 ``<cwd>/.kimi-code/mcp.json``，
    # key 是 ``mcpServers``（与 gemini/cursor/pi 同形，故走基类的通用实现）。
    mcp_workspace_config = (".kimi-code", "mcp.json")

    # 引擎一侧的 MCP 支持从 0.43.1 起就在（``resolveMcpJsonPaths`` / ``loadMcpServers``
    # 两版都有），所以这里是常量而不是探测。
    #
    # 曾经这里是个探测 ``kimi --help`` 有没有 ``mcp`` 子命令的 property，**恒为假** ——
    # 0.43.1 与 2.0.0 都没有那个子命令（``kimi mcp list`` → ``unknown command 'mcp'``），
    # 于是 kimi 永远显示「未接入」。那个写法把「没有 CLI 管理子命令」当成了「引擎不会加载
    # MCP」，是两件事。
    supports_mcp = True

    # kimi 的项目级 skill 目录是 ``.agents/skills``。
    #
    # **曾经写成 ``.claude/skills``**，理由是「与 claude 系同目录、少维护一个路径」。
    # 2026-09-20 真机 A/B 证伪了它：同一份 skill 软链，放 ``.claude/skills`` 时 kimi
    # 的技能清单里只有 Built-in，放 ``.agents/skills`` 时清单里多出 ``Project`` 一节、
    # 金丝雀原样答出。kimi 自己的 bundle 里也从来只有 ``.agents/skills`` 与
    # ``.kimi-code/skills`` 两个字面量，``.claude/skills`` 一次都没出现 —— 当初那个
    # 「kimi 读三个目录」的说法是**推出来的**，不是查出来的（它的 import 技能只说明
    # 能从 ``.claude`` **迁移**进来，迁移与读取是两件事）。
    #
    # 这也正是本项目最怕的那种失效：目录建了、``ls`` 看着对、用户以为配好了，实际
    # 一个字节都没进模型上下文。挑目录不能靠「跟别家一致」推断，只能查字面量 + 真机验。
    skills_workspace_dir = (".agents", "skills")

    def mcp_workspace_files(self, workspace, mcp_servers: Optional[list]) -> list:
        """项目级 ``mcp.json`` 之外，**再补一条 workspace 信任记录**。

        这是 KimiAdapter 独有的额外动作，也是这条路能不能通的全部关键：kimi 引擎走
        ``loadMcpServers({..., includeProject: this.trust.isTrusted()})``，而未授信的
        workspace 只读 user 层 —— project 层那份配置会被**静默**丢掉。无头模式（``-p``）
        永远走不到 TUI 的授权提示，所以这份信任只能由 AICU 写。

        返回的两个路径都由调用方在进程结束后删掉（见 ``_drop_transient``）；
        信任记录如果本来就在（用户自己授的、或上次运行留下的），``_write_kimi_trust``
        返回 None，不会被删 —— 写别人的东西和删别人的东西是两回事。
        """
        files = super().mcp_workspace_files(workspace, mcp_servers)
        if not files:
            # 没落盘 = 本次压根没 mcp，那就一个字节都不写：不配 MCP 的运行不该在
            # 用户的 home 里留下信任记录。
            return files
        record = _write_kimi_trust(workspace)
        return files + [record] if record is not None else files

    def model_arg(self, model: Optional[str], *, api_base_url: Optional[str]) -> Optional[str]:
        """改道时恒为 None —— 模型由 ``KIMI_MODEL_NAME`` 承担，命令行上不能出现 ``--model``。

        没改道（同族模型、用户自己配过 config.toml）则照旧传 ``--model``，与改动前一致。
        这个条件必须与 build_env 里「开不开 KIMI_MODEL_*」的条件严格一致，否则会出现
        「模型名两边都没给」或「--model 盖掉 KIMI_MODEL_NAME」（有测试守着这条不变式）。
        """
        return None if api_base_url else model

    def api_key_env_for_model(self, model: Optional[str]) -> Optional[str]:
        """kimi 读 key 的变量名是 ``KIMI_MODEL_API_KEY``（同族、异族都一样）。

        ``_build_process_env`` 会把 sk 写进这个名字，名字必须与 kimi 期望的逐字一致，
        写错等于没给 —— 与 codex 的 ``env_key`` 是同一个坑。
        """
        return "KIMI_MODEL_API_KEY"

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool,
        mcp_servers: Optional[list] = None,
    ) -> list:
        cmd = [self.command]
        if model:
            cmd.extend(["--model", model])
        # 当前 Kimi Code 的 --prompt 不能与 --yolo/--auto 共用；
        # prompt 模式本身用于非交互执行，因此不追加权限参数。
        return cmd

    def print_flags(self, streaming: bool) -> list:
        fmt = "stream-json" if streaming else "text"
        return ["--output-format", fmt]

    def resume_flag(self, session_id: str) -> list:
        return ["--session", session_id]

    def prompt_args(self, prompt: str) -> tuple[list, bool]:
        # 当前 Kimi Code 要求 prompt 通过 -p/--prompt 传入。
        return (["--prompt", prompt], False)

    def build_env(
        self,
        base_env: dict,
        *,
        api_base_url: Optional[str],
        api_key_env: Optional[str],
        model: Optional[str] = None,
        mcp_servers: Optional[list] = None,
    ) -> dict:
        """开 ``KIMI_MODEL_*`` 环境通道；没有确切端点时什么都不做。

        开了这族变量就等于把「用哪个模型、去哪、怎么认证」整个交给它（因为 model_arg
        同时不传 ``--model`` 了）。所以只在 AICU 拿得出端点（``api_base_url`` 非空）时开：
        同族模型、或判不出厂商的自定义模型名都不开，让 kimi 走它自己的 config.toml 与
        登录态 —— 别把用户自己配好的 kimi 顶掉（与 codex 的同族撤槽同源）。

        这里不写 key：``_build_process_env`` 会在本方法返回后把 sk 写进
        ``api_key_env_for_model`` 指定的 ``KIMI_MODEL_API_KEY``。那条注入只看模型
        厂商、不看有没有端点，所以通道没开时也会带上一个光秃秃的 ``KIMI_MODEL_API_KEY``
        —— 实测无害（开关是 ``KIMI_MODEL_NAME``，没有它这族变量一律不看：0.43.1 在这种
        环境下照样报「Model "X" is not configured in config.toml」）。
        """
        env = dict(base_env)
        if not model or not api_base_url:
            return env
        env["KIMI_MODEL_NAME"] = model
        env["KIMI_MODEL_BASE_URL"] = api_base_url
        # provider type 只有 kimi / anthropic / openai 三种，而 _CLI_NATIVE 里 kimi
        # 的协议是 chat —— AICU 给的端点一律是 OpenAI Chat 形状的。
        env["KIMI_MODEL_PROVIDER_TYPE"] = "openai"
        limit = self._CONTEXT_LIMIT_BY_VENDOR.get(vendor_of_model(model) or "")
        if limit:
            env["KIMI_MODEL_MAX_CONTEXT_SIZE"] = str(limit)
        return env

    def format_stream_event(self, line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
        if not isinstance(event, dict):
            return ""
        role = event.get("role")
        if role == "assistant":
            return _render_openai_content(event.get("content"))
        if role == "tool":
            return str(event.get("content") or "")
        return ""

    def parse_result(self, raw: str) -> Optional[dict]:
        text = raw.strip()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        # stream-json 模式：JSONL 行，收集 assistant 文本 + session_id
        if lines and lines[0].lstrip().startswith("{"):
            result_parts = []
            session_id = None
            for line in lines:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("role") == "assistant":
                    content = _render_openai_content(event.get("content"))
                    if content:
                        result_parts.append(content)
                sid = event.get("session_id") or event.get("session") or event.get("conversation_id")
                if sid:
                    session_id = sid
            result = "\n".join(result_parts).strip() or text
            return {"result": result, "session_id": session_id, "subtype": None, "errors": []}
        # text 模式：整体为最终答案
        return {"result": text, "session_id": None, "subtype": None, "errors": []}


class QwenAdapter(CliAdapter):
    """Qwen Code CLI（``qwen``）。

    依据官方文档核实（qwenlm.github.io/qwen-code-docs 的 Headless Mode 页 +
    docs.qwencloud.com 的 Qwen Code 页）：
      - 命令名 qwen；headless 由 -p/--prompt 或 stdin 触发
      - 模型：--model/-m <id>，且 id 必须已在 ~/.qwen/settings.json 的
        modelProviders 中配置（openai-compatible 协议）。modelProviders 的
        value 是模型数组（ModelConfig[]），每项含 id/envKey/baseUrl 字段，
        结构已按 QwenLM/qwen-code 源码 packages/core/src/models/types.ts 核实。
        模型按套餐不同，如 qwen3.8-max / qwen3-coder-plus / deepseek-v4-pro /
        kimi-k2.7-code / glm-5.2 / MiniMax-M2.5 等，由账号/套餐决定，非固定枚举。
      - 输出：--output-format json | stream-json（无 --print）
      - 续接：--resume <sessionId> / --continue
      - 轮次预算：--max-session-turns（非 --max-turns）
      - 免审批：--yolo / -y；安全模式 --safe-mode
      - 认证 env：BAILIAN_API_KEY（按量付费）/ BAILIAN_TOKEN_PLAN_API_KEY /
        BAILIAN_CODING_PLAN_API_KEY（默认按量付费 BAILIAN_API_KEY）
      - base URL 由 settings.json 的 modelProviders 决定，CLI 不通过 env 覆盖
    本机实测：settings.json 里注册 aicu provider（baseUrl 指 deepseek + envKey）后，
    --model <id> 可正常出结果。
    """

    id = "qwen"
    display_name = "Qwen Code"
    command = "qwen"
    api_key_env = "BAILIAN_API_KEY"

    supports_mcp = True

    # 项目级 skill：``<cwd>/.qwen/skills/<name>/SKILL.md``。qwen 自己的文档与提示词里
    # 都按这个路径写（``.qwen/skills/commit/SKILL.md``、自动生成的 skill 也落在
    # ``.qwen/skills/``）。``--experimental-skills`` 那个 flag 官方已标为废弃
    # （"Skills are now enabled by default. This flag is ignored."），所以不需要 argv，
    # 目录建好即可 —— 这也正合「非 claude 系不走 argv」那条约束。
    skills_workspace_dir = (".qwen", "skills")

    def mcp_workspace_files(self, workspace, mcp_servers: Optional[list]) -> list:
        """写 ``<workspace>/.qwen/settings.json``（qwen 的**项目级**配置）。

        为什么是文件而不是 argv/env：qwen 没有 ``--mcp-config`` 这类 flag，也没有
        ``OPENCODE_CONFIG_CONTENT`` 那种收整份配置的环境变量，``-p`` 那一堆选项里
        没有任何 MCP 口子（``qwen --help`` 已核实）。它唯一认的注入点是项目级配置
        文件 —— ``qwen mcp add -s project`` 写的就是这个路径。

        **不能写用户级的 ``~/.qwen/settings.json``**：那是全局配置，并发节点会互相
        覆盖，还会污染用户自己的环境；而且它正是 ``_provision_qwen_config`` 放
        ``modelProviders.aicu`` 的地方，混在一起写容易把模型路由搞坏。项目级文件按
        cwd 生效，cwd 就是本 CC 独占的 workspace，天然隔离。

        项目级文件与用户级文件**不冲突、是叠加的**：实测在同一目录下，用户级的 server
        与项目级的 server 会同时出现在 ``qwen mcp list`` 里，用户自己配的 server 不会
        被这里写的覆盖掉。
        """
        if not mcp_servers:
            return []
        path = Path(workspace) / ".qwen" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_mcp_servers_settings_json(mcp_servers), encoding="utf-8")
        return [path]

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool,
        mcp_servers: Optional[list] = None,
    ) -> list:
        cmd = [self.command]
        if model:
            cmd.extend(["--model", model])
        # Qwen 0.22+ 的非交互模式要求显式选择认证类型；
        # modelProviders.aicu 通过 OpenAI-compatible endpoint 接入。
        cmd.extend(["--auth-type", "openai"])
        if skip_permissions:
            cmd.append("--yolo")
        # ``enable_project_skills`` 是 claude 系的 argv 通道；qwen 的 skill 由 AICU 落到
        # <cwd>/.qwen/skills/ 生效，不需要也不能加 flag（`--experimental-skills` 已废弃，
        # 而且这家有逐字节 argv 断言）。曾写的「无 project-level skill 概念」是错的。
        return cmd

    def print_flags(self, streaming: bool) -> list:
        fmt = "stream-json" if streaming else "json"
        return ["--output-format", fmt]

    def resume_flag(self, session_id: str) -> list:
        return ["--resume", session_id]

    def max_turns_flag(self, n: int) -> list:
        return ["--max-session-turns", str(n)]

    def prompt_args(self, prompt: str) -> tuple[list, bool]:
        # headless：-p/--prompt 或 stdin；用 -p 明确语义
        return (["-p", prompt], False)

    def api_key_env_for_model(self, model: Optional[str]) -> Optional[str]:
        """注入 sk 用的变量名 —— 必须与 provision 写进 settings.json 的 envKey 逐字一致。

        Qwen Code 从 ``envKey`` 指定的环境变量读 key，而 sk 是 _build_process_env
        按本函数的返回值注入的，两边一旦不一致，CLI 就取不到 key。同族（阿里云）
        沿用 Qwen Code 原生的 BAILIAN_API_KEY，异族用该厂商的标准名。
        """
        vendor = vendor_of_model(model or "")
        if not vendor or vendor == _CLI_NATIVE["qwen"][0]:
            return self.api_key_env
        return api_key_env_for_vendor(vendor) or self.api_key_env

    def build_env(
        self,
        base_env: dict,
        *,
        api_base_url: Optional[str],
        api_key_env: Optional[str],
        model: Optional[str] = None,
        mcp_servers: Optional[list] = None,
    ) -> dict:
        # qwen 的 base URL 由 settings.json 的 modelProviders 决定，CLI 不通过 env 覆盖
        return self._env_with_api_key(base_env, api_key_env)

    managed_slot = True

    def provision(
        self, *, api_base_url: Optional[str], api_key_env: Optional[str], model: Optional[str]
    ) -> None:
        # api_base_url 为 None = 本次不由 AICU 供端点，撤掉上次写进 settings.json 的
        # aicu 条目 —— 早退就会把过期端点当成这一次的事实。
        # 注意这条退路对 qwen 并不好用（撤槽后 CLI 的非交互模式没有 auth type 可用，
        # 见 _SAME_VENDOR_RELEASE_CLIS 的说明），所以 qwen 的同族是接管而非撤槽；
        # 只有「模型判不出厂商」才会走到这里。
        if not api_base_url:
            _release_qwen_config()
            return
        # 同 codex：envKey 走 api_key_env_for_model，与注入 sk 的变量名同源，
        # 显式传入的 api_key_env 只作兜底。
        env_key = (
            api_key_env
            if api_key_env == "OPENROUTER_API_KEY"
            else self.api_key_env_for_model(model) or api_key_env
        )
        _provision_qwen_config(api_base_url, env_key, model)

    def format_stream_event(self, line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
        if not isinstance(event, dict):
            return ""
        # qwen 的 stream-json 用 Claude Code 同形信封，正文藏在
        # {"type":"assistant","message":{"role":"assistant","content":[...]}} 里，
        # 行顶层没有 role。少了这一步整个流式过程一行都吐不出来（实测回调 0 条），
        # 界面上就只会在结束时突然显示最终答案。
        # 复用 _extract_event_text：它只取 type=="text" 的块，thinking 块返回空串。
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            return _extract_event_text(message)
        role = event.get("role")
        if role in ("assistant", "tool"):
            return _render_openai_content(event.get("content"))
        for key in ("content", "text", "message", "output"):
            if isinstance(event.get(key), str) and event[key]:
                return event[key]
        return ""

    def parse_result(self, raw: str) -> Optional[dict]:
        text = raw.strip()
        # qwen 的 --output-format json 实测输出的是事件数组（末条 type=result，
        # 与 Claude Code 同形）——优先按这个形状取，取不到再走下面的 OpenAI 风格兜底。
        if text.startswith("["):
            parsed = _claude_style_result(raw)
            if parsed:
                return parsed
        # 优先尝试单对象 JSON（--output-format json）
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            result = None
            for key in ("result", "output", "text", "answer", "response", "content"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    result = candidate
                    break
                if isinstance(candidate, list):
                    rendered = _render_openai_content(candidate)
                    if rendered:
                        result = rendered
                        break
            # OpenAI 风格 message.content
            if result is None and isinstance(value.get("message"), dict):
                result = _render_openai_content(value["message"].get("content"))
            return {
                "result": result if result is not None else text,
                "session_id": value.get("session_id") or value.get("conversation_id") or value.get("session"),
                "subtype": value.get("subtype"),
                "errors": _coerce_errors(value.get("errors") or value.get("error")),
            }
        # 多行 JSONL（stream-json）：qwen 用的也是 Claude Code 同形的事件流 ——
        # 首行 {"type":"system",...}，中间 {"type":"assistant","message":{...}}，
        # 末行 {"type":"result","result":"可用"}。先按这个形状取；取不到再走下面
        # 的 OpenAI 风格逐行收集兜底。
        # 少了这一步流式输出会被当成纯文本原样返回：下面的兜底只认 role=="assistant"
        # 的行，而 qwen 把正文放在 message 里，收集为空 → 整个 JSONL 变成答案。
        parsed = _claude_style_result(raw)
        if parsed and parsed.get("result"):
            return parsed
        # 多行 JSONL（stream-json）：OpenAI 风格，收集 assistant 文本 + session_id
        result_parts = []
        session_id = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("role") == "assistant":
                content = _render_openai_content(event.get("content"))
                if content:
                    result_parts.append(content)
            sid = event.get("session_id") or event.get("session") or event.get("conversation_id")
            if sid:
                session_id = sid
        if result_parts:
            return {"result": "\n".join(result_parts).strip(), "session_id": session_id, "subtype": None, "errors": []}
        # 纯文本兜底
        return {"result": text, "session_id": None, "subtype": None, "errors": []}


class GenericExecAdapter(CliAdapter):
    """通用 exec 风格 CLI 适配器（Antigravity / Grok 等）。

    这些 CLI 的 headless 约定各不相同，缺省值只是**起点**：直接拿来用会写出
    「看着对、实际起不来」的命令（grok 的 ``--json`` 就是这么栽的，见 GrokAdapter）。
    拿到厂商文档或实测机会，就该升格成子类逐条钉死。

    antigravity 是现在唯一还直接用它缺省值的实例，而它没被接线 —— 见
    AntigravityAdapter（账号/地区不可用）。
    """

    def __init__(
        self,
        cli_id: str,
        display_name: str,
        command: str,
        api_key_env: str,
        model_flag: str = "--model",
        json_flag: str = "--json",
        prompt_flag: Optional[str] = None,
        prompt_via_stdin: bool = False,
        supports_resume: bool = True,
        base_url_env: Optional[str] = None,
        result_fields: tuple = ("result", "output", "text", "answer", "response"),
    ):
        self.id = cli_id
        self.display_name = display_name
        self.command = command
        self.api_key_env = api_key_env
        self.model_flag = model_flag
        self.json_flag = json_flag
        self.prompt_flag = prompt_flag
        self._prompt_via_stdin = prompt_via_stdin
        self.supports_resume = supports_resume
        self.base_url_env = base_url_env
        self.result_fields = result_fields

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool,
        mcp_servers: Optional[list] = None,
    ) -> list:
        cmd = [self.command]
        if model and self.model_flag:
            cmd.extend([self.model_flag, model])
        # 各家「免审批」flags 不一致，保守不加，避免命令报错
        return cmd

    def print_flags(self, streaming: bool) -> list:
        return [self.json_flag] if self.json_flag else []

    def resume_flag(self, session_id: str) -> list:
        return ["--resume", session_id] if self.supports_resume else []

    def max_turns_flag(self, n: int) -> list:
        return ["--max-turns", str(n)]

    def prompt_args(self, prompt: str) -> tuple[list, bool]:
        if self._prompt_via_stdin:
            return ([], True)
        if self.prompt_flag:
            return ([self.prompt_flag, prompt], False)
        return ([prompt], False)

    def build_env(
        self,
        base_env: dict,
        *,
        api_base_url: Optional[str],
        api_key_env: Optional[str],
        model: Optional[str] = None,
        mcp_servers: Optional[list] = None,
    ) -> dict:
        env = dict(base_env)
        if api_base_url and self.base_url_env:
            env[self.base_url_env] = api_base_url
        self._inject_api_key(env, api_key_env or self.api_key_env)
        return env

    def format_stream_event(self, line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
        if not isinstance(event, dict):
            return ""
        for key in ("content", "text", "message", "output"):
            if isinstance(event.get(key), str) and event[key]:
                return event[key]
        return ""

    def parse_result(self, raw: str) -> Optional[dict]:
        try:
            value = json.loads(raw.strip())
        except json.JSONDecodeError:
            # 非 JSON 输出：整体当作文本结果
            return {"result": raw.strip(), "session_id": None, "subtype": None, "errors": []}
        if not isinstance(value, dict):
            return {"result": str(value), "session_id": None, "subtype": None, "errors": []}
        result = None
        for key in self.result_fields:
            if isinstance(value.get(key), str) and value[key]:
                result = value[key]
                break
        if result is None:
            result = raw.strip()
        return {
            "result": result,
            "session_id": value.get("session_id") or value.get("conversation_id"),
            "subtype": value.get("subtype"),
            "errors": _coerce_errors(value.get("errors") or value.get("error")),
        }


class OpenCodeAdapter(CliAdapter):
    """OpenCode CLI（``opencode run``）。

    依据官方文档（opencode.ai/docs/cli）核实：
      - 命令名 opencode；无头模式 = run 子命令：``opencode run <message..>``
      - 模型：--model/-m，格式 provider/model（如 anthropic/claude-sonnet-4-6）
      - prompt：run 的位置参数（message..），无需 -p / stdin
      - 输出：--format json（raw JSON events）
      - 续接：--session/-s <sessionID>（--continue/-c 续上次会话）
      - 免审批：--auto（自动批准未显式拒绝的权限）
      - 认证：opencode auth login 写 ~/.local/share/opencode/auth.json，
        或读标准厂商环境变量（ANTHROPIC_API_KEY / OPENAI_API_KEY 等）与 .env
      - 无 max-turns 类轮次 flag（文档未见）
    """

    id = "opencode"
    display_name = "OpenCode"
    command = "opencode"
    # 多厂商 CLI，无单一认证 env；sk 注入按模型厂商走 api_key_env_for_model 映射
    api_key_env = None

    # 项目级 skill：**不接线**。opencode 与上面几家不同 —— 它**没有**「把 skill 放进某个
    # 目录就自动加载」的项目级行为：外部自动加载只认家目录级的 ``~/.claude/skills`` 与
    # ``~/.agents/skills``；项目级要走配置键 ``skills.paths``。所以接它必须覆盖
    # ``skills_workspace_files`` 去写配置（同 ``_opencode_config_content`` 那条
    # ``OPENCODE_CONFIG_CONTENT`` 通道），**光建一个目录是假接线**。
    #
    # 2026-09-20 真机 A/B 坐实了这个判断：往工作区塞 ``.opencode/skills/``，A、B 两组的
    # 清单**逐条相同**（都只有 opencode 自带的 ``customize-opencode``），A 组没有多出
    # 金丝雀。注意这一家的对照组非空 —— 说明它的技能注入链路本身是通的，模型确实看得见
    # 一份清单，只是那个目录不在它的加载路径上。这条 FAIL 是有效结论，不是没测出来。
    skills_workspace_dir = None

    def api_key_env_for_model(self, model: Optional[str]) -> Optional[str]:
        return api_key_env_for_vendor(vendor_of_model(model))

    def model_arg(self, model: Optional[str], *, api_base_url: Optional[str]) -> Optional[str]:
        # opencode 的 -m 要求 provider/model；第三方端点是以 aicu 这个 provider id
        # 通过 OPENCODE_CONFIG_CONTENT 注册的，所以改道时必须补前缀。
        return f"{_CUSTOM_PROVIDER_ID}/{model}" if api_base_url and model else model

    supports_mcp = True

    def build_env(
        self,
        base_env: dict,
        *,
        api_base_url: Optional[str],
        api_key_env: Optional[str],
        model: Optional[str] = None,
        mcp_servers: Optional[list] = None,
    ) -> dict:
        env = dict(base_env)
        # 条件不能只看 api_base_url：只声明了 mcp、不改道时（api_base_url 为空）同样
        # 需要这个环境变量，否则 MCP 配置没有下发通道、静默丢失。
        if api_base_url or mcp_servers:
            env["OPENCODE_CONFIG_CONTENT"] = _opencode_config_content(
                api_base_url,
                list(dict.fromkeys([
                    *KNOWN_MODELS.get("all", []),
                    *[
                        item_model
                        for preset in list_provider_presets()
                        if model and preset.get("provider_kind") == "gateway"
                        for item_model in (preset.get("models") or [])
                    ],
                    *([model] if model else []),
                ])),
                api_key_env,
                mcp_servers,
            )
        return env

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool,
        mcp_servers: Optional[list] = None,
    ) -> list:
        cmd = [self.command, "run"]
        if model:
            cmd.extend(["--model", model])
        if skip_permissions:
            cmd.append("--auto")
        # ``enable_project_skills`` 是 claude 系的 argv 通道；opencode 的项目级 skill
        # 要走配置键 ``skills.paths``（见 skills_workspace_dir 处的说明），不是往 argv
        # 加参数。曾写的「无 project-level skill 概念」是错的。
        return cmd

    def print_flags(self, streaming: bool) -> list:
        # --format json 输出 raw JSON events；流式与非流式一致，解析端按 JSONL 处理
        return ["--format", "json"]

    def resume_flag(self, session_id: str) -> list:
        return ["--session", session_id]

    def prompt_args(self, prompt: str) -> tuple[list, bool]:
        # run 用位置参数传 message
        return ([prompt], False)

    def format_stream_event(self, line: str) -> str:
        return _format_json_event(line)

    def parse_result(self, raw: str) -> Optional[dict]:
        text = raw.strip()
        if not text:
            return None
        result_parts = []
        session_id = None
        parsed_any = False
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            parsed_any = True
            rendered = _extract_event_text(value)
            if rendered:
                result_parts.append(rendered)
            session_id = _extract_session_id(value)
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                parsed_any = True
                rendered = _extract_event_text(event)
                if rendered:
                    result_parts.append(rendered)
                sid = _extract_session_id(event)
                if sid:
                    session_id = sid
        if not parsed_any:
            return {"result": text, "session_id": None, "subtype": None, "errors": []}
        return {
            "result": "\n".join(result_parts).strip() or text,
            "session_id": session_id,
            "subtype": None,
            "errors": [],
        }


class PiAdapter(CliAdapter):
    """Pi CLI（``pi``，@earendil-works/pi-coding-agent）。

    依据官方文档（pi.dev/docs）核实：
      - 命令名 pi；无头模式 = -p/--print（打印响应后退出）
      - 模型：--model <pattern>，支持 provider/id（如 openai/gpt-4o）与 :thinking
        后缀；另有 --provider <name> 指定厂商（anthropic/openai/google 等）
      - prompt：位置参数 messages...
      - 输出：--mode json（所有事件输出为 JSON lines）；-p 为纯文本最终响应
      - 续接：--session <path|id>（-c/--continue 续最近、-r/--resume 浏览选择）
      - 免审批/信任：--approve/-a（本运行信任项目本地文件）
      - 认证：--api-key <key> 覆盖环境变量；或标准厂商环境变量（多厂商）
      - 无 max-turns 类轮次 flag（文档未见）
    """

    id = "pi"
    display_name = "Pi"
    command = "pi"
    # 多厂商 CLI，无单一认证 env；sk 注入按模型厂商走 api_key_env_for_model 映射
    api_key_env = None

    # pi 本体没有 MCP：0.84.2 的 dist/ 里 `mcpServers` 命中数为 0，配置文件是外部
    # 扩展 pi-mcp-adapter 读的。所以这里不能写常量 True —— 扩展没装时我们写下去的
    # .pi/mcp.json 是个死文件，而角色那边只会看到「工具不见了」。
    @property
    def supports_mcp(self) -> bool:
        return _mcp_capability(self.command, _probe_pi_mcp)

    def mcp_unwired_reason(self) -> str:
        """为假的原因是**本机没装扩展**，不是 AICU 没接线 —— 别让用户去等我们。

        上面那条 ``supports_mcp`` 判否时，AICU 这边的注入路径其实是**齐的**：配置该怎么写
        已经写在 ``mcp_workspace_config`` 里，扩展一装上就生效。缺的只有那个扩展。
        沿用基类默认句，用户读到的是关于 AICU 的假话，于是去做错误的动作 —— 等我们接、
        或者干脆换一个 CLI，而真正该做的是一条 ``pi install``。

        措辞要落在**用户能做的那一步**上，所以把命令一起写出来：这句话会原样嵌进
        ``"CLI 'pi' 的 MCP ___，但本次运行声明了 MCP server。"``，其余部分由守卫补。
        """
        return "需要先安装 pi-mcp-adapter 扩展（pi install npm:pi-mcp-adapter）"

    # pi 的项目级 MCP 配置。配置文件优先级（pi-mcp-adapter README）：
    # 全局 ~/.config/mcp/mcp.json > <Pi agent dir>/mcp.json > .mcp.json > .pi/mcp.json，
    # 越靠后优先级越高 —— 选 .pi/mcp.json 是因为它压得住用户可能已有的 .mcp.json，
    # 而 .mcp.json 是**通用**文件名，很可能被别的工具占用。
    #
    # 注意这不是「多写一份以防万一」：pi 的 MCP 说明里 .pi/mcp.json 就是 Pi 自己的
    # 项目层（`/mcp disable` 写的也是它）。
    mcp_workspace_config = (".pi", "mcp.json")

    # 项目级 skill：``<cwd>/.pi/skills``（``CONFIG_DIR_NAME=".pi"`` 与
    # ``projectSkillsDir = resolve(cwd, CONFIG_DIR_NAME, "skills")``）。
    # pi 是这一轮最稳的一家：它的 ``canonicalizePath`` 明确**解软链**再查重，说明
    # 官方就是按「链接进来的 skill」设计的，不像别家要赌扫描器跟不跟软链。
    skills_workspace_dir = (".pi", "skills")

    def skills_workspace_trust(self, workspace) -> Optional[Path]:
        """pi 还有一道项目信任门禁，光把目录摆好**不会生效** —— 见 ``_write_pi_trust``。

        这是本适配器与其余各家的唯一区别：别家「目录在 = 生效」，pi 是「目录在 ∧ 授过信
        = 生效」。所以这一条不是可选增强，而是这条接线**能不能通**的前提。
        """
        return _write_pi_trust(workspace)

    def api_key_env_for_model(self, model: Optional[str]) -> Optional[str]:
        return api_key_env_for_vendor(vendor_of_model(model))

    def model_arg(self, model: Optional[str], *, api_base_url: Optional[str]) -> Optional[str]:
        # pi 的 --model 支持 provider/id；（--provider 是等价的另一种写法，
        # 用 id 前缀可以让模型名自解释，也少一个 flag）
        return f"{_CUSTOM_PROVIDER_ID}/{model}" if api_base_url and model else model

    managed_slot = True

    def provision(
        self, *, api_base_url: Optional[str], api_key_env: Optional[str], model: Optional[str]
    ) -> None:
        # api_base_url 为 None = 本次不由 AICU 供端点（判不出厂商的模型名），
        # 撤掉上次写进 models.json 的 aicu provider，不留过期值。
        if not api_base_url:
            _release_pi_config()
            return
        # 同 codex / qwen：models.json 里 apiKey 引用的变量名必须与注入 sk 的变量名一致，
        # 两边同源取 api_key_env_for_model；显式传入的 api_key_env 只作兜底。
        env_key = (
            api_key_env
            if api_key_env == "OPENROUTER_API_KEY"
            else self.api_key_env_for_model(model) or api_key_env
        )
        _provision_pi_config(api_base_url, env_key, model)

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool,
        mcp_servers: Optional[list] = None,
    ) -> list:
        cmd = [self.command]
        if model:
            cmd.extend(["--model", model])
        if skip_permissions:
            cmd.append("--approve")
        # ``enable_project_skills`` 是 claude 系的 argv 通道；pi 的 skill 由 AICU 落到
        # <cwd>/.pi/skills/ 生效。曾写的「无 project-level skill 概念」是错的 ——
        # pi 不但有，还是唯一明确支持软链的一家。
        return cmd

    def print_flags(self, streaming: bool) -> list:
        # --mode json 输出所有事件为 JSON lines（流式）；-p 打印最终响应后退出（非流式）
        if streaming:
            return ["--mode", "json"]
        return ["-p"]

    def resume_flag(self, session_id: str) -> list:
        return ["--session", session_id]

    def prompt_args(self, prompt: str) -> tuple[list, bool]:
        # 位置参数传 message
        return ([prompt], False)

    @staticmethod
    def _assistant_text(event: dict) -> str:
        """取事件里的 assistant 文本；非 assistant 的一律返回空串。

        pi 的 ``--mode json`` 会把用户消息也回显成 message_start / message_end
        （实测 message.role == "user"、content 就是 prompt），直接复用通用提取器
        会把用户的问题当成模型的回复。
        """
        message = event.get("message")
        if isinstance(message, dict):
            if message.get("role") != "assistant":
                return ""
            return _extract_event_text(message)
        return _extract_event_text(event)

    def format_stream_event(self, line: str) -> str:
        return _format_json_event(line, self._assistant_text)

    def parse_result(self, raw: str) -> Optional[dict]:
        text = raw.strip()
        if not text:
            return None
        # -p 纯文本模式：整体即最终响应
        result_parts = []
        session_id = None
        parsed_any = False
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            parsed_any = True
            rendered = self._assistant_text(value)
            if rendered:
                result_parts.append(rendered)
            session_id = _extract_session_id(value)
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                parsed_any = True
                rendered = self._assistant_text(event)
                # 同一段回复会在 message_end / turn_end 等事件里重复出现，去重后
                # 再拼接，否则一段回复会被列好几遍。
                if rendered and rendered not in result_parts:
                    result_parts.append(rendered)
                sid = _extract_session_id(event)
                if sid:
                    session_id = sid
        if not parsed_any:
            return {"result": text, "session_id": None, "subtype": None, "errors": []}
        return {
            "result": "\n".join(result_parts).strip() or text,
            "session_id": session_id,
            "subtype": None,
            "errors": [],
        }


class GeminiAdapter(CliAdapter):
    """Gemini CLI（``gemini``，@google/gemini-cli）。

    依据官方文档核实：
      - 命令名 gemini；无头模式 = -p/--prompt <prompt>
      - 模型：--model/-m
      - 输出：--output-format text|json（headless 页仅列 text/json；README 曾列 stream-json，
        保守统一用 json，解析端按单条 JSON 对象处理）
      - 免审批：--yolo/-y（自动批准所有操作；另有 --approval-mode 细粒度控制）
      - 续接：--resume <session_id>（checkpointing 机制；本环境未实测）
      - 认证：GEMINI_API_KEY（Vertex AI 用 GOOGLE_API_KEY + GOOGLE_CLOUD_PROJECT）
      - 无 max-turns 类轮次 flag（文档未见）
    """

    id = "gemini"
    display_name = "Gemini CLI"
    command = "gemini"
    api_key_env = "GEMINI_API_KEY"

    supports_mcp = True
    # 项目级设置文件（gemini 的 getWorkspaceSettingsPath() = <project>/.gemini/settings.json）。
    # 走基类的通用实现即可 —— 内容就是标准的 {"mcpServers": {...}}。
    mcp_workspace_config = (".gemini", "settings.json")
    # **必须是 httpUrl**：gemini 的 server 条目没有 type 字段，传输由键名决定 ——
    # ``url`` 是 SSE（旧的 HTTP+SSE 传输），``httpUrl`` 才是 Streamable HTTP。
    # 写成 ``url`` 不报错、不警告，只是连到一条我们并不支持的传输上。
    mcp_url_key = "httpUrl"

    # 项目级 skill：``<project>/.gemini/skills``（全局那份是 ``~/.gemini/skills``，也是
    # 官方的 skill 安装位置记录点，所以项目级这个路径是同一套命名，不是猜的）。
    # gemini 另有 ``skills.disabled`` 设置用于禁用，但那与我们「装上就启用」的语义相反，
    # 不需要写 —— 不写就是不禁用。
    skills_workspace_dir = (".gemini", "skills")

    def mcp_flags(self, mcp_servers: list) -> list:
        """把本次下发的 server 名逐个塞进允许名单。

        为什么不写进项目 settings.json 的 ``mcp.allowed``：那个值是**各作用域取交集**
        （getConsolidatedAllowedMcpServers：任何作用域没定义就是 undefined，定义了就取
        交集）。而唯一会被名单挡住的场景，恰恰是「用户级 ~/.gemini/settings.json 已经有
        一份不含我们的名单」—— 交集只会更小，项目级怎么写都加不回来，写了等于没写。

        ``--allowed-mcp-server-names`` 不同：命令行一给就直接覆盖 allowed 与 excluded。
        代价是本次运行里用户自己配的其它 MCP server 会被挡掉 —— 对一次 AICU 无头运行是
        可接受的，因为角色要的就是这几个 server，而且**挡掉比静默失效好**：被 exc 挡掉
        时 agent 拿不到工具、绕路或报一个与 MCP 无关的错，正是预检要消除的那类失败。
        """
        flags: list = []
        for server in mcp_servers:
            # gemini 的 flag 声明是 nargs:1 + coerceCommaSeparated，重复给或逗号分隔都收。
            # 逐个给而不拼逗号：server 名里可能有逗号，拼串会被切错。
            flags.extend(["--allowed-mcp-server-names", server.name])
        return flags

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool,
        mcp_servers: Optional[list] = None,
    ) -> list:
        cmd = [self.command]
        if model:
            cmd.extend(["--model", model])
        if skip_permissions:
            # --yolo 自动批准所有操作（含工具调用），等价于 Claude Code 的 skip_permissions
            cmd.append("--yolo")
        # Gemini headless 模式还要求显式信任当前工作目录。
        cmd.append("--skip-trust")
        # ``enable_project_skills`` 是 claude 系的 argv 通道；gemini 的 skill 由 AICU 落到
        # <project>/.gemini/skills/ 生效（它另有 <project>/.gemini/settings.json 那条
        # MCP 路径，两者同一容器）。曾写的「无 project-level skill 概念」是错的。
        return cmd

    def print_flags(self, streaming: bool) -> list:
        # --output-format json 输出单个 JSON 对象（response/sessionId）；headless 页仅列
        # text/json（stream-json 未在该页出现），统一用 json 避免依赖未确认的 stream-json。
        return ["--output-format", "json"]

    def resume_flag(self, session_id: str) -> list:
        return ["--resume", session_id]

    def prompt_args(self, prompt: str) -> tuple[list, bool]:
        # 无头模式经 --prompt 传 prompt
        return (["--prompt", prompt], False)

    def build_env(
        self,
        base_env: dict,
        *,
        api_base_url: Optional[str],
        api_key_env: Optional[str],
        model: Optional[str] = None,
        mcp_servers: Optional[list] = None,
    ) -> dict:
        # Gemini API / Vertex 各有独立 endpoint，无统一 base URL env；api_base_url
        # 未接线（直连官方）
        return self._env_with_api_key(base_env, api_key_env)

    def format_stream_event(self, line: str) -> str:
        return _format_json_event(line)

    def parse_result(self, raw: str) -> Optional[dict]:
        text = raw.strip()
        if not text:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return {"result": text, "session_id": None, "subtype": None, "errors": []}
        if not isinstance(value, dict):
            return {"result": text, "session_id": None, "subtype": None, "errors": []}
        result = _extract_event_text(value)
        return {
            "result": str(result).strip() or text,
            "session_id": _extract_session_id(value),
            "subtype": None,
            "errors": [],
        }


class CursorAdapter(CliAdapter):
    """Cursor CLI（``agent``）。

    依据官方文档核实：
      - 命令名 agent；无头/打印模式 = -p（``agent -p "task"``）
      - 模型：--model（如 --model "gpt-5"）
      - 输出：--output-format text（官方页仅列 text，无 json 流式）
      - 续接：--continue（续最近）或 --resume <chat-id>（指定会话）；另有子命令
        agent ls / agent resume
      - 模式：--mode plan|ask；另有 --plan / --sandbox <mode>
      - 免审批：官方页未列出 skip-permissions 类 flag，保守不加（skip_permissions 忽略）
      - 认证：官方页未列出 API key 环境变量（走 Cursor 账号 OAuth 认证）；api_key_env=None，
        不注入 sk
      - 无 max-turns 类轮次 flag（官方页未列出）
    """

    id = "cursor"
    display_name = "Cursor CLI"
    command = "agent"
    # Cursor 走账号 OAuth 认证、经 Cursor 后端路由模型，无单一厂商 API key env
    api_key_env = None
    # agent 是通用命令名，可能被无关工具占用：--help 含 cursor 才是 Cursor CLI
    install_probe = (["--help"], ["cursor"], [])

    supports_mcp = True
    # 项目级 MCP 配置（官方文档：project-specific tools 放 <project>/.cursor/mcp.json，
    # 全局那份在 ~/.cursor/mcp.json）。内容就是标准的 {"mcpServers": {...}}。
    mcp_workspace_config = (".cursor", "mcp.json")

    # 项目级 skill：``<project>/.cursor/skills``。cursor 同时读 ``.claude/skills`` 与
    # ``.agents/skills``，取自己的 `.cursor` 是因为同名的 MCP 配置也走这条路径容器 ——
    # 一个 CLI 的所有项目级配置待在同一个目录下，用户翻起来只找一处。
    skills_workspace_dir = (".cursor", "skills")

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool,
        mcp_servers: Optional[list] = None,
    ) -> list:
        cmd = [self.command]
        if model:
            cmd.extend(["--model", model])
        # 免审批 flag 官方未列出，保守不加（避免命令报错）
        #
        # 但这**不**包括 --approve-mcps：它跟免审批无关，是 MCP 专属的批准开关
        # （`agent --help` 实测存在：「Automatically approve all MCP servers」）。
        # 项目级 .cursor/mcp.json 里的 server 默认要人工过一遍（`agent mcp enable` 才进
        # 已批准名单），而 AICU 跑无头、没人点批准 —— 不加等于下发一个永远不生效的工具。
        # 只在真有 mcp 时加：老版本 agent 不认这个 flag 时，普通运行不受影响。
        if mcp_servers:
            cmd.append("--approve-mcps")
        return cmd

    def print_flags(self, streaming: bool) -> list:
        # -p 打印模式（无头）；官方仅确认 --output-format text（默认），不设 json 流式
        return ["-p"]

    def resume_flag(self, session_id: str) -> list:
        return ["--resume", session_id]

    def prompt_args(self, prompt: str) -> tuple[list, bool]:
        # -p 后跟位置参数 prompt
        return ([prompt], False)

    def parse_result(self, raw: str) -> Optional[dict]:
        text = raw.strip()
        if not text:
            return None
        # 纯文本输出（--output-format text），整体即最终响应
        return {"result": text, "session_id": None, "subtype": None, "errors": []}


class AntigravityAdapter(GenericExecAdapter):
    """Antigravity CLI（``agy``）。**MCP 未接入**，下面是实测结论，别再试一遍。

    原先只是 ``GenericExecAdapter`` 的一个实例（flags 未经实测，按常见约定猜测），
    升格为类只是为了让「为什么没接 MCP」这件事有个落脚的 docstring，
    **构造参数逐字照抄，行为与升格前完全一致**。

    本机 1.2.3 实测（隔离 HOME + 隔离 workspace，每条都是 A/B 对照）：

      - 官方文档写的 ``<workspace>/.agents/mcp_config.json`` 在 1.2.3 的二进制里
        **一次都没出现**（``strings agy | grep -c`` 得 0）。真实存在的只有
        ``~/.gemini/config/mcp_config.json``（全局）和 ``plugins/<name>/``（插件）。
      - 插件目录**放在 workspace 里不生效**。同样两个文件
        （``plugin.json`` + ``mcp_config.json``），放在 ``$HOME/.gemini/config/plugins/aicu/``
        时 agy 会把 server 真拉起来（``$HOME/.gemini/antigravity-cli/mcp/aicu_probe/
        probe_echo.json`` 缓存了工具 schema，那是完成 ``tools/list`` 握手才可能有的），
        放在 ``<workspace>/.agents/plugins/aicu/`` 时**什么都不发生** —— 连 mcp/ 目录
        都不建。``agy plugin validate`` 对两者都回 ``[ok]``，所以**不能用 validate
        当验收手段**：它只证明格式合法，不证明会被加载。
      - 全局那条路 AICU 走不了：并发节点会互相覆盖，还会污染用户自己的 agy 配置。
        也没有可重定位的配置根（没有 ``ANTIGRAVITY_HOME`` 之类的 env；只有 HOME
        能搬，而那会把用户的登录态一起搬走）。

    留了一条没验到的岔路：项目级插件也许要等**认证之后**、工作区真正打开时才会被发现
    （agy 有 ``--project`` / ``~/.gemini/config/projects/`` 这套概念）。**这条路 2026-09-17
    走死了 —— 不是没验，是登不进去**：人工在终端里跑 ``agy -p hi``，把浏览器给的授权码
    粘回同一个进程（PKCE 要求 verifier 与码在同一个进程里，这一步本身是对的），
    token 也换回来了，最后卡在 eligibility：

        error: Eligibility check failed: Your current account is not eligible for
        Antigravity, because it is not currently available in your location.

    这是**账号/地区级**的拒绝，跟授权码、网络、配置都无关，换多少次码都一样。
    结论：这台机器上 agy 根本没法参与 AICU 的无头编排，所以就算接上也验不了。
    哪天换一个 Antigravity 可用地区的账号，先跑
    ``strings agy | grep -c '.agents/plugins'`` 再决定要不要重新考虑这条路。
    """

    def __init__(self):
        super().__init__("antigravity", "Antigravity", "agy", api_key_env="GEMINI_API_KEY")


class GrokAdapter(GenericExecAdapter):
    """Grok CLI（``grok``，@xai-official/grok）。

    原先只是 ``GenericExecAdapter`` 的一个实例；这里补了两块 —— headless flags
    与 MCP 注入 —— 两块的依据都是本机 1.0.5 实测或二进制里自带的那份用户手册。

    **headless flags**（``--json`` 那条是实测出来的：``grok --json -p hi`` 直接
    ``error: unexpected argument '--json' found``，exit 2，命令根本起不来）：

      - 无头入口是 ``-p/--single``。裸位置参数 ``grok "提问"`` 在 ``--help`` 里写作
        ``[PROMPT] Initial prompt for the interactive session`` —— 那是**交互式 TUI**
        的初始提问，无 TTY 时实测 ``Error: Device not configured (os error 6)``。
      - 输出格式是 ``--output-format plain|json|streaming-json|streaming-messages-json``，
        没有 ``--json`` 这种单段写法。两种要用的形状：

        ``json`` —— 单个对象，字段 ``text`` / ``stopReason`` / ``sessionId`` /
        ``requestId``；失败时是 ``{"type":"error","message":...}``。

        ``streaming-json`` —— NDJSON，每行一个 ``type`` 标签对象；正文是若干条
        ``{"type":"text","data":...}``，``sessionId`` 只在**末条** ``{"type":"end"}`` 上。

        两者都不是基类认的形状，所以 ``parse_result`` 与 ``format_stream_event``
        都覆盖了：基类找顶层 ``result``/``output`` 之类的键、找 ``session_id``
        （grok 是驼峰 ``sessionId``），照抄的后果是「回复成了整段原始 JSON」+
        「``--resume`` 永远拿不到会话 id、多轮对话静默退化成每次全新会话」。
      - 免审批是 ``--always-approve``（``--help`` 里明列；``--yolo`` 是它的别名，
        官方 headless 示例两种写法都有）。

    **MCP：实现留着了，但当前不接线**（``supports_mcp = False``）。走项目级
    ``.grok/config.toml`` 的 ``[mcp_servers.<name>]``，实测过这些：

      - ``grok mcp list`` 在该目录下认出了项目层的 server（标注 ``(project)``），
        且从 cwd 一直向上找到 git 根都会找 —— **没有 git 仓库的普通目录也认**。
      - 但**光有配置文件不够**：``grok mcp doctor`` 报
        ``✗ folder untrusted (repo-local (project-scoped) server not started for an
        untrusted folder)``。AICU 每次跑的都是全新目录，从来不在信任库里，
        所以不额外加 flag 的话就是「写了一份永远不生效的配置」。
      - 加 ``--trust`` 之后同一份配置变成 ``✓ server started / ✓ handshake OK /
        ✓ 1 tools discovered``。``--trust`` 没列在 ``grok --help`` 里（隐藏 flag），
        但 ``grok --trust --version`` 能过解析 —— 别照 binary 里的字符串猜名字。
        （``--trust-folder`` 是它的**别名**，本机 1.0.5 实测同样能过解析、也能带 ``=``
        取值；先前误记成「会报 unexpected argument」，此处已更正。要判断一个新拼写
        认不认，拿 ``--zzzz`` 做对照组 —— clap 对不认识的参数是严格报错的。）

    **为什么验到这个程度还不接线**：上面验的是**CLI 这一侧**（配置认不认、server 起没起、
    握手通不通）。再往上一层 —— 模型真的去调那个工具、把回显写进产物 —— **没测过**，因为
    本机没有 ``XAI_API_KEY``（``provider_presets`` 里 xai 那条 ``available: false`` 正是
    这个原因），认证那条路根本走不到模型。而这个仓库对「MCP 已接入」的口径是
    **端到端跑通**（见 ``tools/mcp_echo_server.py`` 的说明），不是「配置写得对」。

    所以按 kimi 同一套处理：**实现与单测都留着，先把 ``supports_mcp`` 关掉**。
    关掉之后它在 ``mcp_enabled_clis()`` 里消失、前端标「（MCP 未接入）」、角色声明到
    它身上时守卫照常报错 —— 宁可报错说没接线，也不下发一份没人验证过的配置。
    等有 key 后跑通一次端到端，把下面这行改回 ``True`` 即可。
    """

    def __init__(self):
        super().__init__(
            "grok", "Grok Build", "grok", api_key_env="XAI_API_KEY",
            prompt_flag="-p",
            # 基类那个单段式的 --json 在 grok 上不存在，真正的开关在 print_flags
            # 覆盖里（--output-format 是两段）。留 None 免得有人以为它还用得上。
            json_flag=None,
        )

    # 见类 docstring：CLI 侧验过（doctor 握手 OK），模型侧没验过（本机无 XAI_API_KEY）。
    # 端到端跑通后改回 True 即可，实现与单测都在。
    supports_mcp = False
    mcp_workspace_config = (".grok", "config.toml")

    def _mcp_workspace_payload(self, mcp_servers: list) -> str:
        return _grok_mcp_toml(mcp_servers)

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool,
        mcp_servers: Optional[list] = None,
    ) -> list:
        cmd = super().build_base_command(
            model=model,
            skip_permissions=skip_permissions,
            enable_project_skills=enable_project_skills,
            mcp_servers=mcp_servers,
        )
        # 免审批：headless 下没人能点确认，与 qwen / gemini 的 --yolo 同义
        # （官方文档：--always-approve 别名 --yolo，等同 --permission-mode
        # bypassPermissions）。
        if skip_permissions:
            cmd.append("--always-approve")
        # 只在真有 mcp 时加：没有下发的 server 就没人需要这份信任，而 --trust 会把
        # 当前目录写进 grok 的信任库（持久化），没有理由为一次普通运行留下这条记录。
        # supports_mcp 那一半是必需的：关掉接线时基类不会落盘配置文件，此时再加 --trust
        # 就是白往 grok 的信任库里写一条记录，换不来任何东西。
        if mcp_servers and self.supports_mcp:
            cmd.append("--trust")
        return cmd

    def print_flags(self, streaming: bool) -> list:
        """无头输出格式 —— 基类默认的 ``--json`` 对 grok 是个不存在的参数（实测 exit 2）。"""
        return ["--output-format", "streaming-json" if streaming else "json"]

    def format_stream_event(self, line: str) -> str:
        """``streaming-json`` 每行是 ``{"type": ..., "data"/"message": ...}``。

        基类找的是顶层字符串键 ``content``/``text``/``message``/``output``，对 grok
        一条都命中不了（正文那个键叫 ``data``，而 ``type`` 的值才叫 ``text``），
        照抄基类的结果是流式全程空白。
        """
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line  # 非 JSON 的原始输出（启动日志、panic）照常显示
        if not isinstance(event, dict):
            return ""
        kind = event.get("type")
        if kind == "text":
            data = event.get("data")
            return data if isinstance(data, str) else ""
        if kind == "error":
            # 错误得让人看见：grok 的失败收尾就是 {"type":"error","message":...}
            message = event.get("message")
            return f"[grok error] {message}" if message else ""
        # thought / tool_call / tool_call_update / usage / plan / end：没有可展示的正文
        return ""

    def parse_result(self, raw: str) -> Optional[dict]:
        """归一化 ``json``（单对象）与 ``streaming-json``（NDJSON）两种输出。

        取不到任何 JSON 事件时返回 None，由调用方按纯文本兜底。
        """
        events = _result_events(raw)
        if not events:
            return None
        last = events[-1]
        if last.get("type") == "error":
            message = last.get("message")
            if isinstance(message, str) and message:
                return {
                    "result": message,
                    "session_id": last.get("sessionId") or last.get("session_id"),
                    "subtype": last.get("subtype"),
                    "errors": [message],
                }
        chunks = [
            event["data"]
            for event in events
            if event.get("type") == "text" and isinstance(event.get("data"), str)
        ]
        text = last.get("text")
        # streaming-json 的末条是 {"type":"end"}，正文只能靠上面拼；
        # json 的末条就是结果对象本身，正文在 text。
        result = text if isinstance(text, str) and text else "".join(chunks) or raw.strip()
        return {
            "result": result,
            # 驼峰 sessionId：基类找的是 session_id / conversation_id，两个都不是它。
            "session_id": last.get("sessionId") or last.get("session_id"),
            "subtype": last.get("subtype"),
            "errors": _coerce_errors(last.get("errors")),
        }


# 从设置页的 CLI 下拉里隐藏的 CLI。adapter 与探测逻辑一律保留，只是不出现在列表里，
# 将来官方支持了把 id 从这里删掉即可放回。
#
#   gemini：官方确认只支持 Google 自家的模型与端点。docs/reference/configuration.md 里
#     能改端点的只有 GOOGLE_GEMINI_BASE_URL 与 GOOGLE_VERTEX_BASE_URL（都是 Google 自己）；
#     本机 v0.56.0 的 bundle 里 `chat/completions` 与 `OPENAI_COMPATIBLE*` 命中数为 0；
#     官方仓库「Support Open-source LLM providers alongside Gemini」issue 至今仍 open。
#     于是它既接不了别家模型，跨厂商改道对它也没有意义，留在下拉里只会是选不动的项。
#     模型下拉由 list_clis() 提供数据，所以这里摘掉，前端一行都不用改。
#
#   cursor：Cursor Agent CLI（`agent`）不是模型客户端，是 Cursor 自己后端的客户端，
#     因此没有「换个厂商端点」这回事。本机实测（2026.08.11-e8db854）：
#       - `--api-key` 要的是 Cursor 自己的 key（CURSOR_API_KEY），不是厂商 key；
#       - `-e/--endpoint` 默认 https://api2.cursor.sh，换的是 **Cursor 协议的后端**，
#         不是换成 OpenAI/Anthropic 协议。把它指向本地假服务器抓包，第一条请求是
#         `POST /auth/exchange_user_api_key`（application/json）—— 这是 Cursor 私有的
#         握手路径，厂商端点不可能认。所以它接不了 deepseek/智谱这些别家模型。
#       - `--list-models` 直接回 `Authentication required. Run 'agent login'`，
#         当前账号状态是 Not logged in；而 CursorAdapter.api_key_env = None，
#         设置页根本没有可填的 key 位。
#     结论：与 gemini 同类——既接不了别家模型，也没法在设置页配起来。
#     若将来有人有 Cursor 账号并想用它：跑 `agent login`，把 id 从这里删掉即可放回。
#
#   antigravity：**登不进去，不是「没试过」**。2026-09-17 人工在终端跑 `agy -p hi`，
#     把浏览器给的授权码粘回同一个进程（PKCE 要求 verifier 与码同进程，这步是对的），
#     token 都换回来了，最后卡在 eligibility：
#       error: Eligibility check failed: Your current account is not eligible for
#       Antigravity, because it is not currently available in your location.
#     这是账号/地区级的拒绝，跟码、网络、配置都无关 —— 换号之前怎么试都一样，
#     用户手动开 agy 也进不去。它也因此接不了 MCP（项目级插件那条路没法验，
#     见 AntigravityAdapter）。同理，`google` 预设在 cli_models.json 里已置
#     `available: false`（两个 Google 系 CLI 都不可用，那个预设没有落脚点）。
_HIDDEN_CLI_IDS = ("gemini", "cursor", "antigravity")


# 优先级顺序即默认选择顺序（claude > codebuddy > codex > kimi > qwen > opencode > pi > antigravity > grok）。
# 表里保留 gemini / cursor / antigravity 以维持既有下标不变，但这三项在 _HIDDEN_CLI_IDS 里，不进下拉。
_ADAPTERS: list[CliAdapter] = [
    ClaudeCodeAdapter("claude", "Claude Code", "claude"),
    # CodeBuddy Code（腾讯）：Claude Code 协议克隆，复用 ClaudeCodeAdapter，仅覆盖认证 env
    CodeBuddyAdapter(),
    CodexAdapter(),
    KimiAdapter(),
    QwenAdapter(),
    OpenCodeAdapter(),
    PiAdapter(),
    GeminiAdapter(),
    CursorAdapter(),
    # Antigravity 官方安装命令名为 agy，内部 adapter id 保持 antigravity 兼容已有配置。
    AntigravityAdapter(),
    # Grok：官方 README 确认命令名为 grok、认证走浏览器（非简单 API key env）；
    # 无头 flags 未公开，best-guess 部分随类定义（见 GrokAdapter）。
    GrokAdapter(),
]


def get_adapter(cli_id: str) -> CliAdapter:
    """按 id 取 adapter，未知 id 抛 ValueError。"""
    for adapter in _ADAPTERS:
        if adapter.id == cli_id:
            return adapter
    raise ValueError(f"未知 CLI: {cli_id!r}")


def detect_installed() -> list[str]:
    """返回已安装的 CLI id 列表（按优先级顺序）。"""
    return [adapter.id for adapter in _ADAPTERS if adapter.is_installed()]


def mcp_enabled_clis() -> list[str]:
    """已接入 MCP 下发、**且用户在下拉里选得到**的 CLI id（按注册顺序）。

    报错文案要用它告诉用户「换成哪个 CLI 才行」。**必须从 adapter 派生**：写死一份
    名单就等于把同一份事实维护两遍，接一个新 CLI 时漏改的那一处会变成一句假话。

    ``_HIDDEN_CLI_IDS`` 要一并滤掉：gemini / cursor 现在是支持 MCP 的，但它们在设置页
    的下拉里不出现 —— 报错里推荐一个用户根本选不到的 CLI，等于把人指进死胡同。
    （antigravity 也在隐藏名单里，但它本来就没接 MCP，滤不滤都一样；它们自己的
    adapter 仍然可用，显式指定 cli_name 时照常生效。）
    """
    return [
        adapter.id
        for adapter in _ADAPTERS
        if adapter.supports_mcp and adapter.id not in _HIDDEN_CLI_IDS
    ]


def skills_enabled_clis() -> list[str]:
    """已接入 skills 下发、**且用户在下拉里选得到**的 CLI id（按注册顺序）。

    与 ``mcp_enabled_clis`` 同一套纪律，同样的两个理由：从 adapter 派生而不是写死名单；
    滤掉 ``_HIDDEN_CLI_IDS``，免得把用户指到一个他选不到的 CLI 上。

    差别只在于判据 —— 这里问的是 ``skills_workspace_dir is not None``（路径即能力，
    见基类该属性的说明），而不是 ``supports_mcp`` 那个可以独立存在的布尔。
    """
    return [
        adapter.id
        for adapter in _ADAPTERS
        if adapter.skills_workspace_dir is not None
        and adapter.id not in _HIDDEN_CLI_IDS
    ]


def detect_default() -> str:
    """按优先级返回第一个已安装的 CLI id，找不到抛异常。"""
    for adapter in _ADAPTERS:
        if adapter.is_installed():
            return adapter.id
    raise RuntimeError(
        "未找到可用的 CLI 工具，请安装一个受支持的 AI Coding CLI\n"
        "  详情:  见 cli_adapters.py 和 docs/INSTALL.md"
    )


def list_clis() -> list[dict]:
    """返回全部已注册 CLI 的描述（含安装状态），供前端工具切换下拉使用。

    每项: {id, display_name, installed, priority, models, supports_mcp,
    mcp_unwired_reason}，按优先级顺序排列。_HIDDEN_CLI_IDS 里的 CLI 不出现在结果中
    （adapter 本身仍可用 get_adapter 取到）。

    注意 priority 取 _ADAPTERS 里的下标而非结果下标：隐藏项不影响其余 CLI 的
    相对优先级，将来放回时也不用重排。

    ``supports_mcp`` 直接取自 adapter，前端据此在选项上标注「MCP 未接入」。**刻意不在
    前端硬编码名单** —— 那等于把同一份事实维护两遍，将来接一个新 CLI 就必然漏改一处。

    ``mcp_unwired_reason`` 是同一件事再往前一步：接不了时把**原因**也一并给出
    （接得了则为 None，那种情况下这句话没有意义）。前端要在「角色声明了 mcp + 选中的
    CLI 接不了」时给用户一个提示，而「为什么接不了」会随成因变 —— pi 今天是「装个
    扩展就行」，agy / grok 是「AICU 还没接」。让前端各拼一份，后端改了口径前端不会跟着
    变，用户读到的就是一句与实际不符的话，正是上面那条注释要避免的坑。
    """
    result = []
    for index, adapter in enumerate(_ADAPTERS):
        if adapter.id in _HIDDEN_CLI_IDS:
            continue
        # 取一次存下来：supports_mcp 在 pi 上是个探测（要起子进程问 `pi list`），
        # 判真判假各问一遍是白跑一趟。
        supports_mcp = adapter.supports_mcp
        result.append({
            "id": adapter.id,
            "display_name": adapter.display_name,
            "installed": adapter.is_installed(),
            "priority": index,
            "models": list(adapter.known_models),
            "supports_mcp": supports_mcp,
            "mcp_unwired_reason": None if supports_mcp else adapter.mcp_unwired_reason(),
        })
    return result
