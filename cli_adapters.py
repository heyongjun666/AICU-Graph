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
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from runtime.credentials import (
    api_key_env_for_vendor,
    resolve_internal_username,
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
    #   grok：保守取 native —— 本机没有 XAI_API_KEY，改道这条路没验过。真要试，
    #     入口是它自己的 --xai-api-base-url（换的是 xAI API 的后端，未必换成别家协议）。
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


def model_supported_by_cli(cli_name: Optional[str], model: Optional[str]) -> bool:
    """该 CLI 能不能用这个模型 —— 同族直连，异族得有本协议的原生端点。

    供 known_models 过滤掉选不中的组合（如 claude + gemini）。判不出的情形
    （未纳入的 CLI、判不出厂商的内部模型）一律返回 True，即保持现状不过滤。
    """
    native = _CLI_NATIVE.get(cli_name or "")
    if not native:
        return True
    vendor = vendor_of_model(model or "")
    if not vendor:
        return True
    native_vendor, protocol = native
    return vendor == native_vendor or protocol_endpoint(vendor, protocol) is not None


def _has_managed_slot(cli_name: Optional[str]) -> bool:
    """该 CLI 是否有一份 AICU 托管的配置文件槽（未知 id 视为没有）。"""
    try:
        return get_adapter(cli_name or "").managed_slot
    except ValueError:
        return False


def provider_slot_base_url(
    cli_name: Optional[str], model: Optional[str], configured_base_url: Optional[str]
) -> Optional[str]:
    """本次运行 AICU 托管槽该持有的端点；返回 None 表示「把自己上次写的撤掉」。

    与 native_base_url 是两回事，别混：
      - ``native_base_url`` 管「CLI 自身寻址」（环境变量 / 内联配置）。同族返回 None
        是对的 —— 不设变量，CLI 就用自己的认证。
      - 本函数管「AICU 写在 CLI 配置文件里的那个槽」（codex 的 ``model_providers.aicu``、
        qwen 的 ``modelProviders.aicu``、pi 的 ``providers.aicu``）。那个槽**只有 AICU
        会写**，上一次运行留下的值会被这一次当成事实 —— 所以每次运行都必须给出确定
        答案：要么按当前模型所属厂商重写，要么撤掉自己写过的。留着旧值是最坏的一种。

    只有 ``managed_slot`` 为 True 的 CLI 有槽，其余一律返回 None（它们的 provision 是
    no-op，没有可撤的东西）。

    返回 None 的三种情形：没有托管槽；模型判不出厂商（无从下手，撤槽好过留残值）；
    同族且该 CLI 属于 _SAME_VENDOR_RELEASE_CLIS（交还 CLI 自己的认证）。

    用户自己指定的 api_base_url（无论手填还是选了 preset）一律原样写进槽 ——
    那种用户可能自己架了中转，也可能就是要 AICU 走这个端点。
    """
    if not _has_managed_slot(cli_name):
        return None
    if configured_base_url:
        return str(configured_base_url)
    native = _CLI_NATIVE.get(cli_name or "")
    if not native:
        # 有槽却不在 _CLI_NATIVE 里 = 声明漏了（有测试守着），这里不做无声猜测。
        return None
    native_vendor, protocol = native
    vendor = vendor_of_model(model or "")
    if not vendor:
        return None
    if vendor == native_vendor and cli_name in _SAME_VENDOR_RELEASE_CLIS:
        return None
    return protocol_endpoint(vendor, protocol)


def effective_base_url(
    cli_name: Optional[str], model: Optional[str], configured_base_url: Optional[str]
) -> tuple[Optional[str], str]:
    """这次运行**真正会用到**的端点，以及它是怎么定下来的 —— 给界面看的那个答案。

    返回值 ``(url, source)``，``source`` 取值：
      - ``"configured"``：用的就是用户配置里存着的那个地址（预置厂商自家端点，或用户
        自己填的网关）—— 换模型不会改它。
      - ``"model_vendor"``：AICU 按**当前模型所属厂商**改写了地址 —— 换模型就会跟着变。
      - ``"cli_default"``：谁都没指定，CLI 用自己的默认端点（url 可能为 None，表示
        连默认端点都无从得知，例如 google 系 CLI 只认自家原生协议）。

    存在的理由是「界面上显示的地址」和「请求实际发往的地址」必须是同一个来源。
    所以这里不另立规则，而是把 claude_code_agent 建子进程前调用那两个函数的**顺序与取值**
    照搬过来（改动那处接线时必须回来同步这里，否则两边又分家）：
      1. ``native_base_url`` 先按模型厂商改写「CLI 自身寻址」（环境变量 / 内联配置）；
      2. 托管槽（codex 的 ``model_providers.aicu``、qwen 的 ``modelProviders.aicu``、
         pi 的 ``providers.aicu``）用**上一步改写后**的值去写 —— 槽是 CLI 真正读的那份
         （codex 顶层 ``model_provider = "aicu"`` 指过去就是它），照抄配置里的预置地址
         会让「选了 OpenAI 预置再把模型换成 deepseek」打到 openai 去。
    """
    configured = str(configured_base_url) if configured_base_url else None

    endpoint = native_base_url(cli_name, model, configured)
    addressed = endpoint or configured

    slot = provider_slot_base_url(cli_name, model, addressed)
    if slot:
        return slot, "configured" if slot == configured else "model_vendor"

    if endpoint:
        return endpoint, "configured" if endpoint == configured else "model_vendor"

    # 两条路都没改写：要么用户自己的端点原样生效，要么压根没指定过端点。
    if configured:
        return configured, "configured"

    native = _CLI_NATIVE.get(cli_name or "")
    if not native:
        return None, "cli_default"
    # 同族直连时 CLI 走的就是自家端点，替它把话说全（判不出就是判不出，不猜）。
    return protocol_endpoint(native[0], native[1]), "cli_default"


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


def _release_codex_config() -> None:
    """撤掉 AICU 写进 ``~/.codex/config.toml`` 的东西（幂等，文件不存在就什么都不做）。

    删两样，且都只删属于自己的：
      - ``[model_providers.aicu]`` 块：没有 ``model_provider`` 指着它就是死配置，
        留着只会让下一个读它的人以为 AICU 还生效；
      - 顶层 ``model_provider``，**仅当其值就是 AICU 的 id**。别的值是用户自己设的，
        不能碰。

    顶层 ``model`` 不动：它没有「值即归属」的判据，分不清是 AICU 写的还是用户写的，
    宁可留着（AICU 每次都用 ``--model`` 覆盖它，运行期无影响）。
    """
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        return
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return
    released = re.sub(
        r"(?m)^\[model_providers\." + re.escape(_CUSTOM_PROVIDER_ID) + r"\]\s*\n(?:^[^\[]+\n)*",
        "",
        text,
    )
    released = re.sub(
        r'(?m)^model_provider\s*=\s*"' + re.escape(_CUSTOM_PROVIDER_ID) + r'"\s*\n',
        "",
        released,
    )
    if released != text:
        config_path.write_text(released, encoding="utf-8")


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


def _release_qwen_config() -> None:
    """撤掉 AICU 写进 ``~/.qwen/settings.json`` 的 aicu 条目（幂等）。

    只删 ``modelProviders`` / ``providerProtocol`` 里 key 为 ``_CUSTOM_PROVIDER_ID``
    的两项，用户自己的 provider 原样保留；文件或分区不存在时不创建。
    """
    config_path = Path.home() / ".qwen" / "settings.json"
    if not config_path.exists():
        return
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(loaded, dict):
        return
    changed = False
    for section_name in ("modelProviders", "providerProtocol"):
        section = loaded.get(section_name)
        if isinstance(section, dict) and _CUSTOM_PROVIDER_ID in section:
            del section[_CUSTOM_PROVIDER_ID]
            changed = True
    if changed:
        config_path.write_text(
            json.dumps(loaded, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def _provision_pi_config(api_base_url: str, api_key_env: Optional[str], model: Optional[str]) -> None:
    """把第三方端点注册进 ``~/.pi/agent/models.json`` 的 ``providers``（幂等）。

    结构依据 pi 的 ``dist/core/model-config.d.ts`` 核实：顶层 ``providers`` 是
    provider id → ProviderConfig 的映射，ProviderConfig 含 name/baseUrl/apiKey/api/
    models。``api`` 取 ``openai-completions``（OpenAI Chat 协议，baseUrl 含 /v1）。

    apiKey 不写字面量，写 ``$ENV_NAME`` —— pi 的 dist/core/resolve-config-value.js
    会把 ``$VAR`` / ``${VAR}`` 插值成环境变量值（本机实测通过）。键是 AICU 注入的，
    落盘的就只是一个变量名。

    只管理 ``aicu`` 这一项，保留用户其它 provider 配置。
    """
    config_path = Path.home() / ".pi" / "agent" / "models.json"
    data: dict = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    providers = data.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    entry: dict = {"name": "AICU", "baseUrl": api_base_url, "api": "openai-completions"}
    if api_key_env:
        entry["apiKey"] = f"${api_key_env}"
    if model:
        entry["models"] = [{"id": model, "name": model}]
    providers[_CUSTOM_PROVIDER_ID] = entry
    data["providers"] = providers
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _release_pi_config() -> None:
    """撤掉 AICU 写进 ``~/.pi/agent/models.json`` 的 aicu provider（幂等）。

    用户自己的 provider 原样保留；文件或 ``providers`` 不存在时不创建。
    """
    config_path = Path.home() / ".pi" / "agent" / "models.json"
    if not config_path.exists():
        return
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(loaded, dict):
        return
    providers = loaded.get("providers")
    if isinstance(providers, dict) and _CUSTOM_PROVIDER_ID in providers:
        del providers[_CUSTOM_PROVIDER_ID]
        config_path.write_text(
            json.dumps(loaded, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def _opencode_config_content(
    api_base_url: Optional[str],
    models: list,
    api_key_env: Optional[str],
    servers: Optional[list] = None,
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

    **provider 与 mcp 必须拼在同一个 JSON 里**，因为两者共用同一个环境变量：分两次
    设置只会让后写的把先写的整个盖掉。这也是 MCP 的注入点必须落在这个函数里、
    而不是另开一个 mcp 专用 env 的原因。

    ``api_base_url`` 为空表示本次不改道（只可能出现在「有 mcp、无自定义端点」时）：
    那时不加 ``provider`` 段，只下发 ``mcp``。
    """
    config: dict = {"$schema": "https://opencode.ai/config.json"}
    if api_base_url:
        options: dict = {"baseURL": api_base_url}
        if api_key_env:
            options["apiKey"] = "{env:" + api_key_env + "}"
        config["provider"] = {
            _CUSTOM_PROVIDER_ID: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "AICU",
                "options": options,
                "models": {model: {"name": model} for model in models},
            }
        }
    if servers:
        # opencode 的 server 条目：`command` 是**数组**（程序 + 参数一起放），
        # 与 claude 系的 `command` 字符串 + `args` 数组不是一回事，所以不能复用
        # _cc_mcp_json。`type` 与 `enabled` 都是必填/推荐项。
        config["mcp"] = {
            server.name: {
                "type": "local",
                "command": [server.command, *server.args],
                "enabled": True,
                **({"environment": dict(server.env)} if server.env else {}),
            }
            for server in servers
        }
    return json.dumps(config, ensure_ascii=False)


def _result_events(raw: str) -> list:
    """把 CLI 的 JSON 输出拆成事件列表，顺序不变。

    同一个 ``--output-format json`` 在不同 CLI 下有三种形态，这里统一归一：
      - 单个 JSON 对象（claude）
      - 事件数组（codebuddy / qwen —— 实测两者都把事件装进一个 JSON 数组）
      - 每行一个对象的 JSONL（各家 stream-json）
    整段不是 JSON（纯文本回复）时返回空列表，由调用方兜底。
    """
    text = raw.strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [event for event in value if isinstance(event, dict)]
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _claude_style_result(raw: str) -> Optional[dict]:
    """取 Claude Code 风格的结果对象并归一化（claude / codebuddy / qwen 同形）。

    ``--output-format json`` 在 claude 下就是单个结果对象，直接归一化；codebuddy /
    qwen 输出事件数组（末条 ``type=result``），stream-json 则每行一个事件 ——
    后两种取最后一条 result 事件。取不到返回 None，调用方退回原始输出。
    """
    text = raw.strip()
    if not text:
        return None
    events = _result_events(raw)
    candidates = [
        event
        for event in events
        if event.get("type") == "result" or "result" in event
    ]
    # 单个对象且没有 result 字段时它就是结果对象本身（如只有 subtype/errors 的
    # error_during_execution），不能因为挑不出 result 事件就丢掉。
    if len(events) == 1 and not candidates:
        candidates = events
    if not candidates:
        return None
    event = candidates[-1]
    return {
        "result": event.get("result") or event.get("text"),
        "session_id": event.get("session_id") or event.get("conversation_id"),
        "subtype": event.get("subtype"),
        "errors": _coerce_errors(event.get("errors")),
    }


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
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
        # 内容也可能是 OpenAI 风格的内容块数组（如 {"content":[{"type":"text",...}]}）
        if isinstance(value, list):
            rendered = _render_openai_content(value)
            if rendered:
                return rendered
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
    # OpenCode 的事件把内容包在单个 part 里：
    # {"type":"text","part":{"type":"text","text":"..."}}（实测）
    part = event.get("part")
    if isinstance(part, dict):
        return _extract_event_text(part)
    return ""


def _format_json_event(line: str, extract=_extract_event_text) -> str:
    """把一行 JSONL 事件渲染成可见文本（OpenCode / Pi / Gemini 等同形 CLI）。

    两种回退语义故意不对称，缺一不可：
      - 解析不出 JSON → **原样返回该行**，非 JSONL 的原始输出（启动日志、报错）仍能显示；
      - 解析出来但不是对象 → 返回空串，这种行没有可展示的正文，返回原文只会刷屏。

    ``extract`` 由各 CLI 给：Pi 需要 ``_assistant_text``（它的 ``--mode json`` 会把
    用户消息也回显，通用提取器会把提问当成回复），其余用默认的通用提取器。
    """
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(event, dict):
        return ""
    return extract(event)


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
        """已知模型清单（供前端下拉候选）。

        对 claude / codex / kimi 过滤掉「该 CLI 的协议没有对应原生端点」的模型
        （如 claude 选不了 gemini），让下拉里出现的都是真能连上的。
        """
        if self.id == "internal":
            return KNOWN_MODELS.get("internal", [])
        return [
            model
            for model in KNOWN_MODELS.get("all", [])
            if model_supported_by_cli(self.id, model)
        ]

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
    #     skill 就吃过这个亏：软链对所有 CLI 照建，非 claude 系 CLI 上那是死文件，
    #     建了但永远不生效。
    #   子类可以把它改成 @property：kimi / pi 的答案是「看本机装的那个二进制」，
    #     没法用一个类属性写死（见 _mcp_capability）。
    supports_mcp = False

    # 项目级 MCP 配置文件的路径，**相对 CC 的 workspace 逐段给出**。
    #   None（默认）—— 这个 CLI 不走「落盘项目文件」这条路。
    #   (".gemini", "settings.json") 这样 —— 基类的 mcp_workspace_files 照此写盘。
    # 只描述路径；内容由 _mcp_workspace_payload 决定。需要往一处写**多个**文件、
    # 或路径不由 workspace 直接决定的 CLI，自己覆盖 mcp_workspace_files 即可。
    mcp_workspace_config: Optional[tuple] = None

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
        """
        return _mcp_servers_project_json(mcp_servers)

    def mcp_unwired_reason(self) -> str:
        """``supports_mcp`` 为假时，一个能直接嵌进 ``"CLI 'x' 的 MCP ___，但……"``
        这句话里的**原因短语**。

        默认是「尚未接入 AICU」—— 绝大多数情况下这是实话。kimi 覆盖它，因为那边为假
        的原因是**本机装的那个版本**没有可用的 MCP 入口，说成 AICU 没接线同样是关于
        CLI 的假话，而且把用户指向一个他做不了的动作（等我们接）而不是该做的（升级）。
        """
        return "尚未接入 AICU"

    # ---- 环境变量 ----

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


def _cc_mcp_json(servers: list) -> str:
    """Claude Code 协议族的 ``mcpServers`` 配置串。

    形状是 ``{"mcpServers": {<name>: {command, args, env?}}}`` —— ``command`` 是
    字符串、参数单独放 ``args``。注意这**不是**通用形状：opencode 要的是
    ``command: [程序, ...参数]`` 的数组形式，所以那边另写一份（见
    ``_opencode_config_content``）。共用一个序列化器只会让两边都错。
    """
    entries: dict = {}
    for server in servers:
        entry: dict = {"command": server.command, "args": list(server.args)}
        if server.env:
            entry["env"] = dict(server.env)
        entries[server.name] = entry
    return json.dumps({"mcpServers": entries}, ensure_ascii=False)


def _mcp_servers_project_json(servers: list) -> str:
    """项目级 ``mcpServers`` 配置文件的内容（gemini / cursor / pi / kimi 同形）。

    形状与 ``_cc_mcp_json`` 一样是 ``command`` 字符串 + ``args`` 数组 + 可选 ``env``，
    差别只在渲染：这里是**落盘给 CLI 解析的文件**，所以缩进 2 空格并带尾换行（与
    ``_mcp_servers_settings_json`` 的落盘风格一致）。

    为什么另起一份而不是复用/参数化上面那两个：

      - 不复用 ``_cc_mcp_json``：它绑在 claude 系的 argv 路径上（``--mcp-config=<紧凑
        JSON>``），改它等于动那四家已经接好的 CLI，而这一批的前提是「现有行为一个
        字节都不变」。
      - 不参数化 qwen 的 ``trust``：``trust: true`` 是 qwen 自己的无头批准语义
        （见 ``_mcp_servers_settings_json`` 的说明）。gemini 也有 ``trust`` 字段，但
        含义完全不同（"trusted servers may gain additional capabilities"，不是批准
        门禁）；cursor / pi / kimi 根本没有这个键。合并只会让某一方多写一个自己
        不认识或不期望的字段。

    四家同形是**核实过的事实**（各自的官方文档或发行包），不是「看着像」——
    所以这里共用一份是诚实的；哪天某一家分叉，就照 ``_opencode_config_content`` 的
    先例给它单独写一份。grok 走的是同一个钩子但内容是 TOML，另行渲染。
    """
    entries: dict = {}
    for server in servers:
        entry: dict = {"command": server.command, "args": list(server.args)}
        if server.env:
            entry["env"] = dict(server.env)
        entries[server.name] = entry
    return json.dumps({"mcpServers": entries}, ensure_ascii=False, indent=2) + "\n"


# ---- grok 的 TOML 发射 ---------------------------------------------------------
#
# grok 读的是 TOML（``.grok/config.toml``），仓库里没有 TOML 库，codex 那份也是手写
# 正则（见 :func:`_codex_mcp_args`），这里同样手写 —— 要写的形状是固定的，不需要
# 通用序列化器。
#
# **不能拿 json.dumps 冒充 TOML**：两者的转义规则不重合，而差集正好是会被静默吞掉的
# 那一类 ——
#   - U+007F(DEL)：JSON 允许直接写在字符串里（json.dumps 默认就不转义它），TOML 里
#     它是非法字面字符，只能写成 ``\u007F``；
#   - 代理对：JSON 那种 ``😀`` 写法在 TOML 里非法，所以非 ASCII 一律原样
#     输出，一个 ``\u`` 都不发。
# 用 fullmatch 而不是 ``^...$``：Python 的 ``$`` 也匹配「结尾换行之前」，于是名字里
# 带尾随 ``\n`` 时（``mcp_servers.json`` 里写 ``"echo-probe\n"``）会被判成裸键、把换行
# 原样吐进表名，整份 TOML 解析失败 —— 而且是**同文件里其它 server 一起丢**，
# grok 那边只表现为「工具不存在」。``\Z``/fullmatch 没有这个缺口。
_TOML_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+\Z")
_TOML_ESCAPES = {
    "\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f",
}


def _toml_string(value: str) -> str:
    """渲染一个 TOML 基本字符串（含两侧引号）。"""
    out = []
    for ch in value:
        escaped = _TOML_ESCAPES.get(ch)
        if escaped is not None:
            out.append(escaped)
        elif ch < "\x20" or ch == "\x7f":
            # 其余控制符 + DEL：TOML 只认 \uXXXX 形式
            out.append("\\u%04X" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_key(key: str) -> str:
    """表名/键名：裸键只在 ``[A-Za-z0-9_-]+`` 时安全，其余一律加引号。

    加引号不是洁癖：server 名里的 ``.`` 会让 ``[mcp_servers.my.server]`` 被解析成
    三层嵌套表（mcp_servers → my → server），而不是一个叫 ``"my.server"`` 的 server。
    grok 那边于是找不到它，而且不会有任何报错 —— 正是要避免的那种静默失败。
    """
    return key if _TOML_BARE_KEY.match(key) else _toml_string(key)


def _grok_mcp_toml(servers: list) -> str:
    """grok ``.grok/config.toml`` 里的 ``[mcp_servers.<name>]`` 段。

    形状取自 grok 自带的用户手册（``~/.grok/docs/user-guide/07-mcp-servers.md``，
    由二进制解出）：

        [mcp_servers.github]
        command = "npx"
        args = ["-y", "@modelcontextprotocol/server-github"]
        env = { GITHUB_PERSONAL_ACCESS_TOKEN = "ghp_xxx" }

    ``env`` 用**行内表**，与手册一致。这不只是抄格式：写成子表
    ``[mcp_servers.x.env]`` 之后父表就不能再补键，顺序写错即非法。
    env 的值直接写解析好的字面量，**不套 grok 自己的 ``${VAR}`` 插值** ——
    那会让 grok 再查一次环境变量，而 AICU 给的已经是解析好的值。
    """
    lines: list = []
    for server in servers:
        lines.append(f"[mcp_servers.{_toml_key(server.name)}]")
        lines.append(f"command = {_toml_string(server.command)}")
        lines.append("args = [" + ", ".join(_toml_string(a) for a in server.args) + "]")
        if server.env:
            pairs = ", ".join(
                f"{_toml_key(k)} = {_toml_string(v)}" for k, v in server.env.items()
            )
            lines.append(f"env = {{ {pairs} }}")
        lines.append("")
    return "\n".join(lines)


# ---- MCP 能力探测：supports_mcp 得先问本机二进制才能回答的 CLI -------------------
#
# 多数 adapter 的 ``supports_mcp`` 是个常量：AICU 接线了就是 True。但有两家的 MCP
# 入口**取决于本机装的那个版本/扩展**，写常量会说假话：
#
#   pi   —— pi 本体没有 MCP。0.84.2 的 ``dist/`` 里 ``mcpServers`` 命中数为 0，
#           配置是由外部扩展 pi-mcp-adapter 读的（``pi install npm:pi-mcp-adapter``）。
#           扩展没装时 ``.pi/mcp.json`` 是个死文件。
#   kimi —— 引擎里有 MCP，但项目级配置被 workspace trust 门禁挡着，而 trust 只能由
#           交互式 TUI 授予（见 ``KimiAdapter.mcp_unwired_reason``）。
#
# 门禁判否时的对外表现与「还没接线」完全一致（守卫照常报错、前端标未接入）——
# 这正是 ``supports_mcp`` 注释里那条原则：**宁可报错说没接线，也不要写一个不生效的
# 文件还让用户以为配好了。**
_MCP_CAPABILITY: dict = {}
_MCP_CAPABILITY_LOCK = threading.Lock()


def _probe_stdout(argv: list, timeout: int = 15) -> Optional[str]:
    """跑一条探测命令并把 stdout+stderr 一起返回；跑不起来一律返回 None。

    两条流合并是有意的：CLI 报「不认这个子命令」时走 stderr 还是 stdout 各家不同
    （kimi 0.43.1 的 ``unknown command 'mcp'`` 就未必在 stdout），而探测要问的问题
    只有一个 ——「这段输出里有没有那个东西」。

    ``errors="replace"`` 不能省：默认的严格解码遇到非 UTF-8 字节会抛
    ``UnicodeDecodeError``，而它**既不是 OSError 也不是 SubprocessError**，会直接穿过
    下面的 except、再穿过 ``_mcp_capability`` 与 ``supports_mcp`` 一路冒到
    ``/v1/config`` 上去（设置页打不开）。探测的设计是「探不出来就判否」，不该有一条
    路径是「探不出来就炸」；换成替换解码还有个好处：CLI 只是往 help 里混了一个坏字节
    时，剩下的输出照样能判。
    """
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        # 二进制不在、没权限、卡住超时 —— 一律当作「探测不出来」，也就是判否
        return None
    return (proc.stdout or "") + (proc.stderr or "")


def _mcp_capability(command: str, probe) -> bool:
    """跑一次 ``probe`` 并按**解析后的二进制路径**缓存结果。

    键取路径而不是命令名：同一个 CLI 完全可能装着多个版本（npm 全局一份、nvm 或
    项目本地又一份），按名字缓存会把 A 版本上探到的结果用到 B 版本上 —— 而这两个
    版本的 MCP 能力差异正是要区分的东西。

    加锁是必需的：``mcp_enabled_clis()`` 会在 Web 请求线程里挨个读这些属性，
    而首次访问要起子进程。
    """
    path = shutil.which(command) or command
    with _MCP_CAPABILITY_LOCK:
        if path not in _MCP_CAPABILITY:
            _MCP_CAPABILITY[path] = probe(path)
        return _MCP_CAPABILITY[path]


def _probe_pi_mcp(command: str) -> bool:
    """``pi list`` 里有没有 pi-mcp-adapter —— 有扩展才谈得上收 MCP 配置。

    查的是**全局**安装（``pi list`` 不带 ``-l`` 读全局设置）。项目级安装对 AICU 没有
    意义：每次运行的 workspace 都是新生成的，装在里面的扩展下次就没了。
    """
    output = _probe_stdout([command, "list"])
    return output is not None and "pi-mcp-adapter" in output


# ``kimi --help`` 的 Commands 段里，一行行形如 ``  <名字>[|别名] [options] ... 说明``。
# 只认行首（缩进后）那个 token，不认正文里出现的 mcp —— 否则将来帮助文案里提一句
# MCP 就会把没有子命令的版本判成有。
_KIMI_MCP_COMMAND = re.compile(r"^\s+mcp(?:\||\s|\[|$)", re.MULTILINE)


def _probe_kimi_mcp(command: str) -> bool:
    """``kimi`` 暴露了 ``mcp`` 子命令没有 —— 有入口才谈得上收 MCP 配置。

    问的是**有没有入口**，不是「能不能用」：0.43.1 实测 ``kimi mcp list`` 回
    ``unknown command 'mcp'. See 'kimi --help'.``（stderr，退出码 1），该版本的
    ``--help`` Commands 段里也没有它。

    为什么查 ``--help`` 而不是直接跑 ``kimi mcp list``：后者在有这个子命令的版本上
    会**真的去读配置**（读 ~/.kimi-code 与 cwd），而这份配置里可能有需要起子进程、
    需要联网、乃至需要等认证的 server —— 一旦挂住就撞上超时，被当成「没有入口」，
    正好把唯一能判真的情形误判成假。``--help`` 只打印一份静态帮助，没有这些副作用。

    **这道门禁的已知局限**：kimi 引擎里的 MCP 加载还要过 workspace trust，而 trust
    只能由交互式 TUI 授予（``-p`` 走不到那个提示）。所以即使这里判真、即使
    ``.kimi-code/mcp.json`` 写得再对，无头运行时项目级 server 仍可能被静默 gated 掉。
    探到真只是「这个版本有入口」，不等于「这条路走得通」—— 升级后实测确认之前，
    这道门禁给的是**必要非充分**信号。

    （Kimi 平台侧另有一条 MCP 路 —— 托管智能体的远程 MCP，只收 ``type: "url"``，
    与 AICU 的 stdio 下发对不上，刻意没接；见 ``KimiAdapter`` docstring 里的说明。）
    """
    output = _probe_stdout([command, "--help"])
    return output is not None and _KIMI_MCP_COMMAND.search(output) is not None


def _mcp_servers_settings_json(servers: list) -> str:
    """qwen 项目级 ``.qwen/settings.json`` 里的 ``mcpServers`` 段。

    与 ``_cc_mcp_json`` 只差一个 ``trust``：这里同样是 ``command`` 字符串 +
    ``args`` 数组（**不是** opencode 那种数组合一形式），但必须带 ``trust: true``。

    ``trust`` 不是可选项。qwen 把项目级 server 默认置为 "Pending approval" 状态
    （``qwen mcp list`` 实测），而 AICU 跑的是无头模式 —— 没有人在旁边点批准，
    不写 trust 等于下发了一个永远批不下来的工具。``trust`` 的语义是「在受信任的
    工作区里跳过该 server 的工具调用确认」，与 ``--yolo`` 是两回事。

    形状按 qwen 自己 ``mcp add -s project`` 写出来的文件对齐（已实测比对）。
    """
    entries: dict = {}
    for server in servers:
        entry: dict = {
            "command": server.command,
            "args": list(server.args),
            "trust": True,
        }
        if server.env:
            entry["env"] = dict(server.env)
        entries[server.name] = entry
    return json.dumps({"mcpServers": entries}, ensure_ascii=False, indent=2) + "\n"


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
        self, *, model: Optional[str], skip_permissions: bool, enable_project_skills: bool,
        mcp_servers: Optional[list] = None,
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
            allowed_tools = ["Read", "Write", "Edit", "Bash"]
            # 声明了 MCP server 时必须把它的工具一并写进白名单。--allowedTools 是
            # 白名单而非「先问再放行」：dontAsk 模式下不在名单里的工具一律直接拒绝。
            # 实测漏掉这一步的表现是 permission_denials 里出现 mcp__<server>__<tool>、
            # agent 拿不到结果后绕路或报一个与 MCP 毫无关系的错 —— 正是预检要消除的那类
            # 失败（见 runtime/mcp_client.py 的模块说明）。
            # `mcp__<server>` 是 server 级通配，一条即放行该 server 的全部工具，不必逐工具枚举。
            # 名字取自同一个 server 列表（与 mcp_flags 下发的配置同源），不可能对不上。
            allowed_tools.extend(f"mcp__{server.name}" for server in (mcp_servers or []))
            cmd.extend([
                "--allowedTools", " ".join(allowed_tools),
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

    supports_mcp = True

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

    **MCP 这条路默认走不通**（2026-09-17/18 端到端实测 + 旁路判据复核）—— 除非用户在
    自己的 codebuddy settings 里放行那一层壳（见本节末）。codebuddy 把
    **每一个** MCP 工具调用都包在自己的 ``DeferExecuteTool`` 里走，而父类放行的
    ``mcp__<server>`` 是 server 级通配，覆盖不到那层壳。三条证据：

      1. ``probe_echo`` 被拒，codebuddy 原样报出 ``Permission to use DeferExecuteTool
         has been denied because this tool requires approval but permission prompts are
         not available in non-interactive mode``。
      2. 把 ``DeferExecuteTool`` 补进 ``--allowedTools`` **无效** —— 拒绝照旧。codebuddy
         自己的报错就写了出路只有两条：``-y`` / ``--permission-mode bypassPermissions``，
         或把 ``DeferExecuteTool`` 写进 settings 的 ``permissions.allow``。
         ``--allowedTools`` 不在这两条路里。
      3. 探针 server 侧挂旁路调用日志（只有 server 进程写得进去、模型伪造不了）后，
         上述 argv 下 ``tools/call`` **一次都没发生**。

    第 3 条是必需的，因为「产物里有没有 ``probe_echo: AICU-MCP-PROBE-7f3a``」这个判据
    **可被伪造**：工具名就叫 probe_echo、描述写着「回显传入的 text」、输入串又写在任务
    提示词里，模型不调工具也能把这一行编出来。中途确实观测到过一次「产物对」的假通过，
    改用旁路日志后即告消失 —— 本适配器相关的旧结论若与本节冲突，以本节为准。

    所以此处**刻意不加任何额外 flag**：``-y`` 那类会把这个适配器变成比 claude 危险一档
    的形态（审批整个关掉），而 AICU 的立场是宁可报错说没接线，也不下发一份永不生效的
    配置。要用 codebuddy + MCP，得由用户在 settings 里自行 ``permissions.allow``。

    **``supports_mcp`` 仍是 True（继承自父类），这是有意的**，与上面「默认走不通」不矛盾：
    挡路的是 codebuddy 自己的权限层，而那一层是**用户可配置的**。放行过的用户这里就该
    是 True —— AICU 照常下发 ``--mcp-config``，链路是通的。真按「默认不通」关成 False，
    反而是把这类用户的可用配置一起废掉，而且 ``mcp_workspace_files`` 的守卫会让配置
    根本不落盘，从「可能被拒」退化成「一定不生效」。代价是：没放行的用户会看到
    ``supports_mcp=True``、下拉不标「未接入」，实际调用却被拒 —— 这个取舍记在这里，
    等 codebuddy 哪天把 ``DeferExecuteTool`` 挪出审批层再回头收掉。
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
        return env


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
    """
    if not servers:
        return []
    args: list = []
    for server in servers:
        prefix = f"mcp_servers.{server.name}"
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

    supports_mcp = True

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
        mcp_servers: Optional[list] = None,
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
        # 否则 codex 报「Missing environment variable: OPENAI_API_KEY」。后者走
        # api_key_env_for_model（按厂商），所以这里也优先按厂商解析；显式传入的
        # api_key_env 只作兜底（模型判不出厂商时）。
        env_key = api_key_env_for_vendor(vendor_of_model(model)) or api_key_env
        _provision_codex_config(api_base_url, env_key, model)

    def format_stream_event(self, line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
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

    本 CLI 自己那条 MCP 路径的局限见 ``_probe_kimi_mcp``（版本门禁 + workspace trust）。
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
    #
    # 路径写死，但**本次落不落盘由下面的门禁说了算** —— 基类 mcp_workspace_files 里
    # ``not self.supports_mcp`` 那一条就是为它准备的。
    mcp_workspace_config = (".kimi-code", "mcp.json")

    @property
    def supports_mcp(self) -> bool:
        """kimi 的 MCP 入口取决于**本机装的那个版本**有没有 ``mcp`` 子命令。

        0.43.1（本机 ``~/.kimi-code/bin/kimi``）的 ``--help`` 里没有这个子命令；
        2.0.0 才有。所以这里不能写常量：写 True，老版本上会落一份谁也读不了的文件；
        写 False，新版本上会让一个已经能用的通道一直报「未接入」。
        """
        return _mcp_capability(self.command, _probe_kimi_mcp)

    def mcp_unwired_reason(self) -> str:
        """报错文案得说真话：这里为假的**原因**是版本，不是 AICU 没接线。

        基类默认那句「尚未接入 AICU」在这个分支上是关于 CLI 的假话 —— 用户读到它
        只会以为换个 CLI 就好了，而实际要做的动作是升级 kimi。
        """
        return "在当前安装的 kimi 版本上没有可用入口（该版本未暴露 mcp 子命令，升级后重试）"

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
        mcp_servers: Optional[list] = None,
    ) -> dict:
        env = dict(base_env)
        # qwen 的 base URL 由 settings.json 的 modelProviders 决定，CLI 不通过 env 覆盖
        key_env = api_key_env or self.api_key_env
        if key_env:
            api_key = os.environ.get(key_env)
            if api_key:
                env[self.api_key_env] = api_key
        return env

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
        env_key = self.api_key_env_for_model(model) or api_key_env
        _provision_qwen_config(api_base_url, env_key, model)

    def format_stream_event(self, line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return line
        if isinstance(event, dict):
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
                api_base_url, KNOWN_MODELS.get("all", []), api_key_env, mcp_servers
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

    # pi 的项目级 MCP 配置。配置文件优先级（pi-mcp-adapter README）：
    # 全局 ~/.config/mcp/mcp.json > <Pi agent dir>/mcp.json > .mcp.json > .pi/mcp.json，
    # 越靠后优先级越高 —— 选 .pi/mcp.json 是因为它压得住用户可能已有的 .mcp.json，
    # 而 .mcp.json 是**通用**文件名，很可能被别的工具占用。
    #
    # 注意这不是「多写一份以防万一」：pi 的 MCP 说明里 .pi/mcp.json 就是 Pi 自己的
    # 项目层（`/mcp disable` 写的也是它）。
    mcp_workspace_config = (".pi", "mcp.json")

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
        env_key = self.api_key_env_for_model(model) or api_key_env
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
        mcp_servers: Optional[list] = None,
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
    **端到端跑通**（见 ``tests/mcp_echo_server.py`` 的说明），不是「配置写得对」。

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


# 优先级顺序即默认选择顺序（internal > claude > codebuddy > codex > kimi > qwen > opencode > pi > antigravity > grok）。
# 表里保留 gemini / cursor / antigravity 以维持既有下标不变，但这三项在 _HIDDEN_CLI_IDS 里，不进下拉。
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

    每项: {id, display_name, installed, priority, models, supports_mcp}，按优先级顺序排列。
    _HIDDEN_CLI_IDS 里的 CLI 不出现在结果中（adapter 本身仍可用 get_adapter 取到）。

    注意 priority 取 _ADAPTERS 里的下标而非结果下标：隐藏项不影响其余 CLI 的
    相对优先级，将来放回时也不用重排。

    ``supports_mcp`` 直接取自 adapter，前端据此在选项上标注「MCP 未接入」。**刻意不在
    前端硬编码名单** —— 那等于把同一份事实维护两遍，将来接一个新 CLI 就必然漏改一处。
    """
    result = []
    for index, adapter in enumerate(_ADAPTERS):
        if adapter.id in _HIDDEN_CLI_IDS:
            continue
        result.append({
            "id": adapter.id,
            "display_name": adapter.display_name,
            "installed": adapter.is_installed(),
            "priority": index,
            "models": list(adapter.known_models),
            "supports_mcp": adapter.supports_mcp,
        })
    return result
