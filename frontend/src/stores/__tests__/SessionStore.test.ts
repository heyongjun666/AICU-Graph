// @vitest-environment jsdom
// ============================================================================
// SessionStore 换会话时清视图状态（跨会话 404 的回归测试）
//
// 背景：`activeSessionId` 在这个 store，`selectedNodeId` 在 GraphStore —— 两个 store。
// 只改前者的话 React 会先渲染出一帧「新会话 id + 旧选中节点」；而 effect 是**子先父后**，
// NodeInspector 取节点/取日志的两个 effect 比 App 里那个兜底 effect 先跑，于是拿旧节点 id
// 配新会话 id 各发一轮请求，后端如实回 404。
//
// 所以这三条路径（选择 / 新建 / 删除当前会话）都必须在**写入 activeSessionId 的同一个
// 同步块**里把图清掉，React 才会批成一次渲染、那一帧中间态根本不存在。下面钉的就是这个。
//
// 反向的边界同样重要：id 没变就**不能**清 —— 点侧边栏里已选中的那个会话不会走
// App 的 effect，图被抹掉就再没有东西把它加载回来（首屏白图）。
// ============================================================================

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useSessionStore } from '../SessionStore';
import { useGraphStore } from '../GraphStore';
import { NodeStatus, SessionSummary } from '../../types';

vi.mock('../../utils/api', () => ({
  listSessions: vi.fn(),
  createSession: vi.fn(),
  renameSession: vi.fn(),
  deleteSession: vi.fn(),
}));

import * as api from '../../utils/api';

function makeSession(id: string): SessionSummary {
  return {
    id,
    title: `会话 ${id}`,
    created_at: '',
    updated_at: '',
    status: NodeStatus.Ready,
    node_count: 0,
    running_count: 0,
    completed_count: 0,
    failed_count: 0,
  } as SessionSummary;
}

/** 把图铺成「用户正看着 s-1 里的 n-1」—— 换会话前最普通的状态。 */
function seedGraphWithSelection(): void {
  const node = {
    id: 'n-1',
    label: 'W n-1',
    status: NodeStatus.Completed,
    exec_type: 'W' as const,
    parent_id: null,
    role: 'default',
    task: 'task of n-1',
  };
  useGraphStore.setState({ nodes: new Map([['n-1', node]]) });
  useGraphStore.getState().selectNode('n-1');
}

function seedSessions(activeId: string, ...others: string[]): void {
  useSessionStore.setState({
    sessions: [activeId, ...others].map(makeSession),
    activeSessionId: activeId,
    loading: false,
    error: null,
  });
}

beforeEach(() => {
  localStorage.clear();
  useGraphStore.getState().resetGraph();
  useSessionStore.setState({
    sessions: [], activeSessionId: null, loading: false, error: null,
  });
  vi.clearAllMocks();
});

describe('selectSession —— 切换会话时清掉上个会话的选中节点', () => {
  it('切到别的会话 → 选中节点与图一起清掉（这就是那两发 404 的源头）', () => {
    seedSessions('s-1', 's-2');
    seedGraphWithSelection();

    useSessionStore.getState().selectSession('s-2');

    expect(useSessionStore.getState().activeSessionId).toBe('s-2');
    expect(useGraphStore.getState().selectedNodeId).toBeNull();
    expect(useGraphStore.getState().nodes.size).toBe(0);
  });

  it('切到当前已选中的会话 → 图不动（否则点一下侧边栏就白图，且没人会把它加载回来）', () => {
    seedSessions('s-1', 's-2');
    seedGraphWithSelection();

    useSessionStore.getState().selectSession('s-1');

    expect(useGraphStore.getState().selectedNodeId).toBe('n-1');
    expect(useGraphStore.getState().nodes.size).toBe(1);
  });
});

describe('createSession —— 新会话接管时同样清图', () => {
  it('创建成功 → 切到新会话并清掉旧图', async () => {
    seedSessions('s-1');
    seedGraphWithSelection();
    vi.mocked(api.createSession).mockResolvedValue(makeSession('s-new'));

    await useSessionStore.getState().createSession('新会话');

    expect(useSessionStore.getState().activeSessionId).toBe('s-new');
    expect(useGraphStore.getState().selectedNodeId).toBeNull();
    expect(useGraphStore.getState().nodes.size).toBe(0);
  });

  it('带候选角色 → 写进会话 config.candidate_roles；不带则不发 config', async () => {
    vi.mocked(api.createSession).mockResolvedValue(makeSession('s-new'));

    await useSessionStore.getState().createSession('新会话', ['code', 'test']);
    expect(api.createSession).toHaveBeenLastCalledWith({
      task: '', title: '新会话', config: { candidate_roles: ['code', 'test'] },
    });

    await useSessionStore.getState().createSession('另一个');
    expect(api.createSession).toHaveBeenLastCalledWith({ task: '', title: '另一个' });
  });

  it('创建失败 → 当前会话没变，图不能被白白抹掉', async () => {
    seedSessions('s-1');
    seedGraphWithSelection();
    vi.mocked(api.createSession).mockRejectedValue(new Error('backend down'));

    await expect(useSessionStore.getState().createSession('新会话')).rejects.toThrow('backend down');

    expect(useSessionStore.getState().activeSessionId).toBe('s-1');
    expect(useGraphStore.getState().nodes.size).toBe(1);
  });
});

describe('loadSessions —— 首屏载入时若当前会话已不存在也会换人', () => {
  it('持久化的会话已不在列表里 → 落到列表第一个，并清掉选中节点', async () => {
    seedSessions('s-gone');
    seedGraphWithSelection();
    vi.mocked(api.listSessions).mockResolvedValue([makeSession('s-1')]);

    await useSessionStore.getState().loadSessions();

    expect(useSessionStore.getState().activeSessionId).toBe('s-1');
    expect(useGraphStore.getState().selectedNodeId).toBeNull();
  });

  it('当前会话还在列表里 → 不动，图保持原样', async () => {
    seedSessions('s-1');
    seedGraphWithSelection();
    vi.mocked(api.listSessions).mockResolvedValue([makeSession('s-1'), makeSession('s-2')]);

    await useSessionStore.getState().loadSessions();

    expect(useSessionStore.getState().activeSessionId).toBe('s-1');
    expect(useGraphStore.getState().selectedNodeId).toBe('n-1');
    expect(useGraphStore.getState().nodes.size).toBe(1);
  });
});

describe('deleteSession —— 删掉的正是当前会话时也要清图', () => {
  it('删当前会话 → activeSessionId 落到剩下的那个，旧图清掉', async () => {
    seedSessions('s-1', 's-2');
    seedGraphWithSelection();
    vi.mocked(api.deleteSession).mockResolvedValue(undefined);

    await useSessionStore.getState().deleteSession('s-1');

    expect(useSessionStore.getState().activeSessionId).toBe('s-2');
    expect(useGraphStore.getState().selectedNodeId).toBeNull();
    expect(useGraphStore.getState().nodes.size).toBe(0);
  });

  it('删的是别的会话 → 用户还在看着当前这张图，不能动', async () => {
    seedSessions('s-1', 's-2');
    seedGraphWithSelection();
    vi.mocked(api.deleteSession).mockResolvedValue(undefined);

    await useSessionStore.getState().deleteSession('s-2');

    expect(useSessionStore.getState().activeSessionId).toBe('s-1');
    expect(useGraphStore.getState().selectedNodeId).toBe('n-1');
    expect(useGraphStore.getState().nodes.size).toBe(1);
  });
});
