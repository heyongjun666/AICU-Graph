"""tests/test_shared_target.py — AICU V3-2.1 shared_target 单元测试。

验证 shared_target 启用后 CC 直写共享分区（产物唯一权威，零拷贝）：
- _cc_deliverable_dir / _cc_output_path 统一指向分区（modular / 非 modular 一致，
  杜绝 {partition}/output 嵌套副本）。
- _format_instruction：{deliverable_dir}→分区、{output_file}→""。
- _finalize_result：final_output_path==分区，且不再产生 workspace/final_file
  （即 nodes/{id}/output）副本。
- 多 CC 传 shared_target → 同样直写同一分区（peer 编排：产生方落分区，
  审查方在自己 CC 工作区出 review；_copy_best_to_cc0 对同分区自动跳过复制）。
- 默认 shared_target=None → 既有行为不变（私有 deliverable_dir 保留）。
"""

import os
from pathlib import Path

import pytest

from aicu import AICU


def _make_shared_layout(tmp_path, output_path="backend"):
    ws = str(tmp_path / "ws")
    partition = os.path.realpath(os.path.join(ws, "shared", output_path))
    os.makedirs(partition, exist_ok=True)
    return ws, partition


def _make_aicu(tmp_path, *, shared_target=None, num_ccs=1, modular=True,
               output_file=None, role="code", output_path="backend"):
    """构造 AICU。shared_target 为 None → 不传（Legacy）；非 None → 透传给构造。"""
    ws, partition = _make_shared_layout(tmp_path, output_path)
    kwargs = dict(
        role_type="worker",
        role=role,
        parent_workspace=ws,
        num_ccs=num_ccs,
        skills=[],          # 跳过 skills_dir 校验，测试不需要真实 skill
        modular_output=modular,
        output_file=output_file,
    )
    if shared_target is not None:
        kwargs["shared_target"] = shared_target
    aicu = AICU(**kwargs)
    return aicu, ws, partition


# ============================================================================
# 构造与 invariant
# ============================================================================


def test_shared_target_canonicalized(tmp_path):
    """shared_target 以非规范/相对形式给出，仍被 realpath 化为 canonical absolute path。"""
    ws, partition = _make_shared_layout(tmp_path)
    non_canonical = os.path.join(ws, ".", "shared", "backend")
    aicu, _, _ = _make_aicu(tmp_path, shared_target=non_canonical)
    assert aicu.shared_target == partition
    assert os.path.isabs(aicu.shared_target)
    assert aicu.shared_target == os.path.realpath(aicu.shared_target)


def test_multi_cc_shared_target_ok(tmp_path):
    """多 CC 传 shared_target → 不 raise，所有 CC 直写同一分区。"""
    _, partition = _make_shared_layout(tmp_path)
    aicu, _, _ = _make_aicu(tmp_path, shared_target=partition, num_ccs=2)
    assert aicu.shared_target == partition
    # 所有 CC 的交付目录都指向同一分区（peer 编排同分区直写）
    assert aicu._cc_deliverable_dir(0) == aicu._cc_deliverable_dir(1) == Path(partition)


def test_no_shared_target_keeps_private_deliverable_dir(tmp_path):
    """默认 shared_target=None：私有 deliverable_dir 保留（字节级不变）。"""
    aicu, ws, _ = _make_aicu(tmp_path, role="code")  # code.yaml deliverable_dir=deliverables
    assert aicu.shared_target is None
    assert aicu._cc_deliverable_dir(0) == Path(ws) / "cc_0" / "deliverables"


def test_shared_target_skips_dead_deliverables_dir(tmp_path):
    """shared_target 模式不建私有 cc_{i}/deliverables 死目录。"""
    _, partition = _make_shared_layout(tmp_path)
    aicu, ws, _ = _make_aicu(tmp_path, shared_target=partition)
    assert not (Path(ws) / "cc_0" / "deliverables").exists()


# ============================================================================
# 路径语义：_cc_deliverable_dir / _cc_output_path
# ============================================================================


def test_cc_deliverable_dir_is_partition(tmp_path):
    _, partition = _make_shared_layout(tmp_path)
    aicu, _, _ = _make_aicu(tmp_path, shared_target=partition)
    assert os.path.realpath(aicu._cc_deliverable_dir(0)) == partition


def test_cc_output_path_partition_modular(tmp_path):
    """modular 输出：_cc_output_path == 分区本身。"""
    _, partition = _make_shared_layout(tmp_path)
    aicu, _, _ = _make_aicu(tmp_path, shared_target=partition,
                            modular=True, output_file=None)
    assert os.path.realpath(aicu._cc_output_path(0)) == partition


def test_cc_output_path_partition_non_modular(tmp_path):
    """非 modular 输出：_cc_output_path 也 == 分区（杜绝 {partition}/output 嵌套）。"""
    _, partition = _make_shared_layout(tmp_path)
    aicu, _, _ = _make_aicu(tmp_path, shared_target=partition,
                            modular=False, output_file="out.txt")
    assert os.path.realpath(aicu._cc_output_path(0)) == partition


def test_format_instruction_maps_placeholders(tmp_path):
    """{deliverable_dir}→分区；{output_file}→""（模板中不留 output 占位）。"""
    _, partition = _make_shared_layout(tmp_path)
    aicu, _, _ = _make_aicu(tmp_path, shared_target=partition)
    aicu.generate_instruction = "将代码写入 {deliverable_dir}/{output_file}"
    rendered = aicu._format_instruction()
    assert partition in rendered
    assert "{deliverable_dir}" not in rendered
    assert "{output_file}" not in rendered
    # {output_file} 替换为空：路径不以 '/' 结尾残留 output 占位
    assert rendered == f"将代码写入 {partition}/"


# ============================================================================
# 读取语义：_read_cc_file 目录模式（忽略 output_file）
# ============================================================================


def test_read_cc_file_directory_mode_on_partition(tmp_path):
    _, partition = _make_shared_layout(tmp_path)
    aicu, _, _ = _make_aicu(tmp_path, shared_target=partition,
                            modular=False, output_file="out.txt")
    Path(partition, "app.py").write_text("def f(): return 1\n", encoding="utf-8")
    # 共享模式忽略 filename/output_file 参数，统一目录模式读分区
    content = aicu._read_cc_file(0, "any_name_ignored")
    assert "def f(): return 1" in content


# ============================================================================
# _finalize_result：零拷贝交付
# ============================================================================


def _finalize(aicu):
    return aicu._finalize_result(
        question="q", scores={}, best_id=0, consensus_reached=True,
        vote_rounds=1, audit_defects=[])


def test_finalize_result_modular_no_workspace_copy(tmp_path):
    """modular shared：final_output_path==分区，无 workspace/final_file 副本，
    无 nodes/{id}/output 等价物；分区内生成 _INDEX.md。"""
    _, partition = _make_shared_layout(tmp_path)
    aicu, ws, _ = _make_aicu(tmp_path, shared_target=partition,
                             modular=True, output_file=None)
    Path(partition, "app.py").write_text("def f(): return 1\n", encoding="utf-8")
    result = _finalize(aicu)
    assert os.path.realpath(result["final_output_path"]) == partition
    # final_file="output"（code.yaml）→ 旧世界副本路径 ws/output 不应存在
    assert not (Path(ws) / "output").exists()
    # 分区内应有 _INDEX.md 索引（_ensure_index_file 保留）
    assert (Path(partition) / "_INDEX.md").exists()
    # best_solution_summary 仍生成（从分区读取，来源不变）
    assert (Path(ws) / "best_solution_summary.txt").exists()


def test_finalize_result_non_modular_no_workspace_copy(tmp_path):
    """非 modular shared：final_output_path==分区，不落 workspace/final_file 副本。"""
    _, partition = _make_shared_layout(tmp_path)
    aicu, ws, _ = _make_aicu(tmp_path, shared_target=partition,
                             modular=False, output_file="out.txt")
    Path(partition, "out.txt").write_text("hello\n", encoding="utf-8")
    result = _finalize(aicu)
    assert os.path.realpath(result["final_output_path"]) == partition
    # final_file="output"（code.yaml）→ 旧世界副本路径 ws/output 不应存在
    assert not (Path(ws) / "output").exists()
    assert "hello" in result["content"]


def test_finalize_result_default_still_copies_to_workspace(tmp_path):
    """默认（无 shared_target）：既有行为不变，仍落 workspace/final_file。"""
    aicu, ws, _ = _make_aicu(tmp_path, role="code", modular=False,
                             output_file="out.txt")
    Path(aicu._cc_deliverable_dir(0)).mkdir(parents=True, exist_ok=True)
    Path(aicu._cc_deliverable_dir(0), "out.txt").write_text("legacy\n", encoding="utf-8")
    result = _finalize(aicu)
    # final_file="output"（code.yaml）→ 副本落到 ws/output
    assert (Path(ws) / "output").exists()
