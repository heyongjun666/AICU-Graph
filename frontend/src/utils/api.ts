// ============================================================================
// api.ts — API 客户端
// 支持真实请求和 Mock 模式（VITE_MOCK_MODE === 'true'）
// API 基础路径：VITE_API_BASE_URL 或 /v1/
// ============================================================================

import {
  LogEntry,
  GraphNode,
  GraphEdge,
  NodeStatus,
  NodeConfig,
  SessionLifecycleState,
  SessionSummary,
  SessionSettings,
  GlobalConfig,
  GlobalConfigResponse,
  RoleInfo,
  RoleSchema,
  RolePool,
  RolePoolExportPreview,
  RolePoolImportResult,
  BundleConflictPolicy,
  BundleDecisions,
  SkillInfo,
  McpServerInfo,
  McpServerInput,
  WorkspaceListing,
  WorkspaceFile,
  LinkCandidate,
  BrowseScope,
  ResolvedPath,
  UploadResult,
  OutputListing,
  OutputFile,
  AgentEvent,
  ApiKeyResponse,
  EffectiveBaseUrlResponse,
} from '../types';
import { useUIStateStore } from '../stores/UIStateStore';
export type { RoleInfo } from '../types';

// ============================================================================
// 配置
// ============================================================================

const API_BASE = import.meta.env.VITE_API_BASE_URL || '/v1/';
const MOCK_MODE = import.meta.env.VITE_MOCK_MODE === 'true';

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
    // 这个函数是**手搭的对象字面量**，不是 spread：漏一个字段就是界面永远拿不到它。
    // 而且 NodeInspector 渲染的是本地 node state（来自 getSessionNode → 这里），
    // 不是 graph store 里那份 —— 光在后端快照里加是不够的。
    task_history: raw.task_history,
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

export { mapEdgeFromBackend };

// ============================================================================
// API 方法
// ============================================================================

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
 * 获取图快照（多会话前缀）。
 * HTTP 非 2xx 时读取响应体 {detail} 作为错误信息（经 sessionRequest）。
 */
export async function getGraphSnapshot(sessionId: string): Promise<{ nodes: GraphNode[]; edges: GraphEdge[] }> {
  if (MOCK_MODE) {
    return { nodes: [], edges: [] };
  }

  const raw = await sessionRequest<any>(`sessions/${sessionId}/graph`);
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

/**
 * 非 2xx 时抛错：优先用响应体里后端写好的 {detail}，读不出来再退回 `HTTP <status>`。
 * sessionRequest / rawRequest / exportRolePool 共用，错误文案以此为准。
 */
async function throwIfNotOk(response: Response): Promise<void> {
  if (response.ok) return;
  const payload = await response.json().catch(() => null);
  throw new Error(payload?.detail || `HTTP ${response.status}`);
}

async function sessionRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const langHeader = { 'X-Language': useUIStateStore.getState().language };
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: init?.body
      ? { 'Content-Type': 'application/json', ...langHeader, ...init.headers }
      : { ...langHeader, ...init?.headers },
  });
  await throwIfNotOk(response);
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

export function getSessionEventsUrl(sessionId: string, sequenceId?: string): string {
  const suffix = sequenceId ? `/since/${sequenceId}` : '';
  return `${baseUrl}sessions/${sessionId}/graph/events${suffix}`;
}

export function getSessionSettings(sessionId: string): Promise<SessionSettings> {
  return sessionRequest<SessionSettings>(`sessions/${sessionId}/settings`);
}

export function updateSessionSettings(
  sessionId: string,
  config: NodeConfig,
  workspaceRoot?: string,
  sharedWorkspace?: string | null,
): Promise<SessionSummary> {
  // 三态：undefined = 不发送该键（不修改）；字符串 = 设置项目目录；null = 清除（恢复默认）。
  const body: Record<string, unknown> = { config, workspace_root: workspaceRoot };
  if (sharedWorkspace !== undefined) {
    body.shared_workspace = sharedWorkspace;
  }
  return sessionRequest(`sessions/${sessionId}/settings`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export function getGlobalConfig(): Promise<GlobalConfigResponse> {
  return sessionRequest<GlobalConfigResponse>('config');
}

/** 这次运行**真正会用到**的端点，由后端按 (CLI, 模型, 已配置地址) 算出来。
 *
 *  连接表单拿它显示「生效地址」—— 不在这里复刻任何路由规则：地址跟着模型厂商变，
 *  规则在 cli_adapters.effective_base_url 里、与实际发请求用的是同一份。 */
export function getEffectiveBaseUrl(params: {
  cliName?: string | null;
  model?: string | null;
  apiBaseUrl?: string | null;
  providerId?: string | null;
}): Promise<EffectiveBaseUrlResponse> {
  const query = new URLSearchParams();
  if (params.cliName) query.set('cli_name', params.cliName);
  if (params.model) query.set('model', params.model);
  if (params.apiBaseUrl) query.set('api_base_url', params.apiBaseUrl);
  if (params.providerId) query.set('provider_id', params.providerId);
  return sessionRequest<EffectiveBaseUrlResponse>(`effective-base-url?${query.toString()}`);
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
  provider_id?: string | null;
}): Promise<{ ok: boolean; message?: string; result?: string | null }> {
  return sessionRequest('config/test-connection', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

// ---- 凭据（按模型厂商保存的 API Key），明文存储 ----

export function getApiKey(model: string, provider_id?: string | null): Promise<ApiKeyResponse> {
  const query = new URLSearchParams({ model });
  if (provider_id) query.set('provider_id', provider_id);
  return sessionRequest(`config/api-key?${query.toString()}`);
}

export function saveApiKey(model: string, api_key: string, provider_id?: string | null): Promise<ApiKeyResponse> {
  return sessionRequest('config/api-key', {
    method: 'PUT',
    body: JSON.stringify({ model, provider_id, api_key }),
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

// ============================================================================
// 候选角色池 API
//
// 池只在设置页增删改；会话侧只能绑定（手选的角色写进会话配置的 candidate_roles，
// 不落盘为池）。MOCK 分支内联在各函数里（mock 模式下不再有图数据来源，画布为空）。
// ============================================================================

// MOCK 模式下的可变池列表：模块级，保证设置页在 mock 下也能增删改（否则只读不写）。
let mockRolePools: RolePool[] = [
  { id: 'pool-blog', name: '博客开发', roles: ['requirements', 'architecture', 'code', 'test', 'acceptance'] },
  { id: 'pool-code', name: '纯编码', roles: ['code', 'test'] },
];

export async function getRolePools(): Promise<RolePool[]> {
  if (MOCK_MODE) {
    return mockRolePools.map((pool) => ({ ...pool }));
  }
  const payload = await sessionRequest<{ pools: RolePool[] }>('role-pools');
  return payload.pools || [];
}

export function createRolePool(payload: { name: string; roles: string[] }): Promise<RolePool> {
  if (MOCK_MODE) {
    const pool: RolePool = { id: `pool-${Date.now()}`, ...payload };
    mockRolePools = [...mockRolePools, pool];
    return Promise.resolve({ ...pool });
  }
  return sessionRequest('role-pools', { method: 'POST', body: JSON.stringify(payload) });
}

export function updateRolePool(
  poolId: string,
  payload: { name?: string; roles?: string[] },
): Promise<RolePool> {
  if (MOCK_MODE) {
    mockRolePools = mockRolePools.map((pool) =>
      pool.id === poolId ? { ...pool, ...payload } : pool,
    );
    return Promise.resolve({ ...mockRolePools.find((pool) => pool.id === poolId)! });
  }
  return sessionRequest(`role-pools/${poolId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export function deleteRolePool(poolId: string): Promise<void> {
  if (MOCK_MODE) {
    mockRolePools = mockRolePools.filter((pool) => pool.id !== poolId);
    return Promise.resolve();
  }
  return sessionRequest(`role-pools/${poolId}`, { method: 'DELETE' });
}

export function previewRolePoolExport(poolId: string): Promise<RolePoolExportPreview> {
  return sessionRequest(`role-pools/${encodeURIComponent(poolId)}/export/preview`);
}

/** 下载角色池包：fetch → blob → 临时 <a download>（文件名取自 Content-Disposition 的 filename*）。 */
export async function exportRolePool(poolId: string, fallbackName: string): Promise<void> {
  const response = await fetch(`${baseUrl}role-pools/${encodeURIComponent(poolId)}/export`, {
    headers: { 'X-Language': useUIStateStore.getState().language },
  });
  await throwIfNotOk(response);
  const disposition = response.headers.get('Content-Disposition') || '';
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition)?.[1];
  let filename = `${fallbackName}.aicu-pool.zip`;
  if (encoded) {
    try { filename = decodeURIComponent(encoded); } catch { /* 保持兜底文件名 */ }
  }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function importRolePool(
  file: Blob,
  options: { dryRun: boolean; onConflict: BundleConflictPolicy; decisions?: BundleDecisions },
): Promise<RolePoolImportResult> {
  const decide = Object.entries(options.decisions ?? {})
    .map(([key, value]) => `&decide=${encodeURIComponent(`${key}=${value}`)}`)
    .join('');
  return rawRequest<RolePoolImportResult>(
    `role-pools/import?on_conflict=${options.onConflict}&dry_run=${options.dryRun}${decide}`,
    file,
  );
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

export function importSkill(skillId: string, zipFile: File): Promise<SkillInfo> {
  return rawRequest<SkillInfo>(
    `skills/import?skill_id=${encodeURIComponent(skillId)}`,
    zipFile,
  );
}

// ============================================================================
// MCP server 注册表 API
//
// 注册表是全局一份、角色按名字引用（角色 yaml 里只写 `mcp: [echo-probe]`），即仓库根的
// mcp_servers.json。这里只有设置页的增删改查与「测试连接」；怎么把它下发给各家 CLI 是
// 后端的事。与 skills 一样不写 mock 分支 —— 注册表是后端的一个真实文件，mock 下没有它。
// ============================================================================

export async function getMcpServers(): Promise<McpServerInfo[]> {
  const payload = await sessionRequest<{ servers: McpServerInfo[] }>('mcp/servers');
  return payload.servers || [];
}

export function createMcpServer(payload: McpServerInput): Promise<McpServerInfo> {
  return sessionRequest('mcp/servers', { method: 'POST', body: JSON.stringify(payload) });
}

export function updateMcpServer(name: string, payload: McpServerInput): Promise<McpServerInfo> {
  return sessionRequest(`mcp/servers/${name}`, { method: 'PATCH', body: JSON.stringify(payload) });
}

export function deleteMcpServer(name: string): Promise<void> {
  return sessionRequest(`mcp/servers/${name}`, { method: 'DELETE' });
}

/**
 * 连一次这条定义，返回它暴露的工具名；连不上则抛错，错误信息由后端写好（含命令名、
 * 超时、stderr 尾部），前端不重写。
 *
 * 测的是**传进来的值**，不是已保存的那份 —— 改了 command 就该能立刻测，而一个还没保存过
 * 的新 server 根本无从「按名字测」。判据与超时和节点启动时的预检是同一套，所以这里测通了
 * 就意味着节点起得来。
 */
export function probeMcpServer(payload: McpServerInput): Promise<string[]> {
  return sessionRequest<{ tools: string[] }>('mcp/probe', {
    method: 'POST',
    body: JSON.stringify(payload),
  }).then((resp) => resp.tools || []);
}

function workspaceQuery(path: string): string {
  return path ? `?path=${encodeURIComponent(path)}` : '';
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

// ---- 可浏览根：复制路径 / 拖入 / 拖出（V3-2.3） ----
//
// 路径形状与上面那八条只读端点对称：读到 `/shared/file` 就会猜到 `/shared/download`。

function browseBase(sessionId: string, scope: BrowseScope, nodeId?: string): string {
  if (scope === 'shared') return `sessions/${sessionId}/shared`;
  if (!nodeId) throw new Error('scope=results requires nodeId');
  return `sessions/${sessionId}/nodes/${nodeId}/results`;
}

export function resolveBrowsePath(
  sessionId: string,
  scope: BrowseScope,
  path: string,
  nodeId?: string,
): Promise<ResolvedPath> {
  return sessionRequest(`${browseBase(sessionId, scope, nodeId)}/path${workspaceQuery(path)}`);
}

export function revealBrowsePath(
  sessionId: string,
  scope: BrowseScope,
  path: string,
  nodeId?: string,
): Promise<{ revealed: boolean; abs_path: string }> {
  // POST：这是有副作用的端点（会在用户机器上弹 Finder），服务又可能被绑到局域网。
  // GET 会被 <img src> 或链接预取之类的东西触发。
  return sessionRequest(`${browseBase(sessionId, scope, nodeId)}/reveal${workspaceQuery(path)}`, {
    method: 'POST',
  });
}

export function uploadBrowseFile(
  sessionId: string,
  scope: BrowseScope,
  dirPath: string,
  name: string,
  file: Blob,
  nodeId?: string,
): Promise<UploadResult> {
  const query = buildQueryString({ path: dirPath, name });
  // 裸字节，一次一个文件 —— 与 POST /v1/skills/import 同一个范式。
  //
  // **不走 sessionRequest**：它只要看到 body 就无条件盖 Content-Type: application/json
  // （见上面的 sessionRequest）。虽然 init.headers 在展开序里最后、能盖回去，但靠
  // 「谁在后面」取胜太脆 —— JSON 请求和上传字节本来就是两种东西，各走各的。
  return rawRequest(`${browseBase(sessionId, scope, nodeId)}/upload${query}`, file);
}

/** 下载地址。只拼 URL、不发请求 —— 拖出用的 DownloadURL 需要它是**同步**可得的。 */
export function browseDownloadUrl(
  sessionId: string,
  scope: BrowseScope,
  path: string,
  nodeId?: string,
): string {
  const query = buildQueryString({ path });
  const relative = `${baseUrl}${browseBase(sessionId, scope, nodeId)}/download${query}`;
  // 必须是绝对 URL：Chromium 的 DownloadURL 只认 http(s)，给相对串会让 Finder
  // 落一个 .webloc 或者干脆什么都不落。API_BASE 默认是相对的 /v1/，所以要以当前
  // 页面为基准解析一次。
  return new URL(relative, window.location.href).href;
}

async function rawRequest<T>(path: string, body: Blob): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    method: 'POST',
    body,
    headers: {
      'X-Language': useUIStateStore.getState().language,
      'Content-Type': 'application/octet-stream',
    },
  });
  await throwIfNotOk(response);
  return response.json();
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
