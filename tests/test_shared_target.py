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
from types import SimpleNamespace

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


def test_manager_initialization_separates_stable_rules_and_output_contract(tmp_path):
    """角色/长期规则只初始化写入一次，输出协议保留为请求级内容。"""
    ws = str(tmp_path / "manager_ws")
    aicu = AICU(
        role_type="manager",
        role="default",
        parent_workspace=ws,
        num_ccs=1,
        skills=[],
    )

    claude_md = Path(ws) / "cc_0" / "CLAUDE.md"
    stable = claude_md.read_text(encoding="utf-8")
    assert "# Identity" in stable
    assert "## 输出格式" not in stable
    assert "## 输出格式" in aicu._format_output_instruction()


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


def test_generation_prompt_keeps_only_output_contract_on_incremental_turn(tmp_path):
    """增量轮不重复稳定规则/历史任务，但仍携带输出协议。"""
    aicu, _, _ = _make_aicu(tmp_path, role="code", modular=False)
    aicu.generate_instruction = (
        "稳定角色规则\n\n## 输出格式 (JSON Schema)\n只输出 JSON。"
    )
    # Recompute the split explicitly, matching construction-time behavior.
    aicu._stable_generation_instruction, aicu._output_format_instruction = (
        aicu._split_generation_instruction(aicu.generate_instruction)
    )

    first = aicu._build_generation_prompt("首次任务")
    incremental = aicu._build_generation_prompt("当前增量", incremental=True)

    assert "稳定角色规则" in first
    assert "首次任务" in first
    assert "稳定角色规则" not in incremental
    assert "首次任务" not in incremental
    assert "当前增量" in incremental
    assert "## 输出格式" in incremental
    assert "只输出 JSON" in incremental


def test_generation_prompt_uses_incremental_shape_for_resumed_cc(tmp_path):
    """普通 ask() 对已有 CC 会话也不重复稳定规则和历史任务。"""
    aicu, _, _ = _make_aicu(tmp_path, role="code", modular=False)
    aicu.generate_instruction = (
        "稳定角色规则\n\n## 输出格式 (JSON Schema)\n只输出 JSON。"
    )
    aicu._stable_generation_instruction, aicu._output_format_instruction = (
        aicu._split_generation_instruction(aicu.generate_instruction)
    )

    resumed_cc = SimpleNamespace(conversation_id="conversation-1")
    prompt = aicu._build_generation_prompt(
        "本轮任务", cc=resumed_cc
    )

    assert "稳定角色规则" not in prompt
    assert "本轮任务" in prompt
    assert "## 输出格式" in prompt
    assert "只输出 JSON" in prompt


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


# ============================================================================
# 容错解码：分区里混入非 UTF-8 文件（GBK 日志/二进制）不应让 worker 崩溃
# ============================================================================

def test_read_cc_file_tolerates_non_utf8_file(tmp_path):
    """分区里遗留含非法 UTF-8 字节的文件 → 读产出不抛异常。

    回归：CC 在分区里跑测试会遗留 .log / .CORRUPT-* 等非源码文件，其中可能含
    GBK 编码的中文字节（0xac 等）。_list_code_files 若漏判把它当代码，读取时
    必须容错解码（errors='replace'），而不是 UnicodeDecodeError 让整个 worker
    失败（生产事件：节点 5332f799 反复 FAILED，报 utf-8 0xac 解码错误）。
    """
    _, partition = _make_shared_layout(tmp_path)
    aicu, _, _ = _make_aicu(tmp_path, shared_target=partition,
                            modular=False, output_file="out.txt")
    Path(partition, "app.py").write_text("def f(): return 1\n", encoding="utf-8")
    # 模拟 CC 遗留的含 GBK 字节的损坏日志（0xac 是 GBK 中文字节的一部分，
    # 后随「次」的 UTF-8 编码 e6 ac a1）
    Path(partition, "notes.log.CORRUPT-20260923T002128Z").write_bytes(
        b"start...\xac\xe6\xac\xa1...end"
    )
    content = aicu._read_cc_file(0, "any_name_ignored")
    assert "def f(): return 1" in content  # 正常代码文件仍读出
    assert "start" in content  # 坏文件被容错解码，不再抛 UnicodeDecodeError


def test_is_code_file_excludes_log_and_out(tmp_path):
    """日志/命令输出（.log / .out）不是源码，_is_code_file 判为非代码。"""
    _, partition = _make_shared_layout(tmp_path)
    aicu, _, _ = _make_aicu(tmp_path, shared_target=partition)
    for name in ("app_run.log", "manifest.out", "app.py"):
        Path(partition, name).write_text("x", encoding="utf-8")
    assert not aicu._is_code_file(Path(partition, "app_run.log"), Path(partition))
    assert not aicu._is_code_file(Path(partition, "manifest.out"), Path(partition))
    # 对照组：普通 .py 仍算代码（上两条不能是靠「一切非 .md 都不算」蒙对的）
    assert aicu._is_code_file(Path(partition, "app.py"), Path(partition))


def test_read_cc_file_skips_binary_files_of_any_suffix(tmp_path):
    """非代码领域的产出（docx/xlsx 等）与已知二进制后缀都不拼进文本；无扩展名但含 NUL 的同理。"""
    _, partition = _make_shared_layout(tmp_path)
    aicu, _, _ = _make_aicu(tmp_path, shared_target=partition, modular=False)
    Path(partition, "report.md").write_text("# 季度报告\n", encoding="utf-8")
    Path(partition, "report.docx").write_bytes(b"PK\x03\x04\x14\x00\x06\x00garbage")
    Path(partition, "sheet.xlsx").write_bytes(b"PK\x03\x04\x00\x00\x00")
    Path(partition, "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    Path(partition, "blob").write_bytes(b"\x00\x01\x02")
    content = aicu._read_cc_file(0)
    assert content == "# 季度报告\n"


def test_read_text_file_decodes_text_beyond_first_4k(tmp_path):
    """NUL 探测只看头 4 KB，但文本必须完整读出（含 4 KB 之后的部分）。"""
    f = tmp_path / "long.txt"
    f.write_text("a" * 5000 + "TAIL", encoding="utf-8")
    text = AICU._read_text_file(f)
    assert text is not None and text.endswith("TAIL") and len(text) == 5004
