import { create } from 'zustand';
import { SessionSummary } from '../types';
import { useGraphStore } from './GraphStore';
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
  createSession: (title: string, candidateRoles?: string[]) => Promise<SessionSummary>;
  renameSession: (sessionId: string, title: string) => Promise<void>;
  deleteSession: (sessionId: string) => Promise<void>;
  refreshSessions: () => Promise<void>;
  updateSession: (sessionId: string, patch: Partial<SessionSummary>) => void;
}

const ACTIVE_SESSION_KEY = 'graph-workbench.active-session';

/**
 * 会话换人的那一刻，顺手把上个会话的视图状态清掉（图 / 选中节点 / 节点日志）。
 *
 * **为什么要在这里清，而不是只靠 App 里那个 `useEffect(..., [activeSessionId])`：**
 *
 *   1. `activeSessionId` 在本 store，`selectedNodeId` 在 GraphStore —— 两个 store。
 *      只改前者的话，React 一定会先渲染出一帧「新会话 id + 旧选中节点」的组合。
 *   2. React 的 effect 是**子先父后**：NodeInspector 里取节点、取日志的两个 effect
 *      都比 App 的 effect 先跑。它们于是拿旧节点 id 配新会话 id 各发一轮请求，
 *      后端如实回 404 —— 那个节点确实不在新会话里，只是前端还没意识到该换人了。
 *
 * 放在这里、而不是四个调用点（侧边栏选择 / 新建 / 删除当前会话 / 首屏载入会话列表）
 * 各加一行，是因为只有和 `activeSessionId` 的写入挤在同一个同步块里，React 才会把两次
 * 更新批成一次渲染 —— 那一帧中间态根本不存在。App 里那个 effect 保留为兜底。
 *
 * `loadSessions` 是第四条：它只在 App 挂载时跑一次（`App.tsx:67`，依赖是稳定引用），
 * 那会儿 `selectedNodeId` 还没被持久化过、必然是 null，所以今天走不到这个 bug；
 * 但它确实能把 `activeSessionId` 从「已不存在的那个」换成 `sessions[0]`，
 * 将来谁在会话中途再调一次就会原样重现。一并走这里，成本是一行。
 *
 * id 没变就不清：点侧边栏里已经选中的那个会话时 `activeSessionId` 不变，App 的
 * effect 也不会跑；这里若无条件清空，图会被抹掉且没有任何东西会再把它加载回来。
 */
function resetGraphIfSessionChanged(currentId: string | null, nextId: string | null): void {
  if (currentId !== nextId) useGraphStore.getState().resetGraph();
}

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
      resetGraphIfSessionChanged(current, activeSessionId);
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
    resetGraphIfSessionChanged(get().activeSessionId, sessionId);
    localStorage.setItem(ACTIVE_SESSION_KEY, sessionId);
    set({ activeSessionId: sessionId });
  },

  createSession: async (title: string, candidateRoles?: string[]) => {
    // 新会话生成空白图：task 传空串（后端不再自动创建根节点），由用户后续创建节点。
    const session = await createSessionRequest({
      task: '',
      title,
      ...(candidateRoles?.length ? { config: { candidate_roles: candidateRoles } } : {}),
    });
    // 新会话接管 activeSessionId，同样要先清掉上一个会话的选中节点，理由见上面那个函数。
    // 放在 await 之后：请求失败时 activeSessionId 并没有变，不该把当前的图白白抹掉。
    resetGraphIfSessionChanged(get().activeSessionId, session.id);
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
    // 删掉的是当前会话时，activeSessionId 会落到别的会话上 —— 与 selectSession 同一条路径，
    // 同样要先清选中节点。删的是别的会话时两边相等，图不动（用户还在看它）。
    resetGraphIfSessionChanged(get().activeSessionId, activeSessionId);
    set({ sessions: remaining, activeSessionId });
  },
}));
