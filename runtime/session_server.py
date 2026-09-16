"""FastAPI application for multi-session execution graphs."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import subprocess
import tempfile
import termios
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from runtime.models import LogLevel
from runtime.role_pools_manager import RolePoolsManager
from runtime.roles_manager import RolesManager
from runtime.server import _sse_stream
from runtime.session_manager import SessionHandle, SessionManager
from runtime.skills_manager import SkillsManager
from runtime.credentials import vendor_of_model
from runtime.i18n import LanguageMiddleware, localized_detail, t
from cli_adapters import (
    detect_default,
    effective_base_url,
    get_adapter,
    list_clis,
    list_provider_presets,
)
from claude_code_agent import CCExecutionError, ClaudeCodeAgent


class CreateSessionRequest(BaseModel):
    # task 现为可选：新会话默认生成空白图，不再自动创建/执行根节点。
    # 前端不再填写；保留字段仅为 API 兼容（调用方仍可传入并作为标题回退）。
    task: str = Field(default="", max_length=100000)
    title: Optional[str] = Field(default=None, max_length=120)
    role: str = Field(default="default", min_length=1, max_length=80)
    config: dict[str, Any] = Field(default_factory=dict)


class UpdateSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class SessionSettingsRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    workspace_root: Optional[str] = Field(default=None, max_length=4096)
    # 三态：缺省（未发送）= 不修改；null / "" = 清除配置恢复默认；路径 = 设置项目目录。
    shared_workspace: Optional[str] = Field(default=None, max_length=4096)


class GlobalConfigRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


class TestConnectionRequest(BaseModel):
    cli_name: Optional[str] = None
    model: Optional[str] = None
    api_base_url: Optional[str] = None
    api_key_env: Optional[str] = None


class ApiKeyRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    api_key: Optional[str] = Field(default=None, max_length=4096)


class InternalUsernameRequest(BaseModel):
    username: Optional[str] = Field(default=None, max_length=200)


class PauseGraphRequest(BaseModel):
    mode: Literal["drain", "immediate"] = "drain"


class RoleRequest(BaseModel):
    role_id: str = Field(pattern="^[a-z][a-z0-9_]*$")
    role_type: str = Field(default="worker", pattern="^(manager|worker)$")
    content: Optional[str] = Field(default=None, max_length=200000)
    config: Optional[dict[str, Any]] = None


class SkillRequest(BaseModel):
    skill_id: str = Field(pattern="^[a-z][a-z0-9_-]*$")
    content: str = Field(min_length=1, max_length=200000)


class RolePoolRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    roles: list[str] = Field(default_factory=list)


class UpdateRolePoolRequest(BaseModel):
    # 缺省（未发送）= 不修改；池 id 不可变。
    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    roles: Optional[list[str]] = None


class CreateNodeRequest(BaseModel):
    task: str = Field(min_length=1, max_length=100000)
    role: str = Field(default="default", min_length=1, max_length=80)
    exec_type: str = Field(default="worker", pattern="^(manager|worker)$")
    config: dict[str, Any] = Field(default_factory=dict)
    parent_id: Optional[str] = None
    dependencies: list[str] = Field(default_factory=list)


class UpdateNodeRequest(BaseModel):
    task: Optional[str] = Field(default=None, min_length=1, max_length=100000)
    role: Optional[str] = Field(default=None, min_length=1, max_length=80)
    exec_type: Optional[str] = Field(default=None, pattern="^(manager|worker)$")
    config: Optional[dict[str, Any]] = None


class ReviseNodeRequest(BaseModel):
    task: str = Field(min_length=1, max_length=100000)
    config: dict[str, Any] = Field(default_factory=dict)


class CreateEdgeRequest(BaseModel):
    from_node: str
    to_node: str


def create_session_app(
    session_manager: SessionManager,
    roles_manager: Optional[RolesManager] = None,
    skills_manager: Optional[SkillsManager] = None,
    role_pools_manager: Optional[RolePoolsManager] = None,
) -> FastAPI:
    app = FastAPI(title="Execution Graph Workbench API", version="2.0")
    app.add_middleware(LanguageMiddleware)
    project_root = os.path.dirname(os.path.dirname(__file__))
    roles_manager = roles_manager or RolesManager(os.path.join(project_root, "roles"))
    skills_manager = skills_manager or SkillsManager(
        os.path.join(project_root, "skills"),
        extra_dirs=[os.path.expanduser("~/.claude/skills")],
    )
    # 候选角色池存于会话根目录（与 sessions.json / config.json 同级）。
    role_pools_manager = role_pools_manager or RolePoolsManager(
        os.path.join(session_manager.root_dir, "role_pools.json"), roles_manager
    )

    def resolve(session_id: str) -> SessionHandle:
        try:
            return session_manager.get_session(session_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error

    def touch(session_id: str) -> None:
        session_manager.touch(session_id)

    @app.get("/v1/roles")
    def list_roles():
        return {"roles": roles_manager.list_roles()}

    @app.get("/v1/roles/schema/{role_type}")
    def get_role_schema(role_type: str):
        try:
            return roles_manager.get_role_schema(role_type)
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.get("/v1/roles/{role_type}/{role_id}")
    def get_role(role_type: str, role_id: str):
        try:
            return roles_manager.get_role(role_id, role_type)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.post("/v1/roles", status_code=201)
    def create_role(body: RoleRequest):
        try:
            return roles_manager.add_role(
                body.role_id, body.content, body.role_type, body.config
            )
        except ValueError as error:
            status_code = 409 if "already exists" in str(error) else 400
            raise HTTPException(status_code=status_code, detail=localized_detail(error)) from error
        except TypeError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.patch("/v1/roles/{role_type}/{role_id}")
    def update_role(role_type: str, role_id: str, body: RoleRequest):
        try:
            if body.role_id != role_id or body.role_type != role_type:
                raise ValueError("Role id and type cannot be changed")
            return roles_manager.update_role(role_id, body.content, role_type, body.config)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.delete("/v1/roles/{role_type}/{role_id}", status_code=204)
    def delete_role(role_type: str, role_id: str):
        try:
            roles_manager.delete_role(role_id, role_type)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.get("/v1/role-pools")
    def list_role_pools():
        return {"pools": role_pools_manager.list_pools()}

    @app.post("/v1/role-pools", status_code=201)
    def create_role_pool(body: RolePoolRequest):
        try:
            return role_pools_manager.create_pool(body.name, body.roles)
        except ValueError as error:
            status_code = 409 if "already exists" in str(error) else 400
            raise HTTPException(status_code=status_code, detail=localized_detail(error)) from error

    @app.patch("/v1/role-pools/{pool_id}")
    def update_role_pool(pool_id: str, body: UpdateRolePoolRequest):
        try:
            return role_pools_manager.update_pool(pool_id, body.name, body.roles)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except ValueError as error:
            status_code = 409 if "already exists" in str(error) else 400
            raise HTTPException(status_code=status_code, detail=localized_detail(error)) from error

    @app.delete("/v1/role-pools/{pool_id}", status_code=204)
    def delete_role_pool(pool_id: str):
        try:
            role_pools_manager.delete_pool(pool_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error

    @app.get("/v1/skills")
    def list_skills():
        return {"skills": skills_manager.list_skills()}

    @app.get("/v1/skills/{skill_id}")
    def get_skill(skill_id: str):
        try:
            return skills_manager.get_skill(skill_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.post("/v1/skills", status_code=201)
    def create_skill(body: SkillRequest):
        try:
            try:
                skills_manager.get_skill(body.skill_id)
            except KeyError:
                pass
            else:
                raise HTTPException(
                    status_code=409,
                    detail=t(
                        f"Skill '{body.skill_id}' 已存在",
                        f"Skill '{body.skill_id}' already exists",
                    ),
                )
            return skills_manager.save_skill(body.skill_id, body.content)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.patch("/v1/skills/{skill_id}")
    def update_skill(skill_id: str, body: SkillRequest):
        try:
            if body.skill_id != skill_id:
                raise ValueError("Skill id cannot be changed")
            skills_manager.get_skill(skill_id)
            return skills_manager.save_skill(skill_id, body.content)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.delete("/v1/skills/{skill_id}", status_code=204)
    def delete_skill(skill_id: str):
        try:
            skills_manager.delete_skill(skill_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.post("/v1/skills/import", status_code=201)
    async def import_skill(skill_id: str, request: Request):
        """从 zip 归档导入完整的多文件 skill（raw bytes body + skill_id query）。"""
        try:
            raw = await request.body()
            return skills_manager.import_skill_zip(skill_id, raw)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.get("/v1/config")
    def get_global_config():
        """返回全局（系统级）配置 + 可用 CLI 列表 + 默认 CLI。"""
        try:
            default_cli = detect_default()
        except RuntimeError:
            default_cli = None
        return {
            "config": session_manager.get_global_config(),
            "clis": list_clis(),
            "provider_presets": list_provider_presets(),
            "default_cli": default_cli,
        }

    @app.get("/v1/effective-base-url")
    def get_effective_base_url(cli_name: str = "", model: str = "", api_base_url: str = ""):
        """给定 (CLI, 模型, 已配置地址) 时**真正会被用到**的端点。

        连接表单用它显示「生效地址」：换模型就重新问一次，显示的永远是与实际请求同源的
        那个值（cli_adapters.effective_base_url 直接复用建子进程前的那两个函数）。
        传的是表单里的**当前**值而非已保存的值 —— 用户改了模型还没保存时也要能看见。
        """
        base_url, source = effective_base_url(
            cli_name or None, model or None, api_base_url or None
        )
        return {"base_url": base_url, "source": source}

    @app.patch("/v1/config")
    def update_global_config(body: GlobalConfigRequest):
        if "api_key" in body.config:
            raise HTTPException(
                status_code=400,
                detail=t(
                    "请将 API Key 存入环境变量并设置 api_key_env",
                    "Store API keys in an environment variable and set api_key_env",
                ),
            )
        try:
            return {"config": session_manager.update_global_config(body.config)}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.post("/v1/config/test-connection")
    def test_connection(body: TestConnectionRequest):
        """真实端到端连通性测试：用最小 prompt 跑一次所选 CLI+模型。

        始终返回 200，结果封装在 ``{ok, message, result}`` 中；失败（未安装、
        认证缺失、调用报错、超时）一律 ``ok=False``，方便前端直接展示。
        """
        # 连接测试允许只提交部分字段；未提交的字段继承设置页已保存的全局配置。
        saved_config = session_manager.get_global_config()
        cli_id = body.cli_name or saved_config.get("cli_name")
        model = body.model or saved_config.get("model")
        api_base_url = body.api_base_url or saved_config.get("api_base_url")
        api_key_env = body.api_key_env or saved_config.get("api_key_env")

        # 兜底：模型名能匹配公开 provider 预置时，自动补齐连接参数。
        # 这样即使前端尚未保存全局配置，测试也不会回落到 OpenAI 默认 provider。
        if model and (not api_base_url or not api_key_env or not cli_id):
            lowered_model = model.lower()
            for preset in list_provider_presets():
                preset_id = str(preset.get("id", "")).lower()
                if preset_id and preset_id in lowered_model:
                    cli_id = cli_id or preset.get("cli_name")
                    api_base_url = api_base_url or preset.get("api_base_url")
                    api_key_env = api_key_env or preset.get("api_key_env")
                    break

        if not cli_id:
            try:
                cli_id = detect_default()
            except RuntimeError as error:
                return {"ok": False, "message": str(error), "result": None}

        try:
            adapter = get_adapter(cli_id)
        except ValueError as error:
            return {"ok": False, "message": str(error), "result": None}
        if not adapter.is_installed():
            return {"ok": False, "message": f"CLI '{cli_id}' 未安装", "result": None}

        key_env = api_key_env or getattr(adapter, "api_key_env", None)
        key_missing = bool(key_env) and not os.environ.get(key_env)
        if key_missing and model:
            vendor = vendor_of_model(model)
            if vendor and session_manager.get_api_key(vendor):
                key_missing = False

        with tempfile.TemporaryDirectory(prefix="aicu_conn_test_") as tmp:
            try:
                agent = ClaudeCodeAgent(
                    workspace_path=tmp,
                    model=model,
                    skip_permissions=True,
                    cli_name=cli_id,
                    api_base_url=api_base_url,
                    api_key_env=api_key_env,
                )
                result = agent.chat("请只回复两个字：OK", max_turns=1, timeout=120)
            except CCExecutionError as error:
                return {"ok": False, "message": str(error), "result": None}
            except subprocess.TimeoutExpired:
                return {"ok": False, "message": "连接超时（>120s）", "result": None}
            except RuntimeError as error:
                return {"ok": False, "message": str(error), "result": None}
            except Exception as error:  # noqa: BLE001
                return {"ok": False, "message": f"测试失败: {error}", "result": None}

        message = "连接成功"
        if key_missing:
            message += f"（注意：环境变量 {key_env} 未设置，本次可能走了 CLI 内置认证）"
        return {"ok": True, "message": message, "result": (result or "")[:500]}

    @app.get("/v1/config/api-key")
    def get_api_key(model: str = Query(min_length=1, max_length=200)):
        """按模型厂商回显已保存的 sk（明文）；内部模型返回 vendor=null。"""
        vendor = vendor_of_model(model)
        if not vendor:
            return {"vendor": None, "api_key": None}
        return {"vendor": vendor, "api_key": session_manager.get_api_key(vendor)}

    @app.put("/v1/config/api-key")
    def put_api_key(body: ApiKeyRequest):
        """按模型厂商保存 sk；空 api_key 视为删除该厂商凭据。"""
        vendor = vendor_of_model(body.model)
        if not vendor:
            raise HTTPException(
                status_code=400,
                detail=t(
                    f"无法从模型 '{body.model}' 推导出厂商，内部模型无需 sk",
                    f"Cannot infer vendor from model '{body.model}'; internal models need no sk",
                ),
            )
        key = (body.api_key or "").strip()
        if key:
            session_manager.set_api_key(vendor, key)
            return {"vendor": vendor, "saved": True}
        session_manager.delete_api_key(vendor)
        return {"vendor": vendor, "saved": False}

    @app.delete("/v1/config/api-key", status_code=204)
    def delete_api_key(vendor: str = Query(min_length=1, max_length=80)):
        session_manager.delete_api_key(vendor)

    @app.get("/v1/config/internal-username")
    def get_internal_username():
        return {"username": session_manager.get_internal_username()}

    @app.put("/v1/config/internal-username")
    def put_internal_username(body: InternalUsernameRequest):
        username = (body.username or "").strip()
        if username:
            session_manager.set_internal_username(username)
            return {"username": username, "saved": True}
        session_manager.delete_internal_username()
        return {"username": None, "saved": False}

    @app.get("/v1/sessions")
    def list_sessions():
        return {"sessions": session_manager.list_sessions()}

    @app.post("/v1/sessions", status_code=201)
    def create_session(body: CreateSessionRequest):
        try:
            return session_manager.create_session(
                task=body.task,
                title=body.title,
                role=body.role,
                config=body.config,
            )
        except (ValueError, RuntimeError) as error:
            raise HTTPException(status_code=409, detail=localized_detail(error)) from error

    @app.get("/v1/sessions/{session_id}")
    def get_session(session_id: str):
        handle = resolve(session_id)
        return next(
            item for item in session_manager.list_sessions()
            if item["id"] == handle.metadata.id
        )

    @app.patch("/v1/sessions/{session_id}")
    def update_session(session_id: str, body: UpdateSessionRequest):
        resolve(session_id)
        return session_manager.update_session(session_id, body.title)

    @app.get("/v1/sessions/{session_id}/settings")
    def get_session_settings(session_id: str):
        handle = resolve(session_id)
        config = dict(handle.metadata.config)
        credential_env = config.get("api_key_env")
        return {
            "config": config,
            "workspace_root": handle.metadata.workspace_root,
            "shared_workspace": handle.metadata.shared_workspace,
            "credential_configured": bool(
                credential_env and os.environ.get(str(credential_env))
            ),
        }

    @app.patch("/v1/sessions/{session_id}/settings")
    def update_session_settings(session_id: str, body: SessionSettingsRequest):
        resolve(session_id)
        if "api_key" in body.config:
            raise HTTPException(
                status_code=400,
                detail=t(
                    "请将 API Key 存入环境变量并设置 api_key_env",
                    "Store API keys in an environment variable and set api_key_env",
                ),
            )
        try:
            # 三态透传：仅当请求体显式携带 shared_workspace 键时才传给 update_settings
            # （未发送 = 不修改；null/"" = 清除；路径 = 设置）。
            settings_kwargs: dict[str, Any] = {}
            if "shared_workspace" in body.model_fields_set:
                settings_kwargs["shared_workspace"] = body.shared_workspace
            return session_manager.update_settings(
                session_id, body.config, body.workspace_root, **settings_kwargs
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.delete("/v1/sessions/{session_id}", status_code=204)
    def delete_session(session_id: str):
        try:
            session_manager.delete_session(session_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error

    @app.get("/v1/sessions/{session_id}/graph")
    def get_graph(session_id: str):
        return resolve(session_id).runtime.get_graph_snapshot()

    @app.get("/v1/sessions/{session_id}/graph/link-candidates")
    def get_link_candidates(session_id: str, node_id: Optional[str] = None):
        try:
            return {
                "candidates": resolve(session_id).runtime.get_link_candidates(
                    node_id
                )
            }
        except ValueError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error

    @app.get("/v1/sessions/{session_id}/workspace")
    def list_session_workspace(
        session_id: str, path: str = Query(default="", max_length=4096)
    ):
        try:
            return resolve(session_id).runtime.list_workspace(path=path)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.get("/v1/sessions/{session_id}/workspace/file")
    def read_session_workspace_file(
        session_id: str, path: str = Query(min_length=1, max_length=4096)
    ):
        try:
            return resolve(session_id).runtime.read_workspace_file(path=path)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.get("/v1/sessions/{session_id}/shared")
    def list_session_shared(
        session_id: str, path: str = Query(default="", max_length=4096)
    ):
        """会话级「查看产出」：shared 根文件树（分区行带 producer/delivery 元数据）。"""
        try:
            return resolve(session_id).runtime.list_session_shared(path=path)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.get("/v1/sessions/{session_id}/shared/file")
    def read_session_shared_file(
        session_id: str, path: str = Query(min_length=1, max_length=4096)
    ):
        """读取共享根下产物文件（文本白名单预览 / 二进制不可预览）。"""
        try:
            return resolve(session_id).runtime.read_session_shared_file(path=path)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.post("/v1/sessions/{session_id}/graph/pause")
    def pause_graph(session_id: str, body: PauseGraphRequest = PauseGraphRequest()):
        try:
            return session_manager.pause_session(session_id, body.mode)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except (RuntimeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=localized_detail(error)) from error

    @app.post("/v1/sessions/{session_id}/graph/resume")
    def resume_graph(session_id: str):
        try:
            return session_manager.resume_session(session_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=localized_detail(error)) from error

    @app.post("/v1/sessions/{session_id}/graph/stop")
    def stop_graph(session_id: str):
        try:
            return session_manager.stop_session(session_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error

    @app.delete("/v1/sessions/{session_id}/graph")
    def clear_graph(session_id: str):
        resolve(session_id).runtime.clear_graph()
        touch(session_id)
        return {"status": "cleared"}

    @app.post("/v1/sessions/{session_id}/nodes", status_code=201)
    def create_node(session_id: str, body: CreateNodeRequest):
        handle = resolve(session_id)
        runtime = handle.runtime
        # 注入会话配置兜底（model → _session_model，连接参数未显式时回退会话值）
        config = _merge_session_connection(handle.metadata.config, body.config)
        try:
            node = runtime.create_manual_node(
                task=body.task,
                role=body.role,
                exec_type=body.exec_type,
                config=config,
                parent_id=body.parent_id,
                dependencies=body.dependencies,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error
        touch(session_id)
        return _node_detail(runtime, node.id)

    @app.get("/v1/sessions/{session_id}/nodes/{node_id}")
    def get_node(session_id: str, node_id: str):
        runtime = resolve(session_id).runtime
        if runtime.get_node(node_id) is None:
            raise HTTPException(status_code=404, detail=t(f"节点 '{node_id}' 不存在", f"Node '{node_id}' not found"))
        return _node_detail(runtime, node_id)

    @app.patch("/v1/sessions/{session_id}/nodes/{node_id}")
    def update_node(session_id: str, node_id: str, body: UpdateNodeRequest):
        runtime = resolve(session_id).runtime
        updates = body.model_dump(exclude_none=True)
        try:
            runtime.update_manual_node(node_id, **updates)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=localized_detail(error)) from error
        touch(session_id)
        return _node_detail(runtime, node_id)

    @app.delete("/v1/sessions/{session_id}/nodes/{node_id}", status_code=204)
    def delete_node(session_id: str, node_id: str):
        """删除节点：仅叶子节点（无子节点）。Manager 有子节点 → 409（先删其子节点）。

        删除保护：清理子节点是严重操作，只有用户在前端手动逐叶子删除；
        后端不再支持级联删除（cascade 参数已移除）。
        """
        runtime = resolve(session_id).runtime
        try:
            runtime.delete_manual_node(node_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=localized_detail(error)) from error
        touch(session_id)

    @app.post("/v1/sessions/{session_id}/nodes/{node_id}/pause")
    def pause_node(session_id: str, node_id: str):
        try:
            resolve(session_id).runtime.pause_node(node_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        touch(session_id)
        return {"status": "paused", "node_id": node_id}

    @app.post("/v1/sessions/{session_id}/nodes/{node_id}/resume")
    def resume_node(session_id: str, node_id: str):
        try:
            resolve(session_id).runtime.resume_node(node_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        touch(session_id)
        return {"status": "resumed", "node_id": node_id}

    @app.post("/v1/sessions/{session_id}/nodes/{node_id}/stop")
    def stop_node(session_id: str, node_id: str):
        try:
            resolve(session_id).runtime.stop_node(node_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        touch(session_id)
        return {"status": "stopped", "node_id": node_id}

    @app.post("/v1/sessions/{session_id}/nodes/{node_id}/continue")
    def continue_node(session_id: str, node_id: str):
        try:
            node_id = resolve(session_id).runtime.continue_node(node_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=localized_detail(error)) from error
        session_manager._auto_recover_from_failed(session_id)
        touch(session_id)
        return {"status": "running", "node_id": node_id}

    @app.post("/v1/sessions/{session_id}/nodes/{node_id}/revise")
    def revise_node(session_id: str, node_id: str, body: ReviseNodeRequest):
        try:
            resolve(session_id).runtime.revise_node(
                node_id, body.task, body.config
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=localized_detail(error)) from error
        session_manager._auto_recover_from_failed(session_id)
        touch(session_id)
        return {"status": "revising", "node_id": node_id}

    @app.get("/v1/sessions/{session_id}/nodes/{node_id}/workspace")
    def list_node_workspace(
        session_id: str,
        node_id: str,
        path: str = Query(default="", max_length=4096),
    ):
        try:
            return resolve(session_id).runtime.list_workspace(
                path=path, node_id=node_id
            )
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.get("/v1/sessions/{session_id}/nodes/{node_id}/workspace/file")
    def read_node_workspace_file(
        session_id: str,
        node_id: str,
        path: str = Query(min_length=1, max_length=4096),
    ):
        try:
            return resolve(session_id).runtime.read_workspace_file(
                path=path, node_id=node_id
            )
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.get("/v1/sessions/{session_id}/nodes/{node_id}/logs")
    def get_node_logs(
        session_id: str,
        node_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
    ):
        runtime = resolve(session_id).runtime
        try:
            logs = runtime.get_logs(node_id, offset=offset, limit=limit)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        return [_serialize_log(node_id, entry) for entry in logs]

    @app.get("/v1/sessions/{session_id}/nodes/{node_id}/activity")
    def get_node_activity(session_id: str, node_id: str):
        try:
            return {"node_id": node_id, "activities": resolve(session_id).runtime.get_cc_activity(node_id)}
        except ValueError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error

    @app.get("/v1/sessions/{session_id}/nodes/{node_id}/results")
    def list_node_results(session_id: str, node_id: str, path: str = Query(default="", max_length=4096)):
        try:
            return resolve(session_id).runtime.list_node_results(node_id, path)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.get("/v1/sessions/{session_id}/nodes/{node_id}/results/file")
    def read_node_result_file(session_id: str, node_id: str, path: str = Query(min_length=1, max_length=4096)):
        try:
            return resolve(session_id).runtime.read_node_result_file(node_id, path)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error

    @app.get("/v1/sessions/{session_id}/nodes/{node_id}/artifact")
    def get_artifact(session_id: str, node_id: str):
        runtime = resolve(session_id).runtime
        node = runtime.get_node(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail=t(f"节点 '{node_id}' 不存在", f"Node '{node_id}' not found"))
        if not node.artifact_path:
            return {"path": None, "content": None}
        try:
            return {
                "path": node.artifact_path,
                "content": runtime.read_artifact(node_id, node.artifact_path),
            }
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error

    @app.post("/v1/sessions/{session_id}/edges", status_code=201)
    def create_edge(session_id: str, body: CreateEdgeRequest):
        runtime = resolve(session_id).runtime
        try:
            edge = runtime.add_dependency(body.from_node, body.to_node)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=localized_detail(error)) from error
        touch(session_id)
        return {
            "id": edge.id,
            "type": edge.type.value,
            "from_node": edge.from_node,
            "to_node": edge.to_node,
        }

    @app.delete("/v1/sessions/{session_id}/edges/{edge_id}", status_code=204)
    def delete_edge(session_id: str, edge_id: str):
        runtime = resolve(session_id).runtime
        try:
            runtime.remove_dependency(edge_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=localized_detail(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=localized_detail(error)) from error
        touch(session_id)

    @app.get("/v1/sessions/{session_id}/graph/events")
    async def stream_events(session_id: str, request: Request):
        handle = resolve(session_id)
        last_event_id = request.headers.get("last-event-id")
        since_id = int(last_event_id) if last_event_id and last_event_id.isdigit() else None
        return await _sse_stream(
            request,
            handle.runtime,
            handle.sse_manager,
            since_id=since_id,
        )

    @app.get("/v1/sessions/{session_id}/graph/events/since/{sequence_id}")
    async def stream_events_since(session_id: str, sequence_id: int, request: Request):
        handle = resolve(session_id)
        return await _sse_stream(
            request,
            handle.runtime,
            handle.sse_manager,
            since_id=sequence_id,
        )

    @app.get("/v1/sessions/{session_id}/agent-events")
    def list_agent_events(session_id: str):
        """Agent 事件日志（含路由记录 routed_to 与生命周期 status）。

        数据源 = ``runtime.agent_events.list_events()``（events.json 恢复后的事实来源）。
        每条事件解析 producer / 目标的节点标签，前端直接展示，无需再查图。
        """
        runtime = resolve(session_id).runtime
        events = runtime.agent_events.list_events()
        return {"events": [_serialize_agent_event(runtime, msg) for msg in events]}

    @app.websocket("/v1/sessions/{session_id}/terminal")
    async def session_terminal(ws: WebSocket, session_id: str):
        """会话底部终端：在会话「项目目录」（shared 根）启动交互式 bash（PTY）。

        默认 cwd = 会话项目目录（未配置时为 {workspace_root}/shared，见
        Runtime.get_shared_workspace）。前端用 xterm.js 连接；上行消息 = 键盘输入
        原始文本，`{"type":"resize",cols,rows}` 用于窗口尺寸同步；下行消息 = PTY
        输出。WebSocket 断开时杀掉整个进程组。
        """
        handle = resolve(session_id)
        cwd = handle.runtime.get_shared_workspace()
        os.makedirs(cwd, exist_ok=True)

        await ws.accept()

        pid, fd = pty.fork()
        if pid == 0:  # 子进程：切到共享工作区后 exec bash
            try:
                os.chdir(cwd)
                os.environ.setdefault("TERM", "xterm-256color")
            except OSError:
                os._exit(127)
            try:
                os.execvp("bash", ["bash", "-l"])
            except OSError:
                os._exit(127)

        loop = asyncio.get_running_loop()
        closed = False

        def cleanup() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            # pty.fork 的子进程是会话首进程，pgid == pid；杀整组覆盖 bash 派生的子命令。
            for sig in (signal.SIGHUP, signal.SIGKILL):
                try:
                    os.killpg(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass

        async def pty_to_ws() -> None:
            try:
                while True:
                    try:
                        data = await loop.run_in_executor(None, os.read, fd, 65536)
                    except OSError:  # 子进程退出 → 读 master 返回 EIO
                        break
                    if not data:
                        break
                    await ws.send_text(data.decode("utf-8", errors="replace"))
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                cleanup()

        reader = asyncio.create_task(pty_to_ws())

        try:
            while True:
                message = await ws.receive_text()
                if message.startswith('{"type":"resize"'):
                    try:
                        payload = json.loads(message)
                        cols = int(payload.get("cols", 0))
                        rows = int(payload.get("rows", 0))
                        if cols > 0 and rows > 0:
                            fcntl.ioctl(
                                fd,
                                termios.TIOCSWINSZ,
                                struct.pack("HHHH", rows, cols, 0, 0),
                            )
                            os.kill(pid, signal.SIGWINCH)
                    except (ValueError, OSError):
                        pass
                    continue
                try:
                    await loop.run_in_executor(
                        None, os.write, fd, message.encode("utf-8")
                    )
                except OSError:
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            cleanup()

    # Keep this mount last so the explicitly declared /v1 and WebSocket routes
    # above always take precedence.  When the optional React build exists, the
    # same server can therefore provide both the API and the workbench.
    frontend_dist = os.path.join(project_root, "frontend", "dist")
    if os.path.isdir(frontend_dist):
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="workbench")

    return app


# ---- 节点创建：会话配置（继承自系统设置）作为连接兜底注入节点 config ----
# 原则：节点未显式指定的连接参数，回退到会话配置；会话配置来自系统设置
# （WorkbenchSettings → PATCH /v1/config → update_global_config → create_session 继承）。
# model 以独立键 _session_model 注入，与节点显式 model 区分，保证解析优先级：
#   节点显式 model > 角色 yaml 显式 model > 系统设置(_session_model) > CLI 自身默认
# 全程无代码级兜底默认值（不要代码兜底）。
_SESSION_FALLBACK_KEYS = ("cli_name", "api_base_url", "api_key_env")


def _merge_session_connection(
    session_config: Optional[dict], config: Optional[dict]
) -> dict:
    prepared = dict(config or {})
    for key in _SESSION_FALLBACK_KEYS:
        if not prepared.get(key) and (session_config or {}).get(key):
            prepared[key] = session_config[key]
    if (session_config or {}).get("model"):
        prepared["_session_model"] = session_config["model"]
    return prepared


def _node_label(runtime: Any, node_id: str) -> str:
    """节点展示标签（role/exec_type + 短 id），与 _node_detail 的 label 同源。"""
    node = runtime.get_node(node_id)
    if node is None:
        return f"missing {node_id[:8]}"
    return f"{node.role or node.exec_type} {node.id[:8]}"


def _coerce_event_content(content: Any) -> str:
    """事件 content 规范化为字符串（Manager review 类事件 content 为 dict，前端直接渲染会白屏）。"""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(content)


def _serialize_agent_event(runtime: Any, msg: Any) -> dict[str, Any]:
    """Agent 事件 → 前端可读 dict（producer/目标解析为节点标签）。"""
    targets = [
        {"node_id": target_id, "label": _node_label(runtime, target_id)}
        for target_id in (msg.routed_to or [])
    ]
    objective = getattr(msg, "objective", "") or ""
    if not objective:
        # 兼容旧 EventMessage（content 字段）
        objective = getattr(msg, "content", "") or ""
    return {
        "event_id": msg.event_id,
        "producer": msg.producer,
        "producer_label": _node_label(runtime, msg.producer),
        "objective": _coerce_event_content(objective),
        "intent": getattr(msg, "intent", "notification"),
        "affected_roles": list(getattr(msg, "affected_roles", None) or []),
        "scope": getattr(msg, "scope", "related"),
        "action": getattr(msg, "action", ""),
        "created_at": msg.created_at,
        "status": msg.status,
        "routed_to": list(msg.routed_to or []),
        "targets": targets,
    }


def _node_detail(runtime: Any, node_id: str) -> dict[str, Any]:
    node = runtime.get_node(node_id)
    data = json.loads(node.to_json())
    data["config"] = {
        key: value for key, value in (node.config or {}).items()
        if not key.startswith("_")
    }
    data["workspace_path"] = (node.config or {}).get("_workspace_relpath")
    data["label"] = f"{node.role or node.exec_type} {node.id[:8]}"
    data["children"] = [item.id for item in runtime.graph.children(node_id)]
    data["dependencies"] = [item.id for item in runtime.graph.dependencies(node_id)]
    data["dependents"] = [item.id for item in runtime.graph.dependents(node_id)]
    return data


def _serialize_log(node_id: str, entry: Any) -> dict[str, Any]:
    return {
        "id": id(entry) & 0x7FFFFFFF,
        "node_id": node_id,
        "ts": str(entry.ts),
        "timestamp": str(entry.ts),
        "level": entry.level.value if isinstance(entry.level, LogLevel) else str(entry.level),
        "message": entry.message,
        "cc_id": getattr(entry, "cc_id", None),
        "phase": getattr(entry, "phase", None),
        "event": getattr(entry, "event", None),
        "task": getattr(entry, "task", None),
    }
