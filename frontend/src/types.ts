// ============================================================================
// types.ts — 核心类型定义
// ============================================================================

import type { Node, Edge } from 'reactflow';

// ============================================================================
// 枚举定义
// ============================================================================

/** 节点执行状态（7 种） */
export enum NodeStatus {
  Waiting = 'Waiting',
  Ready = 'Ready',
  Running = 'Running',
  Completed = 'Completed',
  Failed = 'Failed',
  Stopped = 'Stopped',
  Paused = 'Paused',
}

/** 边类型 */
export enum EdgeType {
  Parent = 'parent',
  Dependency = 'dependency',
}

/** 日志级别 */
export enum LogLevel {
  DEBUG = 'DEBUG',
  INFO = 'INFO',
  WARN = 'WARN',
  ERROR = 'ERROR',
}

/** SSE 事件类型 */
export enum SSEEventType {
  NodeUpdate = 'node_update',
  NodeBatch = 'node_batch',
  LogEvent = 'log_event',
  GraphStats = 'graph_stats',
  GraphCrashed = 'graph_crashed',
  GraphStarted = 'graph_started',
  GraphCompleted = 'graph_completed',
}

/** SSE 客户端连接状态 */
export enum SSEClientStatus {
  Connecting = 'Connecting',
  Connected = 'Connected',
  Disconnected = 'Disconnected',
}

// ============================================================================
// 数据接口
// ============================================================================

/** 图节点（业务数据） */
export interface GraphNode {
  id: string;
  label: string;
  status: NodeStatus;
  exec_type: 'M' | 'W'; // Manager | Worker
  role?: string;
  task?: string;
  config?: NodeConfig;
  parent_id?: string | null;
  subtree_root?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  iteration?: number;
  max_iterations?: number;
  error?: string | null;
  manager_phase?: string | null;
  artifact_path?: string | null;
  cc_activity?: Record<string, CCActivity>;
  dependencies?: string[];
  dependents?: string[];
  children?: string[];
}

export type SessionLifecycleState =
  | 'running'
  | 'pausing'
  | 'paused'
  | 'resuming'
  | 'recovery_required'
  | 'stopped'
  | 'completed'
  | 'failed';

export interface SessionSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  status: NodeStatus;
  lifecycle_state: SessionLifecycleState;
  desired_state: string;
  pause_mode?: 'drain' | 'immediate' | null;
  restart_safe: boolean;
  recovery_required: boolean;
  interrupted_node_ids: string[];
  state_version?: number;
  node_count: number;
  running_count: number;
  completed_count: number;
  failed_count: number;
  root_node_id?: string;
  config?: NodeConfig;
  workspace_root?: string;
  /** 会话「项目目录」：显式配置时为外部项目目录；null/undefined = 未配置，默认 {workspace_root}/shared。 */
  shared_workspace?: string | null;
}

/** 节点和会话的执行配置。API Key 仅保存环境变量名。 */
export interface NodeConfig {
  /** CLI 名称（来自后端 /v1/config 的动态 clis 注册表；缺省表示使用角色/会话默认）。 */
  cli_name?: string;
  model?: string;
  api_base_url?: string;
  api_key_env?: string;
  skip_permissions?: boolean;
  num_ccs?: number;
  skills?: string[];
  /**
   * 会话候选角色池：绑定后该会话的 Manager 只看到池内角色的摘要（严格模式，面板 == 池）。
   * 空数组/缺省 = 未绑定 → 看到全部角色。存的是角色 id 快照，不是池 id。
   */
  candidate_roles?: string[];
  audit_rounds?: number;
  max_iterations?: number;
  [key: string]: unknown;
}

export interface SessionSettings {
  config: NodeConfig;
  workspace_root: string;
  credential_configured: boolean;
}

/** 单个可用终端 Agent CLI 的描述（来自后端 /v1/config）。 */
export interface CliInfo {
  id: string;
  display_name: string;
  installed: boolean;
  priority: number;
  /** 该 CLI 已知支持的模型名清单（来自 cli_models.json；未配置的 CLI 为空数组）。 */
  models: string[];
  /**
   * AICU 是否已为这个 CLI 接入 MCP 下发。**不代表该 CLI 自身支不支持 MCP** ——
   * codex / qwen 这些自己都有，只是注入方式不同、AICU 还没接线。下拉据此标注
   * 「MCP 未接入」，所以措辞不能写成「不支持」。
   */
  supports_mcp: boolean;
  /**
   * ``supports_mcp`` 为假时**为什么**，可直接嵌进「…的 MCP ___」这句话的原因短语；
   * 为真时为 null。与 ``supports_mcp`` 互补，两者同进同退。
   *
   * 由后端下发而不是前端各写一份：成因会变（pi 现在是「装个扩展就行」，agy / grok 是
   * 「AICU 还没接」），前端硬编码的话后端改了口径前端不会跟着变，用户读到的就是一句与
   * 实际不符的话。
   */
  mcp_unwired_reason: string | null;
}

/** 公开 provider 预置参数，不包含 API Key。 */
export interface ProviderPreset {
  id: string;
  display_name: string;
  cli_name?: string;
  model?: string;
  api_base_url?: string;
  api_key_env?: string;
  wire_api?: string;
  models?: string[];
  available?: boolean;
  note?: string;
}

/** 全局（系统级）配置。 */
export interface GlobalConfig {
  workspace_root: string | null;
  cli_name: string | null;
  model: string | null;
  api_base_url: string | null;
  api_key_env: string | null;
  language?: 'zh' | 'en';
  /** 单节点 agent 数量是否由 Manager 动态规划（false = 全部节点使用 default_num_ccs）。 */
  dynamic_num_ccs?: boolean;
  /** dynamic_num_ccs=false 时所有节点统一的 agent 数量，取值 1–4。 */
  default_num_ccs?: number;
}

/** 后端 /v1/config 响应。 */
export interface GlobalConfigResponse {
  config: GlobalConfig;
  clis: CliInfo[];
  provider_presets?: ProviderPreset[];
  default_cli: string | null;
}

/** 后端 /v1/effective-base-url 响应 —— 这次运行真正会用的端点，以及它是怎么定下来的。
 *  - ``configured``：用的就是配置里存着的地址（换模型不会改它）
 *  - ``model_vendor``：AICU 按当前模型所属厂商改写了它（换模型就跟着变）
 *  - ``cli_default``：谁都没指定，CLI 用自己的默认端点（base_url 可能为 null） */
export interface EffectiveBaseUrlResponse {
  base_url: string | null;
  source: 'configured' | 'model_vendor' | 'cli_default';
}

/** 后端 /v1/config/api-key 响应（按模型厂商回显的 sk）。 */
export interface ApiKeyResponse {
  vendor: string | null;
  api_key: string | null;
}

export interface InternalUsernameResponse {
  username: string | null;
  saved?: boolean;
}

export interface RoleField {
  name: string;
  label: string;
  label_en?: string;
  type: 'text' | 'number' | 'boolean' | 'list' | 'multiline';
  default: unknown;
  has_default?: boolean;
  nullable?: boolean;
  /** 字段的详细说明，鼠标悬浮时显示 */
  description?: string;
  description_en?: string;
  /** 输入框占位提示（示例），有值时优先于 default 显示 */
  placeholder?: string;
  placeholder_en?: string;
  /** 是否属于「高级配置」，普通用户无需理解的字段（前端收进折叠区） */
  advanced?: boolean;
}

export interface RoleSchema {
  role_type: 'manager' | 'worker';
  defaults: Record<string, unknown>;
  fields: RoleField[];
}

export interface RoleInfo {
  id: string;
  display_name: string;
  description?: string;
  type?: 'manager' | 'worker';
  content?: string;
  config?: Record<string, unknown>;
  defaults?: Record<string, unknown>;
  effective_config?: Record<string, unknown>;
  schema?: RoleSchema;
}

/**
 * 候选角色池：若干个角色 id 的命名集合，供会话绑定。
 * 会话绑定的是 roles 的快照（写进会话配置的 candidate_roles），因此改池/删池不影响已绑定的会话。
 */
export interface RolePool {
  id: string;
  name: string;
  roles: string[];
}

export interface SkillInfo {
  id: string;
  name: string;
  description?: string;
  size?: number;
  content?: string;
  license?: string | null;
  allowed_tools?: string[];
  metadata?: Record<string, unknown> | null;
  file_count?: number;
  files?: string[];
}

export interface CCActivity {
  cc_id: string;
  status: 'idle' | 'running' | 'completed' | 'failed';
  phase?: string;
  task?: string;
  latest_output?: string;
  started_at?: string;
  finished_at?: string;
  updated_at?: string;
}

export interface WorkspaceEntry {
  name: string;
  path: string;
  type: 'directory' | 'file';
  size: number;
  modified_at: number;
}

export interface WorkspaceListing {
  path: string;
  entries: WorkspaceEntry[];
  artifact_path?: string | null;
  artifact_kind?: 'file' | 'directory' | null;
  result_root?: string | null;
}

export type ResultListing = WorkspaceListing;

export interface WorkspaceFile {
  path: string;
  size: number;
  content: string;
}

// ============================================================================
// Session Outputs（V3-2.2 会话级「查看产出」）
// OutputEntry 与 WorkspaceEntry 分离：Outputs 是「最终产出浏览器」，不是
// Workspace Debugger。Filesystem = 内容事实源。
// ============================================================================

export interface OutputEntry {
  name: string;
  path: string;
  type: 'directory' | 'file';
  size: number;
  modified_at: number;
}

export interface OutputListing {
  path: string;
  entries: OutputEntry[];
  truncated: boolean;
}

export interface OutputFile {
  path: string;
  size: number;
  mime_type: string | null;
  previewable: boolean;
  content: string | null;
  truncated: boolean;
}

/** Agent 事件状态（与 runtime EventMessage.EVENT_STATUS_* 对应） */
export type AgentEventStatus = 'routed' | 'delivered' | 'consumed';

/** 事件路由目标（节点） */
export interface AgentEventTarget {
  node_id: string;
  label: string;
}

/** 事件受影响角色（参考信号） */
export interface AgentEventAffectedRole {
  role: string;
  duty: string;
}

/** Agent 事件日志条目（backend /agent-events 返回） */
export interface AgentEvent {
  event_id: string;
  producer: string;
  producer_label: string;
  objective: string;
  intent: string;
  affected_roles: AgentEventAffectedRole[];
  scope: string;
  action: string;
  created_at: string;
  status: AgentEventStatus;
  routed_to: string[];
  targets: AgentEventTarget[];
}

export interface LinkCandidate {
  id: string;
  label: string;
  task: string;
  status: NodeStatus;
  /** Backend value used to distinguish Manager parent candidates from Workers. */
  exec_type?: 'manager' | 'worker';
  eligible_as_parent: boolean;
  eligible_as_dependency: boolean;
  reachable: boolean;
}

/** 图边（业务数据） */
export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type: EdgeType;
}

/** SSE 事件 */
export interface SSEEvent {
  type: SSEEventType;
  data: unknown;
  timestamp: string;
}

/** 图统计信息 */
export interface GraphStats {
  total: number;
  waiting: number;
  ready: number;
  running: number;
  completed: number;
  failed: number;
  stopped: number;
  paused: number;
}

/** Toast 通知 */
export interface Toast {
  id: string;
  type: 'success' | 'error' | 'info' | 'warning';
  message: string;
  duration?: number;
}

/** 图快照（用于导出） */
export interface GraphSnapshot {
  nodes: GraphNode[];
  edges: GraphEdge[];
  stats: GraphStats;
  created_at: string;
}

// ============================================================================
// API 请求/响应类型
// ============================================================================

export interface CreateManagerRequest {
  task: string;
  role: string;
  config?: NodeConfig;
}

export interface CreateManagerResponse {
  node_id: string;
  status: NodeStatus;
}

export interface UpdateNodeTaskRequest {
  task?: string;
  config?: NodeConfig;
}

export interface SearchLogsRequest {
  node_id?: string;
  level?: LogLevel;
  keyword?: string;
  offset?: number;
  limit?: number;
}

export interface LogEntry {
  /** Server sequence/id when available; null/undefined means client-generated. */
  id?: number | null;
  node_id: string;
  level: LogLevel;
  message: string;
  timestamp: string;
  cc_id?: string | null;
  phase?: string | null;
  event?: string | null;
  task?: string | null;
}

export interface SearchLogsResponse {
  logs: LogEntry[];
  total: number;
}

// ============================================================================
// ReactFlow 节点/边补充类型
// ============================================================================

/** ReactFlow Node 的 data 字段 */
export type RFNodeData = GraphNode & {
  performanceMode?: boolean;
  /** 是否被事件联动高亮（事件的生产/消费节点） */
  highlighted?: boolean;
};

/** ReactFlow Node 类型 */
export type RFNode = Node<RFNodeData>;

/** ReactFlow Edge 类型 */
export type RFEdge = Edge<GraphEdge>;

// ============================================================================
// 工具函数
// ============================================================================

/** 统计节点状态分布 */
export function computeStats(nodes: Map<string, GraphNode>): GraphStats {
  const stats: GraphStats = {
    total: 0,
    waiting: 0,
    ready: 0,
    running: 0,
    completed: 0,
    failed: 0,
    stopped: 0,
    paused: 0,
  };
  stats.total = nodes.size;
  for (const node of nodes.values()) {
    switch (node.status) {
      case NodeStatus.Waiting:
        stats.waiting++;
        break;
      case NodeStatus.Ready:
        stats.ready++;
        break;
      case NodeStatus.Running:
        stats.running++;
        break;
      case NodeStatus.Completed:
        stats.completed++;
        break;
      case NodeStatus.Failed:
        stats.failed++;
        break;
      case NodeStatus.Stopped:
        stats.stopped++;
        break;
      case NodeStatus.Paused:
        stats.paused++;
        break;
    }
  }
  return stats;
}
