"""runtime/workspace_resolver.py — 会话共享工作区解析（V3-2.1 产物单一权威）。

定位：本模块只负责「解析出产物根」，统一计算「会话共享根 + 节点分区 = 产物工作区」，
替换此前散落在 runtime.py / worker.py / manager.py / registry.py 的 7+ 处重复推导，
保证任何位置对分区路径的推导字节级一致（canonical absolute path）。

命名用 workspace_resolver 而非 workspace.py：避免污染 workspace 概念
（未来可能有 WorkspaceManager / WorkspaceSnapshot / WorkspaceGC / WorkspaceLock），
该模块只负责解析。

不变量（Artifact Semantic Contract，V3-2.1 冻结）：
- 所有返回路径均为 canonical absolute path（os.path.realpath）。
- 分区必须位于共享根内（防穿越）；输出路径非空时 `{shared_root}/{output_path}`，
  空输出路径 = 共享根本身（高权限模式）。
- `_shared_workspace` / `_output_path` 由 Runtime `_prepare_node_config` 注入
  （session 级单一共享根，全节点自动挂载）。

本模块为纯函数（仅依赖 os），无 I/O 副作用（不 mkdir、不扫描、不写文件）。
"""

import os


def resolve_shared_workspace(workspace_root: str) -> str:
    """会话共享根 = {workspace_root}/shared（与 runtime._prepare_node_config 一致）。

    Args:
        workspace_root: 会话工作区根目录。

    Returns:
        canonical absolute path 的共享根。
    """
    return os.path.realpath(os.path.join(workspace_root, "shared"))


def partition_of(shared_root: str, output_path: str) -> str:
    """纯函数：{shared_root}/{output_path}；空 output_path → 共享根本身；防穿越回退共享根。

    与 ``resolve_partition`` 的分工：那个从节点 config 里读键，这个收**显式的**
    (root, path) 两参数，供手上只有这两个值、拿不出 config dict 的调用方使用
    （如 Runtime 注入 `_shared_workspace` 时、Manager 渲染子节点的 children_info 时）。
    本函数是模块公开面的第二半 —— 两处调用方都在别的模块，写成下划线私有名会让
    「能不能用」变成靠 grep 猜的事。

    Args:
        shared_root: 会话共享根。
        output_path: 节点专属分区（相对路径，`_output_path`，可为空串）。

    Returns:
        canonical absolute path 的分区路径；shared_root 为空 → ""。
    """
    if not shared_root:
        return ""
    root = os.path.realpath(shared_root)
    partition = os.path.join(root, output_path) if output_path else root
    # 先 realpath 再 commonpath：os.path.commonpath 会词法折叠 `..`，
    # 若在 realpath 前比较，`shared/../outside` 会被误判为在共享根内（穿越逃逸）。
    partition = os.path.realpath(partition)
    try:
        if os.path.commonpath([root, partition]) != root:
            partition = root
    except ValueError:
        partition = root
    return partition


def resolve_partition(config: dict) -> str:
    """从节点 config 读 _shared_workspace / _output_path 求分区；无共享 → ""。

    这是 worker / manager 侧的公开 API（用户指定签名）：所有需要「节点产物工作区」
    的消费点统一走此函数，不再各自重复推导。

    Args:
        config: 节点 config（含 `_shared_workspace` 与 `_output_path` 键）。

    Returns:
        canonical absolute path 的分区路径；无 `_shared_workspace` → ""。
    """
    shared_root = config.get("_shared_workspace")
    if not shared_root:
        return ""
    return partition_of(shared_root, config.get("_output_path") or "")
