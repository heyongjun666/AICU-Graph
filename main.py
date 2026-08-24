"""
main.py — cc_0 Runtime 入口文件

启动流程（按顺序）：
  1. Runtime.start() — 初始化 Graph/Planner/Scheduler/Persistence
  2. SSEManager.start() — 订阅 EventBus 开始消费事件
  3. create_app() — 创建 FastAPI 应用
  4. uvicorn.run() — 启动 HTTP 服务

关闭流程（逆序）：
  SIGINT/SIGTERM -> SSEManager.stop() -> Runtime.shutdown()
"""

import logging
import os
import signal
import sys

import uvicorn

from executables.manager import Manager
from executables.registry import ExecutableRegistry
from executables.worker import Worker
from runtime.session_manager import SessionManager
from runtime.session_server import create_session_app

logger = logging.getLogger(__name__)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    logger.info("Starting cc_0 Runtime ...")

    # 1. 创建 ExecutableRegistry 并注册 Manager/Worker
    #    通过环境变量 EXEC_MODE=real 让 Worker/Manager 默认走真实 AICU 路径
    os.environ.setdefault("EXEC_MODE", "real")
    registry = ExecutableRegistry()
    registry.register("manager", Manager)
    registry.register("worker", Worker)

    # 2. 恢复并管理所有独立会话 Runtime
    roles_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roles")
    session_manager = SessionManager(registry=registry, roles_dir=roles_dir)

    # 3. 创建多会话 FastAPI 应用
    app = create_session_app(session_manager)

    # 4. 信号处理：graceful shutdown
    def _shutdown(signum, frame):
        logger.info("Received signal %s, shutting down ...", signum)
        session_manager.shutdown()
        logger.info("Shutdown complete")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # 5. 启动 HTTP 服务
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))

    logger.info("Starting HTTP server on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
