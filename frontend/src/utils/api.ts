// ============================================================================
// api.ts — API 客户端
// 支持真实请求和 Mock 模式（VITE_MOCK_MODE === 'true'）
// API 基础路径：VITE_API_BASE_URL 或 /v1/
// ============================================================================

import {
  CreateManagerRequest,
  CreateManagerResponse,
  UpdateNodeTaskRequest,
  SearchLogsRequest,
  SearchLogsResponse,
  LogEntry,
  LogLevel,
  GraphNode,
  GraphEdge,
  NodeStatus,
  SessionLifecycleState,
  SessionSummary,
  SessionSettings,
  GlobalConfig,
  GlobalConfigResponse,
  RoleInfo,
  RoleSchema,
  SkillInfo,
  WorkspaceListing,
  WorkspaceFile,
  LinkCandidate,
  NodeConfig,
  OutputListing,
  OutputFile,
  AgentEvent,
  ApiKeyResponse,
} from '../types';
import { useUIStateStore } from '../stores/UIStateStore';
export type { RoleInfo } from '../types';

// ============================================================================
// 配置
// ============================================================================

const API_BASE = (import.meta as any).env?.VITE_API_BASE_URL || '/v1/';
const MOCK_MODE = (import.meta as any).env?.VITE_MOCK_MODE === 'true';

// 确保 API_BASE 以 / 结尾
const baseUrl = API_BASE.endsWith('/') ? API_BASE : `${API_BASE}/`;

// ============================================================================
// 工具函数
// ============================================================================

function buildQueryString(params: Record<string, string | number | undefined | null>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') {
      parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
    }
  }
  return parts.length > 0 ? `?${parts.join('&')}` : '';
}

// ============================================================================
// Mock 实现
// ============================================================================

let mockLogIdCounter = 100;
const mockLogs: LogEntry[] = [];

function generateMockLogs(nodeId: string): LogEntry[] {
  const logs: LogEntry[] = [];
  const levels = [LogLevel.INFO, LogLevel.DEBUG, LogLevel.WARN, LogLevel.ERROR];
  const messages = [
    'Task initialized',
    'Processing step 1 of 3',
    'Processing step 2 of 3',
    'Processing step 3 of 3',
    'Allocating resources',
    'Validating configuration',
    'Executing subtask',
    'Merging results',
    'Task completed successfully',
    'Connection timeout, retrying...',
    'Memory usage: 45%',
    'Disk I/O in progress',
  ];

  const baseTime = Date.now() - 3600000;
  for (let i = 0; i < 20; i++) {
    mockLogIdCounter++;
    logs.push({
      id: mockLogIdCounter,
      node_id: nodeId,
      level: levels[Math.floor(Math.random() * levels.length)],
      message: messages[Math.floor(Math.random() * messages.length)],
      timestamp: new Date(baseTime + i * 30000).toISOString(),
    });
  }
  return logs;
}

// ============================================================================
// 后端 → 前端字段映射（REST 响应）
// 后端 exec_type 用 "manager"/"worker"，前端用 "M"/"W"
// ============================================================================

export function mapNodeFromBackend(raw: any): GraphNode {
  const execType = raw.exec_type;
  let feExecType: 'M' | 'W' = 'W';
  if (execType === 'manager' || execType === 'M') feExecType = 'M';
  else if (execType === 'worker' || execType === 'W') feExecType = 'W';
  const id = String(raw.id ?? raw.node_id ?? '');
  const error = raw.error === null || raw.error === undefined
    ? raw.error
    : typeof raw.error === 'string'
      ? raw.error
      : raw.error.message;
  return {
    id,
    label: raw.label || `${raw.role || feExecType} ${id.slice(0, 8)}`,
    status: raw.status,
    exec_type: feExecType,
    role: raw.role,
    task: raw.task,
    config: raw.config,
    parent_id: raw.parent_id,
    manager_phase: raw.manager_phase,
    created_at: raw.created_at,
    started_at: raw.started_at,
    finished_at: raw.finished_at ?? raw.ended_at,
    artifact_path: raw.artifact_path,
    cc_activity: raw.cc_activity,
    error,
    iteration: raw.iteration ?? raw.iteration_count,
    max_iterations: raw.max_iterations,
    dependencies: raw.dependencies,
    dependents: raw.dependents,
    children: raw.children,
  } as GraphNode;
}

function mapEdgeFromBackend(raw: any): GraphEdge {
  return {
    id: raw.id,
    source: raw.source ?? raw.from_node,
    target: raw.target ?? raw.to_node,
    type: raw.type ?? 'parent',
  } as GraphEdge;
}

// ============================================================================
// API 方法
// ============================================================================

/**
 * 搜索日志（多会话前缀）。后端日志按节点归档：必须提供 node_id，
 * level/keyword 过滤在客户端完成（后端仅支持 offset/limit）。
 */
export async function searchLogs(
  sessionId: string,
  params: SearchLogsRequest,
): Promise<SearchLogsResponse> {
  if (MOCK_MODE) {
    // 生成 mock 日志
    const logs = generateMockLogs(params.node_id || 'mock-node');
    const filtered = logs.filter((log) => {
      if (params.level && log.level !== params.level) return false;
      if (params.keyword && !log.message.includes(params.keyword)) return false;
      if (params.node_id && log.node_id !== params.node_id) return false;
      return true;
    });
    const offset = params.offset || 0;
    const limit = params.limit || 50;
    return {
      logs: filtered.slice(offset, offset + limit),
      total: filtered.length,
    };
  }

  if (!params.node_id) return { logs: [], total: 0 };
  const query = buildQueryString({ offset: params.offset, limit: params.limit });
  const response = await fetch(
    `${baseUrl}sessions/${sessionId}/nodes/${encodeURIComponent(params.node_id)}/logs${query}`,
  );
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const entries: LogEntry[] = await response.json();
  const filtered = entries.filter((log) => {
    if (params.level && log.level !== params.level) return false;
    if (params.keyword && !log.message.includes(params.keyword)) return false;
    return true;
  });
  const offset = params.offset || 0;
  const limit = params.limit || 50;
  return {
    logs: filtered.slice(offset, offset + limit),
    total: filtered.length,
  };
}

/**
 * 获取节点详情（多会话前缀）。
 */
export async function getNode(sessionId: string, nodeId: string): Promise<GraphNode> {
  if (MOCK_MODE) {
    return {
      id: nodeId,
      label: `Node ${nodeId}`,
      status: NodeStatus.Running,
      exec_type: 'M',
      role: 'developer',
      task: 'Implement feature',
    };
  }

  const response = await fetch(`${baseUrl}sessions/${sessionId}/nodes/${nodeId}`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const raw = await response.json();
  return mapNodeFromBackend(raw);
}

/**
 * 更新节点任务（多会话前缀）。等价于 updateSessionNode，保留旧方法名兼容。
 */
export async function updateNodeTask(
  sessionId: string,
  nodeId: string,
  data: UpdateNodeTaskRequest,
): Promise<void> {
  if (MOCK_MODE) return;

  await updateSessionNode(sessionId, nodeId, data as Record<string, unknown>);
}

/**
 * 继续执行任意 FAILED/STOPPED 节点（Manager/Worker 通用）。
 * 保留已有日志和产出，Manager 有子节点时锁定 max_iterations 防止 re-expand。
 */
export async function continueNode(sessionId: string, nodeId: string): Promise<void> {
  if (MOCK_MODE) return;

  const response = await fetch(`${baseUrl}sessions/${sessionId}/nodes/${nodeId}/continue`, {
    method: 'POST',
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
}

/**
 * 创建 Manager 节点（提交任务，多会话前缀）。等价于 createSessionNode(exec_type='manager')。
 */
export async function createManager(
  sessionId: string,
  data: CreateManagerRequest,
): Promise<CreateManagerResponse> {
  if (MOCK_MODE) {
    return {
      node_id: `manager-${Date.now()}`,
      status: NodeStatus.Ready,
    };
  }

  const node = await createSessionNode(sessionId, {
    task: data.task,
    role: data.role,
    exec_type: 'manager',
    config: data.config,
  });
  return { node_id: node.id, status: node.status };
}

/**
 * 暂停图（多会话前缀）。等价于 controlSession(sessionId, 'pause')。
 */
export async function pauseGraph(sessionId: string): Promise<void> {
  if (MOCK_MODE) return;

  await controlSession(sessionId, 'pause');
}

/**
 * 恢复图（多会话前缀）。等价于 controlSession(sessionId, 'resume')。
 */
export async function resumeGraph(sessionId: string): Promise<void> {
  if (MOCK_MODE) return;

  await controlSession(sessionId, 'resume');
}

/**
 * 停止图（多会话前缀）。等价于 controlSession(sessionId, 'stop')。
 */
export async function stopGraph(sessionId: string): Promise<void> {
  if (MOCK_MODE) {
    // Mock 模式下只清空
    return;
  }

  await controlSession(sessionId, 'stop');
}

/**
 * 清除图（多会话前缀）。
 */
export async function clearGraph(sessionId: string): Promise<void> {
  if (MOCK_MODE) return;

  await sessionRequest(`sessions/${sessionId}/graph`, { method: 'DELETE' });
}

/**
 * 获取图快照（多会话前缀）
 */
export async function getGraphSnapshot(sessionId: string): Promise<{ nodes: GraphNode[]; edges: GraphEdge[] }> {
  if (MOCK_MODE) {
    return { nodes: [], edges: [] };
  }

  const response = await fetch(`${baseUrl}sessions/${sessionId}/graph`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const raw = await response.json();
  return {
    nodes: (raw.nodes || []).map(mapNodeFromBackend),
    edges: (raw.edges || []).map(mapEdgeFromBackend),
  };
}

// ============================================================================
// 角色列表 API
// ============================================================================

export async function getRoles(): Promise<RoleInfo[]> {
  if (MOCK_MODE) {
    return [
      { id: 'dev_lead', display_name: 'Dev Lead', description: '技术负责人', type: 'manager' },
      { id: 'dev_member', display_name: 'Dev Member', description: '开发成员', type: 'worker' },
      { id: 'acceptance', display_name: 'Acceptance', description: '验收专家', type: 'worker' },
      { id: 'architecture', display_name: 'Architecture', description: '架构师', type: 'worker' },
      { id: 'test', display_name: 'Test', description: 'QA工程师', type: 'worker' },
    ];
  }

  const response = await fetch(`${baseUrl}roles`);
  if (!response.ok) throw new Error(`Failed to fetch roles: HTTP ${response.status}`);
  const payload = await response.json();
  return payload.roles || [];
}

/**
 * 判断当前是否为 Mock 模式
 */
export function isMockMode(): boolean {
  return MOCK_MODE;
}

async function sessionRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const langHeader = { 'X-Language': useUIStateStore.getState().language };
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: init?.body
      ? { 'Content-Type': 'application/json', ...langHeader, ...init.headers }
      : { ...langHeader, ...init?.headers },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail || `HTTP ${response.status}`);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

const SESSION_LIFECYCLE_STATES = new Set<SessionLifecycleState>([
  'running', 'pausing', 'paused', 'resuming', 'recovery_required',
  'stopped', 'completed', 'failed',
]);

function isSessionLifecycleState(value: unknown): value is SessionLifecycleState {
  return typeof value === 'string' && SESSION_LIFECYCLE_STATES.has(value as SessionLifecycleState);
}

function normalizeSessionSummary(session: SessionSummary): SessionSummary {
  const legacyState = session.status === NodeStatus.Completed
    ? 'completed'
    : session.status === NodeStatus.Failed
      ? 'failed'
      : session.status === NodeStatus.Stopped
        ? 'stopped'
        : session.status === NodeStatus.Paused
          ? 'paused'
          : 'running';
  const lifecycleState = isSessionLifecycleState(session.lifecycle_state)
    ? session.lifecycle_state
    : legacyState;
  return {
    ...session,
    lifecycle_state: lifecycleState,
    desired_state: session.desired_state || lifecycleState,
    restart_safe: session.restart_safe ?? lifecycleState !== 'running',
    recovery_required: session.recovery_required ?? false,
    interrupted_node_ids: session.interrupted_node_ids || [],
  };
}

export async function listSessions(): Promise<SessionSummary[]> {
  const payload = await sessionRequest<{ sessions: SessionSummary[] }>('sessions');
  return payload.sessions.map(normalizeSessionSummary);
}

export function createSession(payload: {
  task: string;
  title?: string;
  role?: string;
  config?: Record<string, unknown>;
}): Promise<SessionSummary> {
  return sessionRequest<SessionSummary>('sessions', { method: 'POST', body: JSON.stringify(payload) })
    .then(normalizeSessionSummary);
}

export function renameSession(sessionId: string, title: string): Promise<SessionSummary> {
  return sessionRequest<SessionSummary>(`sessions/${sessionId}`, {
    method: 'PATCH',
    body: JSON.stringify({ title }),
  }).then(normalizeSessionSummary);
}

export function deleteSession(sessionId: string): Promise<void> {
  return sessionRequest(`sessions/${sessionId}`, { method: 'DELETE' });
}

export function controlSession(
  sessionId: string,
  action: 'pause' | 'resume' | 'stop',
  payload?: { mode?: 'drain' | 'immediate' },
): Promise<SessionSummary> {
  return sessionRequest<SessionSummary>(`sessions/${sessionId}/graph/${action}`, {
    method: 'POST',
    ...(payload ? { body: JSON.stringify(payload) } : {}),
  }).then(normalizeSessionSummary);
}

export async function getSessionNode(sessionId: string, nodeId: string): Promise<GraphNode> {
  const raw = await sessionRequest<any>(`sessions/${sessionId}/nodes/${nodeId}`);
  return mapNodeFromBackend(raw);
}

export async function createSessionNode(
  sessionId: string,
  payload: {
    task: string;
    role: string;
    exec_type: 'manager' | 'worker';
    parent_id?: string | null;
    dependencies?: string[];
    config?: Record<string, unknown>;
  },
): Promise<GraphNode> {
  const raw = await sessionRequest<any>(`sessions/${sessionId}/nodes`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  return mapNodeFromBackend(raw);
}

export async function updateSessionNode(
  sessionId: string,
  nodeId: string,
  payload: Record<string, unknown>,
): Promise<GraphNode> {
  const raw = await sessionRequest<any>(`sessions/${sessionId}/nodes/${nodeId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
  return mapNodeFromBackend(raw);
}

export function deleteSessionNode(
  sessionId: string,
  nodeId: string,
): Promise<void> {
  // 删除保护：后端不再支持级联删除（cascade 参数已移除），仅允许删除叶子节点；
  // Manager 有子节点时后端返回 409，由调用方（NodeInspector）先行拦截提示。
  return sessionRequest(`sessions/${sessionId}/nodes/${nodeId}`, {
    method: 'DELETE',
  });
}

export function controlSessionNode(
  sessionId: string,
  nodeId: string,
  action: 'pause' | 'resume' | 'stop',
): Promise<{ status: string; node_id: string }> {
  return sessionRequest(`sessions/${sessionId}/nodes/${nodeId}/${action}`, { method: 'POST' });
}

/**
 * 增量续跑已完成 Manager 节点（Revise）。
 * 复用既有子节点（保留产物与 Agent 会话），仅新能力处产出新节点。
 */
export function reviseSessionNode(
  sessionId: string,
  nodeId: string,
  task: string,
  config: Record<string, unknown> = {},
): Promise<{ status: string; node_id: string }> {
  return sessionRequest(`sessions/${sessionId}/nodes/${nodeId}/revise`, {
    method: 'POST',
    body: JSON.stringify({ task, config }),
  });
}

export function createDependency(
  sessionId: string,
  fromNode: string,
  toNode: string,
): Promise<GraphEdge> {
  return sessionRequest(`sessions/${sessionId}/edges`, {
    method: 'POST',
    body: JSON.stringify({ from_node: fromNode, to_node: toNode }),
  });
}

export function deleteDependency(sessionId: string, edgeId: string): Promise<void> {
  return sessionRequest(`sessions/${sessionId}/edges/${edgeId}`, { method: 'DELETE' });
}

export function getSessionNodeLogs(sessionId: string, nodeId: string): Promise<LogEntry[]> {
  return sessionRequest(`sessions/${sessionId}/nodes/${nodeId}/logs?limit=500`);
}

export function getSessionArtifact(
  sessionId: string,
  nodeId: string,
): Promise<{ path: string | null; content: string | null }> {
  return sessionRequest(`sessions/${sessionId}/nodes/${nodeId}/artifact`);
}

export function getSessionEventsUrl(sessionId: string, sequenceId?: string): string {
  const suffix = sequenceId ? `/since/${sequenceId}` : '';
  return `${baseUrl}sessions/${sessionId}/graph/events${suffix}`;
}

export function getSessionSettings(sessionId: string): Promise<SessionSettings> {
  return sessionRequest(`sessions/${sessionId}/settings`);
}

export function updateSessionSettings(
  sessionId: string,
  config: NodeConfig,
  workspaceRoot?: string,
): Promise<SessionSummary> {
  return sessionRequest(`sessions/${sessionId}/settings`, {
    method: 'PATCH',
    body: JSON.stringify({ config, workspace_root: workspaceRoot }),
  });
}

export function getGlobalConfig(): Promise<GlobalConfigResponse> {
  return sessionRequest<GlobalConfigResponse>('config');
}

export function updateGlobalConfig(
  config: Partial<GlobalConfig>,
): Promise<{ config: GlobalConfig }> {
  return sessionRequest('config', {
    method: 'PATCH',
    body: JSON.stringify({ config }),
  });
}

export function testConnection(payload: {
  cli_name?: string | null;
  model?: string | null;
  api_base_url?: string | null;
  api_key_env?: string | null;
}): Promise<{ ok: boolean; message?: string; result?: string | null }> {
  return sessionRequest('config/test-connection', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

// ---- 凭据（按模型厂商保存的 API Key），明文存储 ----

export function getApiKey(model: string): Promise<ApiKeyResponse> {
  return sessionRequest(`config/api-key?model=${encodeURIComponent(model)}`);
}

export function saveApiKey(model: string, api_key: string): Promise<ApiKeyResponse> {
  return sessionRequest('config/api-key', {
    method: 'PUT',
    body: JSON.stringify({ model, api_key }),
  });
}

export function deleteApiKey(vendor: string): Promise<void> {
  return sessionRequest(`config/api-key?vendor=${encodeURIComponent(vendor)}`, {
    method: 'DELETE',
  });
}

export function getRole(roleType: RoleInfo['type'], roleId: string): Promise<RoleInfo> {
  return sessionRequest(`roles/${roleType}/${roleId}`);
}

export function getRoleSchema(roleType: NonNullable<RoleInfo['type']>): Promise<RoleSchema> {
  return sessionRequest(`roles/schema/${roleType}`);
}

export function createRole(payload: {
  role_id: string;
  role_type: RoleInfo['type'];
  content?: string;
  config?: Record<string, unknown>;
}): Promise<RoleInfo> {
  return sessionRequest('roles', { method: 'POST', body: JSON.stringify(payload) });
}

export function updateRole(payload: {
  role_id: string;
  role_type: RoleInfo['type'];
  content?: string;
  config?: Record<string, unknown>;
}): Promise<RoleInfo> {
  return sessionRequest(`roles/${payload.role_type}/${payload.role_id}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export function deleteRole(roleType: NonNullable<RoleInfo['type']>, roleId: string): Promise<void> {
  return sessionRequest(`roles/${roleType}/${roleId}`, { method: 'DELETE' });
}

export async function getSkills(): Promise<SkillInfo[]> {
  const payload = await sessionRequest<{ skills: SkillInfo[] }>('skills');
  return payload.skills;
}

export function getSkill(skillId: string): Promise<SkillInfo> {
  return sessionRequest(`skills/${skillId}`);
}

export function createSkill(payload: { skill_id: string; content: string }): Promise<SkillInfo> {
  return sessionRequest('skills', { method: 'POST', body: JSON.stringify(payload) });
}

export function updateSkill(payload: { skill_id: string; content: string }): Promise<SkillInfo> {
  return sessionRequest(`skills/${payload.skill_id}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export function deleteSkill(skillId: string): Promise<void> {
  return sessionRequest(`skills/${skillId}`, { method: 'DELETE' });
}

export async function importSkill(skillId: string, zipFile: File): Promise<SkillInfo> {
  const response = await fetch(
    `${baseUrl}skills/import?skill_id=${encodeURIComponent(skillId)}`,
    { method: 'POST', body: zipFile, headers: { 'X-Language': useUIStateStore.getState().language } },
  );
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail || `HTTP ${response.status}`);
  }
  return response.json();
}

function workspaceQuery(path: string): string {
  return path ? `?path=${encodeURIComponent(path)}` : '';
}

export function listSessionWorkspace(sessionId: string, path = ''): Promise<WorkspaceListing> {
  return sessionRequest(`sessions/${sessionId}/workspace${workspaceQuery(path)}`);
}

export function listNodeWorkspace(
  sessionId: string,
  nodeId: string,
  path = '',
): Promise<WorkspaceListing> {
  return sessionRequest(`sessions/${sessionId}/nodes/${nodeId}/workspace${workspaceQuery(path)}`);
}

export function readNodeWorkspaceFile(
  sessionId: string,
  nodeId: string,
  path: string,
): Promise<WorkspaceFile> {
  return sessionRequest(
    `sessions/${sessionId}/nodes/${nodeId}/workspace/file${workspaceQuery(path)}`,
  );
}

export function listNodeResults(sessionId: string, nodeId: string, path = ''): Promise<WorkspaceListing> {
  return sessionRequest(`sessions/${sessionId}/nodes/${nodeId}/results${workspaceQuery(path)}`);
}

export function readNodeResultFile(sessionId: string, nodeId: string, path: string): Promise<WorkspaceFile> {
  return sessionRequest(`sessions/${sessionId}/nodes/${nodeId}/results/file${workspaceQuery(path)}`);
}

// ---- Session Outputs：会话级「查看产出」（V3-2.2） ----

export function listSessionShared(sessionId: string, path = ''): Promise<OutputListing> {
  return sessionRequest(`sessions/${sessionId}/shared${workspaceQuery(path)}`);
}

export function readSessionSharedFile(sessionId: string, path: string): Promise<OutputFile> {
  return sessionRequest(`sessions/${sessionId}/shared/file${workspaceQuery(path)}`);
}

export async function getLinkCandidates(
  sessionId: string,
  nodeId?: string,
): Promise<LinkCandidate[]> {
  const query = nodeId ? `?node_id=${encodeURIComponent(nodeId)}` : '';
  const payload = await sessionRequest<{ candidates: LinkCandidate[] }>(
    `sessions/${sessionId}/graph/link-candidates${query}`,
  );
  return payload.candidates;
}

// ---- Agent 事件日志（方案三 Router 路由记录 + 生命周期状态） ----

export async function getAgentEvents(sessionId: string): Promise<AgentEvent[]> {
  if (MOCK_MODE) return [];
  const payload = await sessionRequest<{ events: AgentEvent[] }>(
    `sessions/${sessionId}/agent-events`,
  );
  return payload.events || [];
}
