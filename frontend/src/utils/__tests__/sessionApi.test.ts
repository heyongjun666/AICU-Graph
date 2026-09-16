import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  createDependency,
  createRolePool,
  createSession,
  deleteRolePool,
  getGraphSnapshot,
  getRolePools,
  listSessions,
  listSessionShared,
  readSessionSharedFile,
  updateRolePool,
} from '../api';

const response = (payload: unknown, status = 200) => Promise.resolve({
  ok: status >= 200 && status < 300,
  status,
  json: () => Promise.resolve(payload),
} as Response);

describe('session-scoped API', () => {
  afterEach(() => vi.restoreAllMocks());

  it('lists sessions from the session collection', async () => {
    const fetchMock = vi.fn(() => response({ sessions: [{ id: 's1', title: 'One' }] }));
    vi.stubGlobal('fetch', fetchMock);

    const sessions = await listSessions();

    expect(sessions).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledWith('/v1/sessions', expect.anything());
  });

  it('creates a session with the root task', async () => {
    const fetchMock = vi.fn(() => response({ id: 's1', title: 'Checkout' }, 201));
    vi.stubGlobal('fetch', fetchMock);

    await createSession({ task: 'Build checkout', title: 'Checkout' });

    expect(fetchMock).toHaveBeenCalledWith('/v1/sessions', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ task: 'Build checkout', title: 'Checkout' }),
    }));
  });

  it('loads and maps a graph inside the selected session', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({
      nodes: [{ id: 'n1', exec_type: 'manager', role: 'lead', task: 'Plan', status: 'Running' }],
      edges: [],
    })));

    const graph = await getGraphSnapshot('s1');

    expect(graph.nodes[0].exec_type).toBe('M');
    expect(graph.nodes[0].label).toContain('lead');
  });

  it('getGraphSnapshot surfaces the backend {detail} on a non-2xx response', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: false,
      status: 500,
      json: () => Promise.resolve({ detail: 'graph load failed' }),
    } as Response)));

    await expect(getGraphSnapshot('s1')).rejects.toThrow('graph load failed');
  });

  it('creates dependency edges in the selected session', async () => {
    const fetchMock = vi.fn(() => response({ id: 'e1', from_node: 'a', to_node: 'b', type: 'dependency' }, 201));
    vi.stubGlobal('fetch', fetchMock);

    await createDependency('s1', 'a', 'b');

    expect(fetchMock).toHaveBeenCalledWith('/v1/sessions/s1/edges', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ from_node: 'a', to_node: 'b' }),
    }));
  });

  it('lists session shared outputs at the given relative path', async () => {
    const fetchMock = vi.fn(() => response({
      path: 'backend',
      truncated: false,
      entries: [{
        name: 'api.py',
        path: 'backend/api.py',
        type: 'file',
        size: 17,
        modified_at: 0,
      }],
    }));
    vi.stubGlobal('fetch', fetchMock);

    const listing = await listSessionShared('s1', 'backend');

    expect(fetchMock).toHaveBeenCalledWith('/v1/sessions/s1/shared?path=backend', expect.anything());
    expect(listing.path).toBe('backend');
    expect(listing.entries[0].type).toBe('file');
  });

  it('reads a session shared file preview', async () => {
    const fetchMock = vi.fn(() => response({
      path: 'backend/api.py',
      size: 17,
      mime_type: 'text/x-python',
      previewable: true,
      content: 'def main(): pass\n',
      truncated: false,
    }));
    vi.stubGlobal('fetch', fetchMock);

    const file = await readSessionSharedFile('s1', 'backend/api.py');

    expect(fetchMock).toHaveBeenCalledWith('/v1/sessions/s1/shared/file?path=backend%2Fapi.py', expect.anything());
    expect(file.previewable).toBe(true);
    expect(file.content).toBe('def main(): pass\n');
  });
});

describe('role pool API', () => {
  afterEach(() => vi.restoreAllMocks());

  it('lists pools from the collection wrapper', async () => {
    const fetchMock = vi.fn(() => response({ pools: [{ id: 'p1', name: '博客开发', roles: ['code'] }] }));
    vi.stubGlobal('fetch', fetchMock);

    const pools = await getRolePools();

    expect(pools).toHaveLength(1);
    expect(pools[0].name).toBe('博客开发');
    expect(fetchMock).toHaveBeenCalledWith('/v1/role-pools', expect.anything());
  });

  it('creates a pool with name and roles', async () => {
    const fetchMock = vi.fn(() => response({ id: 'p2', name: '纯编码', roles: ['code'] }, 201));
    vi.stubGlobal('fetch', fetchMock);

    await createRolePool({ name: '纯编码', roles: ['code'] });

    expect(fetchMock).toHaveBeenCalledWith('/v1/role-pools', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ name: '纯编码', roles: ['code'] }),
    }));
  });

  it('patches only the fields it is given', async () => {
    const fetchMock = vi.fn(() => response({ id: 'p2', name: '纯编码', roles: ['test'] }));
    vi.stubGlobal('fetch', fetchMock);

    await updateRolePool('p2', { roles: ['test'] });

    expect(fetchMock).toHaveBeenCalledWith('/v1/role-pools/p2', expect.objectContaining({
      method: 'PATCH',
      body: JSON.stringify({ roles: ['test'] }),
    }));
  });

  it('deletes a pool by id', async () => {
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true, status: 204, json: () => Promise.resolve(null) } as Response));
    vi.stubGlobal('fetch', fetchMock);

    await deleteRolePool('p2');

    expect(fetchMock).toHaveBeenCalledWith('/v1/role-pools/p2', expect.objectContaining({ method: 'DELETE' }));
  });
});
