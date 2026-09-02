import React, { useCallback, useEffect, useRef } from 'react';
import { useGraphStore } from '../stores/GraphStore';
import { useSessionStore } from '../stores/SessionStore';
import { useUIStateStore } from '../stores/UIStateStore';
import { LogEntry, LogLevel, SessionLifecycleState, SessionSummary, SSEClientStatus } from '../types';
import { getGraphSnapshot, getSessionEventsUrl, isMockMode } from '../utils/api';

const SESSION_LIFECYCLE_STATES = new Set<SessionLifecycleState>([
  'running', 'pausing', 'paused', 'resuming', 'recovery_required',
  'stopped', 'completed', 'failed',
]);

interface SSEProviderProps {
  children: React.ReactNode;
  sessionId: string;
}

const GRAPH_EVENTS = [
  'node_added',
  'node_updated',
  'node_removed',
  'node_status_changed',
  'edge_added',
  'edge_removed',
  'graph_updated',
];

const SSEProvider: React.FC<SSEProviderProps> = ({ children, sessionId }) => {
  const batchUpdate = useGraphStore((state) => state.batchUpdate);
  const setClientStatus = useUIStateStore((state) => state.setClientStatus);
  const refreshSessions = useSessionStore((state) => state.refreshSessions);
  const refreshTimer = useRef<number | null>(null);
  const mounted = useRef(true);

  const refreshGraph = useCallback(async () => {
    try {
      const snapshot = await getGraphSnapshot(sessionId);
      if (mounted.current) batchUpdate(snapshot.nodes, snapshot.edges);
    } catch {
      if (mounted.current) setClientStatus(SSEClientStatus.Connecting);
    }
  }, [batchUpdate, sessionId, setClientStatus]);

  const appendNodeLog = useGraphStore((state) => state.appendNodeLog);
  const handleLogEvent = useCallback((event: Event) => {
    const messageEvent = event as MessageEvent<string>;
    try {
      const parsed: unknown = JSON.parse(messageEvent.data);
      const payloads = Array.isArray(parsed) ? parsed : [parsed];
      payloads.forEach((rawPayload) => {
        if (!rawPayload || typeof rawPayload !== 'object') return;
        const payload = rawPayload as Record<string, unknown>;
        const nodeId = payload.node_id;
        const message = payload.message;
        if (typeof nodeId !== 'string' || !nodeId || typeof message !== 'string' || !message) return;

        const rawLevel = payload.level;
        const level = typeof rawLevel === 'string' &&
          (Object.values(LogLevel) as string[]).includes(rawLevel)
          ? rawLevel as LogLevel
          : LogLevel.INFO;
        const rawTimestamp = payload.timestamp ?? payload.ts;
        const timestamp = typeof rawTimestamp === 'string'
          ? rawTimestamp
          : new Date().toISOString();
        const rawId = payload.id;
        const parsedId = typeof rawId === 'number'
          ? rawId
          : typeof rawId === 'string' && rawId.trim() ? Number(rawId) : NaN;

        appendNodeLog(nodeId, {
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
      });
    } catch {
      // Ignore malformed events; the next graph refresh remains authoritative.
    }
  }, [appendNodeLog]);

  const updateSession = useSessionStore((state) => state.updateSession);
  const handleSessionState = useCallback((event: Event) => {
    const messageEvent = event as MessageEvent<string>;
    try {
      const rawPayload = JSON.parse(messageEvent.data) as unknown;
      const payloads = (Array.isArray(rawPayload) ? rawPayload : [rawPayload]) as Array<{
        session_id?: string;
        state?: SessionSummary['lifecycle_state'];
        desired_state?: string;
        pause_mode?: SessionSummary['pause_mode'];
        running_count?: number;
        restart_safe?: boolean;
        recovery_required?: boolean;
        interrupted_node_ids?: string[];
        state_version?: number;
        updated_at?: string;
      }>;
      payloads.forEach((payload) => {
        if (
          typeof payload.session_id !== 'string' ||
          typeof payload.state !== 'string' ||
          !SESSION_LIFECYCLE_STATES.has(payload.state) ||
          (payload.desired_state !== undefined && typeof payload.desired_state !== 'string') ||
          (payload.restart_safe !== undefined && typeof payload.restart_safe !== 'boolean') ||
          (payload.recovery_required !== undefined && typeof payload.recovery_required !== 'boolean') ||
          (payload.state_version !== undefined &&
            (!Number.isInteger(payload.state_version) || payload.state_version < 0)) ||
          (payload.updated_at !== undefined && typeof payload.updated_at !== 'string')
        ) return;
        if (
          payload.pause_mode !== undefined &&
          payload.pause_mode !== null &&
          payload.pause_mode !== 'drain' &&
          payload.pause_mode !== 'immediate'
        ) return;
        if (
          payload.running_count !== undefined &&
          (!Number.isFinite(payload.running_count) || payload.running_count < 0)
        ) return;
        if (
          payload.interrupted_node_ids !== undefined &&
          (!Array.isArray(payload.interrupted_node_ids) ||
            payload.interrupted_node_ids.some((id) => typeof id !== 'string'))
        ) return;
        const patch: Partial<SessionSummary> = {
          lifecycle_state: payload.state,
          desired_state: payload.desired_state || payload.state,
          updated_at: payload.updated_at || new Date().toISOString(),
        };
        if (payload.pause_mode !== undefined) patch.pause_mode = payload.pause_mode;
        if (payload.running_count !== undefined) patch.running_count = payload.running_count;
        if (payload.restart_safe !== undefined) patch.restart_safe = payload.restart_safe;
        if (payload.recovery_required !== undefined) patch.recovery_required = payload.recovery_required;
        if (payload.interrupted_node_ids !== undefined) patch.interrupted_node_ids = payload.interrupted_node_ids;
        if (payload.state_version !== undefined) patch.state_version = payload.state_version;
        updateSession(payload.session_id, patch);
      });
    } catch {
      // The next summary refresh reconciles malformed or missed lifecycle events.
    }
  }, [updateSession]);

  useEffect(() => {
    mounted.current = true;
    refreshGraph();
    if (isMockMode()) {
      setClientStatus(SSEClientStatus.Connected);
      return () => { mounted.current = false; };
    }

    setClientStatus(SSEClientStatus.Connecting);
    const eventSource = new EventSource(getSessionEventsUrl(sessionId));
    const scheduleRefresh = () => {
      if (refreshTimer.current !== null) return;
      refreshTimer.current = window.setTimeout(() => {
        refreshTimer.current = null;
        void refreshGraph();
        void refreshSessions().catch(() => undefined);
      }, 80);
    };

    eventSource.onopen = () => setClientStatus(SSEClientStatus.Connected);
    eventSource.onerror = () => setClientStatus(SSEClientStatus.Connecting);
    GRAPH_EVENTS.forEach((eventName) => eventSource.addEventListener(eventName, scheduleRefresh));
    eventSource.addEventListener('log_appended', handleLogEvent);
    eventSource.addEventListener('session_state_changed', handleSessionState);

    return () => {
      mounted.current = false;
      eventSource.close();
      GRAPH_EVENTS.forEach((eventName) => eventSource.removeEventListener(eventName, scheduleRefresh));
      eventSource.removeEventListener('log_appended', handleLogEvent);
      eventSource.removeEventListener('session_state_changed', handleSessionState);
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    };
  }, [handleLogEvent, handleSessionState, refreshGraph, refreshSessions, sessionId, setClientStatus]);

  return <>{children}</>;
};

export default SSEProvider;
