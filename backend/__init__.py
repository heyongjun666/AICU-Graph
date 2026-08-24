"""
Backend package — combined from dependency outputs.

Extends __path__ and sys.path to include csv_data_layer, auth_service,
markdown_service, and stats_service outputs so their packages are resolvable.
"""
import os
import sys

# ── csv_data_layer (backend.storage, backend.models) ──
_csv_path = os.path.abspath(
    "/home/work/heyongjun/claudecodeAgentPlus_v2_workspace/versions/v5/final_solution/workspaces/40d419d3-e2f3-4ca6-8077-8e05e3fc0eec/output"
)
_csv_backend = os.path.join(_csv_path, "backend")
if os.path.isdir(_csv_backend) and _csv_backend not in __path__:
    __path__.append(_csv_backend)

# ── auth_service (backend.auth) ──
_auth_path = os.path.abspath(
    "/home/work/heyongjun/claudecodeAgentPlus_v2_workspace/versions/v5/final_solution/workspaces/b5fb078f-84d3-4a44-8ff4-502fb3f86f17/output/output"
)
_auth_backend = os.path.join(_auth_path, "backend")
if os.path.isdir(_auth_backend) and _auth_backend not in __path__:
    __path__.append(_auth_backend)

# ── stats_service (backend.services) ──
_stats_path = os.path.abspath(
    "/home/work/heyongjun/claudecodeAgentPlus_v2_workspace/versions/v5/final_solution/workspaces/5d29e811-3eab-45b5-987a-c251bc4b8084/output/output"
)
_stats_backend = os.path.join(_stats_path, "backend")
if os.path.isdir(_stats_backend) and _stats_backend not in __path__:
    __path__.append(_stats_backend)

# ── markdown_service (markdown_service package, not under backend/) ──
_md_path = os.path.abspath(
    "/home/work/heyongjun/claudecodeAgentPlus_v2_workspace/versions/v5/final_solution/workspaces/0de0738d-cbee-4f7b-9761-888450b154b8/output"
)
if os.path.isdir(_md_path) and _md_path not in sys.path:
    sys.path.insert(0, _md_path)
