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
      set({ sessions: await listSessions() });
    } catch {
      // The active graph remains usable during a transient summary refresh failure.
    }
  },

  updateSession: (sessionId, patch) => {
    set((state) => ({
      sessions: state.sessions.map((session) =>
        session.id === sessionId ? { ...session, ...patch } : session
      ),
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
