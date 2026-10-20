"""runtime/text_utils.py — 与业务无关的小文本工具。

只放「多处以完全相同实现出现」的纯函数，避免各处再抄一遍：
  - ``truncate``：Router 的组织图快照与 Manager 的上下文摘要都要把 task 压成
    一行短文本；两边各写一份时，省略号/换行处理很容易分叉。
"""


def truncate(text: str, limit: int = 140) -> str:
    """截断文本到指定长度并加省略号，同时把换行压成空格。"""
    s = (text or "").strip().replace("\n", " ")
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + "…"
