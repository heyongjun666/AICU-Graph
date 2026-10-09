"""Shared filesystem helpers used by Runtime and delivery compatibility modules.

These helpers describe the shared workspace and do not perform delivery validation,
registry updates, or node state changes.
"""

import mimetypes
import os


# Framework directories hidden from shared-workspace listings and artifact scans.
FRAMEWORK_DIR_NAMES = frozenset({
    "events", "event_inbox", "events_inbox",
    "cc_0", "cc_1", "cc_2", "deliverables",
    "__pycache__", "node_modules", ".tox", "venv", ".venv",
    ".codegraph", ".artifacts", ".git",
})


# Text extensions supported by the Session Outputs preview.
TEXT_PREVIEW_EXTENSIONS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml", ".md", ".txt",
    ".html", ".css", ".sql", ".xml", ".csv", ".toml", ".ini", ".cfg", ".sh", ".bash",
    ".go", ".rs", ".java", ".c", ".cpp", ".h", ".hpp", ".m", ".kt", ".rb", ".php",
})

# Dotfiles without extensions that are safe to preview as text.
DOTFILE_PREVIEW_NAMES = frozenset({".env", ".gitignore", ".dockerignore"})


def previewable_path(path: str) -> bool:
    """Return whether a path is eligible for text preview."""
    name = os.path.basename(path).lower()
    if name in DOTFILE_PREVIEW_NAMES:
        return True
    return os.path.splitext(name)[1] in TEXT_PREVIEW_EXTENSIONS


def mime_type_for(path: str) -> str | None:
    """Return the display MIME type guessed from a path extension."""
    return mimetypes.guess_type(path)[0]


__all__ = [
    "FRAMEWORK_DIR_NAMES",
    "previewable_path",
    "mime_type_for",
]
