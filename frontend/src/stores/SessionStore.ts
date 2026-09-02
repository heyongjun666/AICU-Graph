import { create } from 'zustand';
import { SessionSummary } from '../types';
import {
  createSession as createSessionRequest,
  deleteSession as deleteSessionRequest,
  listSessions,
  renameSession as renameSessionRequest,
} from '../utils/api';

interface SessionState {
  sessions: SessionSummary[];
  activeSessionId: string | null;
  loading: boolean;
  error: string | null;
  loadSessions: () => Promise<void>;
  selectSession: (sessionId: string) => void;
  createSession: (task: string, title?: string) => Promise<SessionSummary>;
  renameSession: (sessionId: string, title: string) => Promise<void>;
  deleteSession: (sessionId: string) => Promise<void>;
  refreshSessions: () => Promise<void>;
  updateSession: (sessionId: string, patch: Partial<SessionSummary>) => void;
}

const ACTIVE_SESSION_KEY = 'graph-workbench.active-session';

export const useSessionStore = create<SessionState>((set, get) => ({
  sessions: [],
  activeSessionId: localStorage.getItem(ACTIVE_SESSION_KEY),
  loading: false,
  error: null,

  loadSessions: async () => {
    set({ loading: true, error: null });
    try {
      const sessions = await listSessions();
      const current = get().activeSessionId;
      const activeSessionId = sessions.some((item) => item.id === current)
        ? current
        : sessions[0]?.id || null;
      if (activeSessionId) localStorage.setItem(ACTIVE_SESSION_KEY, activeSessionId);
      else localStorage.removeItem(ACTIVE_SESSION_KEY);
      set({ sessions, activeSessionId, loading: false });
    } catch (error) {
      set({ loading: false, error: error instanceof Error ? error.message : String(error) });
    }
  },

  refreshSessions: async () => {
    try {
      const incoming = await listSessions();
      set((state) => {
        const currentById = new Map(state.sessions.map((session) => [session.id, session]));
        return {
          sessions: incoming.map((session) => {
            const current = currentById.get(session.id);
            if (
              current?.state_version !== undefined &&
              session.state_version !== undefined &&
              session.state_version < current.state_version
            ) {
              return current;
            }
            return session;
          }),
        };
      });
    } catch (error) {
      // Keep the current summary usable, while allowing action callers to
      // distinguish an accepted command from a failed reconciliation refresh.
      throw error;
    }
  },

  updateSession: (sessionId, patch) => {
    set((state) => ({
      sessions: state.sessions.map((session) => {
        if (session.id !== sessionId) return session;
        // Lifecycle events can arrive out of order after reconnects. A
        // versioned older event must not roll the session back to a prior
        // state; unversioned legacy patches remain mergeable.
        if (
          session.state_version !== undefined &&
          patch.state_version !== undefined &&
          patch.state_version < session.state_version
        ) {
          return session;
        }
        return { ...session, ...patch };
      }),
    }));
  },

  selectSession: (sessionId) => {
    localStorage.setItem(ACTIVE_SESSION_KEY, sessionId);
    set({ activeSessionId: sessionId });
  },

  createSession: async (task, title) => {
    const session = await createSessionRequest({ task, title });
    localStorage.setItem(ACTIVE_SESSION_KEY, session.id);
    set((state) => ({
      sessions: [session, ...state.sessions.filter((item) => item.id !== session.id)],
      activeSessionId: session.id,
      error: null,
    }));
    return session;
  },

  renameSession: async (sessionId, title) => {
    const updated = await renameSessionRequest(sessionId, title);
    set((state) => ({
      sessions: state.sessions.map((item) => item.id === sessionId ? updated : item),
    }));
  },

  deleteSession: async (sessionId) => {
    await deleteSessionRequest(sessionId);
    const remaining = get().sessions.filter((item) => item.id !== sessionId);
    const activeSessionId = get().activeSessionId === sessionId
      ? remaining[0]?.id || null
      : get().activeSessionId;
    if (activeSessionId) localStorage.setItem(ACTIVE_SESSION_KEY, activeSessionId);
    else localStorage.removeItem(ACTIVE_SESSION_KEY);
    set({ sessions: remaining, activeSessionId });
  },
}));
