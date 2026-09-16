"""仓库根，以及「相对仓库根解析」这条统一约定的唯一实现。

角色 yaml 的 ``skills_dir`` 和 MCP 注册表的 ``command`` / ``args`` 遵循同一条约定：
绝对路径原样用、``~`` 展开、相对路径按仓库根解析 —— 于是 clone 下来即可用，不必改配置。
这条规则写在用户可见的地方（角色字段说明、``mcp_servers.json`` 的注释），所以实现只能
有一份：各写一遍的话，改规则时漏掉的那一边表现是「路径静默解析到别处」，不报错。
"""
from __future__ import annotations

from pathlib import Path

# 本模块在 runtime/ 下，仓库根是它的上一级
REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve_repo_relative(value: str) -> Path:
    """``~`` 展开；相对路径按仓库根解析；绝对路径原样返回。

    相对分支带 ``.resolve()``：调用方多半要拿结果去和别的 realpath 比对
    （如 skills_dir 的存在性校验），带 ``..`` 或软链的写法必须先归一化才比得上。
    绝对分支不动 —— 那是调用方自己给的路径，替他改写反而不好排查。
    """
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (REPO_ROOT / path).resolve()
