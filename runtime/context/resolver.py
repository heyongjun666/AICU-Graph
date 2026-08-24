"""resolver.py — 确定性提取 + 去重/优先级/预算裁剪。

不调用任何 LLM。只做：
- 从 task/结构化信息确定性提取 file / symbol 候选（不做自然语言语义判断）
- 把 Provider 的 Raw 上下文（可能 >4KB）裁剪到 ≤4KB 注入预算
"""

import re

# 每节点注入预算（≤4KB）。这是 Resolver 的裁剪目标，不是 Provider 的能力上限。
DEFAULT_BUDGET = 4096

# 文件引用：相对/绝对路径 + 常见代码扩展名
_FILE_RE = re.compile(r"[\w./-]+\.(?:py|ts|tsx|js|jsx|java|go|css|html|json)")
# CamelCase 标识符（类名/类型，如 ArticleService）
_CAMEL_RE = re.compile(r"\b[A-Z][a-zA-Z0-9_]*(?:[a-z][a-zA-Z0-9_]*)\b")
# snake_case 标识符（函数/变量，如 create_article）——带下划线，几乎不可能是自然语言
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "you", "your",
    "task", "using", "need", "should", "will", "make", "work", "code",
    "project", "api", "file", "files", "new", "create", "case", "class",
}


def extract_candidates(text: str) -> tuple:
    """确定性提取 file / symbol 候选（各限 8 个，去重保序）。"""
    text = text or ""
    files = list(dict.fromkeys(_FILE_RE.findall(text)))
    symbols: list = []
    for m in _CAMEL_RE.findall(text) + _SNAKE_RE.findall(text):
        if m in _STOPWORDS or m in symbols:
            continue
        symbols.append(m)
        if len(symbols) >= 8:
            break
    return files[:8], symbols[:8]


def truncate(text: str, budget: int = DEFAULT_BUDGET) -> str:
    """超出预算从尾部截断（保留头部结构线索）。"""
    if len(text) <= budget:
        return text
    return text[:budget] + "\n... [truncated]"


def assemble(header: str, disclaimer: str, blocks: list, budget: int) -> str:
    """去重 → 依赖清单优先（blocks 顺序已排）→ 裁剪到 budget。"""
    if not blocks:
        return ""
    body = "\n\n".join(blocks)
    full = f"{header}\n\n{disclaimer}\n\n{body}"
    return truncate(full, budget)
