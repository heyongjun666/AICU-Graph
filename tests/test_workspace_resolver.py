"""tests/test_workspace_resolver.py — V3-2.1 Workspace Resolver 单元测试。

验证 workspace_resolver 的纯函数语义：
- 共享根解析、分区解析（正常 / 空 output_path / 防穿越回退 / 无共享）。
- 所有返回路径为 canonical absolute path（Artifact Semantic Contract）。
"""

import os

from runtime.workspace_resolver import (
    _partition_of,
    resolve_partition,
    resolve_shared_workspace,
)


def test_resolve_shared_workspace(tmp_path):
    ws = str(tmp_path / "ws")
    expected = os.path.realpath(os.path.join(ws, "shared"))
    assert resolve_shared_workspace(ws) == expected
    # canonical absolute path 不变量
    assert os.path.isabs(resolve_shared_workspace(ws))


def test_partition_normal(tmp_path):
    shared = str(tmp_path / "shared")
    assert _partition_of(shared, "backend") == os.path.realpath(
        os.path.join(shared, "backend")
    )


def test_partition_nested_output_path(tmp_path):
    shared = str(tmp_path / "shared")
    assert _partition_of(shared, "backend/app") == os.path.realpath(
        os.path.join(shared, "backend", "app")
    )


def test_partition_empty_output_path_is_shared_root(tmp_path):
    shared = str(tmp_path / "shared")
    assert _partition_of(shared, "") == os.path.realpath(shared)
    assert _partition_of(shared, None) == os.path.realpath(shared)


def test_partition_traversal_falls_back_to_shared_root(tmp_path):
    shared = str(tmp_path / "shared")
    assert _partition_of(shared, "../outside") == os.path.realpath(shared)


def test_partition_absolute_output_path_falls_back(tmp_path):
    shared = str(tmp_path / "shared")
    outside = str(tmp_path / "outside")
    assert _partition_of(shared, outside) == os.path.realpath(shared)


def test_partition_no_shared_root_returns_empty():
    assert _partition_of("", "backend") == ""
    assert _partition_of(None, "backend") == ""


def test_resolve_partition(tmp_path):
    shared = str(tmp_path / "shared")
    config = {"_shared_workspace": shared, "_output_path": "backend"}
    assert resolve_partition(config) == os.path.realpath(
        os.path.join(shared, "backend")
    )


def test_resolve_partition_empty_output_path(tmp_path):
    shared = str(tmp_path / "shared")
    config = {"_shared_workspace": shared, "_output_path": ""}
    assert resolve_partition(config) == os.path.realpath(shared)


def test_resolve_partition_no_shared_returns_empty():
    assert resolve_partition({}) == ""
    assert resolve_partition({"_output_path": "backend"}) == ""


def test_resolve_partition_canonicalizes_shared_root(tmp_path):
    # shared_root 以相对/非规范形式给出，仍解析为 canonical absolute path
    shared = os.path.join(str(tmp_path), ".", "shared")
    config = {"_shared_workspace": shared, "_output_path": "backend"}
    expected = os.path.realpath(os.path.join(str(tmp_path), "shared", "backend"))
    assert resolve_partition(config) == expected
