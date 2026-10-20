"""MCP server 注册表的读取、写入与名字解析。

角色 yaml 只写 server 名字（``mcp: [echo-probe]``），``command`` / ``args`` 集中在仓库根的
``mcp_servers.json``。与 ``skills/`` 同一思路：角色只引用名字，实现在别处。这样角色配置
（可在设置页上编辑）里不会出现「一段将被执行的命令」，命令只在一个地方维护。

与 ``runtime/mcp_client.py`` 的分工：本模块只管**注册表长什么样、名字怎么解析、怎么写回去**，
那边只管**真去连一个 server**。两者失败面完全不同 —— 这里的错是配置写坏了（读文件就能定位），
那边的错是环境问题（命令不存在、缺依赖、握手超时），用户要做的事也不一样，所以报错分成
``McpConfigError`` 与 ``McpProbeError`` 两个类型，不合并。

放在这里而不是塞进 ``cli_adapters.py``：注册表是**与 CLI 无关**的，同一个 server 定义要能
下发给任意一家 CLI。
"""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from runtime.jsonio import atomic_write_json
from runtime.repo_paths import REPO_ROOT, resolve_repo_relative

REGISTRY_FILE = REPO_ROOT / "config" / "mcp_servers.json"

# 两种传输。AICU 只支持这两种：本地 stdio（起子进程）与远程 Streamable HTTP（打一个
# URL）。**不支持 SSE** —— 那是被 Streamable HTTP 取代的旧远程传输，见 transport_of。
TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"

# server 名与 env 键都走白名单。理由不是路径安全（这里不拼文件路径），而是它们会变成
# **CLI 的参数与配置键** —— 见 validate_server 的 docstring。
# 名字的形状与 skills_manager._validate_id 一致：都是「一个会被到处引用的标识符」。
_SERVER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def transport_of(spec: dict) -> str:
    """一条原始注册表条目走哪种传输：有非空 ``url`` 就是 http，否则 stdio。

    **不存 transport 键，由 url 派生。** 只有一种远程传输可写，再存一个能与 ``url``
    打架的键，等于把本项目反复防的那类「存进去了但静默不生效」重新引进来 —— 配置文件
    里看起来两样都在，运行时按哪个走却取决于读的那一方。派生就没有第二个说法。

    读路径（``_parse_entry``）与写路径（``validate_server``）共用本函数，与两者共用
    ``_parse_entry`` 是同一条纪律：两边不能各判一套。
    """
    url = spec.get("url")
    return TRANSPORT_HTTP if isinstance(url, str) and url.strip() else TRANSPORT_STDIO


class McpConfigError(ValueError):
    """注册表本身有问题，或角色引用了一个不存在的 server。"""


@dataclass
class McpServer:
    """注册表里的一条 server 定义，路径已解析成绝对路径。

    这是个**与 CLI 无关**的中立表示：``command`` 与 ``args`` 分开存，不预设哪家
    CLI 想要什么形状。各 adapter 自己决定怎么序列化它 —— claude 系要
    ``{"command": str, "args": [...]}``，opencode 要 ``command: [str, ...]``，
    codex 要拆成一对对 ``-c`` 键值。把序列化放在 adapter 里，是因为那才是
    「这家 CLI 要什么」的知识该待的地方。

    ``transport`` 决定 adapter 读哪组字段：``stdio`` 用 ``command`` / ``args`` / ``env``，
    ``http`` 用 ``url`` / ``headers``，另一组恒为空。它由 ``transport_of`` 从 ``url``
    派生（见那边的理由），不是用户写的字段。
    """

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""
    required: bool = True
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    transport: str = TRANSPORT_STDIO


def _resolve_path(value: str) -> str:
    """把注册表里的路径解析成绝对路径。

    不含路径分隔符的值（如 ``python3``）不是路径，原样保留交由 PATH 查找；其余走
    「``~`` 展开 / 绝对原样 / 相对按仓库根」这条与角色 yaml ``skills_dir`` 共用的规则
    （见 ``runtime/repo_paths.py``），所以 ``tools/mcp_echo_server.py`` 这种写法
    clone 下来即可用，不必改配置。

    两条**先于**上面规则的例外，因为它们在字面上含 ``/`` 却根本不是路径：

    - ``@`` 开头 —— npm 作用域包名（``npx -y @modelcontextprotocol/server-filesystem``）。
      这是市面上绝大多数 MCP server 的标准写法，按路径解析会变成一个不存在的绝对路径，
      npx 拿到后只会报自己的错，用户很难反推回「包名被当路径了」。
    - 含 ``://`` —— URL 形式的参数（``--endpoint https://host/x``）。

    判定刻意不做「文件存不存在」那一档：那会让同一份配置在不同机器上解析出不同结果。
    """
    if value.startswith("@"):
        return value
    if "://" in value:
        return value
    if os.sep not in value and "/" not in value:
        return value
    return str(resolve_repo_relative(value))


def _parse_entry(name: str, spec: Any) -> McpServer:
    """把注册表里的一条原始条目解析成 ``McpServer``。

    读（``load_registry``）与写前校验（``validate_server``）共用这一份实现。分开写的话，
    迟早会出现「界面上存得进去、运行时读不出来」的条目 —— 那是这类管理界面最典型的坏法，
    而且失败点在另一个进程、另一段时间，极难定位。
    """
    if not isinstance(spec, dict):
        raise McpConfigError(f"MCP 注册表条目 '{name}' 不是对象")
    description = str(spec.get("description") or "")
    required = bool(spec.get("required", True))

    if transport_of(spec) == TRANSPORT_HTTP:
        url = spec.get("url")
        if not isinstance(url, str) or not url.strip():
            raise McpConfigError(f"MCP 注册表条目 '{name}' 的 url 不是非空字符串")
        raw_headers = spec.get("headers") or {}
        if not isinstance(raw_headers, dict):
            raise McpConfigError(f"MCP 注册表条目 '{name}' 的 headers 不是对象")
        # url 与 headers **绝不经过 _resolve_path**：它们不是路径，且含 ``://`` 的 url
        # 一旦被当成仓库相对路径就会变成一个不存在的本地路径。这里一个字段一个字段地
        # 取，不经手那个函数 —— 是结构上的保证，不是靠注释提醒。
        return McpServer(
            name=str(name),
            command="",
            args=[],
            env={},
            description=description,
            required=required,
            url=url,
            headers={str(k): str(v) for k, v in raw_headers.items()},
            transport=TRANSPORT_HTTP,
        )

    command = spec.get("command")
    if not command or not isinstance(command, str):
        raise McpConfigError(f"MCP 注册表条目 '{name}' 缺少 command")
    args = spec.get("args") or []
    if not isinstance(args, list):
        raise McpConfigError(f"MCP 注册表条目 '{name}' 的 args 不是列表")
    env = spec.get("env") or {}
    if not isinstance(env, dict):
        raise McpConfigError(f"MCP 注册表条目 '{name}' 的 env 不是对象")
    return McpServer(
        name=str(name),
        command=_resolve_path(command),
        args=[_resolve_path(str(arg)) for arg in args],
        env={str(k): str(v) for k, v in env.items()},
        description=description,
        required=required,
    )


def _load_raw(path: Optional[Path] = None) -> dict:
    """读注册表的**原始文档**：顶层形状校验过，条目一个字段都没动。

    写路径必须从这里出发，不能从 ``load_registry`` 的返回值重建。后者已经把
    ``command`` / ``args`` 解析成绝对路径（``_resolve_path``），照它写回去，
    注册表里 ``tools/mcp_echo_server.py`` 这种相对写法就会变成写死在本机的绝对路径 ——
    功能照常，换台机器才炸，是那种不会有人立刻发现的坏法。``_comment`` 同理，只能靠
    原样读回来才留得住。
    """
    registry_path = Path(path) if path else REGISTRY_FILE
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise McpConfigError(f"MCP 注册表不存在: {registry_path}") from None
    except json.JSONDecodeError as error:
        raise McpConfigError(f"MCP 注册表不是合法 JSON: {registry_path} — {error}") from None

    entries = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        raise McpConfigError(f"MCP 注册表缺少 servers 对象: {registry_path}")
    return raw


def server_names(path: Optional[Path] = None) -> set[str]:
    """返回注册表中的原始 server 名称，不解析条目内容。

    设置页需要在存在坏条目时仍然判断重名；完整解析留给运行时的
    ``load_registry``，否则用户连修复其它条目的机会都没有。
    """
    return {str(name) for name in _load_raw(path)["servers"]}


def load_registry(path: Optional[Path] = None) -> dict[str, McpServer]:
    """读取注册表；文件缺失或损坏时抛 McpConfigError（不静默吞掉）。

    只读语义：返回的 ``McpServer`` 里路径已解析成绝对路径，**不适合再写回文件**。
    要改注册表请用 ``save_server`` / ``delete_server``，它们从原始文档出发。
    """
    raw = _load_raw(path)
    return {str(name): _parse_entry(name, spec) for name, spec in raw["servers"].items()}


def resolve_servers(
    names: Optional[list[str]], registry: Optional[dict[str, McpServer]] = None
) -> list[McpServer]:
    """把角色声明的名字列表解析成 server 定义；名字不存在时报错并列出可用的。"""
    if not names:
        return []
    available = registry if registry is not None else load_registry()
    missing = [name for name in names if name not in available]
    if missing:
        raise McpConfigError(
            f"角色配置的 mcp 在注册表 ({REGISTRY_FILE}) 中未找到: {missing}。"
            f"可用 MCP server: {sorted(available)}"
        )
    return [available[name] for name in names]


def validate_server(name: str, spec: Any) -> dict:
    """校验并规范化一条 server 定义，返回可直接写进注册表的条目。

    名字与 env 键走白名单，理由不是「防目录穿越」（这里根本不拼文件路径），而是它们会
    变成 **CLI 的参数与配置键**：

    - grok 把名字写进 ``cli_adapters._grok_mcp_toml`` 生成的 TOML 表名
      ``[mcp_servers.<name>]``
    - codex 把名字与 env 键拼进 ``cli_adapters._codex_mcp_args`` 生成的
      ``mcp_servers.<name>.env.<KEY>=...`` 键路径
    - gemini 把名字作为 ``cli_adapters.GeminiAdapter.mcp_flags`` 生成的
      ``--allowed-mcp-server-names <name>`` 值

    名字里带 ``.`` 会把 codex 的键路径劈成嵌套，带 ``]`` 会把 TOML 写坏，env 键里带 ``.``
    同样毁掉 codex 那一侧。这些在写盘时不拦，就要等到某个 CLI 报一个与 MCP 毫无关系的错时
    才暴露出来。

    两种传输各有各的规则，互斥：本地看 ``command`` / ``args`` / ``env``，远程看
    ``url`` / ``headers``。**另一组给了就报错，不忽略** —— 一个存得进去却永远不会被下发的
    字段，用户会以为配上了。这条与 ``_resolve_path`` 那个缺陷是同一类坏法。

    返回前用 ``_parse_entry`` 自检一遍，保证写进去的东西运行时一定读得出来。
    """
    if not isinstance(name, str) or not _SERVER_NAME_PATTERN.match(name):
        raise McpConfigError(
            f"非法 MCP server 名字 '{name}'：需以小写字母开头，"
            f"其后只能是小写字母、数字、下划线或连字符"
        )
    if not isinstance(spec, dict):
        raise McpConfigError("MCP server 定义必须是一个对象")

    if transport_of(spec) == TRANSPORT_HTTP:
        entry = _validate_remote_entry(spec)
    else:
        entry = _validate_stdio_entry(spec)

    _parse_entry(name, entry)  # 自检：读得出来才算合法
    return entry


def _common_tail(entry: dict[str, Any], spec: dict) -> None:
    """两种传输共有的尾巴：``description`` → ``required``。

    与 ``command``/``url`` 一起构成文件里的键序，读文件的人可以照着 ``_comment`` 一条条对。
    """
    description = str(spec.get("description") or "")
    if description:
        entry["description"] = description
    entry["required"] = bool(spec.get("required", True))


def _validate_stdio_entry(spec: dict) -> dict:
    """本地 stdio 条目的校验与规范化。"""
    url = spec.get("url")
    if url is not None and not isinstance(url, str):
        raise McpConfigError("url 必须是字符串")
    if isinstance(url, str) and url and not url.strip():
        raise McpConfigError("url 不能只由空白组成")
    # url 为 None 或空串时视为「没写 url」—— 界面清空地址栏后回传的就是空串，
    # 与 transport_of 的判据（非空才算 url）保持一致。

    command = spec.get("command")
    if not isinstance(command, str) or not command.strip():
        raise McpConfigError("command 必填，且不能为空")

    raw_args = spec.get("args") or []
    if not isinstance(raw_args, list):
        raise McpConfigError("args 必须是列表")

    raw_env = spec.get("env") or {}
    if not isinstance(raw_env, dict):
        raise McpConfigError("env 必须是对象")
    env: dict[str, str] = {}
    for key, value in raw_env.items():
        if not isinstance(key, str) or not _ENV_KEY_PATTERN.match(key):
            raise McpConfigError(
                f"非法环境变量名 '{key}'：需以字母或下划线开头，其后只能是字母、数字或下划线"
            )
        env[key] = str(value)

    if spec.get("headers"):
        raise McpConfigError("headers 只能与 url 一起用（远程 server）；本地 server 用 env")

    # 键的书写顺序即文件里看到的顺序：command → args → env → description → required，
    # 与仓库里那份 _comment 说明的字段顺序一致。
    entry: dict[str, Any] = {"command": command}
    args = [str(arg) for arg in raw_args]
    if args:
        entry["args"] = args
    if env:
        entry["env"] = env
    _common_tail(entry, spec)
    return entry


def _validate_remote_entry(spec: dict) -> dict:
    """远程（Streamable HTTP）条目的校验与规范化。"""
    url = spec.get("url")
    if not isinstance(url, str) or not url.strip():
        raise McpConfigError("url 必填，且不能为空")

    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise McpConfigError(
            f"url 的协议必须是 http 或 https（当前是 '{scheme or '没有协议前缀'}'）。"
            f"AICU 只支持 Streamable HTTP 型远程 server，不支持 SSE 与其他协议"
        )

    command = spec.get("command")
    if isinstance(command, str) and command.strip():
        raise McpConfigError("command 与 url 二选一：这是远程 server，请删掉 command")
    for key in ("args", "env"):
        if spec.get(key):
            raise McpConfigError(
                f"{key} 只能用于本地 server：远程 server 的连接信息在 url/headers 里，"
                f"CLI 配置里没有放 {key} 的位置，留着它不会被下发"
            )

    raw_headers = spec.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise McpConfigError("headers 必须是对象")
    headers: dict[str, str] = {}
    for key, value in raw_headers.items():
        header_name = str(key).strip()
        if not header_name:
            raise McpConfigError("headers 的键不能为空")
        header_value = str(value)
        # 换行会把一个 header 劈成两个（HTTP 头注入）。挡在这里而不是等 http.client 报错，
        # 是因为那时用户看到的是一句与 MCP 无关的话。
        if any("\n" in text or "\r" in text for text in (header_name, header_value)):
            raise McpConfigError(f"header '{header_name}' 的值含换行符，这会被当成另一个 HTTP 头")
        # header 名刻意不套 env 那种正则：真实的 header 名带连字符（X-Api-Key）。
        headers[header_name] = header_value

    entry: dict[str, Any] = {"url": url}
    if headers:
        entry["headers"] = headers
    _common_tail(entry, spec)
    return entry


def build_server(name: str, spec: Any) -> McpServer:
    """校验一条定义并解析成可直接连接的 ``McpServer``（路径按仓库根解析）。

    给「测试连接」用：那时要连的是屏幕上那套**还没保存**的值，而不是注册表里的那一份，
    所以不能走 ``load_registry``。解析规则与运行时同一条（都过 ``_parse_entry``），
    否则会出现「页面上测通了、节点起不来」。
    """
    return _parse_entry(name, validate_server(name, spec))


def list_servers(path: Optional[Path] = None) -> list[dict]:
    """给界面的列表：原始条目 + ``name``，按名字排序。

    返回的是**原始** ``command`` / ``args``（未解析成绝对路径），因为界面上要显示和编辑的
    就是用户写下的那份；解析成绝对路径是运行时的事。

    条目坏掉时不抛错，用空值兜住 —— 界面正是用来修它的地方，读不出来就没法修。真正要
    「坏条目必须炸」的是 ``load_registry``，那是运行路径。
    """
    raw = _load_raw(path)
    result: list[dict] = []
    for name in sorted(raw["servers"]):
        spec = raw["servers"][name]
        if not isinstance(spec, dict):
            spec = {}
        args = spec.get("args") or []
        env = spec.get("env") or {}
        item = {
            "name": str(name),
            "command": str(spec.get("command") or ""),
            "args": [str(item) for item in args] if isinstance(args, list) else [],
            "env": {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
            "description": str(spec.get("description") or ""),
            "required": bool(spec.get("required", True)),
        }
        # url / headers 只对远程条目出现。本地条目保持六个键不变 —— 界面上那套「命令 +
        # 参数 + 环境变量」的表单正是照这个形状写的，多两个恒为空的键只会让人以为要填。
        if transport_of(spec) == TRANSPORT_HTTP:
            raw_headers = spec.get("headers") or {}
            item["url"] = str(spec.get("url") or "")
            item["headers"] = (
                {str(k): str(v) for k, v in raw_headers.items()}
                if isinstance(raw_headers, dict)
                else {}
            )
        result.append(item)
    return result


def save_server(name: str, spec: Any, path: Optional[Path] = None) -> dict:
    """新增或整体替换一条 server 定义（界面只有「保存」一个动作，两者同一条路径）。"""
    registry_path = Path(path) if path else REGISTRY_FILE
    entry = validate_server(name, spec)
    raw = _load_raw(registry_path)
    raw["servers"][name] = entry
    atomic_write_json(str(registry_path), raw)
    return {"name": name, **entry}


def delete_server(name: str, path: Optional[Path] = None) -> None:
    """删掉一条 server 定义；不存在时抛 KeyError。

    这里**不检查**还有没有角色在引用它：引用是运行时才解析的（``resolve_servers``），
    用户完全可以先把角色改掉再来删。界面会在确认框里列出引用者，把判断留给用户；
    真删漏了，那条角色下次启动会带着「可用 server 列表」当场报错，不会静默。
    """
    registry_path = Path(path) if path else REGISTRY_FILE
    raw = _load_raw(registry_path)
    if name not in raw["servers"]:
        raise KeyError(f"MCP server '{name}' not found")
    del raw["servers"][name]
    atomic_write_json(str(registry_path), raw)


def read_servers(names: Iterable[str], path: Optional[Path] = None) -> dict[str, dict]:
    """按名字取注册表里的**原始条目**（未知键也保留）；不存在的名字不出现在结果里。

    导出需要原样带走条目，``list_servers`` 会丢掉不认识的键，不能用。
    """
    servers = _load_raw(path)["servers"]
    return {
        name: json.loads(json.dumps(servers[name]))
        for name in names
        if name in servers
    }


def restore_server(name: str, spec: Optional[dict], path: Optional[Path] = None) -> None:
    """回滚用：把条目原样写回（``spec`` 为 None 表示删掉），不做校验。

    被回滚的旧条目可能本来就不合法，走 ``save_server`` 会让回滚自己失败。
    """
    registry_path = Path(path) if path else REGISTRY_FILE
    raw = _load_raw(registry_path)
    if spec is None:
        raw["servers"].pop(name, None)
    else:
        raw["servers"][name] = spec
    atomic_write_json(str(registry_path), raw)


def launch_problems(spec: Any) -> list[dict]:
    """本机上能否起这条 stdio 条目的静态检查（只查文件系统，不启动进程、不联网）。

    返回 ``[{"kind": ..., "detail": ...}]``，空列表表示没发现问题。远程条目不查。
    ``-`` 开头的参数是选项而非路径，不检查；绝对路径参数可能是尚未创建的数据目录，
    同样不检查，只检查按仓库根解析的相对路径（例如自带的 ``tools/mcp_echo_server.py``）。
    """
    if not isinstance(spec, dict) or transport_of(spec) != TRANSPORT_STDIO:
        return []
    problems: list[dict] = []
    command = str(spec.get("command") or "")
    resolved = _resolve_path(command)
    if resolved == command:
        if shutil.which(command) is None:
            problems.append({"kind": "mcp_command_missing", "detail": command})
    elif not Path(resolved).exists():
        problems.append({"kind": "mcp_command_missing", "detail": command})
    args = spec.get("args")
    for arg in args if isinstance(args, list) else []:
        arg = str(arg)
        if arg.startswith("-") or Path(arg).expanduser().is_absolute():
            continue
        resolved = _resolve_path(arg)
        if resolved != arg and not Path(resolved).exists():
            problems.append({"kind": "mcp_arg_missing", "detail": arg})
    return problems


def roles_referencing(name: str, configs: Iterable[tuple[str, str, Any]]) -> list[dict]:
    """在一串 ``(角色 id, 角色类型, 配置)`` 里找出声明了这个 server 的角色。

    只认 ``mcp`` 字段里出现过这个名字，不知道角色文件在哪、怎么合并 default ——
    那是 ``RolesManager`` 的知识，调用方把它的 ``iter_role_configs()`` 喂进来即可。
    拆成两半是为了让两边各自可测，也不必让本模块反向依赖角色模块。

    传进来的必须是**合并 default 之后**的配置：在 ``default.yaml`` 里声明 mcp 是最常见的
    用法，只看角色自己的 yaml 会漏掉全部引用者，用户就会以为这个 server 没人用。

    返回 ``[{"id", "type"}]`` 而不是角色 id 列表：**同名角色在 Manager 与 Worker 下可以
    同时存在**（两个目录各一份），只回 id 会让界面上写着「被 dev 引用」却说不清是哪一个。
    排序与 ``RolesManager.list_roles()`` 一致（先 type 后 id）。
    """
    hits: set[tuple[str, str]] = set()
    for role_id, role_type, config in configs:
        mcp = config.get("mcp") if isinstance(config, dict) else None
        if isinstance(mcp, str):
            # 角色编辑器和运行时都把单数标量视为一个 server 名字，而不是字符序列。
            names = [mcp]
        elif isinstance(mcp, list):
            names = [str(item) for item in mcp]
        else:
            names = []
        if name in names:
            hits.add((str(role_type), str(role_id)))
    return [{"id": role_id, "type": role_type} for role_type, role_id in sorted(hits)]
