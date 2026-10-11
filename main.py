"""
main.py — cc_0 Runtime 入口文件

启动流程（按顺序）：
  1. 创建并配置 ExecutableRegistry，注册 Manager/Worker
  2. 创建 SessionManager，由它构造和管理各会话的 Runtime 与 SSEManager
  3. create_session_app() — 创建多会话 FastAPI 应用
  4. uvicorn.run() — 启动 HTTP 服务

关闭流程：
  SIGINT/SIGTERM -> SessionManager.shutdown()，由会话管理器停止 SSEManager 并关闭 Runtime

信号重入（勿回退此幂等保护）：
  Python 的信号处理函数是在主线程**当前帧内嵌套执行**的。优雅关闭路径会长时间持有
  不可重入的 threading.Lock（runtime.py Runtime._lock、persistence.py Persistence._save_lock，
  后者覆盖 json.dump + fsync + rename）。于是：

  - 若信号落在持锁窗口内，嵌套的 handler 会再次进入 shutdown 并去拿同一把锁 ——
    同一个线程自己等自己，永久死锁：外层帧再也回不去，末尾的 os._exit() 永远执行不到。
  - 若信号落在 logger.info 的 stdout 写入中，嵌套 handler 再写日志会抛
    RuntimeError: reentrant call inside _io.BufferedWriter。
  - 每按一次 Ctrl-C 就多嵌套一层（实测 400 次信号可嵌套 356 层），最终
    RecursionError: maximum recursion depth exceeded 会在任意位置爆栈。

  三者都会让进程**再也退不掉**，只能 kill -9。日志特征是：每个 ^C 都打印
  "Received signal 2, shutting down ..."，却一次都没有 "Shutdown complete"。

  因此 handler 必须幂等：只有第一次调用执行优雅关闭；重入一律立刻返回、绝不碰锁，
  好让卡住的那一层继续跑完并抵达 os._exit()。只有优雅关闭本身超过
  _FORCE_EXIT_AFTER_SECONDS 仍未见底时，重入才强制退出（用户明确表达了不耐烦）。
"""

import logging
import os
import signal
import sys
import time
import uvicorn

from executables.manager import Manager
from executables.registry import ExecutableRegistry
from executables.worker import Worker
from runtime.repo_paths import REPO_ROOT
from runtime.session_manager import SessionManager
from runtime.session_server import create_session_app

logger = logging.getLogger(__name__)

# 优雅关闭开始超过该秒数仍未结束，再次收到信号即强制退出（"确实卡住了"的逃生口）。
_FORCE_EXIT_AFTER_SECONDS = 3.0

# 首次信号时刻（time.monotonic）；None 表示尚未开始关闭。用于识别重入。
_shutdown_started_at: "float | None" = None


def _shutdown_action(now: float) -> str:
    """判定本次信号该做什么，并记录关闭起点。

    返回：
      "proceed" — 首次收到信号，执行优雅关闭
      "ignore"  — 重入且仍在宽限期内：立刻返回，绝不碰锁（见模块 docstring）
      "force"   — 重入且优雅关闭已明显卡住：强制退出

    独立成函数是为了能离线测试（不真的发信号）。
    """
    global _shutdown_started_at

    if _shutdown_started_at is not None:
        if now - _shutdown_started_at > _FORCE_EXIT_AFTER_SECONDS:
            return "force"
        return "ignore"

    _shutdown_started_at = now
    return "proceed"


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
    roles_dir = str(REPO_ROOT / "roles")
    session_manager = SessionManager(registry=registry, roles_dir=roles_dir)

    # 3. 创建多会话 FastAPI 应用
    app = create_session_app(session_manager)

    # 4. 信号处理：graceful shutdown（必须幂等，见模块 docstring）
    def _shutdown(signum, frame):
        action = _shutdown_action(time.monotonic())

        if action != "proceed":
            # 重入。这里绝不调用 session_manager.shutdown()：那会在 Runtime._lock /
            # Persistence._save_lock 上自死锁，进程就永远退不掉了。
            if action == "force":
                # 用 os.write 而不是 logger：此刻可能正嵌套在 logging 的 stdout 写入里，
                # 再走 logging 会抛 reentrant call 错误，反而吞掉这条救命信息。
                os.write(
                    2,
                    (
                        "[main] graceful shutdown still running after %.1fs; "
                        "forcing exit\n" % _FORCE_EXIT_AFTER_SECONDS
                    ).encode("utf-8", "replace"),
                )
                os._exit(130)  # 128 + SIGINT，与 shell 的 Ctrl-C 约定一致
            # 宽限期内：静默忽略。多按的 Ctrl-C 不该打断正在进行的落盘。
            return

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
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))

    logger.info("Starting HTTP server on %s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
