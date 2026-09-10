"""main.py 信号处理回归测试（纯离线，不真的发信号、不真的退出进程）。

锁定的是一个反复回归过的缺陷：``_shutdown`` 曾被写成可重入的。

Python 的信号处理函数在主线程**当前帧内嵌套执行**。优雅关闭路径会长时间持有不可重入的
``threading.Lock``（``Runtime._lock``、``Persistence._save_lock``），所以第二个 Ctrl-C
会在持锁窗口内嵌套进来，再拿同一把锁 —— 同一个线程自己等自己，永久死锁；末尾的
``os._exit()`` 永远执行不到，进程只能 kill -9。实测 400 次信号可嵌套 356 层，并伴随

  - ``RuntimeError: reentrant call inside <_io.BufferedWriter name='<stdout>'>``
    （嵌套发生在 ``logger.info`` 的 stdout 写入中），以及
  - 96 次 ``RecursionError: maximum recursion depth exceeded``。

真实故障日志的特征是：每个 ``^C`` 都打印 "Received signal 2, shutting down ..."，
却**一次都没有** "Shutdown complete"。因此本文件的断言都是"重入次数"级别的，
而不是"最终退出了"级别的 —— 后者在不发信号的前提下无法证明。
"""

import pytest

import main


# --------------------------------------------------------------------------
# 测试替身：把 main() 的依赖全部换掉，只保留被验证的信号处理逻辑
# --------------------------------------------------------------------------
class _FakeOS:
    """代理真实 os，但拦截 _exit / write。

    ``_exit`` 抛 SystemExit 以便观察"本该退出"的位置；不会真的结束 pytest。
    """

    def __init__(self, real_os):
        self._real = real_os
        self.exits = []
        self.writes = []

    def _exit(self, code):
        self.exits.append(code)
        raise SystemExit(code)

    def write(self, fd, data):
        self.writes.append((fd, data))

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now


class _FakeSignal:
    """记录 handler 而不真的安装，避免污染 pytest 进程。"""

    SIGINT = 2
    SIGTERM = 15

    def __init__(self):
        self.handlers = {}

    def signal(self, sig, handler):
        self.handlers[sig] = handler


class _RecordingSessionManager:
    """shutdown() 在内部再次触发信号，复现"关闭到一半又按 Ctrl-C"。"""

    def __init__(self, nested_signals=0, handler_ref=None):
        self.calls = 0
        self.exits_during_shutdown = []
        self._nested = nested_signals
        self._handler_ref = handler_ref

    def shutdown(self):
        self.calls += 1
        handler = self._handler_ref["handler"]
        for _ in range(self._nested):
            handler(self._handler_ref["signum"], None)
            # 每次嵌套调用之后立刻快照，确保断言针对"重入那一刻"而非最终状态
            self.exits_during_shutdown.append(list(self._handler_ref["os"].exits))


class _FakeRegistry:
    def register(self, *args, **kwargs):
        pass


@pytest.fixture
def harness(monkeypatch):
    """跑一次 main()，返回 (handlers, fake_os, clock, session_manager)。"""
    fake_os = _FakeOS(__import__("os"))
    clock = _FakeClock()
    fake_signal = _FakeSignal()
    ref = {"os": fake_os}

    def _run(nested_signals=3, signum=2):
        sm = _RecordingSessionManager(nested_signals=nested_signals, handler_ref=ref)
        ref["handler"] = None
        ref["signum"] = signum

        monkeypatch.setattr(main, "os", fake_os)
        monkeypatch.setattr(main, "time", clock)
        monkeypatch.setattr(main, "signal", fake_signal)
        monkeypatch.setattr(main, "ExecutableRegistry", _FakeRegistry)
        monkeypatch.setattr(main, "SessionManager", lambda **kwargs: sm)
        monkeypatch.setattr(main, "create_session_app", lambda manager: object())
        monkeypatch.setattr(main, "uvicorn", type("U", (), {"run": staticmethod(lambda *a, **k: None)}))
        monkeypatch.setattr(main, "_shutdown_started_at", None)

        main.main()
        ref["handler"] = fake_signal.handlers[fake_signal.SIGINT]
        return fake_signal, fake_os, clock, sm

    return _run


# --------------------------------------------------------------------------
# _shutdown_action：单次判定的语义
# --------------------------------------------------------------------------
class TestShutdownAction:
    def test_first_signal_proceeds(self, monkeypatch):
        monkeypatch.setattr(main, "_shutdown_started_at", None)
        assert main._shutdown_action(1000.0) == "proceed"
        assert main._shutdown_started_at == 1000.0

    def test_duplicate_within_grace_is_ignored(self, monkeypatch):
        monkeypatch.setattr(main, "_shutdown_started_at", 1000.0)
        assert main._shutdown_action(1000.5) == "ignore"
        # 重入不得重置计时起点，否则 force 永远不会触发
        assert main._shutdown_started_at == 1000.0

    def test_duplicate_after_grace_forces(self, monkeypatch):
        monkeypatch.setattr(main, "_shutdown_started_at", 1000.0)
        assert main._shutdown_action(1000.0 + main._FORCE_EXIT_AFTER_SECONDS + 0.01) == "force"
        assert main._shutdown_started_at == 1000.0

    def test_grace_boundary_is_not_force(self, monkeypatch):
        monkeypatch.setattr(main, "_shutdown_started_at", 1000.0)
        assert main._shutdown_action(1000.0 + main._FORCE_EXIT_AFTER_SECONDS) == "ignore"


# --------------------------------------------------------------------------
# 核心回归：重入的 handler 绝不能再进 session_manager.shutdown()
# --------------------------------------------------------------------------
class TestShutdownIsNotReentrant:
    def test_reentrant_signal_does_not_restart_shutdown(self, harness):
        fake_signal, fake_os, clock, sm = harness(nested_signals=5)
        with pytest.raises(SystemExit):
            fake_signal.handlers[fake_signal.SIGINT](fake_signal.SIGINT, None)

        assert sm.calls == 1, (
            "重入的信号又跑了一遍 session_manager.shutdown() —— 这正是自死锁的成因"
        )

    def test_exit_code_is_zero_on_clean_shutdown(self, harness):
        fake_signal, fake_os, clock, sm = harness(nested_signals=5)
        with pytest.raises(SystemExit):
            fake_signal.handlers[fake_signal.SIGINT](fake_signal.SIGINT, None)
        assert fake_os.exits == [0]

    def test_no_exit_happens_while_still_shutting_down(self, harness):
        """重入返回时不能顺手退出，否则会截断正在进行中的持久化。"""
        fake_signal, fake_os, clock, sm = harness(nested_signals=4)
        with pytest.raises(SystemExit):
            fake_signal.handlers[fake_signal.SIGINT](fake_signal.SIGINT, None)
        # 每个嵌套调用发生的瞬间，都还没有任何 os._exit
        assert sm.exits_during_shutdown == [[]] * 4

    def test_sigterm_uses_the_same_guard(self, harness):
        fake_signal, fake_os, clock, sm = harness(nested_signals=2, signum=15)
        assert 15 in fake_signal.handlers
        with pytest.raises(SystemExit):
            fake_signal.handlers[15](15, None)
        assert sm.calls == 1

    def test_hundreds_of_signals_still_shutdown_once(self, harness):
        """对应实测：400 次信号曾导致 356 次重入 + RecursionError 爆栈。"""
        fake_signal, fake_os, clock, sm = harness(nested_signals=400)
        with pytest.raises(SystemExit):
            fake_signal.handlers[fake_signal.SIGINT](fake_signal.SIGINT, None)
        assert sm.calls == 1
        assert fake_os.exits == [0]


# --------------------------------------------------------------------------
# 逃生口：优雅关闭真的卡住时，重入才强制退出
# --------------------------------------------------------------------------
class TestForceExitEscapeHatch:
    def test_late_signal_forces_exit_130(self, harness):
        fake_signal, fake_os, clock, sm = harness(nested_signals=0)
        handler = fake_signal.handlers[fake_signal.SIGINT]

        # 首次信号：进入优雅关闭，但"卡住"了（不返回）
        clock.now = 2000.0
        main._shutdown_action(clock.now)  # 记录起点，模拟已开始
        fake_os.exits.clear()

        clock.now = 2000.0 + main._FORCE_EXIT_AFTER_SECONDS + 0.5
        with pytest.raises(SystemExit):
            handler(fake_signal.SIGINT, None)

        assert fake_os.exits == [130]
        assert sm.calls == 0, "强退路径不得再去跑一遍优雅关闭"

    def test_force_exit_reports_via_raw_write_not_logging(self, harness):
        """此刻可能正嵌套在 logging 的 stdout 写入里，再走 logging 会抛 reentrant 错误。"""
        fake_signal, fake_os, clock, sm = harness(nested_signals=0)
        clock.now = 3000.0
        main._shutdown_action(clock.now)
        clock.now = 3000.0 + main._FORCE_EXIT_AFTER_SECONDS + 0.5

        with pytest.raises(SystemExit):
            fake_signal.handlers[fake_signal.SIGINT](fake_signal.SIGINT, None)

        assert fake_os.writes, "强退时应通过 os.write 直接写 stderr"
        fd, payload = fake_os.writes[0]
        assert fd == 2
        assert b"forcing exit" in payload

    def test_duplicate_within_grace_is_silent(self, harness):
        """宽限期内的重入不得产生任何输出或退出。"""
        fake_signal, fake_os, clock, sm = harness(nested_signals=3)
        with pytest.raises(SystemExit):
            fake_signal.handlers[fake_signal.SIGINT](fake_signal.SIGINT, None)
        assert fake_os.writes == []
        assert fake_os.exits == [0]


# --------------------------------------------------------------------------
# 模块契约
# --------------------------------------------------------------------------
class TestModuleContract:
    def test_both_signals_registered(self, harness):
        fake_signal, _, _, _ = harness()
        assert set(fake_signal.handlers) == {fake_signal.SIGINT, fake_signal.SIGTERM}

    def test_grace_window_is_positive_and_bounded(self):
        assert 0 < main._FORCE_EXIT_AFTER_SECONDS <= 10

    def test_does_not_use_sys_exit_for_the_final_teardown(self):
        """必须用 os._exit：非 daemon 线程可能在退出时 join 卡死（见 main.py 注释）。"""
        import inspect

        source = inspect.getsource(main.main)
        # 去掉注释行再断言，否则注释里那句"不用 sys.exit(0)"会误伤
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        assert "os._exit(0)" in code
        assert "sys.exit(" not in code
