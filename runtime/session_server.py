"""FastAPI application for multi-session execution graphs."""

from __future__ import annotations

import asyncio
import os
import posixpath
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from runtime.credentials import vendor_of_model
from runtime.file_transfer import (
    RevealUnsupported,
    UploadTooLarge,
    receive_upload,
    validate_upload_name,
)
from runtime.i18n import LanguageMiddleware, localized_detail, t
from runtime.origin_guard import OriginGuardMiddleware
from runtime.mcp_client import DEFAULT_PROBE_TIMEOUT, McpProbeError, probe_server
from runtime.mcp_manager import (
    McpConfigError,
    build_server,
    delete_server as delete_mcp_server,
    list_servers as list_mcp_servers,
    roles_referencing,
    server_names,
    save_server as save_mcp_server,
)
from runtime.repo_paths import REPO_ROOT
from runtime.roles_manager import RolesManager
from runtime.role_pool_bundle import BundleTooLarge, MAX_BUNDLE_BYTES, RolePoolBundle
from runtime.role_pools_manager import RolePoolsManager
from runtime.sse_http import _sse_stream
from runtime.session_serializers import (
    _node_detail,
    _serialize_agent_event,
    _serialize_log,
)
from runtime.terminal_ws import serve_terminal
from runtime.shared_io import mime_type_for
from runtime.session_manager import SessionHandle, SessionManager
from runtime.skills_manager import SkillsManager
from cli_adapters import detect_default, get_adapter, list_clis
from cli_providers import effective_base_url, list_provider_presets
from claude_code_agent import CCExecutionError, ClaudeCodeAgent


def _http_error(status_code: int, error: Exception) -> HTTPException:
    """把领域异常转成响应体一致的 HTTP 错误（detail 走本地化，保留异常链由调用方 ``from``）。

    全 app 四十来个路由都在做同一件事：领域层抛 KeyError/ValueError/FileNotFoundError，
    路由层按语义翻成 404 或 400。分散写的话，将来改错误信封（加 code、换 detail 形状）
    要逐个路由去追，漏一个就多一种响应体。
    """
    return HTTPException(status_code=status_code, detail=localized_detail(error))


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
    provider_id: Optional[str] = None


class ApiKeyRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    provider_id: Optional[str] = Field(default=None, max_length=80)
    api_key: Optional[str] = Field(default=None, max_length=4096)


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


class McpServerRequest(BaseModel):
    """一条 MCP server 定义。

    ``name`` 的形状在这里先挡一道（与 skills_manager 的 id 规则一致），业务层
    ``validate_server`` 还会用同一条正则再挡一道 —— 名字最终会变成 CLI 的配置键
    （grok 的 TOML 表名、codex 的 ``-c`` 键路径），不能只靠一层。

    两种传输共用这一个模型：本地填 ``command``（+ ``args`` / ``env``），远程填 ``url``
    （+ ``headers``），哪组算数由 ``validate_server`` 按「有没有 url」判。``command``
    因此是可选 —— 远程条目本来就没有命令。
    """

    name: str = Field(pattern="^[a-z][a-z0-9_-]*$", max_length=80)
    command: Optional[str] = Field(default=None, max_length=1000)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: Optional[str] = Field(default=None, max_length=2000)
    headers: dict[str, str] = Field(default_factory=dict)
    description: str = Field(default="", max_length=2000)
    required: bool = True


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
    mcp_registry_path: Optional[str] = None,
) -> FastAPI:
    app = FastAPI(title="Execution Graph Workbench API", version="2.0")
    app.add_middleware(LanguageMiddleware)
    app.add_middleware(OriginGuardMiddleware)
    project_root = REPO_ROOT
    roles_manager = roles_manager or RolesManager(os.path.join(project_root, "roles"))
    skills_manager = skills_manager or SkillsManager(
        os.path.join(project_root, "skills"),
        extra_dirs=[os.path.expanduser("~/.claude/skills")],
    )
    # 候选角色池存于会话根目录（与 sessions.json / config.json 同级）。
    role_pools_manager = role_pools_manager or RolePoolsManager(
        os.path.join(session_manager.root_dir, "role_pools.json"), roles_manager
    )
    # MCP 注册表默认是 config/mcp_servers.json；参数只为测试能指向 tmp_path。
    registry_path = Path(mcp_registry_path) if mcp_registry_path else None
    role_pool_bundle = RolePoolBundle(
        role_pools_manager, roles_manager, skills_manager, registry_path
    )

    def resolve(session_id: str) -> SessionHandle:
        try:
            return session_manager.get_session(session_id)
        except KeyError as error:
            raise _http_error(404, error) from error

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
            raise _http_error(400, error) from error

    @app.get("/v1/roles/{role_type}/{role_id}")
    def get_role(role_type: str, role_id: str):
        try:
            return roles_manager.get_role(role_id, role_type)
        except KeyError as error:
            raise _http_error(404, error) from error
        except (ValueError, TypeError) as error:
            raise _http_error(400, error) from error

    @app.post("/v1/roles", status_code=201)
    def create_role(body: RoleRequest):
        try:
            return roles_manager.add_role(
                body.role_id, body.content, body.role_type, body.config
            )
        except ValueError as error:
            status_code = 409 if "already exists" in str(error) else 400
            raise _http_error(status_code, error) from error
        except TypeError as error:
            raise _http_error(400, error) from error

    @app.patch("/v1/roles/{role_type}/{role_id}")
    def update_role(role_type: str, role_id: str, body: RoleRequest):
        try:
            if body.role_id != role_id or body.role_type != role_type:
                raise ValueError("Role id and type cannot be changed")
            return roles_manager.update_role(role_id, body.content, role_type, body.config)
        except KeyError as error:
            raise _http_error(404, error) from error
        except (ValueError, TypeError) as error:
            raise _http_error(400, error) from error

    @app.delete("/v1/roles/{role_type}/{role_id}", status_code=204)
    def delete_role(role_type: str, role_id: str):
        try:
            roles_manager.delete_role(role_id, role_type)
        except KeyError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

    @app.get("/v1/role-pools")
    def list_role_pools():
        return {"pools": role_pools_manager.list_pools()}

    @app.post("/v1/role-pools", status_code=201)
    def create_role_pool(body: RolePoolRequest):
        try:
            return role_pools_manager.create_pool(body.name, body.roles)
        except ValueError as error:
            status_code = 409 if "already exists" in str(error) else 400
            raise _http_error(status_code, error) from error

    @app.patch("/v1/role-pools/{pool_id}")
    def update_role_pool(pool_id: str, body: UpdateRolePoolRequest):
        try:
            return role_pools_manager.update_pool(pool_id, body.name, body.roles)
        except KeyError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            status_code = 409 if "already exists" in str(error) else 400
            raise _http_error(status_code, error) from error

    @app.delete("/v1/role-pools/{pool_id}", status_code=204)
    def delete_role_pool(pool_id: str):
        try:
            role_pools_manager.delete_pool(pool_id)
        except KeyError as error:
            raise _http_error(404, error) from error

    # ---- 角色池导入 / 导出（连同池内角色引用的 skill 与 MCP 定义） ----
    # 包格式与校验见 runtime/role_pool_bundle.py。服务本身无认证，但 POST /v1/mcp/servers
    # 本来就能写入可执行命令，导入没有新增能力；dry_run 预览会把将写入的 command / args 摆出来。

    @app.get("/v1/role-pools/{pool_id}/export/preview")
    def preview_role_pool_export(pool_id: str):
        try:
            return role_pool_bundle.preview_export(pool_id)
        except KeyError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

    @app.get("/v1/role-pools/{pool_id}/export")
    def export_role_pool(pool_id: str):
        try:
            pool_name, data = role_pool_bundle.build_export(pool_id)
        except KeyError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error
        filename = urllib.parse.quote(f"{pool_name}.aicu-pool.zip", safe="")
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )

    async def read_limited_body(request: Request) -> bytes:
        """流式读请求体，超过 MAX_BUNDLE_BYTES 即 413（不先把整包读进内存）。"""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BUNDLE_BYTES:
            raise _http_error(413, BundleTooLarge([f"包体超过上限 {MAX_BUNDLE_BYTES} 字节"]))
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_BUNDLE_BYTES:
                raise _http_error(413, BundleTooLarge([f"包体超过上限 {MAX_BUNDLE_BYTES} 字节"]))
            chunks.append(chunk)
        return b"".join(chunks)

    @app.post("/v1/role-pools/import")
    async def import_role_pool(
        request: Request,
        on_conflict: Literal["skip", "overwrite"] = "skip",
        dry_run: bool = False,
        decide: list[str] = Query(default=[]),
    ):
        # 逐项决定：重复的 ?decide=<key>=skip|overwrite，覆盖 on_conflict 对这一项的默认。
        decisions: dict[str, str] = {}
        for entry in decide:
            key, sep, value = entry.rpartition("=")
            if not sep or not key:
                raise _http_error(400, ValueError(f"decide 参数格式应为 <key>=skip|overwrite: {entry!r}"))
            decisions[key] = value
        raw = await read_limited_body(request)
        try:
            return await asyncio.to_thread(
                role_pool_bundle.import_bundle, raw, on_conflict, dry_run, decisions
            )
        except BundleTooLarge as error:
            raise _http_error(413, error) from error
        except (ValueError, KeyError) as error:
            raise _http_error(400, error) from error

    @app.get("/v1/skills")
    def list_skills():
        return {"skills": skills_manager.list_skills()}

    @app.get("/v1/skills/{skill_id}")
    def get_skill(skill_id: str):
        # ``referencing_roles`` 刻意留在 try 之外：它由 roles 那一侧算，抛出来的也是
        # 角色的问题。混进下面这两个 except 里，读 skill 的人会收到一句关于 role_id
        # 的 400 —— 明明请求的是一个完全正常的 skill，报错却指向另一个维度。真要炸，
        # 就该按服务端故障炸出来，而不是伪装成「你的 skill_id 写错了」。
        try:
            detail = skills_manager.get_skill(skill_id)
        except KeyError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error
        return {
            **detail,
            "referencing_roles": roles_manager.roles_referencing_skill(skill_id),
        }

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
            raise _http_error(400, error) from error

    @app.patch("/v1/skills/{skill_id}")
    def update_skill(skill_id: str, body: SkillRequest):
        try:
            if body.skill_id != skill_id:
                raise ValueError("Skill id cannot be changed")
            skills_manager.get_skill(skill_id)
            return skills_manager.save_skill(skill_id, body.content)
        except KeyError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

    @app.delete("/v1/skills/{skill_id}", status_code=204)
    def delete_skill(skill_id: str):
        try:
            skills_manager.delete_skill(skill_id)
        except KeyError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

    @app.post("/v1/skills/import", status_code=201)
    async def import_skill(skill_id: str, request: Request):
        """从 zip 归档导入完整的多文件 skill（raw bytes body + skill_id query）。"""
        raw = await read_limited_body(request)
        try:
            return skills_manager.import_skill_zip(skill_id, raw)
        except ValueError as error:
            raise _http_error(400, error) from error

    # ---- MCP 注册表 ----
    # 注册表是「角色 yaml 只写名字、定义集中一处」的那份全局定义（仓库根
    # mcp_servers.json）。这里只管增删改查与「测试连接」；怎么把它下发给各家 CLI 是
    # cli_adapters 的事，怎么解析名字是 mcp_manager 的事。

    def mcp_spec(body: McpServerRequest) -> dict:
        """请求体 → ``validate_server`` 认的那份字典。

        两种传输的字段一起透传，由 ``validate_server`` 按「有没有 url」判该走哪一支 ——
        在这里先判一次就会多出第二份判据（与 mcp_manager.transport_of 各说各话）。
        """
        return {
            "command": body.command,
            "args": body.args,
            "env": body.env,
            "url": body.url,
            "headers": body.headers,
            "description": body.description,
            "required": body.required,
        }

    @app.get("/v1/mcp/servers")
    def list_mcp_registry():
        try:
            servers = list_mcp_servers(registry_path)
        except McpConfigError as error:
            raise _http_error(400, error) from error
        # 角色配置扫一次就好：逐个 server 各扫一遍是 N×M 次读文件，而这里问的是同一份。
        configs = list(roles_manager.iter_role_configs())
        return {
            "servers": [
                {**item, "referencing_roles": roles_referencing(item["name"], configs)}
                for item in servers
            ],
        }

    @app.post("/v1/mcp/servers", status_code=201)
    def create_mcp_registry_entry(body: McpServerRequest):
        try:
            if body.name in server_names(registry_path):
                raise HTTPException(
                    status_code=409,
                    detail=t(
                        f"MCP server '{body.name}' 已存在",
                        f"MCP server '{body.name}' already exists",
                    ),
                )
            return save_mcp_server(body.name, mcp_spec(body), registry_path)
        # McpConfigError 是 ValueError 的子类（注册表坏掉、名字或字段不合法），一并覆盖。
        except ValueError as error:
            raise _http_error(400, error) from error

    @app.patch("/v1/mcp/servers/{name}")
    def update_mcp_registry_entry(name: str, body: McpServerRequest):
        try:
            # 名字是注册表的键、也是角色 yaml 里的引用，改它要同步重写所有引用方，
            # 与角色 id 同一惯例：不可改，要改就是新建 + 删旧。
            if body.name != name:
                raise ValueError("MCP server name cannot be changed")
            if name not in server_names(registry_path):
                raise KeyError(f"MCP server '{name}' not found")
            return save_mcp_server(name, mcp_spec(body), registry_path)
        except KeyError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

    @app.delete("/v1/mcp/servers/{name}", status_code=204)
    def delete_mcp_registry_entry(name: str):
        try:
            delete_mcp_server(name, registry_path)
        except KeyError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

    @app.post("/v1/mcp/probe")
    def probe_mcp_registry_entry(body: McpServerRequest):
        """真连一次这条定义，返回它暴露的工具名列表。

        测的是**请求体里的值**，不是已保存的那份：改了 command 就该能立刻测，而一个还没
        保存过的新 server 根本无从「按名字测」。判据与超时都走运行时预检那一套
        （``probe_server`` + ``DEFAULT_PROBE_TIMEOUT``），否则会出现「页面上测试通过、
        节点起不来」。这里刻意不走 ``probe_servers``：那个对 ``required=False`` 的 server
        会吞掉错误只打告警，而手动点「测试」时要的恰恰是那句错误。
        """
        try:
            server = build_server(body.name, mcp_spec(body))
        except McpConfigError as error:
            raise _http_error(400, error) from error
        try:
            return {"tools": probe_server(server, timeout=DEFAULT_PROBE_TIMEOUT)}
        except McpProbeError as error:
            raise _http_error(400, error) from error

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
    def get_effective_base_url(
        cli_name: str = "",
        model: str = "",
        api_base_url: str = "",
        provider_id: str = "",
    ):
        """给定 (CLI, 模型, 已配置地址) 时**真正会被用到**的端点。

        连接表单用它显示「生效地址」：换模型就重新问一次，显示的永远是与实际请求同源的
        那个值（cli_adapters.effective_base_url 直接复用建子进程前的那两个函数）。
        传的是表单里的**当前**值而非已保存的值 —— 用户改了模型还没保存时也要能看见。
        """
        base_url, source = effective_base_url(
            cli_name or None, model or None, api_base_url or None, provider_id
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
            raise _http_error(400, error) from error

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
        provider_id = body.provider_id or saved_config.get("provider_id")

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
                    provider_id = provider_id or preset.get("id")
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
            vendor = provider_id or vendor_of_model(model)
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
                    provider_id=provider_id,
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
    def get_api_key(
        model: str = Query(min_length=1, max_length=200),
        provider_id: str = Query(default="", max_length=80),
    ):
        """按显式 provider 或模型厂商回显已保存的 sk（明文）。"""
        vendor = provider_id.strip().lower() or vendor_of_model(model)
        if not vendor:
            return {"vendor": None, "api_key": None}
        return {"vendor": vendor, "api_key": session_manager.get_api_key(vendor)}

    @app.put("/v1/config/api-key")
    def put_api_key(body: ApiKeyRequest):
        """按显式 provider 或模型厂商保存 sk；空 api_key 视为删除。"""
        vendor = (body.provider_id or "").strip().lower() or vendor_of_model(body.model)
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
        except ValueError as error:
            raise _http_error(400, error) from error
        except RuntimeError as error:
            raise _http_error(409, error) from error

    @app.get("/v1/sessions/{session_id}")
    def get_session(session_id: str):
        handle = resolve(session_id)
        for item in session_manager.list_sessions():
            if item["id"] == handle.metadata.id:
                return item
        raise HTTPException(status_code=404, detail=t(f"会话 '{session_id}' 不存在", f"Session '{session_id}' not found"))

    @app.patch("/v1/sessions/{session_id}")
    def update_session(session_id: str, body: UpdateSessionRequest):
        resolve(session_id)
        return session_manager.update_session(session_id, body.title)

    @app.get("/v1/sessions/{session_id}/settings")
    def get_session_settings(session_id: str):
        resolve(session_id)
        settings = session_manager.get_settings(session_id)
        credential_env = settings["config"].get("api_key_env")
        return {
            **settings,
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
            raise _http_error(400, error) from error
        except RuntimeError as error:
            raise _http_error(409, error) from error

    @app.delete("/v1/sessions/{session_id}", status_code=204)
    def delete_session(session_id: str):
        try:
            session_manager.delete_session(session_id)
        except KeyError as error:
            raise _http_error(404, error) from error

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
            raise _http_error(404, error) from error

    @app.get("/v1/sessions/{session_id}/workspace")
    def list_session_workspace(
        session_id: str, path: str = Query(default="", max_length=4096)
    ):
        try:
            return resolve(session_id).runtime.list_workspace(path=path)
        except FileNotFoundError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

    @app.get("/v1/sessions/{session_id}/workspace/file")
    def read_session_workspace_file(
        session_id: str, path: str = Query(min_length=1, max_length=4096)
    ):
        try:
            return resolve(session_id).runtime.read_workspace_file(path=path)
        except FileNotFoundError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

    @app.get("/v1/sessions/{session_id}/shared")
    def list_session_shared(
        session_id: str, path: str = Query(default="", max_length=4096)
    ):
        """会话级「查看产出」：shared 根文件树（分区行带 producer/delivery 元数据）。"""
        try:
            return resolve(session_id).runtime.list_session_shared(path=path)
        except FileNotFoundError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

    @app.get("/v1/sessions/{session_id}/shared/file")
    def read_session_shared_file(
        session_id: str, path: str = Query(min_length=1, max_length=4096)
    ):
        """读取共享根下产物文件（文本白名单预览 / 二进制不可预览）。"""
        try:
            return resolve(session_id).runtime.read_session_shared_file(path=path)
        except FileNotFoundError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

    @app.post("/v1/sessions/{session_id}/graph/pause")
    def pause_graph(session_id: str, body: PauseGraphRequest = PauseGraphRequest()):
        try:
            return session_manager.pause_session(session_id, body.mode)
        except KeyError as error:
            raise _http_error(404, error) from error
        except (RuntimeError, ValueError) as error:
            raise _http_error(409, error) from error

    @app.post("/v1/sessions/{session_id}/graph/resume")
    def resume_graph(session_id: str):
        try:
            return session_manager.resume_session(session_id)
        except KeyError as error:
            raise _http_error(404, error) from error
        except RuntimeError as error:
            raise _http_error(409, error) from error

    @app.post("/v1/sessions/{session_id}/graph/stop")
    def stop_graph(session_id: str):
        try:
            return session_manager.stop_session(session_id)
        except KeyError as error:
            raise _http_error(404, error) from error

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
            raise _http_error(400, error) from error
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
            raise _http_error(404, error) from error
        except RuntimeError as error:
            raise _http_error(409, error) from error
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
            raise _http_error(404, error) from error
        except RuntimeError as error:
            raise _http_error(409, error) from error
        touch(session_id)

    @app.post("/v1/sessions/{session_id}/nodes/{node_id}/pause")
    def pause_node(session_id: str, node_id: str):
        try:
            resolve(session_id).runtime.pause_node(node_id)
        except ValueError as error:
            raise _http_error(404, error) from error
        touch(session_id)
        return {"status": "paused", "node_id": node_id}

    @app.post("/v1/sessions/{session_id}/nodes/{node_id}/resume")
    def resume_node(session_id: str, node_id: str):
        try:
            resolve(session_id).runtime.resume_node(node_id)
        except ValueError as error:
            raise _http_error(404, error) from error
        touch(session_id)
        return {"status": "resumed", "node_id": node_id}

    @app.post("/v1/sessions/{session_id}/nodes/{node_id}/stop")
    def stop_node(session_id: str, node_id: str):
        try:
            resolve(session_id).runtime.stop_node(node_id)
        except ValueError as error:
            raise _http_error(404, error) from error
        touch(session_id)
        return {"status": "stopped", "node_id": node_id}

    @app.post("/v1/sessions/{session_id}/nodes/{node_id}/continue")
    def continue_node(session_id: str, node_id: str):
        try:
            node_id = resolve(session_id).runtime.continue_node(node_id)
        except ValueError as error:
            raise _http_error(409, error) from error
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
            raise _http_error(409, error) from error
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
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

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
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

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
            raise _http_error(404, error) from error
        return [_serialize_log(node_id, entry) for entry in logs]

    @app.get("/v1/sessions/{session_id}/nodes/{node_id}/activity")
    def get_node_activity(session_id: str, node_id: str):
        try:
            return {"node_id": node_id, "activities": resolve(session_id).runtime.get_cc_activity(node_id)}
        except ValueError as error:
            raise _http_error(404, error) from error

    @app.get("/v1/sessions/{session_id}/nodes/{node_id}/results")
    def list_node_results(session_id: str, node_id: str, path: str = Query(default="", max_length=4096)):
        try:
            return resolve(session_id).runtime.list_node_results(node_id, path)
        except FileNotFoundError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

    @app.get("/v1/sessions/{session_id}/nodes/{node_id}/results/file")
    def read_node_result_file(session_id: str, node_id: str, path: str = Query(min_length=1, max_length=4096)):
        try:
            return resolve(session_id).runtime.read_node_result_file(node_id, path)
        except FileNotFoundError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

    # ---- 可浏览根：复制路径 / 拖入 / 拖出（V3-2.3） ----
    #
    # 四个动作 × 两个根 = 八条路由。没有收敛成 `/files/*?scope=` 那样一条：既有的八条
    # 只读端点（/shared、/shared/file、/nodes/{nid}/results…）已经是「每上下文一套」的
    # 形状，再加一个通用入口就是同一个概念两种写法。真逻辑全在 Runtime 与 file_transfer
    # 里，这里重复的只有下面四行异常翻译。

    def _browse_info(runtime, scope: str, node_id: Optional[str], path: str) -> dict:
        """resolve / download 共用：路径越界与缺失的翻译只有一份。"""
        try:
            return runtime.resolve_browse_path(scope, path, node_id)
        except FileNotFoundError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

    async def _browse_upload(
        request: Request, runtime, scope: str, node_id: Optional[str],
        path: str, name: str,
    ) -> dict:
        # 先校验落点再收 body：越界、没有产出目录这类失败不该等收完 100MB 才回。
        try:
            target_dir, rel_dir = runtime.prepare_upload_dir(scope, path, node_id)
            validate_upload_name(name)
        except FileNotFoundError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error
        try:
            result = await receive_upload(target_dir, name, request.stream())
        except UploadTooLarge as error:  # 必须是 ValueError 之前 —— 它是 ValueError 子类
            raise _http_error(413, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error
        result["path"] = posixpath.join(rel_dir, result["name"]).lstrip("./")
        return result

    def _browse_download(runtime, scope: str, node_id: Optional[str], path: str):
        info = _browse_info(runtime, scope, node_id, path)
        if info["type"] != "file":
            raise _http_error(400, ValueError("Cannot download a directory"))
        # content_disposition 默认 attachment —— 这是拖出成立的前提：Chromium 的
        # DownloadURL 对 inline 的可渲染类型（text/plain 等）可能只落一个链接。
        # 中文名的 RFC 5987 编码由 FileResponse 自己处理（starlette 1.x 会发
        # `filename*=utf-8''…`），不用手工拼头。
        return FileResponse(
            info["abs_path"],
            media_type=mime_type_for(info["name"]) or "application/octet-stream",
            filename=info["name"],
        )

    def _browse_reveal(runtime, scope: str, node_id: Optional[str], path: str) -> dict:
        try:
            return runtime.reveal_browse_path(scope, path, node_id)
        except RevealUnsupported as error:
            raise _http_error(501, error) from error
        except FileNotFoundError as error:
            raise _http_error(404, error) from error
        except ValueError as error:
            raise _http_error(400, error) from error

    @app.get("/v1/sessions/{session_id}/shared/path")
    def resolve_session_shared_path(
        session_id: str, path: str = Query(default="", max_length=4096)
    ):
        """把共享根下的相对路径解析成绝对路径（供「复制路径」）。

        ``path=""`` 合法，返回共享根本身。这是唯一一处把真实根路径交出去的端点 ——
        列表响应里刻意不带（见 ``list_session_shared``），因为那是每次刷新都会下发的
        payload，而「我正看的这个东西在哪」是用户显式索要的一次性解析。
        """
        return _browse_info(resolve(session_id).runtime, "shared", None, path)

    @app.post("/v1/sessions/{session_id}/shared/upload", status_code=201)
    async def upload_session_shared_file(
        session_id: str,
        request: Request,
        path: str = Query(default="", max_length=4096),
        name: str = Query(min_length=1, max_length=255),
    ):
        """把请求体当作一个文件写进共享根下的 ``path`` 目录，文件名 ``name``。

        裸 body、一次一个文件，与 ``POST /v1/skills/import`` 同一个范式。同名自动改名为
        ``a-1.txt``，**绝不覆盖**。
        """
        return await _browse_upload(request, resolve(session_id).runtime, "shared", None, path, name)

    @app.get("/v1/sessions/{session_id}/shared/download")
    def download_session_shared_file(
        session_id: str, path: str = Query(min_length=1, max_length=4096)
    ):
        return _browse_download(resolve(session_id).runtime, "shared", None, path)

    @app.post("/v1/sessions/{session_id}/shared/reveal")
    def reveal_session_shared_file(
        session_id: str, path: str = Query(default="", max_length=4096)
    ):
        """在访达中选中该路径。

        必须是 POST：这是有副作用的端点，而服务可能被绑到局域网（``HOST=0.0.0.0``，见 ``main.py``），
        GET 会被 ``<img src>`` 或链接预取之类的东西触发。前端要的是「用户点了才弹」。
        """
        return _browse_reveal(resolve(session_id).runtime, "shared", None, path)

    @app.get("/v1/sessions/{session_id}/nodes/{node_id}/results/path")
    def resolve_node_result_path(
        session_id: str, node_id: str, path: str = Query(default="", max_length=4096)
    ):
        return _browse_info(resolve(session_id).runtime, "results", node_id, path)

    @app.post("/v1/sessions/{session_id}/nodes/{node_id}/results/upload", status_code=201)
    async def upload_node_result_file(
        session_id: str,
        node_id: str,
        request: Request,
        path: str = Query(default="", max_length=4096),
        name: str = Query(min_length=1, max_length=255),
    ):
        """写进节点**声明的产出目录**，不是它的工作区。

        节点没有 ``artifact_path``（还没跑过）→ 404；产出是单个文件 → 400。两条都
        **不**退回到别处去落 —— 用户要的是「所见即所得」，落到工作区就是所见非所得。
        """
        return await _browse_upload(request, resolve(session_id).runtime, "results", node_id, path, name)

    @app.get("/v1/sessions/{session_id}/nodes/{node_id}/results/download")
    def download_node_result_file(
        session_id: str, node_id: str, path: str = Query(min_length=1, max_length=4096)
    ):
        return _browse_download(resolve(session_id).runtime, "results", node_id, path)

    @app.post("/v1/sessions/{session_id}/nodes/{node_id}/results/reveal")
    def reveal_node_result_file(
        session_id: str, node_id: str, path: str = Query(default="", max_length=4096)
    ):
        return _browse_reveal(resolve(session_id).runtime, "results", node_id, path)

    @app.get("/v1/sessions/{session_id}/nodes/{node_id}/artifact")
    def get_artifact(session_id: str, node_id: str):
        runtime = resolve(session_id).runtime
        node = runtime.get_node(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail=t(f"节点 '{node_id}' 不存在", f"Node '{node_id}' not found"))
        if not node.artifact_path:
            return {"path": None, "content": None}
        try:
            # 产物是一整个目录时（Manager 的产物就是它的 workspace）没有「文件内容」
            # 可读：此前直接 open() 会抛 IsADirectoryError 变成 500。改为返回一层
            # 目录清单，条目口径与 list_node_results / 工作区视图一致。
            entries = runtime.list_artifact_dir(node_id, node.artifact_path)
            if entries is not None:
                return {
                    "path": node.artifact_path,
                    "content": None,
                    "artifact_kind": "directory",
                    "entries": entries,
                }
            return {
                "path": node.artifact_path,
                "content": runtime.read_artifact(node_id, node.artifact_path),
            }
        except (FileNotFoundError, ValueError) as error:
            raise _http_error(404, error) from error

    @app.post("/v1/sessions/{session_id}/edges", status_code=201)
    def create_edge(session_id: str, body: CreateEdgeRequest):
        runtime = resolve(session_id).runtime
        try:
            edge = runtime.add_dependency(body.from_node, body.to_node)
        except ValueError as error:
            raise _http_error(400, error) from error
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
            raise _http_error(404, error) from error
        except RuntimeError as error:
            raise _http_error(409, error) from error
        touch(session_id)

    @app.get("/v1/sessions/{session_id}/graph/events")
    async def stream_events(session_id: str, request: Request):
        handle = resolve(session_id)
        last_event_id = request.headers.get("last-event-id")
        since_id = int(last_event_id) if last_event_id and last_event_id.isascii() and last_event_id.isdigit() else None
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

        传输细节（PTY 读写、resize、进程组清理）在 ``runtime.terminal_ws``；这里只把
        会话项目目录解析出来。
        """
        handle = resolve(session_id)
        await serve_terminal(ws, handle.runtime.get_shared_workspace())

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
_SESSION_FALLBACK_KEYS = ("cli_name", "api_base_url", "api_key_env", "provider_id")


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


