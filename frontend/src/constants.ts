// ============================================================================
// constants.ts — 全局常量定义（单一事实来源）
// ============================================================================

/** 任务描述最大字符数（前后端需保持一致） */
export const MAX_TASK_LENGTH = 5000;

/** 节点状态颜色映射（7 种状态） */
export const STATUS_COLOR_MAP: Record<string, string> = {
  WAITING: '#94a3b8',
  READY: '#3b82f6',
  RUNNING: '#f59e0b',
  COMPLETED: '#22c55e',
  FAILED: '#ef4444',
  STOPPED: '#6b7280',
  PAUSED: '#8b5cf6',
};
