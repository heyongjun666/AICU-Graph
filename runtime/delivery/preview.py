"""runtime/delivery/preview.py — Session Outputs 文件预览判定（文本白名单 + mime）。

职责：决定产物文件是否可做文本预览。Filesystem 是内容事实源，本模块只做
「可预览性」判定，不读取内容；具体读取与 1MB 截断在 Runtime 层完成。
"""

import mimetypes
import os

# 可预览文本扩展名白名单：命中 → 返回内容；未命中 → previewable=false，不强制 decode（防乱码）。
TEXT_PREVIEW_EXTENSIONS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml", ".md", ".txt",
    ".html", ".css", ".sql", ".xml", ".csv", ".toml", ".ini", ".cfg", ".sh", ".bash",
    ".go", ".rs", ".java", ".c", ".cpp", ".h", ".hpp", ".m", ".kt", ".rb", ".php",
})

# dotfile 无扩展名（os.path.splitext(".env")[1] == ""），需 basename 特判。
DOTFILE_PREVIEW_NAMES = frozenset({".env", ".gitignore", ".dockerignore"})


def previewable_path(path: str) -> bool:
    """文件路径是否可做文本预览（扩展名白名单 / dotfile 特判）。"""
    name = os.path.basename(path).lower()
    if name in DOTFILE_PREVIEW_NAMES:
        return True
    return os.path.splitext(name)[1] in TEXT_PREVIEW_EXTENSIONS


def mime_type_for(path: str) -> str | None:
    """按扩展名猜测 mime_type（仅供展示，未来 Monaco/Markdown 判定不依赖系统猜测）。"""
    return mimetypes.guess_type(path)[0]
