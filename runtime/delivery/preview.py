"""Compatibility exports for shared workspace preview helpers.

The implementations live in :mod:`runtime.shared_io`; this module remains so
existing imports continue to work during the migration.
"""

from runtime.shared_io import (
    DOTFILE_PREVIEW_NAMES,
    TEXT_PREVIEW_EXTENSIONS,
    mime_type_for,
    previewable_path,
)

__all__ = [
    "TEXT_PREVIEW_EXTENSIONS",
    "DOTFILE_PREVIEW_NAMES",
    "previewable_path",
    "mime_type_for",
]
