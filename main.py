"""
main.py — cc_0 Runtime 入口文件

启动流程（按顺序）：
  1. 创建并配置 ExecutableRegistry，注册 Manager/Worker
  2. 创建 SessionManager，由它构造和管理各会话的 Runtime 与 SSEManager
  3. create_session_app() — 创建多会话 FastAPI 应用
  4. uvicorn.run() — 启动 HTTP 服务

关闭流程：
  SIGINT/SIGTERM -> SessionManager.shutdown()，由会话管理器停止 SSEManager 并关闭 Runtime
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

    # 1. 创建 ExecutableRegistry 并注册 Manager/Worker。
    #    服务入口始终使用真实 AICU；测试替身只能由测试代码显式注入。
    logger.info("External CLI execution required")
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
        try:
            session_manager.shutdown()
        except Exception:  # noqa: BLE001  退出兜底，不掩盖信号处理
            logger.exception("Graceful shutdown raised; forcing exit")
        logger.info("Shutdown complete")
        # 关键：不用 sys.exit(0)。进程可能仍有非 daemon 线程存活 —— 如正在执行的
        # ThreadPoolExecutor worker（scheduler 线程池 / worker.py 里跑 aicu.ask 的
        # 单任务 executor）在等待 AICU 子进程结束；interpreter 退出时 _python_exit
        # 会 join 它们，若任务挂起则 join 永不完成 → Ctrl+C 后进程停不下来。
        # 状态已由 session_manager.shutdown() 完成持久化，这里直接强制退出。
        os._exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # 5. 启动 HTTP 服务
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))

    logger.info("Starting HTTP server on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
