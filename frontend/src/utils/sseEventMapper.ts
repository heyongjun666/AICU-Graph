// ============================================================================
// sseEventMapper.ts — SSE 事件类型映射层
// 将后端线格式事件 → 前端 SSEEvent
// 解决前后端 SSE 事件类型不匹配问题
//
// 后端线格式：{ event_type: "node_added", payload: {...}, timestamp: 1234567890.0 }
// 前端期望：  { type: SSEEventType.NodeUpdate, data: {...}, timestamp: "2024-..." }
// ============================================================================

import { SSEEvent, SSEEventType, GraphNode, GraphEdge } from '../types';

// ============================================================================
// 后端线格式事件类型 → 前端 SSEEventType 映射
// ============================================================================

const BACKEND_TO_FRONTEND_TYPE: Record<string, SSEEventType> = {
  // 节点变更 → 前端节点更新
  'node_added': SSEEventType.NodeUpdate,
  'node_updated': SSEEventType.NodeUpdate,
  'node_removed': SSEEventType.NodeBatch,
  // node_status_changed 也映射为 NodeUpdate（只更新状态）
  'node_status_changed': SSEEventType.NodeUpdate,

  // 边变更 → 前端批量刷新
  'edge_added': SSEEventType.NodeBatch,
  'edge_removed': SSEEventType.NodeBatch,

  // 日志追加 → 前端日志事件
  'log_appended': SSEEventType.LogEvent,

  // 图快照 → 前端统计信息
  'graph_updated': SSEEventType.GraphStats,

  // 声明性事件（后端尚未全部实现，保留映射以备扩展）
  'graph_started': SSEEventType.GraphStarted,
  'graph_completed': SSEEventType.GraphCompleted,
  'graph_crashed': SSEEventType.GraphCrashed,
};

// ============================================================================
// 后端 → 前端字段映射
// ============================================================================

/**
 * 后端 exec_type ("manager"/"worker") → 前端 ("M"/"W")
 */
function mapExecType(execType: unknown): 'M' | 'W' | undefined {
  if (execType === 'manager' || execType === 'M') return 'M';
  if (execType === 'worker' || execType === 'W') return 'W';
  return undefined;
}

/**
 * 把后端 node payload 字段映射为前端 GraphNode
 * 后端字段：id, exec_type("manager"/"worker"), parent_id, role, task, status, manager_phase
 * 前端字段：id, label, exec_type("M"/"W"), parent_id, role, task, status
 */
function mapNodePayload(raw: Record<string, unknown>): Partial<GraphNode> {
  const out: Partial<GraphNode> = {};
  const rawId = raw.id ?? raw.node_id;
  if (typeof rawId === 'string') out.id = rawId;
  if (raw.label !== undefined) {
    out.label = raw.label as string;
  } else if (typeof rawId === 'string') {
    // 后端不带 label 时用 id + role 合成
    out.label = `${raw.role || raw.exec_type || 'node'} ${rawId.slice(0, 8)}`;
  }
  const et = mapExecType(raw.exec_type);
  if (et) out.exec_type = et;
  if (raw.parent_id !== undefined) out.parent_id = raw.parent_id as string | null;
  if (raw.role !== undefined) out.role = raw.role as string;
  if (raw.task !== undefined) out.task = raw.task as string;
  if (raw.status !== undefined) out.status = raw.status as any;
  if (raw.config !== undefined) out.config = raw.config as any;
  if (raw.manager_phase !== undefined) out.manager_phase = raw.manager_phase as string | null;
  if (raw.created_at !== undefined) out.created_at = raw.created_at as string | null;
  if (raw.started_at !== undefined) out.started_at = raw.started_at as string | null;
  if (raw.finished_at !== undefined) out.finished_at = raw.finished_at as string | null;
  if (raw.artifact_path !== undefined) out.artifact_path = raw.artifact_path as string | null;
  if (raw.error !== undefined) {
    out.error = raw.error === null
      ? null
      : typeof raw.error === 'string'
        ? raw.error
        : String((raw.error as Record<string, unknown>)?.message || raw.error);
  }
  if (raw.cc_activity !== undefined) out.cc_activity = raw.cc_activity as GraphNode['cc_activity'];
  if (raw.iteration !== undefined) out.iteration = raw.iteration as number;
  if (raw.iteration_count !== undefined) out.iteration = raw.iteration_count as number;
  if (raw.max_iterations !== undefined) out.max_iterations = raw.max_iterations as number;
  if (raw.dependencies !== undefined) out.dependencies = raw.dependencies as string[];
  if (raw.dependents !== undefined) out.dependents = raw.dependents as string[];
  if (raw.children !== undefined) out.children = raw.children as string[];
  return out;
}

/**
 * 把后端 edge payload 字段映射为前端 GraphEdge
 * 后端字段：id, from_node, to_node, type
 * 前端字段：id, source, target, type
 */
function mapEdgePayload(raw: Record<string, unknown>): Partial<GraphEdge> | null {
  if (!raw.id) return null;
  const source = (raw.source ?? raw.from_node) as string | undefined;
  const target = (raw.target ?? raw.to_node) as string | undefined;
  if (!source || !target) return null;
  return {
    id: raw.id as string,
    source,
    target,
    type: (raw.type as any) ?? 'parent',
  };
}

// ============================================================================
// 映射函数
// ============================================================================

/**
 * 将后端线格式的 SSE 事件转换为前端 SSEEvent
 * @param raw - 从 EventSource 收到的原始 JSON 解析对象
 * @returns 映射后的 SSEEvent，或 null（无法识别/无效的事件）
 */
export function mapBackendEvent(raw: unknown): SSEEvent | null {
  if (!raw || typeof raw !== 'object') return null;

  const obj = raw as Record<string, unknown>;

  // 后端使用 event_type 字段标识事件类型
  const eventType = obj['event_type'] as string | undefined;
  if (!eventType) {
    console.warn('[SSEMapper] Missing event_type in backend event');
    return null;
  }

  // 映射到前端事件类型
  const frontendType = BACKEND_TO_FRONTEND_TYPE[eventType];
  if (!frontendType) {
    console.warn(`[SSEMapper] Unknown backend event type: "${eventType}"`);
    return null;
  }

  // 时间戳转换：后端为 unix 秒（number），前端为 ISO 字符串
  let timestamp: string;
  const rawTs = obj['timestamp'];
  if (typeof rawTs === 'number') {
    timestamp = new Date(rawTs * 1000).toISOString();
  } else if (typeof rawTs === 'string') {
    timestamp = rawTs;
  } else {
    timestamp = new Date().toISOString();
  }

  // 字段映射：后端 payload → 前端 data
  const payload = (obj['payload'] ?? {}) as Record<string, unknown>;

  // 根据事件类型做 payload 字段转换
  let data: unknown = payload;
  if (eventType === 'node_added' || eventType === 'node_updated' || eventType === 'node_status_changed') {
    data = mapNodePayload(payload);
  } else if (eventType === 'node_removed') {
    // node_removed payload 只有 {node_id}，转成 {id} 供前端 removeNode 使用
    data = { id: payload.node_id ?? payload.id };
  } else if (eventType === 'edge_added') {
    data = mapEdgePayload({
      id: payload.edge_id ?? payload.id,
      from_node: payload.from_node,
      to_node: payload.to_node,
      type: payload.edge_type ?? payload.type,
    });
  } else if (eventType === 'edge_removed') {
    data = { id: payload.edge_id ?? payload.id };
  } else if (eventType === 'graph_updated') {
    // 快照事件：payload 含 nodes/edges/stats，需逐个映射
    data = mapGraphSnapshotPayload(payload);
  }

  return {
    type: frontendType,
    data,
    timestamp,
  };
}

/**
 * 映射 graph_updated 快照事件的 payload
 * payload 结构：{ ts, summary, nodes: [...], edges: [...], stats: {...} }
 */
function mapGraphSnapshotPayload(payload: Record<string, unknown>): {
  nodes: GraphNode[];
  edges: GraphEdge[];
  stats: unknown;
} {
  const rawNodes = (payload.nodes as Record<string, unknown>[]) || [];
  const rawEdges = (payload.edges as Record<string, unknown>[]) || [];
  const stats = payload.stats;
  const nodes: GraphNode[] = rawNodes
    .map((n) => mapNodePayload(n) as GraphNode)
    .filter((n) => n.id);
  const edges: GraphEdge[] = rawEdges
    .map((e) => mapEdgePayload(e) as GraphEdge)
    .filter((e) => e && e.id);
  return { nodes, edges, stats };
}

/**
 * 判断后端事件是否为已知类型
 */
export function isKnownBackendEventType(eventType: string): boolean {
  return eventType in BACKEND_TO_FRONTEND_TYPE;
}
