// @vitest-environment jsdom
// ============================================================================
// sseEvents.test.ts — SSE 事件 → store 应用逻辑（M7 实时展示核心）
// 纯展示层：把 M6 推送的 A 类状态事件 / C 类 graph_updated 快照应用到
// zustand stores，实现页面节点/边/状态/日志实时更新，无需手动刷新。
// jsdom 环境：SessionStore 模块初始化时读取 localStorage。
// ============================================================================

import { beforeEach, describe, expect, it } from 'vitest';
import { useGraphStore } from '../../stores/GraphStore';
import { useSessionStore } from '../../stores/SessionStore';
import { applySSEFrame } from '../sseEvents';
import { EdgeType, LogLevel, NodeStatus } from '../../types';

function resetStores() {
  useGraphStore.getState().resetGraph();
  useSessionStore.setState({ sessions: [], activeSessionId: null, loading: false, error: null });
}

beforeEach(() => {
  resetStores();
});

describe('applySSEFrame — node_added', () => {
  it('creates a minimal node in the graph store from a sparse payload', () => {
    applySSEFrame({
      event: 'node_added',
      data: JSON.stringify({ node_id: 'n-1', exec_type: 'manager', parent_id: null }),
    });

    const node = useGraphStore.getState().nodes.get('n-1');
    expect(node).toBeDefined();
    expect(node?.id).toBe('n-1');
    expect(node?.exec_type).toBe('M');
    expect(node?.parent_id).toBeNull();
    expect(node?.status).toBeDefined();
  });

  it('ignores payloads without a node id', () => {
    applySSEFrame({ event: 'node_added', data: JSON.stringify({ exec_type: 'worker' }) });
    expect(useGraphStore.getState().nodes.size).toBe(0);
  });
});

describe('applySSEFrame — node_status_changed / node_updated / node_removed', () => {
  it('updates a node status from node_status_changed', () => {
    useGraphStore.getState().addNode({
      id: 'n-1',
      label: 'n-1',
      status: NodeStatus.Waiting,
      exec_type: 'W',
    });

    applySSEFrame({
      event: 'node_status_changed',
      data: JSON.stringify({ node_id: 'n-1', status: 'Running' }),
    });

    expect(useGraphStore.getState().nodes.get('n-1')?.status).toBe(NodeStatus.Running);
  });

  it('merges sparse fields from node_updated into the existing node', () => {
    useGraphStore.getState().addNode({
      id: 'n-1',
      label: 'n-1',
      status: NodeStatus.Waiting,
      exec_type: 'W',
    });

    applySSEFrame({
      event: 'node_updated',
      data: JSON.stringify({ node_id: 'n-1', status: 'Completed', task: 'Write tests', role: 'test' }),
    });

    const node = useGraphStore.getState().nodes.get('n-1');
    expect(node?.status).toBe(NodeStatus.Completed);
    expect(node?.task).toBe('Write tests');
    expect(node?.role).toBe('test');
  });

  it('removes the node (and its edges) from node_removed', () => {
    useGraphStore.getState().addNode({
      id: 'n-1', label: 'n-1', status: NodeStatus.Waiting, exec_type: 'W',
    });
    useGraphStore.getState().addEdge({
      id: 'e-1', source: 'n-1', target: 'n-2', type: EdgeType.Parent,
    });

    applySSEFrame({ event: 'node_removed', data: JSON.stringify({ node_id: 'n-1' }) });

    expect(useGraphStore.getState().nodes.has('n-1')).toBe(false);
    expect(useGraphStore.getState().edges.has('e-1')).toBe(false);
  });

  it('ignores node_updated for an unknown node id', () => {
    applySSEFrame({ event: 'node_updated', data: JSON.stringify({ node_id: 'ghost', status: 'Running' }) });
    expect(useGraphStore.getState().nodes.size).toBe(0);
  });

  // `applyNodeUpdated` 只搬运 NODE_FIELD_MAP 白名单里的 key，漏一个就是「实时 Revise
  // 之后面板要等下次全量快照才更新」——界面看着像没反应。
  it('carries task_history through node_updated (not dropped by the field whitelist)', () => {
    useGraphStore.getState().addNode({
      id: 'n-1', label: 'n-1', status: NodeStatus.Waiting, exec_type: 'W', task: 'old task',
    });

    applySSEFrame({
      event: 'node_updated',
      data: JSON.stringify({
        node_id: 'n-1',
        task: 'incremental task',
        task_history: [{ task: 'old task', at: '2026-09-01T10:00:00', reason: 'revise' }],
      }),
    });

    const node = useGraphStore.getState().nodes.get('n-1');
    expect(node?.task).toBe('incremental task');
    expect(node?.task_history).toEqual([
      { task: 'old task', at: '2026-09-01T10:00:00', reason: 'revise' },
    ]);
  });
});

describe('applySSEFrame — edge_added / edge_removed', () => {
  // M6 事件侧 runtime/graph.py:307 用 str(edge.type) 序列化 (str, Enum) 成员，
  // edge_type 实际 wire 值 = 'EdgeType.PARENT' / 'EdgeType.DEPENDENCY'（快照侧
  // 才是 .value 的 'parent' / 'dependency'）。测试夹具对齐真实契约。
  it('adds a parent edge from a sparse payload', () => {
    applySSEFrame({
      event: 'edge_added',
      data: JSON.stringify({ edge_id: 'e-1', edge_type: 'EdgeType.PARENT', from_node: 'a', to_node: 'b' }),
    });

    const edge = useGraphStore.getState().edges.get('e-1');
    expect(edge).toEqual({ id: 'e-1', source: 'a', target: 'b', type: EdgeType.Parent });
  });

  it('adds a dependency edge (wire form str(EdgeType.DEPENDENCY))', () => {
    applySSEFrame({
      event: 'edge_added',
      data: JSON.stringify({ edge_id: 'e-2', edge_type: 'EdgeType.DEPENDENCY', from_node: 'a', to_node: 'b' }),
    });

    expect(useGraphStore.getState().edges.get('e-2')?.type).toBe(EdgeType.Dependency);
  });

  it('defensively normalizes edge_type dual-form variants to Dependency', () => {
    const variants = ['EdgeType.DEPENDENCY', 'DEPENDENCY', 'dependency'];
    variants.forEach((edge_type, index) => {
      applySSEFrame({
        event: 'edge_added',
        data: JSON.stringify({ edge_id: `e-dep-${index}`, edge_type, from_node: 'a', to_node: 'b' }),
      });
      expect(useGraphStore.getState().edges.get(`e-dep-${index}`)?.type).toBe(EdgeType.Dependency);
    });
  });

  it('defaults unknown edge_type variants to Parent (no event dropped)', () => {
    const variants = ['EdgeType.PARENT', 'PARENT', 'parent', 'weird'];
    variants.forEach((edge_type, index) => {
      applySSEFrame({
        event: 'edge_added',
        data: JSON.stringify({ edge_id: `e-par-${index}`, edge_type, from_node: 'a', to_node: 'b' }),
      });
      expect(useGraphStore.getState().edges.get(`e-par-${index}`)?.type).toBe(EdgeType.Parent);
    });
  });

  it('removes an edge from edge_removed', () => {
    useGraphStore.getState().addEdge({ id: 'e-1', source: 'a', target: 'b', type: EdgeType.Parent });

    applySSEFrame({ event: 'edge_removed', data: JSON.stringify({ edge_id: 'e-1' }) });

    expect(useGraphStore.getState().edges.has('e-1')).toBe(false);
  });
});

describe('applySSEFrame — graph_updated snapshot (C 类)', () => {
  it('applies a full snapshot via batchUpdate', () => {
    applySSEFrame({
      event: 'graph_updated',
      data: JSON.stringify({
        ts: 1,
        summary: { node_count: 1, edge_count: 1 },
        nodes: [
          { id: 'm-1', exec_type: 'manager', status: 'Running', role: 'dev_lead', task: 'T' },
          { id: 'w-1', exec_type: 'worker', status: 'Completed', parent_id: 'm-1', role: 'dev_member' },
        ],
        edges: [
          { id: 'e-1', source: 'm-1', target: 'w-1', type: 'parent' },
          { id: 'e-2', source: 'w-1', target: 'm-1', type: 'dependency' },
        ],
        stats: { total: 2, completed: 1, running: 1 },
      }),
    });

    const state = useGraphStore.getState();
    expect(state.nodes.size).toBe(2);
    expect(state.nodes.get('m-1')?.status).toBe(NodeStatus.Running);
    expect(state.nodes.get('m-1')?.exec_type).toBe('M');
    expect(state.nodes.get('w-1')?.exec_type).toBe('W');
    expect(state.edges.size).toBe(2);
    expect(state.edges.get('e-1')?.type).toBe(EdgeType.Parent);
    expect(state.edges.get('e-2')?.type).toBe(EdgeType.Dependency);
  });

  it('treats the snapshot as authoritative (removes nodes absent from it)', () => {
    useGraphStore.getState().addNode({ id: 'old', label: 'old', status: NodeStatus.Running, exec_type: 'W' });

    applySSEFrame({
      event: 'graph_updated',
      data: JSON.stringify({ nodes: [{ id: 'new', exec_type: 'worker', status: 'Ready' }], edges: [] }),
    });

    const state = useGraphStore.getState();
    expect(state.nodes.has('old')).toBe(false);
    expect(state.nodes.has('new')).toBe(true);
  });
});

describe('applySSEFrame — log_appended', () => {
  it('appends a realtime log entry to the node', () => {
    applySSEFrame({
      event: 'log_appended',
      data: JSON.stringify({
        node_id: 'n-1',
        level: 'ERROR',
        message: 'connection refused',
        ts: '2026-09-06T00:00:00Z',
        cc_id: 'cc-1',
        phase: 'run',
      }),
    });

    const logs = useGraphStore.getState().getNodeLogs('n-1');
    expect(logs).toHaveLength(1);
    expect(logs[0].message).toBe('connection refused');
    expect(logs[0].level).toBe(LogLevel.ERROR);
    expect(logs[0].cc_id).toBe('cc-1');
    expect(logs[0].phase).toBe('run');
  });

  it('defaults unknown log level to INFO and tolerates missing timestamp', () => {
    applySSEFrame({ event: 'log_appended', data: JSON.stringify({ node_id: 'n-1', message: 'hi' }) });

    const logs = useGraphStore.getState().getNodeLogs('n-1');
    expect(logs[0].level).toBe(LogLevel.INFO);
    expect(logs[0].timestamp).toBeDefined();
  });
});

describe('applySSEFrame — session_state_changed', () => {
  it('updates the session summary lifecycle fields', () => {
    useSessionStore.setState({
      sessions: [{
        id: 's-1',
        title: 't',
        created_at: '',
        updated_at: '',
        status: NodeStatus.Ready,
        lifecycle_state: 'running',
        desired_state: 'running',
        restart_safe: true,
        recovery_required: false,
        interrupted_node_ids: [],
        node_count: 0,
        running_count: 0,
        completed_count: 0,
        failed_count: 0,
      } as never],
    });

    applySSEFrame({
      event: 'session_state_changed',
      data: JSON.stringify({
        session_id: 's-1',
        state: 'paused',
        desired_state: 'paused',
        pause_mode: 'drain',
        running_count: 2,
        state_version: 3,
        updated_at: '2026-09-06T00:00:00Z',
      }),
    });

    const session = useSessionStore.getState().sessions.find((s) => s.id === 's-1');
    expect(session?.lifecycle_state).toBe('paused');
    expect(session?.pause_mode).toBe('drain');
    expect(session?.running_count).toBe(2);
    expect(session?.state_version).toBe(3);
  });
});

describe('applySSEFrame — merged frames (M6→M7 消费契约)', () => {
  it('applies every payload of a same-type merged frame (data as array)', () => {
    // 真实流：节点已存在（快照/node_added 建立），随后状态变更以微批合并帧到达。
    useGraphStore.getState().addNode({ id: 'a', label: 'a', status: NodeStatus.Waiting, exec_type: 'W' });
    useGraphStore.getState().addNode({ id: 'b', label: 'b', status: NodeStatus.Waiting, exec_type: 'W' });

    applySSEFrame({
      event: 'node_status_changed',
      data: JSON.stringify([
        { node_id: 'a', status: 'Running' },
        { node_id: 'b', status: 'Completed' },
      ]),
    });

    const state = useGraphStore.getState();
    expect(state.nodes.get('a')?.status).toBe(NodeStatus.Running);
    expect(state.nodes.get('b')?.status).toBe(NodeStatus.Completed);
  });

  it('ignores unknown event types (B 类 scheduler_tick 等不订阅)', () => {
    applySSEFrame({
      event: 'scheduler_tick',
      data: JSON.stringify({ active_count: 3 }),
    });
    expect(useGraphStore.getState().nodes.size).toBe(0);
  });

  it('ignores malformed JSON data instead of throwing', () => {
    expect(() => applySSEFrame({ event: 'node_added', data: '{not json' })).not.toThrow();
    expect(useGraphStore.getState().nodes.size).toBe(0);
  });
});
