// ============================================================================
// GraphStore.test.ts — 图数据 store 的边操作（SSE edge_added/edge_removed 支撑）
// ============================================================================

import { beforeEach, describe, expect, it } from 'vitest';
import { useGraphStore } from '../GraphStore';
import { EdgeType, NodeStatus } from '../../types';

beforeEach(() => {
  useGraphStore.getState().resetGraph();
});

describe('GraphStore edge actions', () => {
  it('addEdge inserts a single edge', () => {
    useGraphStore.getState().addEdge({
      id: 'e-1',
      source: 'a',
      target: 'b',
      type: EdgeType.Dependency,
    });

    expect(useGraphStore.getState().edges.get('e-1')).toEqual({
      id: 'e-1',
      source: 'a',
      target: 'b',
      type: EdgeType.Dependency,
    });
  });

  it('removeEdge deletes a single edge', () => {
    useGraphStore.getState().addEdge({ id: 'e-1', source: 'a', target: 'b', type: EdgeType.Parent });
    useGraphStore.getState().removeEdge('e-1');

    expect(useGraphStore.getState().edges.has('e-1')).toBe(false);
  });

  it('removeEdge is a no-op for an unknown id', () => {
    useGraphStore.getState().addEdge({ id: 'e-1', source: 'a', target: 'b', type: EdgeType.Parent });
    useGraphStore.getState().removeEdge('missing');

    expect(useGraphStore.getState().edges.size).toBe(1);
  });

  it('removeNode also removes edges touching the node', () => {
    useGraphStore.getState().addNode({ id: 'a', label: 'a', status: NodeStatus.Waiting, exec_type: 'W' });
    useGraphStore.getState().addEdge({ id: 'e-1', source: 'a', target: 'b', type: EdgeType.Parent });
    useGraphStore.getState().removeNode('a');

    expect(useGraphStore.getState().edges.has('e-1')).toBe(false);
  });
});
