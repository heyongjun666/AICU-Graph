"""Multi-session lifecycle management for independent execution graphs."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from executables.registry import ExecutableRegistry
from runtime.credentials import CredentialStore, set_credential_store
from runtime.event_bus import SSEEvent
from runtime.persistence import Persistence
from runtime.runtime import Runtime
from runtime.sse_manager import SSEManager


_SESSION_CONFIG_KEYS = {
    "cli_name",
    "model",
    "api_base_url",
    "api_key_env",
    "skip_permissions",
    "skills",
    "num_ccs",
    "audit_rounds",
    "max_iterations",
    "language",
}

# 全局（系统级）配置字段：跨会话生效，供新会话继承默认值。
_GLOBAL_CONFIG_KEYS = {
    "workspace_root",
    "cli_name",
    "model",
    "api_base_url",
    "api_key_env",
    "language",
}

_GLOBAL_CONFIG_DEFAULTS = {
    "workspace_root": None,
    "cli_name": None,
    "model": None,
    "api_base_url": None,
    "api_key_env": None,
    "language": "zh",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_session_config(config: Optional[dict]) -> dict[str, Any]:
    if not config:
        return {}
    return {key: value for key, value in config.items() if key in _SESSION_CONFIG_KEYS}


@dataclass
class SessionMetadata:
    id: str
    title: str
    created_at: str
    updated_at: str
    config: dict[str, Any] = field(default_factory=dict)
    workspace_root: Optional[str] = None
    lifecycle_state: str = "running"
    desired_state: str = "running"
    pause_mode: Optional[str] = None
    restart_safe: bool = False
    recovery_required: bool = False
    interrupted_node_ids: list[str] = field(default_factory=list)
    state_version: int = 0


@dataclass
class SessionHandle:
    metadata: SessionMetadata
    runtime: Runtime
    sse_manager: SSEManager


class SessionManager:
    """Owns isolated Runtime and SSE instances for every conversation."""

    def __init__(
        self,
        registry: ExecutableRegistry,
        root_dir: str = "runtime_sessions",
        max_concurrency: int = 4,
        workspace_root: Optional[str] = None,
        restore: bool = True,
        roles_dir: Optional[str] = None,
    ):
        self.registry = registry
        self.root_dir = os.path.abspath(root_dir)
        self.max_concurrency = max_concurrency
        self.workspace_root = os.path.abspath(workspace_root or os.getcwd())
        self.roles_dir = roles_dir
        self._index_path = os.path.join(self.root_dir, "sessions.json")
        self._config_path = os.path.join(self.root_dir, "config.json")
        self._sessions: dict[str, SessionHandle] = {}
        self._lock = threading.RLock()
        os.makedirs(self.root_dir, exist_ok=True)
        self.global_config = self._load_global_config()
        self.credential_store = CredentialStore(os.path.join(self.root_dir, "api_keys.json"))
        set_credential_store(self.credential_store)
        if restore:
            self._restore_sessions()

    def create_session(
        self,
        task: str,
        title: Optional[str] = None,
        role: str = "default",
        config: Optional[dict] = None,
    ) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        now = _now()
        requested_workspace = (config or {}).get("workspace_root")
        # 会话未显式指定的 CLI/model 等配置，继承全局默认值。
        inherited = {
            key: self.global_config.get(key)
            for key in ("cli_name", "model", "api_base_url", "api_key_env", "language")
            if self.global_config.get(key)
        }
        metadata = SessionMetadata(
            id=session_id,
            title=(title or task.strip().splitlines()[0][:60] or "Untitled session"),
            created_at=now,
            updated_at=now,
            config={**inherited, **_safe_session_config(config)},
            workspace_root=(
                os.path.abspath(os.path.expanduser(requested_workspace))
                if isinstance(requested_workspace, str) and requested_workspace.strip()
                else None
            ),
        )
        handle = self._build_handle(metadata)
        with self._lock:
            self._sessions[session_id] = handle
            self._save_index()
        try:
            root_node_id = handle.runtime.submit_root_manager(
                task, role, dict(metadata.config)
            )
        except Exception:
            with self._lock:
                self._sessions.pop(session_id, None)
                self._save_index()
            handle.sse_manager.stop()
            handle.runtime.shutdown()
            shutil.rmtree(self._session_dir(session_id), ignore_errors=True)
            raise
        result = self._summary(handle)
        result["root_node_id"] = root_node_id
        return result

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            handles = list(self._sessions.values())
        sessions = [self._summary(handle) for handle in handles]
        return sorted(sessions, key=lambda item: item["updated_at"], reverse=True)

    def get_session(self, session_id: str) -> SessionHandle:
        with self._lock:
            handle = self._sessions.get(session_id)
        if handle is None:
            raise KeyError(f"Session '{session_id}' not found")
        return handle

    def update_session(self, session_id: str, title: str) -> dict[str, Any]:
        handle = self.get_session(session_id)
        with self._lock:
            handle.metadata.title = title.strip()
            handle.metadata.updated_at = _now()
            self._save_index()
        return self._summary(handle)

    def update_settings(
        self,
        session_id: str,
        config: Optional[dict] = None,
        workspace_root: Optional[str] = None,
    ) -> dict[str, Any]:
        handle = self.get_session(session_id)
        with self._lock:
            handle.metadata.config.update(_safe_session_config(config))
            if workspace_root is not None:
                value = workspace_root.strip()
                if not value:
                    raise ValueError("workspace_root cannot be empty")
                resolved = os.path.abspath(os.path.expanduser(value))
                os.makedirs(resolved, exist_ok=True)
                handle.metadata.workspace_root = resolved
                handle.runtime.workspace_root = resolved
            handle.metadata.updated_at = _now()
            self._save_index()
        return self._summary(handle)

    def touch(self, session_id: str) -> None:
        handle = self.get_session(session_id)
        with self._lock:
            handle.metadata.updated_at = _now()
            self._save_index()

    def get_global_config(self) -> dict[str, Any]:
        """返回全局（系统级）配置的当前快照。"""
        with self._lock:
            return dict(self.global_config)

    def update_global_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """更新全局（系统级）配置并持久化。

        仅接受 ``_GLOBAL_CONFIG_KEYS`` 中的字段；``workspace_root`` 会解析为
        绝对路径并确保目录存在，空值表示清除该项。
        """
        unknown = {key for key in config if key not in _GLOBAL_CONFIG_KEYS}
        if unknown:
            raise ValueError(f"Unknown global config fields: {sorted(unknown)}")
        with self._lock:
            for key, value in config.items():
                if key == "workspace_root" and value:
                    resolved = os.path.abspath(os.path.expanduser(str(value).strip()))
                    os.makedirs(resolved, exist_ok=True)
                    self.global_config[key] = resolved
                elif key == "language":
                    self.global_config[key] = value if value in ("zh", "en") else "zh"
                else:
                    self.global_config[key] = value or None
            self._save_global_config()
            return dict(self.global_config)

    # ---- 凭据（按模型厂商保存的 API Key）—— 委托 CredentialStore ----

    def get_api_key(self, vendor: str) -> Optional[str]:
        return self.credential_store.get(vendor)

    def set_api_key(self, vendor: str, sk: str) -> None:
        self.credential_store.set(vendor, sk)

    def delete_api_key(self, vendor: str) -> None:
        self.credential_store.delete(vendor)

    def list_api_key_vendors(self) -> list[str]:
        return self.credential_store.vendors()

    def _load_global_config(self) -> dict[str, Any]:
        if not os.path.exists(self._config_path):
            return dict(_GLOBAL_CONFIG_DEFAULTS)
        try:
            with open(self._config_path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return dict(_GLOBAL_CONFIG_DEFAULTS)
        merged = dict(_GLOBAL_CONFIG_DEFAULTS)
        if isinstance(data, dict):
            for key in _GLOBAL_CONFIG_KEYS:
                if key in data:
                    merged[key] = data[key]
        return merged

    def _save_global_config(self) -> None:
        tmp_path = self._config_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as file:
            json.dump(self.global_config, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, self._config_path)

    def pause_session(self, session_id: str, mode: str = "drain") -> dict[str, Any]:
        if mode not in {"drain", "immediate"}:
            raise ValueError("pause mode must be 'drain' or 'immediate'")
        handle = self.get_session(session_id)
        with self._lock:
            metadata = handle.metadata
            if metadata.lifecycle_state in {"paused", "recovery_required"}:
                return self._summary(handle)
            if metadata.lifecycle_state == "pausing":
                if metadata.pause_mode == mode:
                    return self._summary(handle)
                raise RuntimeError("session is already pausing")
            if metadata.lifecycle_state in {"stopped", "completed"}:
                raise RuntimeError(
                    f"cannot pause session in {metadata.lifecycle_state} state"
                )
            metadata.lifecycle_state = "pausing"
            metadata.desired_state = "paused"
            metadata.pause_mode = mode
            metadata.restart_safe = False
            metadata.recovery_required = False
            metadata.interrupted_node_ids = []
            self._advance_metadata(metadata)
            self._save_index()
        self._publish_lifecycle(handle)
        try:
            pause_result = handle.runtime.global_pause(mode)
        except Exception:
            with self._lock:
                metadata.lifecycle_state = "recovery_required"
                metadata.desired_state = "paused"
                metadata.restart_safe = False
                metadata.recovery_required = True
                self._advance_metadata(metadata)
                self._save_index()
            self._publish_lifecycle(handle)
            raise
        interrupted = pause_result.get("interrupted_node_ids", [])
        with self._lock:
            if metadata.lifecycle_state == "pausing" and interrupted:
                metadata.interrupted_node_ids = list(dict.fromkeys(interrupted))
                self._advance_metadata(metadata)
                self._save_index()
        if interrupted:
            self._publish_lifecycle(handle)
        return self._summary(handle)

    def _auto_recover_from_failed(self, session_id: str) -> None:
        """若会话处于不可恢复的终态，自动转为 paused。

        配合 continue_node / revise_manager_node 使用：用户续接或增量恢复失败节点后，会话应允许恢复。
        """
        handle = self._sessions.get(session_id)
        if handle is None:
            return
        metadata = handle.metadata
        if metadata.lifecycle_state in ("failed", "recovery_required", "stopped"):
            with self._lock:
                metadata.lifecycle_state = "paused"
                metadata.desired_state = "paused"
                metadata.restart_safe = True
                metadata.recovery_required = False
                self._advance_metadata(metadata)
                self._save_index()
            self._publish_lifecycle(handle)

    def resume_session(self, session_id: str) -> dict[str, Any]:
        handle = self.get_session(session_id)
        with self._lock:
            metadata = handle.metadata
            if metadata.lifecycle_state == "running":
                return self._summary(handle)
            if metadata.lifecycle_state == "pausing":
                raise RuntimeError("session is still draining; resume after it is paused")
            if metadata.lifecycle_state == "completed":
                graph_status = self._summary(handle)["status"]
                if graph_status not in {"Waiting", "Ready", "Running", "Paused"}:
                    raise RuntimeError(
                        "cannot resume completed session without schedulable work"
                    )
            if metadata.lifecycle_state not in {
                "paused", "recovery_required", "failed", "completed"
            }:
                raise RuntimeError(
                    f"cannot resume session in {metadata.lifecycle_state} state"
                )
            interrupted = list(metadata.interrupted_node_ids)
            metadata.lifecycle_state = "resuming"
            metadata.desired_state = "running"
            metadata.restart_safe = False
            self._advance_metadata(metadata)
            self._save_index()
        self._publish_lifecycle(handle)

        try:
            handle.runtime.global_resume(interrupted)
        except Exception:
            with self._lock:
                metadata.lifecycle_state = "recovery_required"
                metadata.desired_state = "paused"
                metadata.recovery_required = True
                metadata.restart_safe = True
                self._advance_metadata(metadata)
                self._save_index()
            self._publish_lifecycle(handle)
            raise

        with self._lock:
            metadata.lifecycle_state = "running"
            metadata.desired_state = "running"
            metadata.pause_mode = None
            metadata.restart_safe = False
            metadata.recovery_required = False
            metadata.interrupted_node_ids = []
            self._advance_metadata(metadata)
            self._save_index()
        self._publish_lifecycle(handle)
        return self._summary(handle)

    def stop_session(self, session_id: str) -> dict[str, Any]:
        handle = self.get_session(session_id)
        handle.runtime.global_stop()
        with self._lock:
            metadata = handle.metadata
            metadata.lifecycle_state = "stopped"
            metadata.desired_state = "stopped"
            metadata.pause_mode = None
            metadata.restart_safe = True
            metadata.recovery_required = False
            metadata.interrupted_node_ids = []
            self._advance_metadata(metadata)
            self._save_index()
        self._publish_lifecycle(handle)
        return self._summary(handle)

    def _complete_pause(
        self, session_id: str, interrupted_node_ids: list[str]
    ) -> None:
        try:
            handle = self.get_session(session_id)
        except KeyError:
            return
        with self._lock:
            metadata = handle.metadata
            if metadata.desired_state != "paused" or metadata.lifecycle_state not in {
                "pausing",
                "recovery_required",
            }:
                return
            metadata.lifecycle_state = "paused"
            metadata.restart_safe = True
            metadata.recovery_required = False
            metadata.interrupted_node_ids = list(dict.fromkeys(interrupted_node_ids))
            self._advance_metadata(metadata)
            self._save_index()
        self._publish_lifecycle(handle)

    def _reconcile_terminal_state(self, session_id: str) -> None:
        """Persist terminal session state when the graph reaches a terminal result."""
        try:
            handle = self.get_session(session_id)
        except KeyError:
            return
        summary = self._summary(handle)
        terminal = {
            "Completed": "completed",
            "Failed": "failed",
            "Stopped": "stopped",
        }.get(summary["status"])
        if terminal is None:
            return
        with self._lock:
            metadata = handle.metadata
            if metadata.lifecycle_state != "running":
                return
            metadata.lifecycle_state = terminal
            metadata.desired_state = terminal
            metadata.pause_mode = None
            metadata.restart_safe = True
            metadata.recovery_required = False
            metadata.interrupted_node_ids = []
            self._advance_metadata(metadata)
            self._save_index()
        self._publish_lifecycle(handle)

    @staticmethod
    def _advance_metadata(metadata: SessionMetadata) -> None:
        metadata.updated_at = _now()
        metadata.state_version += 1

    def _publish_lifecycle(self, handle: SessionHandle) -> None:
        summary = self._summary(handle)
        handle.runtime.event_bus.publish(
            SSEEvent(
                event_type="session_state_changed",
                sequence_id=0,
                timestamp=time.time(),
                payload={
                    "session_id": handle.metadata.id,
                    "state": handle.metadata.lifecycle_state,
                    "desired_state": handle.metadata.desired_state,
                    "pause_mode": handle.metadata.pause_mode,
                    "running_count": summary["running_count"],
                    "restart_safe": handle.metadata.restart_safe,
                    "recovery_required": handle.metadata.recovery_required,
                    "interrupted_node_ids": list(
                        handle.metadata.interrupted_node_ids
                    ),
                    "state_version": handle.metadata.state_version,
                    "updated_at": handle.metadata.updated_at,
                },
            )
        )

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            handle = self._sessions.pop(session_id, None)
            if handle is None:
                raise KeyError(f"Session '{session_id}' not found")
            self._save_index()
        handle.sse_manager.stop()
        handle.runtime.shutdown()
        shutil.rmtree(self._session_dir(session_id), ignore_errors=True)

    def shutdown(self) -> None:
        with self._lock:
            handles = list(self._sessions.values())
        for handle in handles:
            handle.sse_manager.stop()
            handle.runtime.shutdown()

    def _build_handle(
        self, metadata: SessionMetadata, *, restoring: bool = False
    ) -> SessionHandle:
        session_dir = self._session_dir(metadata.id)
        os.makedirs(session_dir, exist_ok=True)
        workspace_dir = (
            metadata.workspace_root
            or self.global_config.get("workspace_root")
            or os.path.join(session_dir, "workspace")
        )
        workspace_dir = os.path.abspath(os.path.expanduser(workspace_dir))
        metadata.workspace_root = workspace_dir
        os.makedirs(workspace_dir, exist_ok=True)
        runtime = Runtime(
            registry=self.registry,
            persistence=Persistence(os.path.join(session_dir, "graph.json")),
            max_concurrency=self.max_concurrency,
            workspace_root=workspace_dir,
            roles_dir=self.roles_dir,
        )
        runtime.set_pause_completion_callback(
            lambda interrupted, session_id=metadata.id: self._complete_pause(
                session_id, interrupted
            )
        )
        runtime.set_node_status_callback(
            lambda _node_id, _status, session_id=metadata.id: self._reconcile_terminal_state(
                session_id
            )
        )
        runtime.start(paused=restoring, safe_recovery=restoring)
        if restoring:
            self._apply_restored_lifecycle(metadata, runtime)
        sse_manager = SSEManager(event_bus=runtime.event_bus)
        sse_manager.start()
        return SessionHandle(metadata=metadata, runtime=runtime, sse_manager=sse_manager)

    def _apply_restored_lifecycle(
        self, metadata: SessionMetadata, runtime: Runtime
    ) -> None:
        """Migrate old metadata and keep every restored graph fail-safe."""
        snapshot = runtime.get_graph_snapshot()
        nodes = snapshot.get("nodes", [])
        statuses = {str(node.get("status", "Waiting")) for node in nodes}
        interrupted = list(
            dict.fromkeys(
                list(metadata.interrupted_node_ids)
                + runtime.recovered_interrupted_node_ids
            )
        )
        if metadata.lifecycle_state in {"running", "pausing", "resuming"}:
            if statuses and statuses.issubset({"Completed"}):
                metadata.lifecycle_state = "completed"
                metadata.desired_state = "completed"
                metadata.restart_safe = True
            elif statuses and statuses.issubset({"Stopped"}):
                metadata.lifecycle_state = "stopped"
                metadata.desired_state = "stopped"
                metadata.restart_safe = True
            else:
                metadata.lifecycle_state = "recovery_required"
                metadata.desired_state = "paused"
                metadata.recovery_required = True
                metadata.restart_safe = True
                metadata.interrupted_node_ids = interrupted
        elif metadata.lifecycle_state in {"paused", "recovery_required"}:
            metadata.desired_state = "paused"
            metadata.restart_safe = True
            metadata.recovery_required = metadata.lifecycle_state == "recovery_required"
            metadata.interrupted_node_ids = interrupted
        else:
            metadata.restart_safe = metadata.lifecycle_state in {
                "stopped", "completed", "failed"
            }
        self._advance_metadata(metadata)

    def _summary(self, handle: SessionHandle) -> dict[str, Any]:
        snapshot = handle.runtime.get_graph_snapshot()
        nodes = snapshot.get("nodes", [])
        counts: dict[str, int] = {}
        for node in nodes:
            status = str(node.get("status", "Waiting"))
            counts[status] = counts.get(status, 0) + 1
        if counts.get("Running"):
            status = "Running"
        elif counts.get("Paused"):
            status = "Paused"
        elif counts.get("Failed") and not (
            counts.get("Ready") or counts.get("Waiting") or counts.get("Running") or counts.get("Paused")
        ):
            # 仅当没有活跃/已重启节点时才算 Failed（避免重启后立即被标记回 Failed）
            status = "Failed"
        elif nodes and counts.get("Completed") == len(nodes):
            status = "Completed"
        elif nodes and counts.get("Stopped") == len(nodes):
            status = "Stopped"
        else:
            status = "Waiting"
        lifecycle_state = handle.metadata.lifecycle_state
        desired_state = handle.metadata.desired_state
        restart_safe = handle.metadata.restart_safe
        if lifecycle_state == "running" and status == "Completed":
            lifecycle_state = "completed"
            desired_state = "completed"
            restart_safe = True
        elif lifecycle_state == "running" and status == "Failed":
            lifecycle_state = "failed"
            desired_state = "failed"
            restart_safe = True
        return {
            **asdict(handle.metadata),
            "lifecycle_state": lifecycle_state,
            "desired_state": desired_state,
            "restart_safe": restart_safe,
            "status": status,
            "node_count": len(nodes),
            "running_count": max(
                counts.get("Running", 0), handle.runtime.scheduler.active_execution_count
            ),
            "completed_count": counts.get("Completed", 0),
            "failed_count": counts.get("Failed", 0),
        }

    def _restore_sessions(self) -> None:
        if not os.path.exists(self._index_path):
            return
        try:
            with open(self._index_path, "r", encoding="utf-8") as file:
                records = json.load(file)
        except (OSError, json.JSONDecodeError):
            return
        for record in records:
            try:
                metadata = SessionMetadata(**record)
                self._sessions[metadata.id] = self._build_handle(
                    metadata, restoring=True
                )
            except (TypeError, OSError, ValueError):
                continue
        with self._lock:
            self._save_index()

    def _save_index(self) -> None:
        records = [asdict(handle.metadata) for handle in self._sessions.values()]
        tmp_path = self._index_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as file:
            json.dump(records, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, self._index_path)

    def _session_dir(self, session_id: str) -> str:
        return os.path.join(self.root_dir, session_id)
