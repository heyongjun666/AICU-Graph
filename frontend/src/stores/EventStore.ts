// ============================================================================
// EventStore — Agent 事件 + 高亮状态管理（Zustand）
// 集中管理会话级 Agent 事件（producer → targets），供 GraphCanvas 绘制虚线
// 事件边、NodeInspector「Event」标签页展示每节点事件，以及点击生产/消费节点
// 时的高亮联动。
// ============================================================================

import { create } from 'zustand';
import { AgentEvent } from '../types';
import { getAgentEvents } from '../utils/api';

interface EventState {
  events: AgentEvent[];
  loading: boolean;
  /** 当前被强调的事件（其生产/消费节点与虚线边一起高亮） */
  highlightedEventId: string | null;
  /** 高亮的节点 ID 列表（= 事件的 producer + 全部 targets） */
  highlightedNodeIds: string[];
  loadEvents: (sessionId: string) => Promise<void>;
  clear: () => void;
  highlightEvent: (event: AgentEvent | null) => void;
  clearHighlight: () => void;
}

export const useEventStore = create<EventState>((set) => ({
  events: [],
  loading: false,
  highlightedEventId: null,
  highlightedNodeIds: [],

  loadEvents: async (sessionId) => {
    set({ loading: true });
    try {
      const events = await getAgentEvents(sessionId);
      set({ events, loading: false });
    } catch {
      set({ events: [], loading: false });
    }
  },

  clear: () =>
    set({
      events: [],
      highlightedEventId: null,
      highlightedNodeIds: [],
    }),

  highlightEvent: (event) => {
    if (!event) {
      set({ highlightedEventId: null, highlightedNodeIds: [] });
      return;
    }
    const ids = [event.producer, ...event.targets.map((t) => t.node_id)].filter(
      (id): id is string => Boolean(id),
    );
    set({ highlightedEventId: event.event_id, highlightedNodeIds: ids });
  },

  clearHighlight: () => set({ highlightedEventId: null, highlightedNodeIds: [] }),
}));
