"""codegraph.py — CodeGraph CLI provider 层。

只做原始查询（返回可能 >4KB 的 Raw 文本），不做语义判断、不做预算裁剪。
预算/去重/优先级由 resolver.py / __init__.py 编排负责。
所有异常向上抛，由入口 fail-silent 兜底。
"""

import os
import subprocess
from typing import List

_CODEGRAPH_BIN = os.environ.get("CODEGRAPH_BIN", "codegraph")


def _run_cli(args: List[str], cwd: str, timeout: int = 120) -> str:
    """运行 codegraph CLI，返回 stdout；非零退出抛异常。"""
    proc = subprocess.run(
        [_CODEGRAPH_BIN, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"codegraph {' '.join(args)} failed rc={proc.returncode}: "
            f"{proc.stderr[-500:]}"
        )
    return proc.stdout


def _project_root() -> str:
    """从当前 cwd 向上找最近的 .codegraph 目录（codegraph 同款 walk-up 解析）。"""
    cur = os.path.abspath(os.getcwd())
    while True:
        if os.path.isdir(os.path.join(cur, ".codegraph")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise RuntimeError("no .codegraph found by walk-up from cwd")
        cur = parent


def query_group_inventory(shared_root: str) -> str:
    """产出组图文件+符号清单（Raw）。调用前需 GroupGraphState.ensure_ready。"""
    return _run_cli(["files"], shared_root)


def query_symbol(symbol: str, kind: str = "node") -> str:
    """对项目图查询单个符号：node / callers / callees / impact。"""
    return _run_cli([kind, symbol], _project_root())
