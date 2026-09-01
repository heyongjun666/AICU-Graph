"""
runtime/graph.py — 内存 Graph 存储引擎

支持节点/边的 CRUD 和 O(1) 派生查询。
Graph 只存 Node + Edge，children/dependencies/dependents 由邻接索引反查，
不维护冗余的派生字段。
"""

import copy
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from runtime.models import Node, Edge, EdgeType, NodeStatus

# Mutation event callback type
MutationCallback = Callable[[str, dict], None]
"""Graph 变更回调：参数为 (event_type, payload_dict)。"""

# update_node 中属于"组织结构"的字段（Router 据此切换上下文）。
# 状态/config/task/log 等运行态变化不算结构变更。
_STRUCTURAL_FIELDS = frozenset({"parent_id", "exec_type", "role", "step_ref"})


class DependencyError(Exception):
    """删除节点时存在被依赖关系。

    当 remove_node 的目标节点仍有其他节点依赖它时抛出。

    字段:
        node_id: 被依赖的节点 ID。
        dependents: 依赖该节点的 Node 对象列表。
    """

    def __init__(self, node_id: str, dependents: list):
        self.node_id = node_id
        self.dependents = dependents
        super().__init__(
            f"Cannot remove node '{node_id}': {len(dependents)} dependent(s) exist"
        )


@dataclass
class GraphSnapshot:
    """Graph 快照。

    用于持久化和启动恢复。

    字段:
        nodes: 所有 Node 的深拷贝 dict。
        edges: 所有 Edge 的深拷贝 dict。
        crashed: 快照时处于 Running 状态的 Node 列表（崩溃后需降级）。
        graph_version: 快照时的版本号。
    """
    nodes: dict
    edges: dict
    crashed: list
    graph_version: int


class Graph:
    """内存 Graph 存储引擎。

    职责:
      - 维护 nodes / edges 主存储
      - 维护 3 组邻接索引（parent_children / dependency_from / dependency_to）
            实现 O(1) 派生查询
      - 维护 2 组反向边索引（edges_by_from_node / edges_by_to_node）
            实现 O(1) 级联删除关联 Edge
      - 线程安全（锁保护所有写操作）
      - graph_version 追踪 / is_dirty 标记
    """

    def __init__(self):
        # ---- 主存储 ----
        self.nodes: dict[str, Node] = {}       # node_id → Node
        self.edges: dict[str, Edge] = {}       # edge_id → Edge

        # ---- 邻接索引（派生查询） ----
        # parent 边：from=parent, to=child
        self.parent_children: dict[str, list[str]] = {}     # parent_id → [child_id, ...]
        # dependency 边：from=被依赖方, to=依赖方
        self.dependency_from: dict[str, list[str]] = {}     # 被依赖方 → [依赖方, ...]  (dependents)
        self.dependency_to: dict[str, list[str]] = {}       # 依赖方 → [被依赖方, ...]  (dependencies)

        # ---- 反向边索引（按 node 定位 edge） ----
        self.edges_by_from_node: dict[str, list[str]] = {}  # from_node → [edge_id, ...]
        self.edges_by_to_node: dict[str, list[str]] = {}    # to_node → [edge_id, ...]

        # ---- 并发控制 ----
        # 用 RLock：读方法加锁后，若未来某写方法内部再调用读方法不会死锁。
        self._lock = threading.RLock()

        # ---- 状态追踪 ----
        self._is_dirty = False
        self._graph_version = 0
        self._structure_version = 0

        # ---- 变更回调 ----
        self._on_mutation: Optional[MutationCallback] = None

    # ================================================================
    # Dirty / Version
    # ================================================================

    @property
    def is_dirty(self) -> bool:
        return self._is_dirty

    def mark_clean(self) -> None:
        self._is_dirty = False

    def mark_clean_if(self, version: int) -> None:
        """仅在 graph_version 未变化时清除脏标记。

        用于解决 save() 中的竞态条件：
        snapshot() 释放锁后到 mark_clean() 之间，并发 mutate 可能已递增版本号，
        此时不应清除 _is_dirty，否则后续 periodic save 会遗漏变更。
        """
        with self._lock:
            if self._graph_version == version:
                self._is_dirty = False

    @property
    def graph_version(self) -> int:
        return self._graph_version

    @property
    def structure_version(self) -> int:
        """结构版本号：只随组织结构变更自增（新增/删除节点或边、换 parent、
        改 exec_type/role/step_ref）。运行状态变化（status 等）不触发——
        Router 据此决定何时切换上下文。
        """
        return self._structure_version

    def restore_version(self, version: int) -> None:
        """恢复持久化的版本号（崩溃恢复路径）。

        重放 add_node/add_edge 时 _bump_version 会从 0 重新累加，
        与快照保存时的版本号不一致。恢复后必须重设为持久化值，
        保证 SSE/外部消费方看到的 graph_version 跨重启单调一致。

        注意：不标记 dirty —— 恢复本身不构成新的变更。
        """
        with self._lock:
            self._graph_version = version

    def _bump_version(self) -> None:
        """每次 mutate 操作后调用：递增版本 + 标记脏。"""
        self._graph_version += 1
        self._is_dirty = True

    def _bump_structure(self) -> None:
        """组织结构变更后调用：递增结构版本（Router 据此切换上下文）。

        仅在 add/remove node/edge 与 update_node 触及 _STRUCTURAL_FIELDS 时调用。
        """
        self._structure_version += 1

    # ================================================================
    # Node CRUD
    # ================================================================

    def add_node(self, node: Node) -> None:
        with self._lock:
            self.nodes[node.id] = node
            self._bump_version()
            self._bump_structure()
        self._trigger_mutation("add_node", node_id=node.id, exec_type=node.exec_type, parent_id=node.parent_id)

    def get_node(self, node_id: str) -> Optional[Node]:
        return self.nodes.get(node_id)

    def update_node(self, node_id: str, **updates) -> None:
        with self._lock:
            node = self.nodes.get(node_id)
            if node is None:
                raise ValueError(f"Node '{node_id}' not found")
            for key, value in updates.items():
                if hasattr(node, key):
                    setattr(node, key, value)
            self._bump_version()
            if updates.keys() & _STRUCTURAL_FIELDS:
                self._bump_structure()
        self._trigger_mutation("update_node", node_id=node_id, **updates)

    def validate_parent(
        self, child_id: Optional[str], parent_id: Optional[str]
    ) -> None:
        """Validate an organizational parent relationship without mutation.

        Explicit parents must be existing Manager nodes.  ``None`` remains
        valid for legacy manually-created root nodes and restore data.
        """
        if parent_id is None:
            return
        with self._lock:
            parent = self.nodes.get(parent_id)
            if parent is None:
                raise ValueError(f"Parent node '{parent_id}' not found")
            if parent.exec_type != "manager":
                raise ValueError(f"Parent node '{parent_id}' must be a Manager")
            if child_id is None:
                return
            if child_id == parent_id:
                raise ValueError("A node cannot be its own parent")

            cursor = parent_id
            visited: set[str] = set()
            while cursor and cursor not in visited:
                if cursor == child_id:
                    raise ValueError("Parent relationship would create a cycle")
                visited.add(cursor)
                ancestor = self.nodes.get(cursor)
                cursor = ancestor.parent_id if ancestor is not None else None

    def append_log(self, node_id: str, entry: object) -> None:
        with self._lock:
            node = self.nodes.get(node_id)
            if node is None:
                raise ValueError(f"Node '{node_id}' not found")
            node.log.append(entry)
            if len(node.log) > 10000:
                del node.log[:-10000]
            self._bump_version()
        self._trigger_mutation("append_log", node_id=node_id, log=[entry])

    def remove_node(self, node_id: str) -> None:
        with self._lock:
            if node_id not in self.nodes:
                raise ValueError(f"Node '{node_id}' not found")

            # 前置检查：存在依赖本节点的节点
            dependent_ids = self.dependency_from.get(node_id, [])
            if dependent_ids:
                dependent_nodes = [
                    self.nodes[dep_id] for dep_id in dependent_ids
                    if dep_id in self.nodes
                ]
                raise DependencyError(node_id, dependent_nodes)

            # 级联删除所有关联 Edge
            related_edge_ids = set()
            related_edge_ids.update(self.edges_by_from_node.get(node_id, []))
            related_edge_ids.update(self.edges_by_to_node.get(node_id, []))
            for eid in related_edge_ids:
                edge = self.edges.pop(eid, None)
                if edge is not None:
                    self._remove_edge_from_indexes(edge)

            # 从邻接索引中清除 node_id 的条目
            self.parent_children.pop(node_id, None)
            self.dependency_from.pop(node_id, None)
            self.dependency_to.pop(node_id, None)
            self.edges_by_from_node.pop(node_id, None)
            self.edges_by_to_node.pop(node_id, None)

            # 从其他节点的索引值列表中移除对 node_id 的引用
            for pid in list(self.parent_children):
                if node_id in self.parent_children[pid]:
                    self.parent_children[pid].remove(node_id)

            for fid in list(self.dependency_from):
                if node_id in self.dependency_from[fid]:
                    self.dependency_from[fid].remove(node_id)

            for tid in list(self.dependency_to):
                if node_id in self.dependency_to[tid]:
                    self.dependency_to[tid].remove(node_id)

            # 删除节点本身
            del self.nodes[node_id]
            self._bump_version()
            self._bump_structure()
        self._trigger_mutation("remove_node", node_id=node_id)

    # ================================================================
    # Edge CRUD
    # ================================================================

    def add_edge(self, edge: Edge) -> None:
        with self._lock:
            if edge.from_node not in self.nodes:
                raise ValueError(
                    f"from_node '{edge.from_node}' not found in graph"
                )
            if edge.to_node not in self.nodes:
                raise ValueError(
                    f"to_node '{edge.to_node}' not found in graph"
                )

            self.edges[edge.id] = edge

            # 更新邻接索引
            if edge.type == EdgeType.PARENT:
                self.parent_children.setdefault(edge.from_node, []).append(edge.to_node)
            elif edge.type == EdgeType.DEPENDENCY:
                self.dependency_from.setdefault(edge.from_node, []).append(edge.to_node)
                self.dependency_to.setdefault(edge.to_node, []).append(edge.from_node)

            # 更新反向边索引
            self.edges_by_from_node.setdefault(edge.from_node, []).append(edge.id)
            self.edges_by_to_node.setdefault(edge.to_node, []).append(edge.id)

            self._bump_version()
            self._bump_structure()
        self._trigger_mutation("add_edge", edge_id=edge.id, edge_type=str(edge.type),
                               from_node=edge.from_node, to_node=edge.to_node)

    def remove_edge(self, edge_id: str) -> None:
        with self._lock:
            edge = self.edges.pop(edge_id, None)
            if edge is None:
                raise ValueError(f"Edge '{edge_id}' not found")
            self._remove_edge_from_indexes(edge)
            self._bump_version()
            self._bump_structure()
        self._trigger_mutation("remove_edge", edge_id=edge_id)

    def _remove_edge_from_indexes(self, edge: Edge) -> None:
        """从全部索引中移除一条 Edge 的引用（不操作 self.edges）。"""
        # 邻接索引
        if edge.type == EdgeType.PARENT:
            children = self.parent_children.get(edge.from_node, [])
            if edge.to_node in children:
                children.remove(edge.to_node)
        elif edge.type == EdgeType.DEPENDENCY:
            dep_from = self.dependency_from.get(edge.from_node, [])
            if edge.to_node in dep_from:
                dep_from.remove(edge.to_node)
            dep_to = self.dependency_to.get(edge.to_node, [])
            if edge.from_node in dep_to:
                dep_to.remove(edge.from_node)

        # 反向边索引
        from_edges = self.edges_by_from_node.get(edge.from_node, [])
        if edge.id in from_edges:
            from_edges.remove(edge.id)
        to_edges = self.edges_by_to_node.get(edge.to_node, [])
        if edge.id in to_edges:
            to_edges.remove(edge.id)

    # ================================================================
    # 派生查询
    # ================================================================

    def children(self, node_id: str) -> list[Node]:
        """通过 parent_children 索引 O(1) 反查子节点。"""
        with self._lock:
            child_ids = self.parent_children.get(node_id, [])
            return [self.nodes[cid] for cid in child_ids if cid in self.nodes]

    def dependencies(self, node_id: str) -> list[Node]:
        """通过 dependency_to 索引 O(1) 反查依赖项。"""
        with self._lock:
            dep_ids = self.dependency_to.get(node_id, [])
            return [self.nodes[did] for did in dep_ids if did in self.nodes]

    def dependents(self, node_id: str) -> list[Node]:
        """通过 dependency_from 索引 O(1) 反查依赖方。"""
        with self._lock:
            dep_ids = self.dependency_from.get(node_id, [])
            return [self.nodes[did] for did in dep_ids if did in self.nodes]

    # ================================================================
    # 快照 & 统计
    # ================================================================

    def snapshot(self) -> GraphSnapshot:
        with self._lock:
            crashed = [
                n for n in self.nodes.values()
                if n.status == NodeStatus.RUNNING
            ]
            return GraphSnapshot(
                nodes=copy.deepcopy(self.nodes),
                edges=copy.deepcopy(self.edges),
                crashed=crashed,
                graph_version=self._graph_version,
            )

    def get_stats(self) -> dict[str, int]:
        """按 status 统计节点数。"""
        with self._lock:
            stats: dict[str, int] = {}
            for node in self.nodes.values():
                status = node.status.value if isinstance(node.status, NodeStatus) else str(node.status)
                stats[status] = stats.get(status, 0) + 1
            return stats

    def all_nodes(self) -> list[Node]:
        """返回所有 Node 的拷贝列表（持锁，线程安全）。

        替代外部直接迭代 self.nodes.values()：后者在并发
        add_node/remove_node 扩容/收缩字典时可能抛 RuntimeError。
        调用方如需 O(1) 单点查询仍可用 get_node。
        """
        with self._lock:
            return list(self.nodes.values())

    def all_edges(self) -> list[Edge]:
        """返回所有 Edge 的拷贝列表（持锁，线程安全）。

        替代外部直接迭代 self.edges.values()，理由同 all_nodes。
        """
        with self._lock:
            return list(self.edges.values())

    # ================================================================
    # Mutation callback
    # ================================================================

    def set_on_mutation(self, callback: Optional[MutationCallback]) -> None:
        """注册 Graph 变更回调，在节点/边发生变更时触发。

        Args:
            callback: 接收 (event_type: str, payload: dict) 的回调函数。
                     传入 None 可取消注册。
        """
        self._on_mutation = callback

    def _trigger_mutation(self, event_type: str, **payload) -> None:
        """触发变更回调（在锁外调用以避免死锁）。"""
        callback = self._on_mutation
        if callback is not None:
            try:
                callback(event_type, payload)
            except Exception:
                pass
