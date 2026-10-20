"""
runtime/persistence.py — Graph 状态持久化与崩溃恢复

支持原子写盘、双文件容错、周期性自动保存。
"""

import json
import logging
import os
import threading
from typing import Optional

from runtime.graph import Graph, GraphSnapshot
from runtime.jsonio import atomic_write_json
from runtime.models import Node, Edge

logger = logging.getLogger(__name__)


class Persistence:
    """Graph 状态持久化。

    职责:
      - 原子写盘（.tmp → fsync → rename）
      - 加载时自动容错（.json 损坏则尝试 .tmp）
      - 周期性自动保存（每 2s 检查 is_dirty）
      - 清空持久化文件
    """

    def __init__(self, file_path: str = "runtime_state.json"):
        self.file_path = file_path
        self._timer: Optional[threading.Timer] = None
        self._timer_lock = threading.Lock()
        # 串行化 save()：定时线程 + API/主线程（start/shutdown/global_resume…）可能
        # 并发触发写盘，若不互斥会双写同一 .tmp → 污染 primary + FileNotFoundError。
        self._save_lock = threading.Lock()

    # ================================================================
    # Save
    # ================================================================

    def save(self, graph: Graph) -> None:
        """原子写盘保存 Graph 快照。

        写盘细节见 ``runtime.jsonio.atomic_write_json``（tmp → fsync → os.replace）。

        通过 graph.snapshot() 获取快照（持有读锁），不丢变更。
        使用 mark_clean_if 避免 snapshot() 释放锁后并发 mutate 错误清脏。

        并发安全：_save_lock 串行化整个保存过程（含 snapshot 与 mark_clean_if），
        防止定时线程与 API/主线程并发写同一 .tmp 导致 primary 被污染。
        """
        with self._save_lock:
            version = graph.graph_version
            snapshot = graph.snapshot()
            self._save_snapshot(snapshot)
            graph.mark_clean_if(version)

    def _save_snapshot(self, snapshot: GraphSnapshot) -> None:
        """将 GraphSnapshot 序列化并原子写入磁盘。"""
        atomic_write_json(self.file_path, self._snapshot_to_dict(snapshot))

    def _snapshot_to_dict(self, snapshot: GraphSnapshot) -> dict:
        """将 GraphSnapshot 转换为可 JSON 序列化的 dict。"""
        nodes_dict = {}
        for nid, node in snapshot.nodes.items():
            nodes_dict[nid] = json.loads(node.to_json())
        edges_dict = {}
        for eid, edge in snapshot.edges.items():
            edges_dict[eid] = json.loads(edge.to_json())
        crashed_list = [json.loads(n.to_json()) for n in snapshot.crashed]
        return {
            "graph_version": snapshot.graph_version,
            "nodes": nodes_dict,
            "edges": edges_dict,
            "crashed": crashed_list,
        }

    # ================================================================
    # Load
    # ================================================================

    def load(self) -> Optional[GraphSnapshot]:
        """从磁盘加载 Graph 快照。

        优先读正式文件，损坏则尝试 .tmp；
        双坏返回 None 并 logging.error 告警。

        "损坏"同时覆盖两层：
          - 语法损坏（非合法 JSON）
          - 语义损坏（合法 JSON 但结构非法，如非法 status 枚举 / 坏 datetime /
            缺必需字段 —— _dict_to_snapshot 反序列化失败）

        Returns:
            GraphSnapshot 或 None（双文件损坏时）。
        """
        data = self._try_load_path(self.file_path)
        if data is not None:
            snapshot = self._dict_to_snapshot(data)
            if snapshot is not None:
                return snapshot

        # 正式文件损坏，尝试 .tmp
        tmp_path = self.file_path + ".tmp"
        data = self._try_load_path(tmp_path)
        if data is not None:
            snapshot = self._dict_to_snapshot(data)
            if snapshot is not None:
                logger.warning(
                    "Primary file corrupted, recovered from .tmp backup: %s", tmp_path
                )
                return snapshot

        # 双坏
        logger.error(
            "Both persistence files corrupted. primary=%s backup=%s",
            self.file_path, tmp_path,
        )
        return None

    def _try_load_path(self, path: str) -> Optional[dict]:
        """尝试从指定路径加载并解析 JSON。失败返回 None 并记录错误。"""
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load persistence file %s: %s", path, exc)
            return None

    def _dict_to_snapshot(self, data: dict) -> Optional[GraphSnapshot]:
        """将 dict 反序列化为 GraphSnapshot。

        语义损坏（结构非法）时返回 None 而非抛异常，交由 load() 回退 .tmp，
        避免整个恢复路径因单个损坏节点崩溃。逐条反序列化失败仅丢该节点，
        已成功部分可用的场景也优先保留（返回 None 会让 load 走 .tmp，
        而 .tmp 通常完整）。

        Args:
            data: 从磁盘 JSON 解析出的 dict。

        Returns:
            GraphSnapshot 或 None（语义损坏）。
        """
        try:
            nodes = {}
            for nid, ndata in data.get("nodes", {}).items():
                nodes[nid] = Node.from_json(json.dumps(ndata))

            edges = {}
            for eid, edata in data.get("edges", {}).items():
                edges[eid] = Edge.from_json(json.dumps(edata))

            crashed = []
            for ndata in data.get("crashed", []):
                crashed.append(Node.from_json(json.dumps(ndata)))

            return GraphSnapshot(
                nodes=nodes,
                edges=edges,
                crashed=crashed,
                graph_version=data.get("graph_version", 0),
            )
        except Exception as exc:
            logger.error(
                "Failed to deserialize persistence data (semantic corruption): %s", exc
            )
            return None

    # ================================================================
    # Clear
    # ================================================================

    def clear(self) -> None:
        """清空持久化文件（删除正式文件和 .tmp 文件）。"""
        for path in (self.file_path, self.file_path + ".tmp"):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as exc:
                logger.error("Failed to remove persistence file %s: %s", path, exc)

    # ================================================================
    # Periodic save
    # ================================================================

    def start_periodic_save(self, graph: Graph, interval: float = 2.0) -> None:
        """启动周期性自动保存。

        每 interval 秒检查 graph.is_dirty，有变更则写盘。

        Args:
            graph: 要监视的 Graph 实例。
            interval: 检查间隔（秒），默认 2.0。
        """
        with self._timer_lock:
            self._stop_timer_locked()
            self._start_timer_locked(graph, interval)

    def stop_periodic_save(self) -> None:
        """停止周期性自动保存。"""
        with self._timer_lock:
            self._stop_timer_locked()

    def _start_timer_locked(self, graph: Graph, interval: float) -> None:
        """持有锁时启动定时器。"""
        def _tick():
            try:
                if graph.is_dirty:
                    self.save(graph)
            except Exception as exc:
                logger.error("Periodic save failed, will retry: %s", exc)
            with self._timer_lock:
                if self._timer is not None:
                    self._timer = threading.Timer(interval, _tick)
                    self._timer.daemon = True
                    self._timer.start()

        self._timer = threading.Timer(interval, _tick)
        self._timer.daemon = True
        self._timer.start()

    def _stop_timer_locked(self) -> None:
        """持有锁时停止定时器。"""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
