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

新增一个 CLI 只需新增一个 adapter（或一条 GenericExecAdapter spec），
不触碰 ClaudeCodeAgent 的编排逻辑（子进程/流式/超时/续接/重试）。

各 CLI 的已知模型清单（供前端下拉候选）集中维护在 cli_models.json。

flags 依据（截至 2026-08）：
  - claude：Claude Code 协议
  - kimi：官方命令参考 https://moonshotai.github.io/kimi-cli/en/reference/kimi-command
  - qwen：官方文档 https://qwenlm.github.io/qwen-code-docs（headless 页）已核实
  - opencode / pi / codebuddy / gemini / cursor：官方文档已核实（见各 adapter docstring）
  - codex / antigravity / grok：最佳猜测（未实测）
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from runtime.credentials import (
    api_key_env_for_vendor,
    resolve_internal_username,
    resolve_sk_for_model,
    vendor_of_model,
)

# 各 CLI 的已知模型清单集中维护在 cli_models.json（便于后续增删模型）。
_MODELS_FILE = Path(__file__).with_name("cli_models.json")


def _load_known_models() -> dict:
    """从 cli_models.json 读取各 CLI 的已知模型清单；缺失/损坏时回退空 dict。"""
    try:
        data = json.loads(_MODELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(cli_id): [str(model) for model in models]
        for cli_id, models in data.items()
        if isinstance(models, list)
    }


KNOWN_MODELS = _load_known_models()


def list_provider_presets() -> list[dict]:
    """Return public provider metadata for the settings UI; never include secrets."""
    try:
        data = json.loads(_MODELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    presets = data.get("provider_presets", [])
    return [item for item in presets if isinstance(item, dict) and item.get("id")]


# ---- 跨厂商模型：直连厂商对应协议的原生端点 ------------------------------------
#
# 背景：每个 CLI 只认自己那一种协议（claude 只会说 Anthropic Messages，
# codex 只会说 OpenAI Responses，kimi 只会说 OpenAI Chat），而多数厂商同时提供
# 这三种协议的原生端点。于是「claude 用上 GLM」不需要任何协议翻译，
# 只要把 base_url 指向该厂商的 Anthropic 端点即可。
# 协议对应关系表本身是纯数据，维护在 cli_models.json 的 protocol_endpoints 里。

# 键是 CLI id，值是 (原生厂商, 该 CLI 说的协议)。这是 CLI 的硬事实而非配置项 ——
# CodexAdapter 里硬编码的 wire_api = "responses"（见 _provision_codex_config）即是佐证。
#
# 多厂商网关型 CLI（codebuddy / opencode / pi）自身是网关，没有「原生厂商」——
# 任何厂商的模型都该按本 CLI 的协议改道。用 _MULTI_VENDOR 占位：vendor_of_model
# 只会返回 None 或真实厂商名，所以这个值永远匹配不上，同族短路逻辑自然不生效。
_MULTI_VENDOR = "__multi_vendor__"

# AICU 在各 CLI 配置里注册第三方端点用的 provider id（codex 的 model_providers、
# qwen 的 modelProviders、opencode / pi 的自定义 provider 都用它）。opencode 与 pi
# 要求 --model 写成 ``<provider>/<model>``，这个 id 就是那个前缀。
_CUSTOM_PROVIDER_ID = "aicu"

# claude CLI 走第三方 Anthropic 兼容端点时用的认证变量名（Bearer 头）。
# 只给 ANTHROPIC_API_KEY 会被 CLI 判成「未登录」，见 ClaudeCodeAdapter.api_key_env_for_model。
_ANTHROPIC_AUTH_TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"

_CLI_NATIVE: dict[str, tuple] = {
    "claude": ("anthropic", "messages"),
    "codex": ("openai", "responses"),
    "kimi": ("moonshot", "chat"),
    "qwen": ("aliyun", "chat"),
    "codebuddy": (_MULTI_VENDOR, "chat"),
    "opencode": (_MULTI_VENDOR, "chat"),
    "pi": (_MULTI_VENDOR, "chat"),
    # 下面三个统统领的是自家协议，跨厂商改道对它们没有意义，所以协议名取 "native"：
    # 数据表里不存在这个协议键，于是既不会改道（protocol_endpoint 恒为 None），
    # 又能在 known_models 里把异族模型滤掉（下拉不再列连不上的模型）。
    #   gemini / antigravity：只说 Gemini 原生 generateContent，不是 OpenAI Chat，
    #     指向别家端点没有任何意义；
    #   grok：二进制在本机缺失、无头 flags 与认证方式均未实测，adapter 未接线。
    "gemini": ("google", "native"),
    "antigravity": ("google", "native"),
    "grok": ("xai", "native"),
}

# 同族（模型厂商就是该 CLI 自己的原生厂商）、且用户没指定端点时，**撤掉 AICU 写过的槽**
# 而不是照写自家端点的 CLI。没列在这里的托管槽 CLI（qwen）同族照样接管。
#
#   codex 在此列，依据是它自己的登录态：``codex login`` 拿到的 ChatGPT 订阅写在
#   ~/.codex/auth.json（本机就是这种状态，tokens 非空），是 codex 的主用法。AICU 若照写
#   ``requires_openai_auth = false`` + ``env_key = OPENAI_API_KEY``，订阅用户会被踢到
#   API-key 路径上，报「Missing environment variable: OPENAI_API_KEY」。
#   撤槽则让 codex 回到它自己的 provider 与认证，与「AICU 没插手」逐字节一致。
#
#   qwen 故意不在列：它没有可撤的退路。非交互模式强制要求显式 auth type，撤槽后
#   ``--auth-type openai`` 会去要 OPENAI_API_KEY，不带该参数则直接报
#   「No auth type is selected」—— 两条路都走不通（2026-09-16 本机实测，见
#   /tmp/aicu_smoke/exp_qwen_samevendor.py 的 A/B 失败、C 通过）。所以同族也只能由
#   AICU 供端点。
_SAME_VENDOR_RELEASE_CLIS = ("codex",)


def _load_protocol_endpoints() -> dict:
    """从 cli_models.json 读取「厂商 → 协议 → base_url」；缺失/损坏时回退空 dict。

    厂商键与 runtime.credentials.vendor_of_model 的返回值一致，所以拿到模型判出
    厂商后可以直接查表。某个协议没有条目就是「该厂商不支持该协议」——
    不写空串占位，查不到即不可用。

    表格取值来自本机 curl 实测（每条都带伪造路径对照组，伪造 404 / 真实 401
    才算「端点存在」）。两处例外，因为探测不可判（厂商鉴权先于路由，伪造路径
    也返回 401），采信其官方文档 / 官方 helper 包：deepseek 的 messages 与
    responses。

    zhipu.responses 曾按官方 helper 包填了 https://open.bigmodel.cn/api/coding/paas/v4，
    2026-09-16 用真实调用证伪：codex + glm-5.2 稳定 404，报
    `.../api/coding/paas/v4/responses`；换成非 coding 基址 /api/paas/v4 同样 404。
    即智谱没有实现 OpenAI Responses API，故删掉该键 —— 删键即「该组合不可用」，
    codex 的模型下拉于是不再列出 glm。同批验过的 deepseek / moonshot / aliyun
    三个 responses 端点都是真实可用的（各跑通一次 codex 调用）。

    anthropic.chat 是个兼容垫片而非原生实现：Anthropic 官方说明它「primarily
    intended to test and compare model capabilities, and is not considered a
    long-term or production-ready solution」，工具调用可用但 `strict` 被忽略、
    prompt caching 不支持。留着它是为了让 kimi CLI 也能选 Claude——
    要撤掉只需删掉 cli_models.json 里这一个键。
    """
    try:
        data = json.loads(_MODELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    endpoints = data.get("protocol_endpoints", {})
    if not isinstance(endpoints, dict):
        return {}
    return {
        str(vendor): {
            str(protocol): str(url)
            for protocol, url in protocols.items()
            if isinstance(url, str) and url
        }
        for vendor, protocols in endpoints.items()
        if isinstance(protocols, dict)
    }


PROTOCOL_ENDPOINTS = _load_protocol_endpoints()


def protocol_endpoint(vendor: Optional[str], protocol: str) -> Optional[str]:
    """厂商在某协议下的原生端点；该厂商不支持该协议时返回 None。"""
    if not vendor:
        return None
    return PROTOCOL_ENDPOINTS.get(vendor, {}).get(protocol)


def _preset_base_urls() -> set:
    """provider preset 声明用过的 api_base_url —— 用来分辨「预设值」与「用户自定义端点」。"""
    return {
        str(preset["api_base_url"])
        for preset in list_provider_presets()
        if preset.get("api_base_url")
    }


def native_base_url(
    cli_name: Optional[str], model: Optional[str], configured_base_url: Optional[str]
) -> Optional[str]:
    """该 (CLI, 模型) 组合应当直连的厂商原生端点；不需要改写时返回 None。

    claude_code_agent 在构造 CLI 子进程前调用它，把返回值写回 api_base_url。

    返回 None 的四种情形，都表示「保持现状、什么都不动」：
      - CLI 不在 _CLI_NATIVE 里（opencode / pi / qwen / gemini / grok …）
      - 模型判不出厂商（auto 等内部模型）
      - 模型厂商就是该 CLI 的原生厂商 —— 同族直连，行为与改动前逐字节一致
      - 该厂商没有本 CLI 所需协议的原生端点（如 Anthropic 没有 /responses）
    另外，用户手填的非预设 api_base_url 一律尊重 —— 那种用户可能自己架了中转。
    """
    native = _CLI_NATIVE.get(cli_name or "")
    if not native:
        return None

    if configured_base_url and str(configured_base_url) not in _preset_base_urls():
        return None

    native_vendor, protocol = native
    vendor = vendor_of_model(model or "")
    if not vendor or vendor == native_vendor:
        return None

    return protocol_endpoint(vendor, protocol)


def _provision_codex_config(api_base_url: str, api_key_env: Optional[str], model: Optional[str]) -> None:
    """把第三方端点注册进 ``~/.codex/config.toml`` 的 ``model_providers``（幂等）。

    结构依据 openai/codex 源码 ``codex-rs/config/src/config_toml.rs`` 与
    ``codex-rs/model-provider-info/src/lib.rs`` 核实：
      - ``model_providers.<id>`` 为 provider 定义（name/base_url/env_key/wire_api）
      - ``wire_api`` 仅支持 ``responses``（``chat`` 已移除）
      - ``requires_openai_auth = false`` 跳过登录，API key 从 ``env_key`` 指定的环境变量读
      - 顶层 ``model_provider`` 选择使用哪个 provider
    只管理名为 ``aicu`` 的 provider 块，保留用户其它配置。
    """
    config_path = Path.home() / ".codex" / "config.toml"
    env_key = api_key_env or "OPENAI_API_KEY"
    block = (
        "[model_providers.aicu]\n"
        'name = "AICU"\n'
        f'base_url = {json.dumps(api_base_url, ensure_ascii=False)}\n'
        f'env_key = {json.dumps(env_key, ensure_ascii=False)}\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = false\n'
    )
    existing = ""
    if config_path.exists():
        try:
            existing = config_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    # 替换已存在的 [model_providers.aicu] 块，否则追加到末尾。
    # 该块不能包含顶层 model_provider，否则 TOML 会把它解析为 provider 字段。
    pattern = re.compile(r"(?m)^\[model_providers\.aicu\]\s*\n(?:^[^\[]+\n)*")
    if pattern.search(existing):
        text = pattern.sub(block, existing)
    else:
        text = f"{existing.rstrip()}\n\n{block}" if existing.strip() else block

    def _set_top_level(text: str, key: str, value: str) -> str:
        line = f"{key} = {json.dumps(value, ensure_ascii=False)}"
        # 顶层设置必须位于第一个 [table] 之前；追加到文件末尾会落入上一张表。
        table_match = re.search(r"(?m)^\s*\[[^\n]+\]\s*$", text)
        prefix = text[:table_match.start()] if table_match else text
        suffix = text[table_match.start():] if table_match else ""
        pat = re.compile(r"(?m)^" + re.escape(key) + r"\s*=.*$", re.MULTILINE)
        if pat.search(prefix):
            prefix = pat.sub(line, prefix)
        else:
            prefix = f"{prefix.rstrip()}\n{line}\n"
        return prefix + suffix

    if model:
        text = _set_top_level(text, "model", model)
    text = _set_top_level(text, "model_provider", "aicu")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")


def _provision_qwen_config(api_base_url: str, api_key_env: Optional[str], model: Optional[str]) -> None:
    """把第三方端点注册进 ``~/.qwen/settings.json`` 的 ``modelProviders``（幂等）。

    结构依据 QwenLM/qwen-code 源码 ``packages/core/src/models/types.ts`` 核实：
      - ``modelProviders`` 的 value 是 ``ModelConfig[]``（模型数组，非 provider 对象）
      - ``ModelConfig`` 字段：``id``（模型名）/``envKey``（读 API key 的环境变量名）/
        ``baseUrl``（端点）；``--model <id>`` 按 ``id`` 命中注册项
    只管理名为 ``aicu`` 的 provider 条目，保留用户其它配置。
    """
    config_path = Path.home() / ".qwen" / "settings.json"
    data: dict = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    providers = data.get("modelProviders")
    if not isinstance(providers, dict):
        providers = {}
    entry: dict = {}
    if model:
        entry["id"] = model
    if api_key_env:
        entry["envKey"] = api_key_env
    if api_base_url:
        entry["baseUrl"] = api_base_url
    providers["aicu"] = [entry]
    data["modelProviders"] = providers
    # 新版 Qwen Code 要求显式声明自定义 provider 的协议类型；
    # aicu 使用 OpenAI-compatible endpoint。
    protocols = data.get("providerProtocol")
    if not isinstance(protocols, dict):
        protocols = {}
    protocols["aicu"] = "openai"
    data["providerProtocol"] = protocols
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _opencode_config_content(
    api_base_url: str, models: list, api_key_env: Optional[str]
) -> str:
    """opencode 的 ``OPENCODE_CONFIG_CONTENT`` 取值 —— 内联配置，不落任何文件。

    opencode 支持用该环境变量直接传配置 JSON（二进制里的 OPENCODE_CONFIG_CONTENT），
    于是不需要像 codex / qwen 那样改写用户的 ``~/.config/opencode/opencode.json``，
    也不会往 agent 的工作目录里塞文件。本机实测通过。

    结构依据 opencode 官方配置格式：``provider.<id>`` 含 npm/name/options/models；
    ``options.baseURL`` 已含 /v1，走 OpenAI Chat 协议；``{env:VAR}`` 是 opencode 自己的
    环境变量插值语法（与 pi 的 ``$VAR`` 不同）。

    ``models`` 必须把选择器里可能出现的模型都列上，否则 opencode 找不到该 id。
    这里直接取 ``KNOWN_MODELS["all"]`` —— 与下拉候选同源，改一处两边同步。
    """
    options: dict = {"baseURL": api_base_url}
    if api_key_env:
        options["apiKey"] = "{env:" + api_key_env + "}"
    return json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                _CUSTOM_PROVIDER_ID: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "AICU",
                    "options": options,
                    "models": {model: {"name": model} for model in models},
                }
            },
        },
        ensure_ascii=False,
    )


def _coerce_errors(errors_value) -> list:
    """把各类 errors 表示归一化为字符串列表。"""
    if errors_value is None:
        return []
    if isinstance(errors_value, str):
        return [errors_value]
    if isinstance(errors_value, list):
        return [str(item) for item in errors_value]
    return [str(errors_value)]


def _render_openai_content(content) -> str:
    """把 OpenAI 风格 message.content（str 或 list）渲染为纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") in ("tool_use", "tool_call", "function"):
                    name = item.get("name") or (item.get("function") or {}).get("name", "unknown")
                    parts.append(f"Tool: {name}")
        return "\n".join(parts)
    return ""


def _extract_event_text(event: dict) -> str:
    """从通用 JSONL 事件 dict 提取文本（OpenCode / Pi / Gemini 等 json 事件）。"""
    for key in ("text", "content", "output", "response"):
        if isinstance(event.get(key), str) and event[key]:
            return event[key]
    msg = event.get("message")
    if isinstance(msg, dict):
        for key in ("content", "text"):
            content = msg.get(key)
            if isinstance(content, str) and content:
                return content
            if isinstance(content, list):
                rendered = _render_openai_content(content)
                if rendered:
                    return rendered
    parts = event.get("parts")
    if isinstance(parts, list):
        chunks = [
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
        ]
        if chunks:
            return "\n".join(chunks)
    return ""


def _extract_session_id(event: dict) -> Optional[str]:
    """从通用 JSONL 事件 dict 提取会话 id（OpenCode / Pi 等）。"""
    for key in ("sessionID", "sessionId", "session_id", "session", "conversation_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


class CliAdapter:
    """单个终端 Agent CLI 的协议抽象基类。"""

    id: str = ""
    display_name: str = ""
    command: str = ""

    @property
    def known_models(self) -> list[str]:
        """已知模型清单（供前端下拉候选）。"""
        if self.id == "internal":
            return KNOWN_MODELS.get("internal", [])
        return KNOWN_MODELS.get("all", [])

    # 可选同名冲突甄别：(探测 argv, 必须含子串, 必须排除子串)。None 表示只用 which。
    # 命令名可能被无关工具占用（如本机 codex 是同名的无关工具）时，设此属性以甄别。
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
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool
    ) -> list:
        raise NotImplementedError

    def print_flags(self, streaming: bool) -> list:
        raise NotImplementedError

    def resume_flag(self, session_id: str) -> list:
        return []

    def max_turns_flag(self, n: int) -> list:
        return []

    def prompt_args(self, prompt: str) -> tuple[list, bool]:
        """返回 (追加到 cmd 的 prompt 参数, 是否仍需通过 stdin 传 prompt)。默认位置参数。"""
        return ([prompt], False)

    # ---- 环境变量 ----

    def build_env(
        self,
        base_env: dict,
        *,
        api_base_url: Optional[str],
        api_key_env: Optional[str],
        model: Optional[str] = None,
    ) -> dict:
        """给子进程的环境变量（默认原样传下去）。

        ``model`` 是本次运行的模型名：绝大多数 CLI 用命令行参数拿模型，不需要它；
        但 kimi 只有一个环境变量通道（``KIMI_MODEL_NAME``），模型名必须和环境一起给
        （见 KimiAdapter.build_env）。
        """
        return base_env

    def api_key_env_for_model(self, model: Optional[str]) -> Optional[str]:
        """注入 sk 时使用的认证环境变量名（默认该 CLI 的 ``api_key_env``）。

        多厂商 CLI（如 opencode）按模型厂商覆盖，返回对应厂商的标准环境变量名。
        """
        return getattr(self, "api_key_env", None)

    # ---- 第三方模型供给（provision） ----

    def provision(
        self, *, api_base_url: Optional[str], api_key_env: Optional[str], model: Optional[str]
    ) -> None:
        """预写该 CLI 的第三方模型配置（默认 no-op）。

        只有 api_base_url 非空（指向第三方端点）时才需要预写；部分 CLI
        走环境变量，无需写配置文件。codex/qwen 覆盖此方法把端点注册进各自的
        配置文件（config.toml / settings.json），使运行时 ``--model <id>`` 能命中。
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

    @staticmethod
    def _internal_model_name(model: str) -> str:
        """将旧配置中的模型 ID 兼容映射为 Internal 网关 display name。"""
        key = re.sub(r"[^a-z0-9]", "", model.lower())
        candidates = KNOWN_MODELS.get("internal", [])
        # 同一模型可能同时存在内部网关名和公开 display name；保持
        # 历史公开 ID 的语义，不把它隐式改成“内部”模型。
        if "内部" not in model:
            candidates = [name for name in candidates if "内部" not in name]
        for display_name in candidates:
            if re.sub(r"[^a-z0-9]", "", display_name.lower()) == key:
                return display_name
        return model

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool
    ) -> list:
        cmd = [self.command]
        if model:
            command_model = self._internal_model_name(model) if self.id == "internal" else model
            cmd.extend(["--model", command_model])
        if self.id == "internal":
            username = (
                resolve_internal_username()
                or os.environ.get("INTERNAL_USERNAME")
                or os.environ.get("CC_USERNAME")
            )
            if username:
                cmd.extend(["--username", username])
        if skip_permissions:
            # root 用户下 --dangerously-skip-permissions 被禁止，
            # 用 --allowedTools 只开放必要的文件操作工具
            cmd.extend([
                "--allowedTools", "Read Write Edit Bash",
                "--permission-mode", "dontAsk",
            ])
        if enable_project_skills:
            # 限定只加载 workspace/.claude/skills/ 中的 project-level skill
            cmd.extend(["--setting-sources", "project"])
        return cmd

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

    def build_env(
        self,
        base_env: dict,
        *,
        api_base_url: Optional[str],
        api_key_env: Optional[str],
        model: Optional[str] = None,
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
        （CODEBUDDY_API_KEY，实测可直连第三方端点），internal 走 --username，
        两者都不做这个 Bearer 替换。
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
        try:
            value = json.loads(raw.strip())
            if not isinstance(value, dict):
                return None
            return self._normalize(value)
        except json.JSONDecodeError:
            # 非单条 JSON：可能是一组 stream-json 行，取最后一条 result 事件
            events = []
            for line in raw.splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    events.append(value)
            for event in reversed(events):
                if event.get("type") == "result" or "result" in event:
                    return self._normalize(event)
            return None

    @staticmethod
    def _normalize(value: dict) -> dict:
        return {
            "result": value.get("result") or value.get("text"),
            "session_id": value.get("session_id") or value.get("conversation_id"),
            "subtype": value.get("subtype"),
            "errors": _coerce_errors(value.get("errors")),
        }


class CodeBuddyAdapter(ClaudeCodeAdapter):
    """CodeBuddy Code CLI（``codebuddy``，@tencent-ai/codebuddy-code）。

    腾讯的 Claude Code 协议克隆，flags 与 Claude Code 几乎一致（--print /
    --output-format text|json|stream-json / --max-turns / --resume /
    --allowedTools / --setting-sources 等），复用 ClaudeCodeAdapter 的协议实现，
    仅覆盖认证环境变量。

    认证 env（官方文档核实）：
      - CODEBUDDY_API_KEY：API 密钥，用于模型接口调用，非交互模式 (-p) 下始终使用
      - CODEBUDDY_AUTH_TOKEN：平台认证令牌，用于所有平台接口调用
    """

    api_key_env = "CODEBUDDY_API_KEY"

    def __init__(self):
        super().__init__("codebuddy", "CodeBuddy Code", "codebuddy")

    def build_env(
        self,
        base_env: dict,
        *,
        api_base_url: Optional[str],
        api_key_env: Optional[str],
        model: Optional[str] = None,
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
        return env


class CodexAdapter(CliAdapter):
    """OpenAI Codex CLI（``codex exec``）。

    flags 依据官方 developer-commands 参考（learn.chatgpt.com/docs/developer-commands）
    核实；本环境无法访问该站（connection refused），内容由用户提供。

    注意：本机 /usr/bin/codex 是同名的无关工具（与 OpenAI Codex
    同名冲突），探测时按适配器列表顺序选择默认 CLI。

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
    # 本机 /usr/bin/codex 是同名的无关工具（同名冲突）：--help 含 exec 且
    # 不含 unrelated-tool 才是 OpenAI Codex。
    install_probe = (["--help"], ["exec"], ["unrelated-tool"])

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool
    ) -> list:
        cmd = [self.command, "exec"]
        if model:
            cmd.extend(["--model", model])
        if skip_permissions:
            # 等价于 Claude Code 的 skip_permissions；文档警示仅在专用沙箱 VM 内使用
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        # codex 无 project-level skill 概念，enable_project_skills 忽略
        return cmd

    def print_flags(self, streaming: bool) -> list:
        # --json 输出 NDJSON 事件流，最终回复在最后一条事件里，parse_result 直接提取。
        # 不再用 --output-last-message：该 flag 需要 <FILE> 值（写文件而非打印 stdout），
        # 且会吞掉其后的位置参数，导致 prompt 丢失。
        return ["--json"]

    def resume_flag(self, session_id: str) -> list:
        # resume 是 exec 的子命令（codex exec resume <id>），非 --resume flag
        return ["resume", session_id]

    def max_turns_flag(self, n: int) -> list:
        # 文档未见 codex 的 --max-turns，保守不加
        return []

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
    ) -> dict:
        env = dict(base_env)
        if api_base_url:
            env["OPENAI_BASE_URL"] = api_base_url
        if api_key_env:
            api_key = os.environ.get(api_key_env)
            if api_key:
                env["OPENAI_API_KEY"] = api_key
        return env

    def api_key_env_for_model(self, model: Optional[str]) -> Optional[str]:
        # Codex 的自定义 provider 通过 config.toml 的 env_key 读取对应厂商 Key。
        return api_key_env_for_vendor(vendor_of_model(model))

    def provision(
        self, *, api_base_url: Optional[str], api_key_env: Optional[str], model: Optional[str]
    ) -> None:
        if not api_base_url:
            return
        _provision_codex_config(api_base_url, api_key_env, model)

    def format_stream_event(self, line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
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
            sid = value.get("session_id") or value.get("conversation_id")
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
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool
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

    def max_turns_flag(self, n: int) -> list:
        # kimi 无 --max-turns（仅 --max-steps-per-turn，语义不同），保守不加
        return []

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

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool
    ) -> list:
        cmd = [self.command]
        if model:
            cmd.extend(["--model", model])
        # Qwen 0.22+ 的非交互模式要求显式选择认证类型；
        # modelProviders.aicu 通过 OpenAI-compatible endpoint 接入。
        cmd.extend(["--auth-type", "openai"])
        if skip_permissions:
            cmd.append("--yolo")
        # qwen 无 Claude project-level skill 概念，enable_project_skills 忽略
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
    ) -> dict:
        env = dict(base_env)
        # qwen 的 base URL 由 settings.json 的 modelProviders 决定，CLI 不通过 env 覆盖
        key_env = api_key_env or self.api_key_env
        if key_env:
            api_key = os.environ.get(key_env)
            if api_key:
                env[self.api_key_env] = api_key
        return env

    def provision(
        self, *, api_base_url: Optional[str], api_key_env: Optional[str], model: Optional[str]
    ) -> None:
        if not api_base_url:
            return
        _provision_qwen_config(api_base_url, api_key_env, model)

    def format_stream_event(self, line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
        if isinstance(event, dict):
            role = event.get("role")
            if role in ("assistant", "tool"):
                return _render_openai_content(event.get("content"))
        for key in ("content", "text", "message", "output"):
            if isinstance(event.get(key), str) and event[key]:
                return event[key]
        return ""

    def parse_result(self, raw: str) -> Optional[dict]:
        text = raw.strip()
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
    """通用 exec 风格 CLI 适配器（Qwen / Antigravity / Grok 等）。

    这些 CLI 未在本环境安装、flags 未经实测，按常见约定做最佳猜测；
    新增工具只需传入一条 spec，后续拿到厂商文档再精修。
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
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool
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
    ) -> dict:
        env = dict(base_env)
        if api_base_url and self.base_url_env:
            env[self.base_url_env] = api_base_url
        key_env = api_key_env or self.api_key_env
        if key_env:
            api_key = os.environ.get(key_env)
            if api_key:
                env[self.api_key_env] = api_key
        return env

    def format_stream_event(self, line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
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

    def api_key_env_for_model(self, model: Optional[str]) -> Optional[str]:
        return api_key_env_for_vendor(vendor_of_model(model))

    def model_arg(self, model: Optional[str], *, api_base_url: Optional[str]) -> Optional[str]:
        # opencode 的 -m 要求 provider/model；第三方端点是以 aicu 这个 provider id
        # 通过 OPENCODE_CONFIG_CONTENT 注册的，所以改道时必须补前缀。
        return f"{_CUSTOM_PROVIDER_ID}/{model}" if api_base_url and model else model

    def build_env(
        self,
        base_env: dict,
        *,
        api_base_url: Optional[str],
        api_key_env: Optional[str],
        model: Optional[str] = None,
    ) -> dict:
        env = dict(base_env)
        if api_base_url:
            env["OPENCODE_CONFIG_CONTENT"] = _opencode_config_content(
                api_base_url, KNOWN_MODELS.get("all", []), api_key_env
            )
        return env

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool
    ) -> list:
        cmd = [self.command, "run"]
        if model:
            cmd.extend(["--model", model])
        if skip_permissions:
            cmd.append("--auto")
        # opencode 无 project-level skill 概念，enable_project_skills 忽略
        return cmd

    def print_flags(self, streaming: bool) -> list:
        # --format json 输出 raw JSON events；流式与非流式一致，解析端按 JSONL 处理
        return ["--format", "json"]

    def resume_flag(self, session_id: str) -> list:
        return ["--session", session_id]

    def max_turns_flag(self, n: int) -> list:
        # 文档未见 max-turns 类轮次 flag，保守不加
        return []

    def prompt_args(self, prompt: str) -> tuple[list, bool]:
        # run 用位置参数传 message
        return ([prompt], False)

    def format_stream_event(self, line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
        if not isinstance(event, dict):
            return ""
        return _extract_event_text(event)

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

    def api_key_env_for_model(self, model: Optional[str]) -> Optional[str]:
        return api_key_env_for_vendor(vendor_of_model(model))

    def model_arg(self, model: Optional[str], *, api_base_url: Optional[str]) -> Optional[str]:
        # pi 的 --model 支持 provider/id；（--provider 是等价的另一种写法，
        # 用 id 前缀可以让模型名自解释，也少一个 flag）
        return f"{_CUSTOM_PROVIDER_ID}/{model}" if api_base_url and model else model

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool
    ) -> list:
        cmd = [self.command]
        if model:
            cmd.extend(["--model", model])
        if skip_permissions:
            cmd.append("--approve")
        # pi 无 project-level skill 概念，enable_project_skills 忽略
        return cmd

    def print_flags(self, streaming: bool) -> list:
        # --mode json 输出所有事件为 JSON lines（流式）；-p 打印最终响应后退出（非流式）
        if streaming:
            return ["--mode", "json"]
        return ["-p"]

    def resume_flag(self, session_id: str) -> list:
        return ["--session", session_id]

    def max_turns_flag(self, n: int) -> list:
        # 文档未见 max-turns 类轮次 flag，保守不加
        return []

    def prompt_args(self, prompt: str) -> tuple[list, bool]:
        # 位置参数传 message
        return ([prompt], False)

    def format_stream_event(self, line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
        if not isinstance(event, dict):
            return ""
        return _extract_event_text(event)

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

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool
    ) -> list:
        cmd = [self.command]
        if model:
            cmd.extend(["--model", model])
        if skip_permissions:
            # --yolo 自动批准所有操作（含工具调用），等价于 Claude Code 的 skip_permissions
            cmd.append("--yolo")
        # Gemini headless 模式还要求显式信任当前工作目录。
        cmd.append("--skip-trust")
        # gemini 无 project-level skill 概念，enable_project_skills 忽略
        return cmd

    def print_flags(self, streaming: bool) -> list:
        # --output-format json 输出单个 JSON 对象（response/sessionId）；headless 页仅列
        # text/json（stream-json 未在该页出现），统一用 json 避免依赖未确认的 stream-json。
        return ["--output-format", "json"]

    def resume_flag(self, session_id: str) -> list:
        return ["--resume", session_id]

    def max_turns_flag(self, n: int) -> list:
        # 文档未见 max-turns 类轮次 flag，保守不加
        return []

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
    ) -> dict:
        env = dict(base_env)
        # Gemini API / Vertex 各有独立 endpoint，无统一 base URL env；api_base_url 未接线（直连官方）
        key_env = api_key_env or self.api_key_env
        if key_env:
            api_key = os.environ.get(key_env)
            if api_key:
                env[self.api_key_env] = api_key
        return env

    def format_stream_event(self, line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
        if not isinstance(event, dict):
            return ""
        return _extract_event_text(event)

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

    def build_base_command(
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool
    ) -> list:
        cmd = [self.command]
        if model:
            cmd.extend(["--model", model])
        # 免审批 flag 官方未列出，保守不加（避免命令报错）
        return cmd

    def print_flags(self, streaming: bool) -> list:
        # -p 打印模式（无头）；官方仅确认 --output-format text（默认），不设 json 流式
        return ["-p"]

    def resume_flag(self, session_id: str) -> list:
        return ["--resume", session_id]

    def max_turns_flag(self, n: int) -> list:
        return []

    def prompt_args(self, prompt: str) -> tuple[list, bool]:
        # -p 后跟位置参数 prompt
        return ([prompt], False)

    def parse_result(self, raw: str) -> Optional[dict]:
        text = raw.strip()
        if not text:
            return None
        # 纯文本输出（--output-format text），整体即最终响应
        return {"result": text, "session_id": None, "subtype": None, "errors": []}


# 优先级顺序即默认选择顺序（internal > claude > codebuddy > codex > kimi > qwen > opencode > pi > gemini > cursor > antigravity > grok）
_ADAPTERS: list[CliAdapter] = [
    ClaudeCodeAdapter("internal", "Internal", "internal"),
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
    GenericExecAdapter("antigravity", "Antigravity", "agy", api_key_env="GEMINI_API_KEY"),
    # Grok：官方 README 确认命令名为 grok、认证走浏览器（非简单 API key env）；无头 flags 未公开
    GenericExecAdapter("grok", "Grok Build", "grok", api_key_env="XAI_API_KEY"),
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

    每项: {id, display_name, installed, priority, models}，按优先级顺序排列。
    """
    result = []
    for index, adapter in enumerate(_ADAPTERS):
        result.append({
            "id": adapter.id,
            "display_name": adapter.display_name,
            "installed": adapter.is_installed(),
            "priority": index,
            "models": list(adapter.known_models),
        })
    return result
