import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  createDependency,
  createSession,
  getGraphSnapshot,
  listSessions,
  listSessionShared,
  readSessionSharedFile,
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
