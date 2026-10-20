"""runtime/terminal_ws.py — 会话底部终端（PTY <-> WebSocket 桥）。

从 ``session_server`` 拆出：这是纯传输层的进程/文件描述符管理，与会话图、节点、
鉴权都无关。路由只负责把「会话项目目录」解析出来交给 :func:`serve_terminal`。

上行消息 = 键盘原始文本；``{"type":"resize",cols,rows}`` 同步窗口尺寸。
下行消息 = PTY 输出。WebSocket 断开时杀掉整个进程组（bash 派生的子命令一起走）。
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import termios

from fastapi import WebSocket, WebSocketDisconnect


async def serve_terminal(ws: WebSocket, cwd: str) -> None:
    """在 ``cwd`` 启动交互式 bash（PTY），把双向数据接到 ``ws`` 上，断开即清理。"""
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
        if not reader.done():
            reader.cancel()
