"""拒绝跨站请求：浏览器页面不能借用户本机的服务去执行命令。

服务没有认证，且提供终端 WebSocket 与会改写本地配置的接口。浏览器不对 WebSocket
做同源限制，任何网页都能连 ``ws://localhost:8080``；跨站 POST 也能发出（只是读不到响应）。
浏览器总会带上 ``Origin``，所以在这里校验它：没有 Origin 的（curl、脚本）放行。
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _extra_origins() -> set[str]:
    raw = os.environ.get("AICU_ALLOWED_ORIGINS", "")
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


def origin_allowed(origin: str | None, host: str | None) -> bool:
    if not origin:
        return True
    origin = origin.rstrip("/")
    parts = urlsplit(origin)
    if not parts.scheme or not parts.hostname:
        return False
    if host and parts.netloc.lower() == host.lower():
        return True
    # 前端开发服务器：vite 代理会改写 Host、不改 Origin；两端都是回环地址即视为本机。
    host_name = urlsplit(f"//{host}").hostname if host else None
    if parts.hostname.lower() in _LOOPBACK and (host_name or "").lower() in _LOOPBACK:
        return True
    return origin in _extra_origins()


class OriginGuardMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if kind == "http" and scope["method"] in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if origin_allowed(headers.get("origin"), headers.get("host")):
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"detail": "Cross-origin request rejected"}).encode()
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
