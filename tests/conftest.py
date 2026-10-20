"""Pytest fixtures for cc_0 integration tests."""

import time

import pytest
from fastapi.testclient import TestClient

from runtime.event_bus import EventBus
from runtime.models import NodeStatus
from runtime.runtime import Runtime
from runtime.session_manager import SessionManager
from runtime.session_server import create_session_app
from runtime.sse_manager import SSEManager
from executables.base import Executable, ExecutableContext


# ============================================================================
# Helper executables for testing
# ============================================================================


class _TestExecutable(Executable):
    """Minimal executable that auto-completes immediately."""

    def __init__(self, context: ExecutableContext, **kwargs):
        super().__init__()

    def run(self, ctx: ExecutableContext) -> NodeStatus:
        return NodeStatus.COMPLETED


def wait_until(condition, timeout=5.0, interval=0.1):
    """Wait until condition callable returns truthy."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def _assume_a_cli_is_installed(monkeypatch):
    """假定跑测试的机器上装了 AI Coding CLI。

    ``ClaudeCodeAgent`` 在**构造期**就会探测本机 CLI（``claude_code_agent.py`` 的
    ``get_adapter(detect_cli())``），探测不到直接抛 ``RuntimeError``。可绝大多数用例
    关心的是 prompt 拼装 / 工作区布局 / 评审解析这类逻辑，跟「本机装了哪个 CLI」
    无关 —— 它们已经把 ``cc.chat`` 打了桩。

    不钉住的话，测试结果就取决于跑测试的机器：开发机装齐了 7 个 CLI 所以全绿，
    CI runner 一个都没有所以 55 个用例红。「本地绿」与「CI 绿」不是同一个集合。

    为什么 patch 在 ``detect_cli`` 这一层，而不是 ``cli_adapters.detect_default``：
    探测逻辑本身仍要被真实覆盖 —— ``tests/test_cli_adapters.py::TestDetect`` 的三个
    用例自己 patch ``shutil.which``，从下游验证优先级排序与「都没装则抛错」。打在
    ``detect_cli`` 上只影响构造路径，不碰那条线。

    固定成 ``"claude"`` 在任何机器上都成立：``get_adapter`` 只查注册表，不检查是否
    真的装了（「装没装」由 ``is_installed`` 单独回答）。
    """
    import claude_code_agent

    monkeypatch.setattr(claude_code_agent, "detect_cli", lambda: "claude")


@pytest.fixture(autouse=True)
def _kimi_home_is_never_the_users(tmp_path, monkeypatch):
    """把 kimi 的 home 钉到临时目录 —— 被测代码里**唯一**会往用户 home 写东西的就是它。

    ``KimiAdapter.mcp_workspace_files`` 会往 kimi home 里补一条 workspace 信任记录
    （kimi 的 project 层 MCP 配置没它就会被静默丢掉，见 cli_adapters._write_kimi_trust）。
    那是产线的必要行为，但**测试里绝不能落到开发机真实那份** —— 跑一次测试就在
    用户的 ``~/.kimi-code/workspace-trust/`` 里拉一堆记录。

    钉在这里而不是各个测试类里：这条路径会被 ``TestMcpSupportFlag._injects_anything``
    这类「挨个问 adapter 注入口子」的用例间接走到，那些用例的主题与 home 无关，靠它们
    自觉加隔离是守不住的。钉住被测代码真正读的那个变量，一处生效、全套受益。

    （``KIMI_CODE_HOME`` 优先于 ``Path.home()``，与 kimi 自己的 ``resolveKimiHome`` 同序，
    所以钉它即可；个别测试类另有 ``Path.home`` 的 patch，两者不冲突。）
    """
    home = tmp_path / "kimi-code-home"
    monkeypatch.setenv("KIMI_CODE_HOME", str(home))
    return home


@pytest.fixture
def event_bus():
    """EventBus fixture."""
    return EventBus()


@pytest.fixture
def runtime(tmp_path):
    """Runtime fixture with registered test executables.

    显式给 workspace_root：不传时 Runtime 退回 ``os.getcwd()``，于是
    ``rt.start()`` 会在当前目录落下 ``events.json`` / ``nodes/`` / ``shared/``。
    从仓库根跑 pytest 就是把它们丢进仓库（虽已被 .gitignore 挡住，但每次跑都重建）。
    这里与各测试自建 Runtime 的写法保持一致，统一用 tmp_path。
    """
    rt = Runtime(workspace_root=str(tmp_path))
    rt.persistence.clear()
    rt.registry.register("manager", _TestExecutable)
    rt.registry.register("worker", _TestExecutable)
    rt.start()
    yield rt
    try:
        rt.shutdown()
        rt.persistence.clear()
    except Exception:
        pass


@pytest.fixture
def sse_manager(event_bus):
    """SSEManager fixture (not started by default)."""
    return SSEManager(event_bus=event_bus)


@pytest.fixture
def session_manager(runtime, tmp_path):
    manager = SessionManager(
        registry=runtime.registry,
        root_dir=str(tmp_path / "sessions"),
        workspace_root=str(tmp_path),
        restore=False,
    )
    yield manager
    manager.shutdown()


@pytest.fixture
def session_client(session_manager):
    app = create_session_app(session_manager)
    with TestClient(app) as test_client:
        yield test_client
