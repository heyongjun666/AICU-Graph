// ============================================================================
// sseEvents.ts — SSE 事件 → zustand store 应用（M7 实时展示核心）
//
// 纯展示层：把 M6 SSE 推送的 A 类状态事件 / C 类 graph_updated 快照，解析并
// 应用到 GraphStore / SessionStore，实现页面节点/边/状态/日志 UI 实时更新，
// 无需手动刷新。不含任何业务校验/状态推导逻辑——事件合法性由 M3/M6 契约保证，
// 本层只做「防御性字段解析 + 展示层字段映射」（与 SSEProvider 既有 log/session
// 处理保持同一风格）。
//
// 契约（module_division.md §4-D6 / design.md §4.2）：
//   - A 类（M3 构造 · 状态事件，forward 客户端，8 个）：
//     node_added / node_updated / node_removed / node_status_changed /
//     edge_added / edge_removed / log_appended / session_state_changed
//   - C 类（M6 构造 · 视图/快照，forward 客户端）：
//     graph_updated（断线重连空 → 快照兜底）
//   - B 类 scheduler_tick 内部观测不 forward；D 类 stats/activity 待 M6 放行。
//   - 合并帧形态：data 可能是单条 payload 对象或同 type 微批 payload 数组
//     （Array.isArray(data) ? data : [data] 双形态解析）。
// ============================================================================

import { useGraphStore } from '../stores/GraphStore';
import { useSessionStore } from '../stores/SessionStore';
import {
  EdgeType,
  GraphEdge,
  GraphNode,
  LogLevel,
  NodeStatus,
  SessionLifecycleState,
  SessionSummary,
} from '../types';
import { mapEdgeFromBackend, mapNodeFromBackend } from '../utils/api';

// ---------------------------------------------------------------------------
// 类型
// ---------------------------------------------------------------------------

export interface SSEFrame {
  /** SSE 事件类型（event: 行） */
  event: string;
  /** SSE 事件数据（data: 行原始字符串，或已解析的 payload 对象/数组） */
  data: unknown;
  /** 消费水位（id: 行，sequence_id） */
  id?: number | null;
}

// ---------------------------------------------------------------------------
// 防御性解析工具
// ---------------------------------------------------------------------------

/** 双形态解析：合并帧 data 为数组，未合并帧 data 为单条对象。 */
export function parseSSEPayload(data: unknown): unknown[] {
  if (Array.isArray(data)) return data;
  if (data === undefined || data === null) return [];
  return [data];
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object') return null;
  return value as Record<string, unknown>;
}

function asString(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function asNodeStatus(value: unknown): NodeStatus | null {
  if (typeof value === 'string' && (Object.values(NodeStatus) as string[]).includes(value)) {
    return value as NodeStatus;
  }
  return null;
}

function asExecType(value: unknown): 'M' | 'W' {
  return value === 'manager' || value === 'M' ? 'M' : 'W';
}

/**
 * edge_type 字段防御性归一（M6 事件侧与快照侧存在双形态序列化）：
 *   - 事件侧 runtime/graph.py:307 用 str(edge.type) 序列化 (str, Enum) 成员，
 *     产出 'EdgeType.PARENT' / 'EdgeType.DEPENDENCY'（M6 sse_manager 原样转发）；
 *   - 快照侧 runtime/sse_manager.py 用 edge.type.value，产出 'parent' / 'dependency'。
 * 统一取「末段成员名大写」归一：'EdgeType.DEPENDENCY' / 'DEPENDENCY' / 'dependency'
 * 均识别为 Dependency；其余（含 'EdgeType.PARENT' / 'parent' / 未知值）归 Parent，
 * 保持缺省容错，不因未知形态丢弃事件。
 */
function asEdgeType(value: unknown): EdgeType {
  const raw = String(value ?? '');
  const member = raw.slice(raw.lastIndexOf('.') + 1).toUpperCase();
  return member === 'DEPENDENCY' ? EdgeType.Dependency : EdgeType.Parent;
}

// ---------------------------------------------------------------------------
// 事件应用
// ---------------------------------------------------------------------------

/**
 * 应用一条 SSE 事件（event: 行 + data: 行）到对应 store。
 * 入口：SSEProvider 的 addEventListener 回调。
 */
export function applySSEFrame(frame: SSEFrame): void {
  let payload = frame.data;
  if (typeof payload === 'string') {
    try {
      payload = JSON.parse(payload);
    } catch {
      return; // 畸形 data 忽略，后续快照/重连兜底
    }
  }

  switch (frame.event) {
    case 'graph_updated':
      // 快照是权威全量态；合并帧只取最后一条（最新快照）。
      applySnapshot(lastOf(parseSSEPayload(payload)));
      break;
    case 'node_added':
      parseSSEPayload(payload).forEach((item) => applyNodeAdded(item));
      break;
    case 'node_updated':
      parseSSEPayload(payload).forEach((item) => applyNodeUpdated(item));
      break;
    case 'node_removed':
      parseSSEPayload(payload).forEach((item) => applyNodeRemoved(item));
      break;
    case 'node_status_changed':
      parseSSEPayload(payload).forEach((item) => applyNodeStatusChanged(item));
      break;
    case 'edge_added':
      parseSSEPayload(payload).forEach((item) => applyEdgeAdded(item));
      break;
    case 'edge_removed':
      parseSSEPayload(payload).forEach((item) => applyEdgeRemoved(item));
      break;
    case 'log_appended':
      parseSSEPayload(payload).forEach((item) => applyLogAppended(item));
      break;
    case 'session_state_changed':
      parseSSEPayload(payload).forEach((item) => applySessionStateChanged(item));
      break;
    default:
      // 未订阅事件（scheduler_tick 等）不处理。
      break;
  }
}

function lastOf<T>(items: T[]): T | undefined {
  return items.length > 0 ? items[items.length - 1] : undefined;
}

/** C 类：graph_updated 全量快照 → batchUpdate（全量语义）。 */
function applySnapshot(raw: unknown): void {
  const payload = asRecord(raw);
  if (!payload) return;
  const rawNodes = Array.isArray(payload.nodes) ? payload.nodes : [];
  const rawEdges = Array.isArray(payload.edges) ? payload.edges : [];
  const nodes = rawNodes
    .map((item) => (asRecord(item) ? mapNodeFromBackend(item) : null))
    .filter((node): node is GraphNode => node !== null);
  const edges = rawEdges
    .map((item) => (asRecord(item) ? mapEdgeFromBackend(item) : null))
    .filter((edge): edge is GraphEdge => edge !== null);
  useGraphStore.getState().batchUpdate(nodes, edges);
}

/** A 类：node_added → 新建节点（payload 稀疏：node_id/exec_type/parent_id）。 */
function applyNodeAdded(raw: unknown): void {
  const payload = asRecord(raw);
  if (!payload) return;
  const id = asString(payload.node_id);
  if (!id) return;
  const execType = asExecType(payload.exec_type);
  const node: GraphNode = {
    id,
    exec_type: execType,
    parent_id: typeof payload.parent_id === 'string' ? payload.parent_id : null,
    // 展示层默认：节点立即上屏，status/label 等详情由后续 node_updated /
    // node_status_changed / graph_updated 快照补齐。
    label: `${execType} ${id.slice(0, 8)}`,
    status: asNodeStatus(payload.status) ?? NodeStatus.Waiting,
  };
  useGraphStore.getState().addNode(node);
}

/** A 类：node_updated → 稀疏字段合并到已有节点。 */
function applyNodeUpdated(raw: unknown): void {
  const payload = asRecord(raw);
  if (!payload) return;
  const id = asString(payload.node_id);
  if (!id) return;
  const updates: Partial<GraphNode> = {};
  if (payload.status !== undefined) {
    const status = asNodeStatus(payload.status);
    if (status) updates.status = status;
  }
  if (payload.exec_type !== undefined) updates.exec_type = asExecType(payload.exec_type);
  for (const key of NODE_FIELD_MAP.keys()) {
    const value = payload[key];
    if (value === undefined) continue;
    updates[NODE_FIELD_MAP.get(key) as keyof GraphNode] = value as never;
  }
  if (Object.keys(updates).length === 0) return;
  useGraphStore.getState().updateNode(id, updates);
}

/** A 类：node_removed → 删除节点（含其关联边）。 */
function applyNodeRemoved(raw: unknown): void {
  const payload = asRecord(raw);
  if (!payload) return;
  const id = asString(payload.node_id);
  if (!id) return;
  useGraphStore.getState().removeNode(id);
}

/** A 类：node_status_changed → 仅更新节点状态。 */
function applyNodeStatusChanged(raw: unknown): void {
  const payload = asRecord(raw);
  if (!payload) return;
  const id = asString(payload.node_id);
  if (!id) return;
  const status = asNodeStatus(payload.status);
  if (!status) return;
  useGraphStore.getState().updateNode(id, { status });
}

/** A 类：edge_added → 新增边（parent / dependency，edge_type 双形态归一）。 */
function applyEdgeAdded(raw: unknown): void {
  const payload = asRecord(raw);
  if (!payload) return;
  const id = asString(payload.edge_id);
  const source = asString(payload.from_node);
  const target = asString(payload.to_node);
  if (!id || !source || !target) return;
  const type = asEdgeType(payload.edge_type);
  useGraphStore.getState().addEdge({ id, source, target, type });
}

/** A 类：edge_removed → 删除边。 */
function applyEdgeRemoved(raw: unknown): void {
  const payload = asRecord(raw);
  if (!payload) return;
  const id = asString(payload.edge_id);
  if (!id) return;
  useGraphStore.getState().removeEdge(id);
}

/** A 类：log_appended → 追加节点实时日志。 */
function applyLogAppended(raw: unknown): void {
  const payload = asRecord(raw);
  if (!payload) return;
  const nodeId = asString(payload.node_id);
  const message = asString(payload.message);
  if (!nodeId || !message) return;
  const rawLevel = payload.level;
  const level =
    typeof rawLevel === 'string' && (Object.values(LogLevel) as string[]).includes(rawLevel)
      ? (rawLevel as LogLevel)
      : LogLevel.INFO;
  const rawTimestamp = payload.ts ?? payload.timestamp;
  const timestamp = typeof rawTimestamp === 'string' ? rawTimestamp : new Date().toISOString();
  const rawId = payload.id;
  const parsedId = typeof rawId === 'number'
    ? rawId
    : typeof rawId === 'string' && rawId.trim()
      ? Number(rawId)
      : NaN;
  useGraphStore.getState().appendNodeLog(nodeId, {
    id: Number.isFinite(parsedId) && parsedId > 0 ? parsedId : undefined,
    node_id: nodeId,
    level,
    message,
    timestamp,
    cc_id: typeof payload.cc_id === 'string' ? payload.cc_id : null,
    phase: typeof payload.phase === 'string' ? payload.phase : null,
    event: typeof payload.event === 'string' ? payload.event : null,
    task: typeof payload.task === 'string' ? payload.task : null,
  });
}

const SESSION_LIFECYCLE_STATES = new Set<SessionLifecycleState>([
  'running', 'pausing', 'paused', 'resuming', 'recovery_required',
  'stopped', 'completed', 'failed',
]);

/** A 类：session_state_changed → 更新会话摘要。 */
function applySessionStateChanged(raw: unknown): void {
  const payload = asRecord(raw);
  if (!payload) return;
  const sessionId = asString(payload.session_id);
  const state = asString(payload.state);
  if (!sessionId || !state || !SESSION_LIFECYCLE_STATES.has(state as SessionLifecycleState)) {
    return;
  }
  const patch: Partial<SessionSummary> = {
    lifecycle_state: state as SessionLifecycleState,
    desired_state:
      typeof payload.desired_state === 'string' ? payload.desired_state : state,
    updated_at:
      typeof payload.updated_at === 'string' ? payload.updated_at : new Date().toISOString(),
  };
  if (payload.pause_mode === 'drain' || payload.pause_mode === 'immediate') {
    patch.pause_mode = payload.pause_mode;
  }
  if (typeof payload.running_count === 'number' && payload.running_count >= 0) {
    patch.running_count = payload.running_count;
  }
  if (typeof payload.restart_safe === 'boolean') patch.restart_safe = payload.restart_safe;
  if (typeof payload.recovery_required === 'boolean') {
    patch.recovery_required = payload.recovery_required;
  }
  if (
    Array.isArray(payload.interrupted_node_ids) &&
    payload.interrupted_node_ids.every((item) => typeof item === 'string')
  ) {
    patch.interrupted_node_ids = payload.interrupted_node_ids as string[];
  }
  if (Number.isInteger(payload.state_version) && (payload.state_version as number) >= 0) {
    patch.state_version = payload.state_version as number;
  }
  useSessionStore.getState().updateSession(sessionId, patch);
}

// 后端 Graph.update_node 字段名 → 前端 GraphNode 字段名
const NODE_FIELD_MAP: ReadonlyMap<string, keyof GraphNode> = new Map([
  ['label', 'label'],
  ['role', 'role'],
  ['task', 'task'],
  ['parent_id', 'parent_id'],
  ['manager_phase', 'manager_phase'],
  ['created_at', 'created_at'],
  ['started_at', 'started_at'],
  ['finished_at', 'finished_at'],
  ['artifact_path', 'artifact_path'],
  ['error', 'error'],
  ['iteration', 'iteration'],
  ['max_iterations', 'max_iterations'],
  ['cc_activity', 'cc_activity'],
  ['dependencies', 'dependencies'],
  ['dependents', 'dependents'],
  ['children', 'children'],
  ['subtree_root', 'subtree_root'],
  ['config', 'config'],
]);
