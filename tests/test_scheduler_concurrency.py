"""Hot scheduler limits with real, event-controlled executor threads."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from executables.base import Executable
from executables.registry import ExecutableRegistry
from runtime.graph import Graph
from runtime.models import Node, NodeStatus
from runtime.scheduler import Scheduler


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


@pytest.fixture
def controlled_scheduler():
    graph = Graph()
    registry = ExecutableRegistry()
    started = [threading.Event() for _ in range(9)]
    release = [threading.Event() for _ in started]
    calls = []

    class BlockingExecutable(Executable):
        def __init__(self, context):
            pass

        def run(self, ctx):
            index = int(ctx.node.id)
            calls.append(index)
            started[index].set()
            assert release[index].wait(10), "test failed to release executable"
            return NodeStatus.COMPLETED

    registry.register("controlled", BlockingExecutable)
    delegate = Mock()
    scheduler = Scheduler(graph, registry, delegate, None, max_concurrency=1)
    for index in range(len(started)):
        graph.add_node(Node(
            id=str(index), exec_type="controlled", role="test", task="controlled task",
            config={}, status=NodeStatus.READY,
        ))
    yield scheduler, started, release, calls, delegate
    for event in release:
        event.set()
    scheduler.stop()


def test_grow_pool_keeps_old_futures_and_completes_once(controlled_scheduler):
    scheduler, started, release, calls, delegate = controlled_scheduler
    scheduler.start()
    assert started[0].wait(3)
    old_pool = scheduler._thread_pool
    # The executable may start before submit() returns and registers its Future.
    with scheduler._state_lock, scheduler._futures_lock:
        original_future = scheduler._futures["0"]
    scheduler.set_max_concurrency(8)
    assert scheduler._thread_pool is not old_pool
    assert all(event.wait(3) for event in started[:8])
    assert not started[8].is_set()
    assert not original_future.cancelled()
    with scheduler._futures_lock:
        assert scheduler._futures["0"] is original_future
    for event in release:
        event.set()
    wait_for(lambda: all(n.status == NodeStatus.COMPLETED for n in scheduler.graph.all_nodes()))
    wait_for(lambda: scheduler.active_execution_count == 0)
    assert sorted(calls) == list(range(9))
    completed = [call.args[0] for call in delegate.on_node_status_changed.call_args_list
                 if call.args[1] == NodeStatus.COMPLETED]
    assert sorted(completed) == [str(i) for i in range(9)]
    assert original_future.result(timeout=1) == NodeStatus.COMPLETED
    wait_for(lambda: not any(thread.is_alive() for thread in old_pool._threads))


def test_shrink_waits_for_running_tasks_without_cancelling(controlled_scheduler):
    scheduler, started, release, calls, _ = controlled_scheduler
    scheduler.set_max_concurrency(3)
    scheduler.start()
    assert all(event.wait(3) for event in started[:3])
    pool = scheduler._thread_pool
    with scheduler._futures_lock:
        futures = dict(scheduler._futures)
    scheduler.set_max_concurrency(1)
    assert scheduler._thread_pool is pool
    assert not any(future.cancelled() for future in futures.values())
    release[0].set()
    release[1].set()
    wait_for(lambda: scheduler.graph.get_node("0").status == NodeStatus.COMPLETED
             and scheduler.graph.get_node("1").status == NodeStatus.COMPLETED)
    assert scheduler.graph.get_node("2").status == NodeStatus.RUNNING
    assert not started[3].wait(0.6)
    release[2].set()
    assert started[3].wait(3)
    assert not started[4].wait(0.6)
    assert calls == [0, 1, 2, 3]


def test_same_limit_and_shrink_do_not_rebuild_pool(controlled_scheduler):
    scheduler, *_ = controlled_scheduler
    pool = scheduler._thread_pool
    scheduler.set_max_concurrency(1)
    assert scheduler._thread_pool is pool
    scheduler.set_max_concurrency(10)
    enlarged = scheduler._thread_pool
    scheduler.set_max_concurrency(2)
    scheduler.set_max_concurrency(10)
    assert scheduler._thread_pool is enlarged
    assert scheduler._pool_capacity == 12


@pytest.mark.parametrize("value", [0, -1, 17, 2.5, 4.0, "4", True, None])
def test_invalid_limit_is_atomic(controlled_scheduler, value):
    scheduler, *_ = controlled_scheduler
    pool = scheduler._thread_pool
    with pytest.raises(ValueError, match="max_concurrency"):
        scheduler.set_max_concurrency(value)
    assert scheduler.max_concurrency == 1
    assert scheduler._thread_pool is pool


def test_pool_creation_failure_leaves_limit_unchanged(controlled_scheduler, monkeypatch):
    scheduler, *_ = controlled_scheduler
    pool = scheduler._thread_pool
    def fail_pool(**kwargs):
        raise RuntimeError("cannot allocate pool")
    monkeypatch.setattr("runtime.scheduler.ThreadPoolExecutor", fail_pool)
    with pytest.raises(RuntimeError, match="cannot allocate"):
        scheduler.set_max_concurrency(16)
    assert scheduler.max_concurrency == 1
    assert scheduler._thread_pool is pool


def test_pause_stop_start_and_watchdog_use_new_limit(controlled_scheduler):
    scheduler, started, *_ = controlled_scheduler
    scheduler.pause()
    scheduler.start()
    scheduler.set_max_concurrency(7)
    assert scheduler.is_paused
    assert not started[0].wait(0.6)
    scheduler.stop()
    closed_pool = scheduler._thread_pool
    scheduler.set_max_concurrency(12)
    assert not scheduler.is_running
    assert scheduler._thread_pool is closed_pool
    scheduler._handle_watchdog_error(RuntimeError("after stop"))
    assert scheduler._pool_shutdown
    scheduler.start()
    assert scheduler._pool_capacity == 14
    assert scheduler.is_paused
    # Simulate unexpected pool shutdown while the scheduler remains active.
    with scheduler._state_lock:
        scheduler._thread_pool.shutdown(wait=False)
        scheduler._pool_shutdown = True
    scheduler._last_watchdog_time = 0
    scheduler._handle_watchdog_error(RuntimeError("unexpected pool shutdown"))
    assert not scheduler._pool_shutdown
    assert scheduler._pool_capacity == 14


def test_updates_and_stop_do_not_submit_to_closed_pool(controlled_scheduler):
    scheduler, started, release, *_ = controlled_scheduler
    scheduler.start()
    assert started[0].wait(3)
    gate = threading.Barrier(3)
    def change_limits():
        gate.wait(timeout=3)
        for value in (16, 1, 8, 2, 15, 4):
            scheduler.set_max_concurrency(value)
    def stop():
        gate.wait(timeout=3)
        scheduler.stop()
    with ThreadPoolExecutor(max_workers=2) as pool:
        changing = pool.submit(change_limits)
        stopping = pool.submit(stop)
        gate.wait(timeout=3)
        changing.result(timeout=5)
        stopping.result(timeout=5)
    assert scheduler.max_concurrency == 4
    assert scheduler._pool_shutdown
    assert not scheduler.is_running
    assert scheduler._watchdog_retries == 0
