"""Manager 的解析层：AICU 输出 -> ExecutionPlan / 评审评估。

从 ``executables/manager.py`` 拆出的纯函数层。这里的函数不读节点状态、不发事件、
不建子进程，只做「这段文本/这个 dict 到底是什么」的判断，便于单独测试与复用。

历史：这些逻辑原本都是 ``Manager`` 的 staticmethod，散落在评审与派活两段流程之间；
其中 ``read_final_output`` 被三个解析入口各调一次，``parse_markdown_plan`` 还要
借类常量做表头别名 —— 都是「与 Manager 的状态无关」的信号。
"""

import json
import logging
import os
from typing import Any, Optional

from runtime.models import ExecutionPlan, Step

logger = logging.getLogger(__name__)


def extract_review_json(text: str) -> Optional[dict]:
    """从 review 文本中提取 JSON 对象（供 parse_review_assessments 使用）。

    review CC 的产出可能包含多种形态：
    - 纯 JSON（推荐）
    - markdown ``` 代码围栏包裹的 JSON
    - 叙述性 markdown + 末尾嵌入的 JSON（review CC 常见输出）

    提取策略依次尝试：
    1. 围栏提取 → json.loads
    2. 整体 json.loads
    3. 反向扫描最后一个平衡大括号块 → json.loads
    （反向扫描而非正向，是因为叙述部分可能包含 { } 等花括号字符，
     而 JSON 结构通常位于文本末尾。）

    Args:
        text: review 输出文本。

    Returns:
        解析出的 JSON dict，或 None。
    """
    text = text.strip()

    # 1. markdown fence 包裹
    if "```" in text:
        lines = text.split("\n")
        json_lines = []
        in_fence = False
        for line in lines:
            if line.strip().startswith("```"):
                if in_fence:
                    break
                in_fence = True
                continue
            if in_fence:
                json_lines.append(line)
        if json_lines:
            try:
                data = json.loads("\n".join(json_lines).strip())
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass

    # 2. 整体解析
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # 3. 反向扫描最后一个平衡大括号块
    depth = 0
    close_idx = -1
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if ch == "}":
            if depth == 0:
                close_idx = i
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[i:close_idx + 1])
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError:
                    pass
                break
    return None


def ledger_fix_entry(a: dict) -> dict:
    """从单条失败评估中提取闭环修复条目（含 B1 契约扩展字段透传）。"""
    fix = a.get("fix") or {}
    entry = {
        "step_ref": a.get("step_ref", ""),
        "role": fix.get("role", ""),
        "task": fix.get("task", ""),
    }
    for key in ("file", "line", "desc", "expected_action", "check"):
        if fix.get(key):
            entry[key] = fix[key]
    return entry


def read_final_output(raw_output: Any, *, log_tag: str) -> Optional[str]:
    """读取 AICU result 元数据里 ``final_output_path`` 指向的正文。

    plan / review content / assessments 三个解析入口共用这段「取路径 → 检查
    可读 → 读文本 → 失败只告警」逻辑。返回 ``None`` 表示没有可读文件（不是
    空文件 —— 空文件会返回 ``""``），调用方据此决定是否继续走内存里的内容。
    """
    if not isinstance(raw_output, dict):
        return None
    output_path = raw_output.get("final_output_path")
    if not (
        output_path
        and isinstance(output_path, str)
        and os.path.isfile(output_path)
    ):
        return None
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        logger.warning(
            "%s: failed to read output file %s: %s", log_tag, output_path, exc,
        )
        return None


def resolve_review_content(raw_output: Any) -> Any:
    """统一解析 review 输出的一等内容，消除"各解析器各自重读 final_output_path
    文件、漏读即挂"的抽象泄漏。

    real 模式下 aicu.ask() 返回元数据 dict（scores/final_output_path/...），真正
    review 契约内容（overall_passed / assessments）在
    content / content_parsed 字段或 final_output_path 指向的文件里。此处只解析
    一次，产出同一份内容供 parse_review_assessments / _write_review_output
    消费（单一事实来源）。

    优先级：
    1. content_parsed（已是 dict）→ 直接返回
    2. content（str/dict）→ str 时尝试提取 review JSON，成功返回 dict，否则返回原 str
    3. final_output_path（文件可读）→ 读取文本 → 同样尝试提取 JSON
    4. 原样返回（stub / 单测直传 dict 兼容）

    Args:
        raw_output: aicu.ask() 的原始返回。

    Returns:
        dict（可解析出契约）或 str（仅文本）。
    """
    if not isinstance(raw_output, dict):
        return raw_output

    content = raw_output.get("content_parsed")
    if isinstance(content, dict):
        return content

    content = raw_output.get("content")
    if isinstance(content, dict):
        return content
    if isinstance(content, str) and content.strip():
        data = extract_review_json(content)
        return data if data is not None else content

    text = read_final_output(raw_output, log_tag="resolve_review_content")
    if text is not None:
        data = extract_review_json(text)
        return data if data is not None else text

    return raw_output


def extract_plan_json(text: str) -> Optional[dict]:
    """从文本中提取 plan JSON（尝试直接解析或从 execution_plan_json 字段提取）。"""
    text = text.strip()
    # 直接解析 JSON；顶层若不是对象，再尝试把字符串值本身当作内嵌 JSON
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            pass
    # 尝试用平衡大括号提取
    start = text.find('{')
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i+1])
                except json.JSONDecodeError:
                    break
    return None


def with_no_action_reason(plan: ExecutionPlan, payload: Any) -> ExecutionPlan:
    """空计划时带上 LLM 给出的理由（``reason`` / ``no_action_reason``）。

    「本轮不需要行动」是 Manager 自己的判断，理由只用于节点日志留痕（让用户
    看得见为什么没派活），不参与任何判定。
    """
    if plan.steps or not isinstance(payload, dict):
        return plan
    reason = payload.get("no_action_reason") or payload.get("reason")
    if isinstance(reason, str) and reason.strip():
        plan.no_action_reason = reason.strip()
    return plan


def execution_plan_from_payload(payload: Any) -> Optional[ExecutionPlan]:
    """从已解析的 AICU payload 提取执行计划。"""
    if not isinstance(payload, dict):
        return None

    if "steps" in payload and isinstance(payload["steps"], list):
        return with_no_action_reason(
            dict_to_execution_plan(payload["steps"]), payload
        )

    nested = payload.get("execution_plan_json")
    if isinstance(nested, dict):
        return execution_plan_from_payload(nested)
    if isinstance(nested, str) and nested.strip():
        nested_data = extract_plan_json(nested)
        if nested_data is not None:
            return execution_plan_from_payload(nested_data)
    return None


def dict_to_execution_plan(steps_data: list[dict]) -> ExecutionPlan:
    """从 dict 列表构造 ExecutionPlan。"""
    steps = []
    for s in steps_data:
        steps.append(Step(
            step_id=s["step_id"],
            exec_type=s.get("exec_type", "worker"),
            role=s.get("role", "default"),
            task=s.get("task", ""),
            task_summary=s.get("task_summary", ""),
            config=s.get("config", {}),
            depends_on=s.get("depends_on", []),
            reuse_step_ref=s.get("reuse_step_ref"),
        ))
    return ExecutionPlan(steps=steps)


# 列名别名映射（大小写不敏感）
_STEP_ID_KEYS = ('id', 'step', 'name', 'step_id', 'step name', 'node', 'node_id')
_EXEC_TYPE_KEYS = ('类型', 'type', 'exec_type', 'kind', '节点类型')
_ROLE_KEYS = ('角色', 'role', 'assignee', 'responsible')
_DEPENDS_KEYS = ('依赖', 'depends_on', 'depends', 'depends on', 'dependencies', 'prerequisite')
_TASK_KEYS = ('task', 'description', 'covers', 'covers requirements', 'notes', 'details', 'responsibilities')


def normalize_header(h: str) -> str:
    """标准化表头名称（去空格/去星号/全小写）。"""
    return h.strip().strip('*').strip().lower()


def parse_markdown_plan(text: str) -> list[Step]:
    """从 Markdown 表格文本中解析 Step 列表（LLM 未按要求输出 JSON 时的兜底方案）。

    支持的格式:
    | Step | ID | 类型 | 角色 | 依赖 |
    |------|----|------|------|------|
    | 1 | **design_spec** | Manager | default | — |

    也支持无 ID 列的表（通过 Step 列或行号生成 step_id）。
    """
    steps = []
    lines = text.strip().split('\n')
    in_table = False
    headers = []
    for line in lines:
        line = line.strip()
        # 检测表格行
        if line.startswith('|') and line.endswith('|'):
            cells = [c.strip() for c in line.split('|')[1:-1]]
            if not in_table:
                # 表头行
                headers = cells
                in_table = True
                continue
            # 分隔行（|---|----|）
            if all(set(c.strip('- :')) == set() or all(ch in '- :' for ch in c) for c in cells):
                continue
            # 数据行
            if headers:
                row = dict(zip(headers, cells))
                norm_row = {normalize_header(k): v for k, v in row.items()}

                # step_id: 多别名查找，找不到用行号生成
                step_id = ''
                for key in _STEP_ID_KEYS:
                    if key in norm_row:
                        step_id = norm_row[key].strip('*').strip('**').strip()
                        break
                if not step_id:
                    step_id = f'step_{len(steps)}'

                # exec_type
                exec_type_raw = 'worker'
                for key in _EXEC_TYPE_KEYS:
                    if key in norm_row:
                        exec_type_raw = norm_row[key].lower()
                        break
                exec_type = 'manager' if 'manager' in exec_type_raw else 'worker'

                # role
                role = 'default'
                for key in _ROLE_KEYS:
                    if key in norm_row:
                        role = norm_row[key].strip('*').strip('**').strip()
                        break

                # depends_on
                depends_raw = ''
                for key in _DEPENDS_KEYS:
                    if key in norm_row:
                        depends_raw = norm_row[key]
                        break
                depends = [d.strip().strip('*').strip('**').strip() for d in depends_raw.split(',') if d.strip() and d.strip() != '—']

                # task
                task = step_id
                for key in _TASK_KEYS:
                    if key in norm_row:
                        task = norm_row[key].strip('*').strip('**').strip()
                        break

                steps.append(Step(
                    step_id=step_id,
                    exec_type=exec_type,
                    role=role or 'default',
                    task=task,
                    task_summary=task,
                    config={},
                    depends_on=depends,
                ))
        elif in_table and line == '':
            break  # 表格结束
    return steps


def parse_review_assessments(
    raw_output: Any,
    children_info: list[dict],
) -> tuple[list[dict], str]:
    """解析 AICU 的结构化评估输出。

    期望格式:
    {
      "overall_passed": false,
      "assessments": [
        {"step_ref": "s1", "passed": true, "reason": "..."},
        {"step_ref": "s2", "passed": false, "reason": "...",
         "fix": {"role": "default", "task": "fix X by doing Y"}}
      ]
    }

    Returns:
        (assessments, status)：
          - status="parsed"       已成功解析出逐子节点评估（可能含失败项）。
          - status="unparseable"  无法解析出 assessments（JSON 缺失/空/非法）。
            此时返回全量失败清单；调用方应触发修复性重试或按 AICU 元数据降级，
            避免"格式失败→重建→再格式失败"的无效循环（历史问题：Review AICU
            输出成 markdown/产物回显而非 JSON 契约，导致 Manager 反复重建至
            max_iterations 而 FAILED）。
    """
    # 提取 JSON（可能被 markdown fences 包裹 / 文末嵌入 / AICU result 元数据 dict）
    if isinstance(raw_output, dict):
        # AICU.ask() 在 real 模式返回的是 result 元数据 dict（scores/final_output_path
        # 等，不含 assessments）。若 dict 无 assessments，先读 final_output_path
        # 指向的实际 review 产出内容再解析 —— 否则 assessments 恒为空，永远降级
        # 为全量失败。
        if not raw_output.get("assessments"):
            text = read_final_output(
                raw_output, log_tag="parse_review_assessments",
            )
            if text is not None:
                raw_output = text
        if isinstance(raw_output, dict):
            data = raw_output
        else:
            data = extract_review_json(str(raw_output))
    elif isinstance(raw_output, str):
        data = extract_review_json(raw_output)
    else:
        data = None

    if data is None:
        logger.warning(
            "parse_review_assessments: failed to parse AICU output as JSON, "
            "returning full-failure (status=unparseable)"
        )
        return (
            [
                {
                    "step_ref": c["step_ref"],
                    "passed": False,
                    "reason": "AICU output unparseable",
                    "fix": {"role": c.get("role", "default"), "task": c.get("task", "")},
                }
                for c in children_info
            ],
            "unparseable",
        )

    assessments = data.get("assessments", [])

    # 无逐项 assessments，但输出 dict 携带明确整体结论（legacy review_passed /
    # passed / overall_passed）：按整体结论构造逐子节点评估。这是真实判定
    # （review 确实给出了结论），不应误入"格式失败"降级路径。
    if not assessments and isinstance(data, dict):
        verdict = data.get("review_passed", data.get("passed"))
        if verdict is None and "overall_passed" in data:
            verdict = bool(data.get("overall_passed"))
        if verdict is not None:
            defects = data.get("defects") or data.get("audit_defects") or []
            if verdict:
                return (
                    [
                        {"step_ref": c["step_ref"], "passed": True,
                         "reason": "Overall review passed"}
                        for c in children_info
                    ],
                    "parsed",
                )
            reason = "; ".join(str(d) for d in defects) if defects else "Overall review failed"
            return (
                [
                    {
                        "step_ref": c["step_ref"],
                        "passed": False,
                        "reason": reason,
                        "fix": {"role": c.get("role", "default"), "task": c.get("task", "")},
                    }
                    for c in children_info
                ],
                "parsed",
            )

    # 若 assessments 为空或缺失且无整体结论，视为格式失败（非质量信号），
    # 交由调用方修复重试 / 按元数据降级，而不是直接触发全量重建。
    if not assessments:
        logger.warning(
            "parse_review_assessments: empty assessments array, "
            "returning full-failure (status=unparseable)"
        )
        return (
            [
                {
                    "step_ref": c["step_ref"],
                    "passed": False,
                    "reason": "No assessment provided",
                    "fix": {"role": c.get("role", "default"), "task": c.get("task", "")},
                }
                for c in children_info
            ],
            "unparseable",
        )

    # 补全缺失的 step_ref（AICU 可能遗漏某些子节点）
    assessed_refs = {a.get("step_ref", "") for a in assessments}
    for c in children_info:
        if c["step_ref"] not in assessed_refs:
            assessments.append({
                "step_ref": c["step_ref"],
                "passed": False,
                "reason": "Not assessed by reviewer",
                "fix": {"role": c.get("role", "default"), "task": c.get("task", "")},
            })

    # 为每个 assessment 确保 fix 字段存在
    for a in assessments:
        if not a.get("passed", False) and "fix" not in a:
            original = next(
                (c for c in children_info if c["step_ref"] == a.get("step_ref")),
                None,
            )
            a["fix"] = {
                "role": original.get("role", "default") if original else "default",
                "task": original.get("task", a.get("reason", "Fix issue")) if original else "Fix issue",
            }

    return assessments, "parsed"
