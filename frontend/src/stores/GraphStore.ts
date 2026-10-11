// ============================================================================
// GraphStore — 图数据状态管理（Zustand）
// 管理节点/边的新增、更新、删除、查询、批量操作
// ============================================================================

import { create } from 'zustand';
import {
  GraphNode,
  GraphEdge,
  GraphStats,
  LogEntry,
  computeStats,
} from '../types';

// ============================================================================
// 状态接口
// ============================================================================

interface GraphState {
  /** 节点 Map（id -> GraphNode） */
  nodes: Map<string, GraphNode>;
  /** 边 Map（id -> GraphEdge） */
  edges: Map<string, GraphEdge>;
  /** 当前选中的节点 ID */
  selectedNodeId: string | null;
  /** 图统计信息 */
  stats: GraphStats;
  /** 按节点保存的日志 */
  logsByNode: Map<string, LogEntry[]>;
}

// ============================================================================
// 操作接口
// ============================================================================

interface GraphActions {
  /** 添加单个节点 */
  addNode: (node: GraphNode) => void;
  /** 更新单个节点 */
  updateNode: (id: string, updates: Partial<GraphNode>) => void;
  /** 删除单个节点 */
  removeNode: (id: string) => void;
  /** 新增单条边（SSE edge_added 实时应用） */
  addEdge: (edge: GraphEdge) => void;
  /** 删除单条边（SSE edge_removed 实时应用） */
  removeEdge: (edgeId: string) => void;
  /** 批量应用图快照；节点按字段合并，快照外节点按全量语义移除 */
  batchUpdate: (nodes: GraphNode[], edges: GraphEdge[]) => void;
  /** 收起所有 Manager 子树 */
  collapseAll: () => void;
  /** 展开所有节点 */
  expandAll: () => void;
  /** 选中节点 */
  selectNode: (id: string | null) => void;
  /** 获取节点 */
  getNode: (id: string) => GraphNode | undefined;
  /** 重置图数据 */
  resetGraph: () => void;
  /** 重新计算统计信息 */
  computeStats: () => void;
  /** 合并节点历史日志 */
  setNodeLogs: (nodeId: string, entries: LogEntry[]) => void;
  /** 追加一条节点实时日志 */
  appendNodeLog: (nodeId: string, entry: LogEntry) => void;
  /** 获取节点日志 */
  getNodeLogs: (nodeId: string) => LogEntry[];
}

type GraphStore = GraphState & GraphActions;

/** Merge only fields explicitly supplied by a backend patch. */
function mergeNodeFields(
  existing: GraphNode | undefined,
  incoming: Partial<GraphNode>,
): GraphNode {
  if (!existing) return { ...incoming } as GraphNode;
  const merged = { ...existing } as Record<string, unknown>;
  for (const [key, value] of Object.entries(incoming)) {
    // undefined means "not included in this sparse payload"; null is an
    // intentional clear and must therefore be retained.
    if (value !== undefined) merged[key] = value;
  }
  return merged as unknown as GraphNode;
}

function logKey(entry: LogEntry, nodeId: string): string {
  if (entry.id !== undefined && entry.id !== null && Number.isFinite(entry.id) && entry.id > 0) {
    return `server:${nodeId}:${entry.id}`;
  }
  return `content:${JSON.stringify([
    nodeId,
    entry.timestamp,
    entry.level,
    entry.message,
    entry.cc_id ?? null,
    entry.phase ?? null,
    entry.event ?? null,
    entry.task ?? null,
  ])}`;
}

function mergeLogEntries(
  nodeId: string,
  existing: LogEntry[],
  incoming: LogEntry[],
): LogEntry[] {
  const merged = new Map<string, LogEntry>();
  for (const entry of [...existing, ...incoming]) {
    const normalized = { ...entry, node_id: nodeId };
    merged.set(logKey(normalized, nodeId), normalized);
  }
  return Array.from(merged.values())
    .sort((a, b) => {
      const left = Date.parse(a.timestamp);
      const right = Date.parse(b.timestamp);
      if (Number.isNaN(left) || Number.isNaN(right) || left === right) return 0;
      return left - right;
    })
    .slice(-10000);
}

// ============================================================================
// 初始状态
// ============================================================================

const initialState: GraphState = {
  nodes: new Map(),
  edges: new Map(),
  selectedNodeId: null,
  stats: {
    total: 0,
    waiting: 0,
    ready: 0,
    running: 0,
    completed: 0,
    failed: 0,
    stopped: 0,
    paused: 0,
  },
  logsByNode: new Map(),
};

// ============================================================================
// Store 创建
// ============================================================================

export const useGraphStore = create<GraphStore>((set, get) => ({
  // ---- 初始状态 ----
  ...initialState,

  // ---- 操作实现 ----

  addNode: (node: GraphNode) => {
    set((state) => {
      const nodes = new Map(state.nodes);
      nodes.set(node.id, mergeNodeFields(nodes.get(node.id), node));
      return {
        nodes,
        stats: computeStats(nodes),
      };
    });
  },

  updateNode: (id: string, updates: Partial<GraphNode>) => {
    set((state) => {
      const existing = state.nodes.get(id);
      if (!existing) return state;
      const nodes = new Map(state.nodes);
      nodes.set(id, mergeNodeFields(existing, updates));
      return {
        nodes,
        stats: computeStats(nodes),
      };
    });
  },

  removeNode: (id: string) => {
    set((state) => {
      const nodes = new Map(state.nodes);
      const edges = new Map(state.edges);
      const logsByNode = new Map(state.logsByNode);
      nodes.delete(id);
      logsByNode.delete(id);
      // 删除关联边
      for (const [edgeId, edge] of edges) {
        if (edge.source === id || edge.target === id) {
          edges.delete(edgeId);
        }
      }
      return {
        nodes,
        edges,
        logsByNode,
        stats: computeStats(nodes),
      };
    });
  },

  removeEdge: (edgeId: string) => {
    set((state) => {
      const edges = new Map(state.edges);
      edges.delete(edgeId);
      return { edges };
    });
  },

  addEdge: (edge: GraphEdge) => {
    set((state) => {
      const edges = new Map(state.edges);
      edges.set(edge.id, edge);
      return { edges };
    });
  },

  batchUpdate: (incomingNodes: GraphNode[], edges: GraphEdge[]) => {
    set((state) => {
      // A graph snapshot is authoritative about membership, but a node object
      // may still be sparse (for example after a status-only SSE refresh).
      const nodesMap = new Map<string, GraphNode>();
      const edgesMap = new Map<string, GraphEdge>();

      for (const node of incomingNodes) {
        nodesMap.set(node.id, mergeNodeFields(state.nodes.get(node.id), node));
      }

      for (const edge of edges) {
        edgesMap.set(edge.id, edge);
      }

      return {
        nodes: nodesMap,
        edges: edgesMap,
        stats: computeStats(nodesMap),
      };
    });
  },

  collapseAll: () => {
    // 由 UIStateStore 的 collapsedNodeIds 处理，GraphStore 不做实际折叠
    // 这里仅作为触发信号
    set((state) => ({ stats: computeStats(state.nodes) }));
  },

  expandAll: () => {
    set((state) => ({ stats: computeStats(state.nodes) }));
  },

  selectNode: (id: string | null) => {
    set({ selectedNodeId: id });
  },

  getNode: (id: string) => {
    return get().nodes.get(id);
  },

  resetGraph: () => {
    set({ ...initialState });
  },

  computeStats: () => {
    set((state) => ({ stats: computeStats(state.nodes) }));
  },

  setNodeLogs: (nodeId: string, entries: LogEntry[]) => {
    set((state) => {
      const logsByNode = new Map(state.logsByNode);
      logsByNode.set(nodeId, mergeLogEntries(
        nodeId,
        logsByNode.get(nodeId) || [],
        entries,
      ));
      return { logsByNode };
    });
  },

  appendNodeLog: (nodeId: string, entry: LogEntry) => {
    set((state) => {
      const logsByNode = new Map(state.logsByNode);
      const existing = logsByNode.get(nodeId) || [];
      const merged = mergeLogEntries(nodeId, existing, [entry]);
      // Avoid publishing a new state for a duplicate event.
      if (merged.length === existing.length && merged.every((item, index) => item === existing[index])) {
        return state;
      }
      logsByNode.set(nodeId, merged);
      return { logsByNode };
    });
  },

  getNodeLogs: (nodeId: string) => get().logsByNode.get(nodeId) || [],
}));
