"""
Backend package — combined from dependency outputs.

Extends __path__ and sys.path to include csv_data_layer, auth_service,
markdown_service, and stats_service outputs so their packages are resolvable.

服务产物来自仓库内的独立服务（csv_data_layer / auth_service / stats_service /
markdown_service），使用相对路径按项目根解析，不依赖机器路径或进程启动目录。
服务产物按以下约定挂载到 backend/deps/ 下（缺失时自动跳过，不影响仓库导入与打包）：

  backend/deps/csv_data_layer/output/backend/        → backend.storage / backend.models
  backend/deps/auth_service/output/output/backend/   → backend.auth
  backend/deps/stats_service/output/output/backend/  → backend.services
  backend/deps/markdown_service/output/              → markdown_service
"""
import os
import sys

# 项目根 = 本文件上溯两级（backend/__init__.py → 仓库根）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEPS_ROOT = os.path.join(_PROJECT_ROOT, "backend", "deps")


def _append_if_dir(path: str, target: list) -> None:
    """目录存在且未加入时追加；缺失时静默跳过（构建产物可选）。"""
    if os.path.isdir(path) and path not in target:
        target.append(path)


# ── csv_data_layer (backend.storage, backend.models) ──
_append_if_dir(
    os.path.join(_DEPS_ROOT, "csv_data_layer", "output", "backend"), __path__
)

# ── auth_service (backend.auth) ──
_append_if_dir(
    os.path.join(_DEPS_ROOT, "auth_service", "output", "output", "backend"), __path__
)

# ── stats_service (backend.services) ──
_append_if_dir(
    os.path.join(_DEPS_ROOT, "stats_service", "output", "output", "backend"), __path__
)

# ── markdown_service (markdown_service package, not under backend/) ──
_append_if_dir(
    os.path.join(_DEPS_ROOT, "markdown_service", "output"), sys.path
)
