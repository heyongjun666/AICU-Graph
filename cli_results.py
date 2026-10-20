"""CLI 输出解析：把各家 ``--output-format json`` 的产物归一成事件 / 文本。

纯函数，不碰子进程。``cli_adapters`` 再导出这些名字供各 adapter 使用。
"""

import json
from typing import Optional

def _result_events(raw: str) -> list:
    """把 CLI 的 JSON 输出拆成事件列表，顺序不变。

    同一个 ``--output-format json`` 在不同 CLI 下有三种形态，这里统一归一：
      - 单个 JSON 对象（claude）
      - 事件数组（codebuddy / qwen —— 实测两者都把事件装进一个 JSON 数组）
      - 每行一个对象的 JSONL（各家 stream-json）
    整段不是 JSON（纯文本回复）时返回空列表，由调用方兜底。
    """
    text = raw.strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [event for event in value if isinstance(event, dict)]
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _claude_style_result(raw: str) -> Optional[dict]:
    """取 Claude Code 风格的结果对象并归一化（claude / codebuddy / qwen 同形）。

    ``--output-format json`` 在 claude 下就是单个结果对象，直接归一化；codebuddy /
    qwen 输出事件数组（末条 ``type=result``），stream-json 则每行一个事件 ——
    后两种取最后一条 result 事件。取不到返回 None，调用方退回原始输出。
    """
    text = raw.strip()
    if not text:
        return None
    events = _result_events(raw)
    candidates = [
        event
        for event in events
        if event.get("type") == "result" or "result" in event
    ]
    # 单个对象且没有 result 字段时它就是结果对象本身（如只有 subtype/errors 的
    # error_during_execution），不能因为挑不出 result 事件就丢掉。
    if len(events) == 1 and not candidates:
        candidates = events
    if not candidates:
        return None
    event = candidates[-1]
    return {
        "result": event.get("result") or event.get("text"),
        "session_id": event.get("session_id") or event.get("conversation_id"),
        "subtype": event.get("subtype"),
        "errors": _coerce_errors(event.get("errors")),
    }


def _coerce_errors(errors_value) -> list:
    """把各类 errors 表示归一化为字符串列表。"""
    if errors_value is None:
        return []
    if isinstance(errors_value, str):
        return [errors_value]
    if isinstance(errors_value, list):
        return [str(item) for item in errors_value]
    return [str(errors_value)]


def _render_openai_content(content) -> str:
    """把 OpenAI 风格 message.content（str 或 list）渲染为纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") in ("tool_use", "tool_call", "function"):
                    name = item.get("name") or (item.get("function") or {}).get("name", "unknown")
                    parts.append(f"Tool: {name}")
        return "\n".join(parts)
    return ""


def _extract_event_text(event: dict) -> str:
    """从通用 JSONL 事件 dict 提取文本（OpenCode / Pi / Gemini 等 json 事件）。"""
    for key in ("text", "content", "output", "response"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
        # 内容也可能是 OpenAI 风格的内容块数组（如 {"content":[{"type":"text",...}]}）
        if isinstance(value, list):
            rendered = _render_openai_content(value)
            if rendered:
                return rendered
    msg = event.get("message")
    if isinstance(msg, dict):
        for key in ("content", "text"):
            content = msg.get(key)
            if isinstance(content, str) and content:
                return content
            if isinstance(content, list):
                rendered = _render_openai_content(content)
                if rendered:
                    return rendered
    parts = event.get("parts")
    if isinstance(parts, list):
        chunks = [
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
        ]
        if chunks:
            return "\n".join(chunks)
    # OpenCode 的事件把内容包在单个 part 里：
    # {"type":"text","part":{"type":"text","text":"..."}}（实测）
    part = event.get("part")
    if isinstance(part, dict):
        return _extract_event_text(part)
    return ""


def _format_json_event(line: str, extract=_extract_event_text) -> str:
    """把一行 JSONL 事件渲染成可见文本（OpenCode / Pi / Gemini 等同形 CLI）。

    两种回退语义故意不对称，缺一不可：
      - 解析不出 JSON → **原样返回该行**，非 JSONL 的原始输出（启动日志、报错）仍能显示；
      - 解析出来但不是对象 → 返回空串，这种行没有可展示的正文，返回原文只会刷屏。

    ``extract`` 由各 CLI 给：Pi 需要 ``_assistant_text``（它的 ``--mode json`` 会把
    用户消息也回显，通用提取器会把提问当成回复），其余用默认的通用提取器。
    """
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(event, dict):
        return ""
    return extract(event)


def _extract_session_id(event: dict) -> Optional[str]:
    """从通用 JSONL 事件 dict 提取会话 id（OpenCode / Pi 等）。"""
    for key in ("sessionID", "sessionId", "session_id", "session", "conversation_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None
