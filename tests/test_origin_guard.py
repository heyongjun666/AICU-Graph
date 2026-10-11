"""跨站请求防护：浏览器页面不能借本机无认证的服务写配置或开终端。"""

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from runtime.origin_guard import OriginGuardMiddleware, origin_allowed


@pytest.mark.parametrize(
    "origin,host,allowed",
    [
        (None, "localhost:8080", True),
        ("http://localhost:8080", "localhost:8080", True),
        ("http://192.168.1.5:8080", "192.168.1.5:8080", True),
        ("http://localhost:8001", "127.0.0.1:8080", True),
        ("http://evil.example", "localhost:8080", False),
        ("http://evil.example:8080", "evil.example:8080", True),
        ("null", "localhost:8080", False),
        ("http://evil.example", "127.0.0.1:8080", False),
        ("http://localhost:8001", "192.168.1.5:8080", False),
    ],
)
def test_origin_allowed(origin, host, allowed):
    assert origin_allowed(origin, host) is allowed


def test_extra_origins_from_env(monkeypatch):
    monkeypatch.setenv("AICU_ALLOWED_ORIGINS", "https://ui.example.com, https://b.example.com/")
    assert origin_allowed("https://ui.example.com", "10.0.0.2:8080")
    assert origin_allowed("https://b.example.com", "10.0.0.2:8080")
    assert not origin_allowed("https://c.example.com", "10.0.0.2:8080")


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(OriginGuardMiddleware)

    @app.get("/read")
    def read():
        return {"ok": True}

    @app.post("/write")
    def write():
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_text("hi")
        await socket.close()

    return TestClient(app)


def test_cross_site_post_is_rejected(client):
    assert client.post("/write", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/write", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/write").status_code == 200


def test_cross_site_read_is_not_blocked_here(client):
    assert client.get("/read", headers={"Origin": "http://evil.example"}).status_code == 200


def test_cross_site_websocket_is_rejected(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"Origin": "http://evil.example"}):
            pass
    with client.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as socket:
        assert socket.receive_text() == "hi"
