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
  NodeStatus,
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
  /** 是否崩溃 */
  crashed: boolean;
  /** 崩溃时间 */
  crashedAt: string | null;
  /** 受影响的节点 ID 列表 */
  affectedNodes: string[];
  /** 根节点 ID */
  rootNodeId: string | null;
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
  /** 批量更新节点（全量替换） */
  batchUpdate: (nodes: GraphNode[], edges: GraphEdge[]) => void;
  /** 收起所有 Manager 子树 */
  collapseAll: () => void;
  /** 展开所有节点 */
  expandAll: () => void;
  /** 选中节点 */
  selectNode: (id: string | null) => void;
  /** 获取节点 */
  getNode: (id: string) => GraphNode | undefined;
  /** 获取根节点 */
  getRootNode: () => GraphNode | undefined;
  /** 获取指定节点的所有依赖节点 */
  getNodeDependents: (id: string) => GraphNode[];
  /** 获取指定节点的所有子节点（parent edge 指向的子节点） */
  getChildren: (id: string) => GraphNode[];
  /** 高亮指定节点 */
  highlightNodes: (ids: string[]) => void;
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
  crashed: false,
  crashedAt: null,
  affectedNodes: [],
  rootNodeId: null,
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
      nodes.set(node.id, node);
      // 如果是第一个 Manager 节点，设为根节点
      const rootNodeId = state.rootNodeId || (node.exec_type === 'M' ? node.id : null);
      return {
        nodes,
        rootNodeId,
        stats: computeStats(nodes),
      };
    });
  },

  updateNode: (id: string, updates: Partial<GraphNode>) => {
    set((state) => {
      const nodes = new Map(state.nodes);
      const existing = nodes.get(id);
      if (!existing) return state;
      nodes.set(id, { ...existing, ...updates });
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
      nodes.delete(id);
      // 删除关联边
      for (const [edgeId, edge] of edges) {
        if (edge.source === id || edge.target === id) {
          edges.delete(edgeId);
        }
      }
      return {
        nodes,
        edges,
        stats: computeStats(nodes),
      };
    });
  },

  batchUpdate: (nodes: GraphNode[], edges: GraphEdge[]) => {
    set((state) => {
      const nodesMap = new Map<string, GraphNode>();
      const edgesMap = new Map<string, GraphEdge>();

      // 保留已有节点中 crashed / affectedNodes 信息
      for (const node of nodes) {
        nodesMap.set(node.id, node);
      }

      for (const edge of edges) {
        edgesMap.set(edge.id, edge);
      }

      // 计算根节点
      const rootNodeId = state.rootNodeId || nodes.find((n) => n.exec_type === 'M')?.id || null;

      return {
        nodes: nodesMap,
        edges: edgesMap,
        rootNodeId,
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

  getRootNode: () => {
    const { nodes, rootNodeId } = get();
    return rootNodeId ? nodes.get(rootNodeId) : undefined;
  },

  getNodeDependents: (id: string) => {
    const { edges, nodes } = get();
    const dependents: GraphNode[] = [];
    for (const edge of edges.values()) {
      if (edge.type === 'dependency' && edge.target === id) {
        const node = nodes.get(edge.source);
        if (node) dependents.push(node);
      }
    }
    return dependents;
  },

  getChildren: (id: string) => {
    const { edges, nodes } = get();
    const children: GraphNode[] = [];
    for (const edge of edges.values()) {
      if (edge.type === 'parent' && edge.source === id) {
        const node = nodes.get(edge.target);
        if (node) children.push(node);
      }
    }
    return children;
  },

  highlightNodes: (ids: string[]) => {
    // 由 UI 层根据 selectedNodeId 自行高亮
    // 此方法保留供外部调用
    set({ selectedNodeId: ids[0] || null });
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
      const existing = logsByNode.get(nodeId) || [];
      const merged = new Map<string, LogEntry>();
      for (const entry of [...existing, ...entries]) {
        const key = `${entry.id}:${entry.timestamp}:${entry.message}`;
        merged.set(key, { ...entry, node_id: nodeId });
      }
      logsByNode.set(nodeId, Array.from(merged.values()).sort((a, b) => (
        new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
      )));
      return { logsByNode };
    });
  },

  appendNodeLog: (nodeId: string, entry: LogEntry) => {
    set((state) => {
      const logsByNode = new Map(state.logsByNode);
      const existing = logsByNode.get(nodeId) || [];
      const key = `${entry.id}:${entry.timestamp}:${entry.message}`;
      if (existing.some((item) => `${item.id}:${item.timestamp}:${item.message}` === key)) {
        return state;
      }
      logsByNode.set(nodeId, [...existing, { ...entry, node_id: nodeId }].slice(-10000));
      return { logsByNode };
    });
  },

  getNodeLogs: (nodeId: string) => get().logsByNode.get(nodeId) || [],
}));
