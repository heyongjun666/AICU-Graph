"""CLI 的厂商路由、端点与 AICU 托管配置槽。

从 ``cli_adapters`` 拆出的一层：只回答「这次运行该把请求发到哪个端点」以及
「该把 CLI 自己那份配置文件改成什么样」，不涉及 adapter 类、子进程与输出解析。
``cli_adapters`` 仍原样再导出这些名字，外部调用方的导入路径不变。

三组东西，别混：
  - ``vendor_of_model`` / ``_CLI_NATIVE``：模型属于哪家厂商、该 CLI 原生说哪种协议；
  - ``native_base_url`` / ``provider_slot_base_url``：CLI 自身寻址 vs AICU 托管槽；
  - ``_provision_*`` / ``_release_*``：把选择落盘到 codex / qwen / pi 的配置文件。
"""

import json
import re
from pathlib import Path
from typing import Optional

from runtime.credentials import vendor_of_model
from runtime.mcp_manager import TRANSPORT_HTTP

# _remote_fields 是 MCP 条目的序列化细节，放在 mcp_codecs；这里只用到 opencode 的
# 内联配置（provider 与 mcp 共用一个环境变量，必须拼在同一份 JSON 里）。
from mcp_codecs import _remote_fields


# 各 CLI 的已知模型清单集中维护在 cli_models.json（便于后续增删模型）。
_MODELS_FILE = Path(__file__).resolve().parent / "config" / "cli_models.json"


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


def provider_route(provider_id: Optional[str], cli_name: Optional[str]) -> Optional[dict]:
    """Resolve a provider's CLI-specific connection profile.

    Legacy presets only have top-level connection fields, so they continue to return
    a synthesized profile. Gateway presets may override protocol and endpoint per CLI.
    """
    provider = (provider_id or "").strip().lower()
    cli = (cli_name or "").strip().lower()
    if not provider or not cli:
        return None
    preset = next((item for item in list_provider_presets() if str(item.get("id", "")).lower() == provider), None)
    if not preset:
        return None
    routes = preset.get("routes")
    if isinstance(routes, dict) and cli not in routes:
        return None
    route = routes.get(cli) if isinstance(routes, dict) else None
    if route is not None and not isinstance(route, dict):
        return None
    if route is not None and route.get("enabled") is False:
        return None
    result = {
        "provider_id": provider,
        "cli_name": cli,
        "api_base_url": preset.get("api_base_url"),
        "api_key_env": preset.get("api_key_env"),
        "wire_api": preset.get("wire_api"),
    }
    if isinstance(route, dict):
        result.update({key: value for key, value in route.items() if value is not None})
    return result


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
#   「No auth type is selected」—— 两条路都走不通（2026-09-16 本机实测：撤槽的 A/B 方案失败，
#   显式供端点的 C 方案通过）。所以同族也只能由
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


def _preset_base_urls(*, gateway_only: bool = False) -> set:
    """provider preset 声明用过的 api_base_url。

    ``gateway_only`` 只保留显式网关预置地址（含 OpenRouter）—— 模型原厂商路由
    不得覆盖这些地址。两种视图共用同一次收集，避免过滤条件各写一遍后漂移。
    """
    return {
        str(preset["api_base_url"])
        for preset in list_provider_presets()
        if preset.get("api_base_url")
        and (
            not gateway_only
            or preset.get("provider_kind") == "gateway"
            or preset.get("id") == "openrouter"
        )
    }


def _gateway_preset_base_urls() -> set:
    """返回显式网关预置地址，模型原厂商路由不得覆盖这些地址。"""
    return _preset_base_urls(gateway_only=True)


def native_base_url(
    cli_name: Optional[str], model: Optional[str], configured_base_url: Optional[str],
    provider_id: Optional[str] = None,
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
    # Gateway routes are already explicit and must never be rerouted by the
    # model's original vendor (e.g. openrouter/anthropic/* -> api.anthropic.com).
    if provider_route(provider_id, cli_name):
        return None

    if configured_base_url and str(configured_base_url) not in _preset_base_urls():
        return None
    if configured_base_url and str(configured_base_url) in _gateway_preset_base_urls():
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
    """该 CLI 是否有一份 AICU 托管的配置文件槽（未知 id 视为没有）。

    这里对 ``cli_adapters.get_adapter`` 做函数内导入：adapter 类依赖本模块（导入
    端点函数），模块级反向导入会成环；调用发生在运行期，那时 cli_adapters 早已加载完。
    """
    from cli_adapters import get_adapter

    try:
        return get_adapter(cli_name or "").managed_slot
    except ValueError:
        return False


def provider_slot_base_url(
    cli_name: Optional[str], model: Optional[str], configured_base_url: Optional[str],
    provider_id: Optional[str] = None,
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
    if provider_route(provider_id, cli_name):
        return str(configured_base_url) if configured_base_url else None
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
    cli_name: Optional[str], model: Optional[str], configured_base_url: Optional[str],
    provider_id: Optional[str] = None,
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
    route = provider_route(provider_id, cli_name)
    if route:
        route_url = route.get("api_base_url") or configured_base_url
        return (str(route_url) if route_url else None), "configured"

    configured = str(configured_base_url) if configured_base_url else None

    endpoint = native_base_url(cli_name, model, configured, provider_id=provider_id)
    addressed = endpoint or configured

    slot = provider_slot_base_url(cli_name, model, addressed, provider_id=provider_id)
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
        # opencode 的本地 server 条目：`command` 是**数组**（程序 + 参数一起放），
        # 与 claude 系的 `command` 字符串 + `args` 数组不是一回事，所以不能复用
        # _cc_mcp_json。`type` 与 `enabled` 都是必填/推荐项。
        #
        # 远程条目是 `type: "remote"` + url + headers —— 与 `local` 是两个形状不同的
        # 联合类型（`command` 与 `url` 各自只属于其中一支），所以这里也必须分支而不是
        # 统一塞键。注意顶层键是 `mcp`（**不是** `mcpServers`），这是 opencode 独有的
        # 写法，见 _opencode_config_content 的调用方。
        entries: dict = {}
        for server in servers:
            if server.transport == TRANSPORT_HTTP:
                entries[server.name] = {
                    "type": "remote",
                    **_remote_fields(server, "url"),
                    "enabled": True,
                }
                continue
            entries[server.name] = {
                "type": "local",
                "command": [server.command, *server.args],
                "enabled": True,
                **({"environment": dict(server.env)} if server.env else {}),
            }
        config["mcp"] = entries
    return json.dumps(config, ensure_ascii=False)
