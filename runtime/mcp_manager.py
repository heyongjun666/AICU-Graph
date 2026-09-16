"""MCP server 注册表的读取与名字解析。

角色 yaml 只写 server 名字（``mcp: [echo-probe]``），``command`` / ``args`` 集中在仓库根的
``mcp_servers.json``。与 ``skills/`` 同一思路：角色只引用名字，实现在别处。这样角色配置
（可在设置页上编辑）里不会出现「一段将被执行的命令」，命令只在一个地方维护。

与 ``runtime/mcp_client.py`` 的分工：本模块只管**注册表长什么样、名字怎么解析**，那边
只管**真去连一个 server**。两者失败面完全不同 —— 这里的错是配置写坏了（读文件就能定位），
那边的错是环境问题（命令不存在、缺依赖、握手超时），用户要做的事也不一样，所以报错分成
``McpConfigError`` 与 ``McpProbeError`` 两个类型，不合并。

放在这里而不是塞进 ``cli_adapters.py``：注册表是**与 CLI 无关**的，同一个 server 定义要能
下发给任意一家 CLI。前端后续要加 MCP 的增删改查入口时，写盘与校验也归本模块。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from runtime.repo_paths import REPO_ROOT, resolve_repo_relative

REGISTRY_FILE = REPO_ROOT / "mcp_servers.json"


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
    """

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""
    required: bool = True


def _resolve_path(value: str) -> str:
    """把注册表里的路径解析成绝对路径。

    不含路径分隔符的值（如 ``python3``）不是路径，原样保留交由 PATH 查找；其余走
    「``~`` 展开 / 绝对原样 / 相对按仓库根」这条与角色 yaml ``skills_dir`` 共用的规则
    （见 ``runtime/repo_paths.py``），所以 ``tests/mcp_echo_server.py`` 这种写法
    clone 下来即可用，不必改配置。
    """
    if os.sep not in value and "/" not in value:
        return value
    return str(resolve_repo_relative(value))


def load_registry(path: Optional[Path] = None) -> dict[str, McpServer]:
    """读取注册表；文件缺失或损坏时抛 McpConfigError（不静默吞掉）。"""
    registry_path = path or REGISTRY_FILE
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise McpConfigError(f"MCP 注册表不存在: {registry_path}") from None
    except json.JSONDecodeError as error:
        raise McpConfigError(f"MCP 注册表不是合法 JSON: {registry_path} — {error}") from None

    entries = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        raise McpConfigError(f"MCP 注册表缺少 servers 对象: {registry_path}")

    servers: dict[str, McpServer] = {}
    for name, spec in entries.items():
        if not isinstance(spec, dict):
            raise McpConfigError(f"MCP 注册表条目 '{name}' 不是对象")
        command = spec.get("command")
        if not command or not isinstance(command, str):
            raise McpConfigError(f"MCP 注册表条目 '{name}' 缺少 command")
        args = spec.get("args") or []
        if not isinstance(args, list):
            raise McpConfigError(f"MCP 注册表条目 '{name}' 的 args 不是列表")
        env = spec.get("env") or {}
        if not isinstance(env, dict):
            raise McpConfigError(f"MCP 注册表条目 '{name}' 的 env 不是对象")
        servers[str(name)] = McpServer(
            name=str(name),
            command=_resolve_path(command),
            args=[_resolve_path(str(arg)) for arg in args],
            env={str(k): str(v) for k, v in env.items()},
            description=str(spec.get("description") or ""),
            required=bool(spec.get("required", True)),
        )
    return servers


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
