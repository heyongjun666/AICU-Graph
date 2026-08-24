"""Lightweight i18n helpers for user-facing backend messages.

语言来源：请求头 ``X-Language``（``zh`` / ``en``），经 ``LanguageMiddleware``
写入 contextvar，供 ``t()`` / ``localized_detail()`` 在请求处理期间读取。
未命中映射的异常保留英文兜底。
"""

from __future__ import annotations

import contextvars
import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

LANG_HEADER = "X-Language"
DEFAULT_LANG = "zh"

_current_lang: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aicu_lang", default=DEFAULT_LANG
)


def t(zh: str, en: str) -> str:
    """返回当前请求语言对应的文案。"""
    return zh if _current_lang.get() == "zh" else en


def localized_detail(exc: BaseException) -> str:
    """把常见业务异常本地化（英文直接透传，中文做关键词映射）。"""
    message = str(exc)
    if _current_lang.get() != "zh":
        return message
    return _translate_to_zh(message)


class LanguageMiddleware(BaseHTTPMiddleware):
    """从 X-Language 请求头解析语言，写入 contextvar 供 t() 读取。"""

    async def dispatch(self, request: Request, call_next):
        header = request.headers.get(LANG_HEADER)
        token = _current_lang.set(header if header in ("zh", "en") else DEFAULT_LANG)
        try:
            return await call_next(request)
        finally:
            _current_lang.reset(token)


_ZH_PATTERNS: list[tuple[str, str]] = [
    (r"^Skill '([^']+)' not found$", "找不到 Skill '{0}'"),
    (r"^Role '([^']+)' not found$", "找不到 Role '{0}'"),
    (r"^Cannot delete the default role$", "不能删除默认角色（default.yaml 是继承基类）"),
    (r"^Session '([^']+)' not found$", "找不到会话 '{0}'"),
    (r"^Node '([^']+)' not found$", "找不到节点 '{0}'"),
    (r"^Skill '([^']+)' already exists$", "Skill '{0}' 已存在"),
    (r"^Role '([^']+)' already exists$", "Role '{0}' 已存在"),
    (r"^Unknown global config fields: (.+)$", "未知的全局配置字段: {0}"),
    (r"^Unknown session config fields: (.+)$", "未知的会话配置字段: {0}"),
    (r"^Invalid zip archive$", "无效的 zip 归档"),
    (r"^Zip archive is empty$", "zip 归档为空"),
    (r"^Skill archive exceeds (.+) bytes$", "Skill 归档超出 {0} 字节"),
    (r"^Skill archive must contain a SKILL\.md entrypoint$", "Skill 归档必须包含 SKILL.md 入口文件"),
    (r"^Invalid entry path in archive: (.+)$", "归档中存在非法路径: {0}"),
    (r"^Skill id must match \[a-z\]\[a-z0-9_-\]\*$", "Skill id 必须匹配 [a-z][a-z0-9_-]*"),
    (r"^Skill content cannot be empty$", "Skill 内容不能为空"),
    (r"^Skill content exceeds (.+) bytes$", "Skill 内容超出 {0} 字节"),
    (r"^Invalid skill path$", "非法的 skill 路径"),
    (r"^Role config must be a YAML mapping$", "Role 配置必须是 YAML 映射"),
    (r"^Store API keys in an environment variable and set api_key_env$",
     "请将 API Key 存入环境变量并设置 api_key_env"),
]


def _translate_to_zh(message: str) -> str:
    for pattern, template in _ZH_PATTERNS:
        match = re.fullmatch(pattern, message)
        if match:
            return template.format(*match.groups())
    return message
