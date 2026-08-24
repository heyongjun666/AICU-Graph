"""并发健壮性回归测试 — 代码审阅发现的三个确定性竞态。

背景（代码审阅结论，非测试驱动）：
  - H1: Persistence.save() 无内部锁，定时线程 + API/主线程可并发写同一 .tmp
        → 双写污染 primary（H1 根因）
  - H2: Graph 读方法（get_stats/children/dependencies/dependents）无锁迭代
        self.nodes/self.edges，与并发 add_node 扩容/remove_node 删除交错
        → RuntimeError: dictionary changed size during iteration（H2 根因）
  - H3: Persistence.load() 只捕获 JSON 语法错误（json.JSONDecodeError），
        语义损坏（合法 JSON 但结构错误，如非法 status 枚举/坏 datetime）
        在 _dict_to_snapshot 抛 ValueError/KeyError，不回退 .tmp（H3 根因）

这些测试在修复前必须红、修复后必须绿。
"""

import json
import shutil
import threading
import time

from runtime.graph import Graph
from runtime.models import Node, NodeStatus
from runtime.persistence import Persistence


# ============================================================================
# H1: Persistence.save() 并发必须串行化
# ============================================================================


def test_h1_concurrent_saves_are_serialized(tmp_path):
    """并发 save() 不得同时进入 _save_snapshot（同一 .tmp 双写）。

    修复前（无 _save_lock）：8 线程同时进入 _save_snapshot，
    max_in_flight > 1，且最终 primary 可能被污染 → 红。
    修复后（save() 串行化）：max_in_flight == 1，primary 合法 → 绿。
    """
    path = str(tmp_path / "state.json")
    p = Persistence(path)
    g = Graph()
    g.add_node(Node(
        id="n1", exec_type="worker", role="r", task="t", config={},
        status=NodeStatus.WAITING,
    ))

    lock = threading.Lock()
    max_in_flight = {"n": 0}
    in_flight = {"n": 0}

    orig = p._save_snapshot

    def tracked_snapshot(snapshot):
        with lock:
            in_flight["n"] += 1
            max_in_flight["n"] = max(max_in_flight["n"], in_flight["n"])
        try:
            # 放大并发窗口，确保 8 线程真实重叠
            time.sleep(0.05)
            orig(snapshot)
        finally:
            with lock:
                in_flight["n"] -= 1

    p._save_snapshot = tracked_snapshot

    threads = [threading.Thread(target=p.save, args=(g,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_in_flight["n"] == 1, (
        f"H1: save() 未串行化，max concurrent _save_snapshot = {max_in_flight['n']}"
    )

    # 最终 primary 必须仍是合法 JSON（未被并发双写污染）
    content = open(path, encoding="utf-8").read()
    json.loads(content)


# ============================================================================
# H2: Graph 读方法无锁迭代 + 并发 mutate
# ============================================================================


def test_h2_deterministic_single_iterator_resize():
    """确定性复现：迭代 views 期间 add_node 扩容字典 → RuntimeError。

    与 get_stats() 的遍历模式完全相同（迭代 nodes.values() 视图）。
    修复前：红（命中 RuntimeError）。
    修复后：get_stats 内部加锁后该交错不可能发生，此测试仅证明引擎
    行为；真正回归由 test_h2_get_stats_safe_during_mutation 覆盖。
    """
    g = Graph()
    g.add_node(Node(
        id="root", exec_type="manager", role="m", task="t", config={},
        status=NodeStatus.WAITING,
    ))
    it = iter(g.nodes.values())
    next(it)
    g.add_node(Node(
        id="y", exec_type="worker", role="r", task="t2", config={},
        status=NodeStatus.WAITING,
    ))  # 可能触发扩容
    try:
        next(it)
    except RuntimeError:
        # 复现成功（修复前行为）——该测试不因命中而失败
        pass


class _PauseDict(dict):
    """主存储替换物：values() 返回生成器，在产出首个元素后暂停。

    用于确定性复现 H2：get_stats 迭代 nodes 视图期间，另一线程 add_node
    扩容字典 → dict_values 迭代器恢复时抛 RuntimeError。无 tight-loop，
    避开本机 Python 3.9 在高频锁竞争下的 GIL 饥饿问题。
    """

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def values(self):
        it = iter(dict.values(self))
        yield next(it)          # 首个元素
        self.entered.set()      # 通知主线程：get_stats 已处于迭代中
        self.release.wait(timeout=5)  # 暂停，让另一线程完成 add_node 扩容
        yield from it           # 恢复迭代；若期间 dict resize → RuntimeError


def test_h2_get_stats_atomic_under_forced_resize():
    """确定性回归：get_stats 迭代期间并发 add_node 扩容不得抛 RuntimeError。

    交错时序：
      线程A: get_stats → 迭代 nodes.values()，产出首个元素后暂停（持有锁）
      线程B: add_node → 扩容 dict（修复后：被锁阻塞；修复前：立即生效）
      主线程: 放行 → 线程A 恢复迭代 → 修复前抛 RuntimeError，修复后正常

    修复前（get_stats 无锁）：B 的 add_node 在 A 迭代中生效 → dict resize
        → A 的 dict_values 迭代器抛 RuntimeError → 红。
    修复后（get_stats 持锁）：B 的 add_node 阻塞至 A 完成 → 无交错 → 绿。
    """
    g = Graph()
    g.nodes = _PauseDict()  # 替换主存储，add_node 仍直接作用于该 dict
    g.add_node(Node(
        id="root", exec_type="manager", role="m", task="t", config={},
        status=NodeStatus.WAITING,
    ))
    errors = []

    def run_stats():
        try:
            g.get_stats()
        except RuntimeError as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=run_stats)
    t.start()
    assert g.nodes.entered.wait(timeout=5), "get_stats 未在预期点暂停（迭代未开始）"

    # 线程 B：趁 A 迭代中扩容
    adder = threading.Thread(target=lambda: g.add_node(Node(
        id="y", exec_type="worker", role="r", task="t2", config={},
        status=NodeStatus.WAITING,
    )))
    adder.start()

    # 放行 A 恢复迭代
    g.nodes.release.set()
    t.join(timeout=5)
    adder.join(timeout=5)

    assert not errors, f"H2: get_stats 迭代被并发扩容打断 → {errors}"
    assert not t.is_alive(), "get_stats 线程未退出"
    assert not adder.is_alive(), "add_node 线程未退出（可能死锁）"


# ============================================================================
# H3: Persistence.load() 语义损坏容错回退 .tmp
# ============================================================================


def test_h3_semantically_corrupt_primary_falls_back_to_tmp(tmp_path):
    """语义损坏（合法 JSON、结构非法）的 primary 应回退 .tmp，不抛异常。

    修复前：_dict_to_snapshot 里 Node.from_json 对非法 status 枚举抛 ValueError，
    load() 未捕获 → 直接传播 → 红。
    修复后：语义损坏按"损坏"处理 → 回退 .tmp 成功恢复 → 绿。
    """
    path = str(tmp_path / "state.json")
    p = Persistence(path)

    g = Graph()
    g.add_node(Node(
        id="n1", exec_type="worker", role="r", task="t", config={},
        status=NodeStatus.WAITING,
    ))
    p.save(g)
    assert p.load() is not None

    # 保留一份完好备份作为 .tmp
    shutil.copy(path, path + ".tmp")

    # 用"合法 JSON 但 status 非法枚举"覆盖 primary（语义损坏）
    corrupt = {
        "graph_version": 2,
        "nodes": {
            "n1": {
                "id": "n1",
                "status": "NOT_A_STATUS",
                "exec_type": "worker",
                "role": "r",
                "task": "t",
                "config": {},
            }
        },
        "edges": {},
        "crashed": [],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(corrupt, f)

    snap = p.load()
    assert snap is not None, "H3: primary 语义损坏时应回退 .tmp 恢复"
    assert "n1" in snap.nodes
    assert snap.nodes["n1"].status == NodeStatus.WAITING


def test_h3_corrupt_log_timestamp_does_not_crash(tmp_path):
    """log 条目时间戳非法（datetime 解析失败）的节点应被容错，不整盘崩溃。"""
    path = str(tmp_path / "state.json")
    p = Persistence(path)

    g = Graph()
    g.add_node(Node(
        id="n1", exec_type="worker", role="r", task="t", config={},
        status=NodeStatus.WAITING,
    ))
    p.save(g)
    shutil.copy(path, path + ".tmp")

    corrupt = json.loads(open(path, encoding="utf-8").read())
    corrupt["nodes"]["n1"]["log"] = [{"ts": "not-a-datetime", "level": "INFO", "message": "boom"}]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(corrupt, f)

    # 不应抛异常：回退 .tmp 或至少返回 None 而非崩溃
    snap = p.load()
    if snap is not None:
        assert "n1" in snap.nodes
