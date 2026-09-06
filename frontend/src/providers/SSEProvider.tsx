import React, { useCallback, useEffect, useRef } from 'react';
import { useGraphStore } from '../stores/GraphStore';
import { useSessionStore } from '../stores/SessionStore';
import { useUIStateStore } from '../stores/UIStateStore';
import { SSEClientStatus } from '../types';
import { getGraphSnapshot, getSessionEventsUrl, isMockMode } from '../utils/api';
import { applySSEFrame } from './sseEvents';

// ============================================================================
// SSEProvider — SSE 订阅与页面实时更新（M7 实时展示核心）
//
// 契约（module_division.md §4-D6 / design.md §4.2，M6 为提供方）：
//   - 连接 GET /v1/sessions/{id}/graph/events（首连 → 服务端发 graph_updated
//     快照 + 实时流）
//   - 断线重连：以本地已消费 sequence_id 走 GET .../events/since/{sequence_id}
//     增量续拉；服务端缓冲未命中（空）→ graph_updated 快照兜底
//   - 订阅 A 类 8 状态事件 + C 类 graph_updated；B 类 scheduler_tick 不订阅
//   - 收到事件后直接应用到 zustand stores（节点/边/状态/日志/会话），
//     页面无需手动刷新
// ============================================================================

interface SSEProviderProps {
  children: React.ReactNode;
  sessionId: string;
}

/** 订阅的 A 类 8 状态事件 + C 类 graph_updated 快照（D6 白名单 forward 子集）。 */
const SUBSCRIBED_EVENTS = [
  'node_added',
  'node_updated',
  'node_removed',
  'node_status_changed',
  'edge_added',
  'edge_removed',
  'graph_updated',
  'log_appended',
  'session_state_changed',
];

const INITIAL_RECONNECT_DELAY_MS = 1000;
const MAX_RECONNECT_DELAY_MS = 30000;
/** 会话列表计数（node_count/running_count）后台节流对齐，非手动刷新。 */
const SESSION_REFRESH_THROTTLE_MS = 5000;

const SSEProvider: React.FC<SSEProviderProps> = ({ children, sessionId }) => {
  const batchUpdate = useGraphStore((state) => state.batchUpdate);
  const setClientStatus = useUIStateStore((state) => state.setClientStatus);
  const refreshSessions = useSessionStore((state) => state.refreshSessions);

  const mounted = useRef(true);
  const eventSourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectDelayRef = useRef(INITIAL_RECONNECT_DELAY_MS);
  /** 本地已消费的 sequence_id（SSE 帧 id: 行），断线重连水位。 */
  const lastSequenceRef = useRef<number | null>(null);
  const lastSessionRefreshRef = useRef(0);

  /** 初始 REST 快照暖启动；失败不致命（SSE 首连快照 / 重连快照兜底）。 */
  const refreshGraph = useCallback(async () => {
    try {
      const snapshot = await getGraphSnapshot(sessionId);
      if (mounted.current) batchUpdate(snapshot.nodes, snapshot.edges);
    } catch {
      // SSE 快照会兜底，此处静默。
    }
  }, [batchUpdate, sessionId]);

  const refreshSessionsThrottled = useCallback(() => {
    const now = Date.now();
    if (now - lastSessionRefreshRef.current < SESSION_REFRESH_THROTTLE_MS) return;
    lastSessionRefreshRef.current = now;
    void refreshSessions().catch(() => undefined);
  }, [refreshSessions]);

  const handleMessage = useCallback((event: Event) => {
    const messageEvent = event as MessageEvent<string>;
    // 更新本地已消费 sequence_id（SSE 帧 id: 行）
    const rawId = messageEvent.lastEventId;
    if (rawId && /^\d+$/.test(rawId)) {
      lastSequenceRef.current = Number(rawId);
    }
    // A 类状态事件 / C 类快照 → 直接应用到 stores（无需手动刷新）
    applySSEFrame({ event: messageEvent.type, data: messageEvent.data });
    // 图结构/状态变化会改变会话计数，后台节流对齐会话列表；日志与生命周期
    // 事件已由 applySSEFrame 精确更新，不再额外触发。
    if (messageEvent.type !== 'log_appended' && messageEvent.type !== 'session_state_changed') {
      refreshSessionsThrottled();
    }
  }, [refreshSessionsThrottled]);

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const connect = useCallback((sequenceId: number | null) => {
    if (!mounted.current) return;
    eventSourceRef.current?.close();

    // 断线重连：以本地已消费 sequence_id 走 /since/{seq} 增量续拉；
    // 未消费过（首连/重连前无事件）走主端点，服务端发全量快照。
    const url =
      sequenceId != null && sequenceId > 0
        ? getSessionEventsUrl(sessionId, String(sequenceId))
        : getSessionEventsUrl(sessionId);
    const eventSource = new EventSource(url);
    eventSourceRef.current = eventSource;

    eventSource.onopen = () => {
      if (!mounted.current || eventSourceRef.current !== eventSource) return;
      reconnectDelayRef.current = INITIAL_RECONNECT_DELAY_MS;
      setClientStatus(SSEClientStatus.Connected);
    };

    eventSource.onerror = () => {
      // 已被更新的连接取代（如 session 切换）则忽略。
      if (eventSourceRef.current !== eventSource) return;
      eventSource.close();
      eventSourceRef.current = null;
      if (!mounted.current) return;
      setClientStatus(SSEClientStatus.Connecting);
      // 手动重连（指数退避，封顶 30s）：关闭原生自动重连，走 /since/{seq} 增量续拉。
      const delay = reconnectDelayRef.current;
      reconnectDelayRef.current = Math.min(delay * 2, MAX_RECONNECT_DELAY_MS);
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null;
        connect(lastSequenceRef.current);
      }, delay);
    };

    SUBSCRIBED_EVENTS.forEach((name) => eventSource.addEventListener(name, handleMessage));
  }, [handleMessage, sessionId, setClientStatus]);

  useEffect(() => {
    mounted.current = true;
    clearReconnectTimer();
    refreshGraph();

    if (isMockMode()) {
      // VITE_MOCK_MODE 仅离线 mock（MockProvider 另行驱动 demo 数据）。
      setClientStatus(SSEClientStatus.Connected);
      return () => { mounted.current = false; };
    }

    setClientStatus(SSEClientStatus.Connecting);
    connect(null);

    return () => {
      mounted.current = false;
      clearReconnectTimer();
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
    };
  }, [clearReconnectTimer, connect, refreshGraph, sessionId, setClientStatus]);

  return <>{children}</>;
};

export default SSEProvider;
