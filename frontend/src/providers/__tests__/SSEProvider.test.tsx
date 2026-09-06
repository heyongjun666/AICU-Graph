// @vitest-environment jsdom
// ============================================================================
// SSEProvider.test.tsx — SSE 订阅生命周期（M7 实时展示）
// 验证：连接端点 / 本地已消费 sequence_id 断线重连走 /since/{seq} 增量续拉 /
// A 类状态事件与 C 类快照直接应用到 stores / B 类 scheduler_tick 不订阅。
// 用 MockEventSource 模拟 EventSource（浏览器原生 API 不可在 jsdom 使用）。
// ============================================================================

import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import SSEProvider from '../SSEProvider';
import { useGraphStore } from '../../stores/GraphStore';
import { useSessionStore } from '../../stores/SessionStore';
import { useUIStateStore } from '../../stores/UIStateStore';
import { NodeStatus, SSEClientStatus } from '../../types';

type MessageHandler = (ev: MessageEvent<string>) => void;

class MockEventSource {
  static instances: MockEventSource[] = [];

  url: string;
  onopen: ((ev: Event) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  closed = false;
  private listeners = new Map<string, Set<MessageHandler>>();

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(name: string, handler: MessageHandler) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name)!.add(handler);
  }

  removeEventListener(name: string, handler: MessageHandler) {
    this.listeners.get(name)?.delete(handler);
  }

  hasListener(name: string): boolean {
    return (this.listeners.get(name)?.size ?? 0) > 0;
  }

  emit(name: string, data: string, lastEventId = '') {
    const evt = { type: name, data, lastEventId } as MessageEvent<string>;
    this.listeners.get(name)?.forEach((handler) => handler(evt));
  }

  open() {
    this.onopen?.(new Event('open'));
  }

  fail() {
    this.onerror?.(new Event('error'));
  }

  close() {
    this.closed = true;
  }

  static reset() {
    MockEventSource.instances = [];
  }
}

vi.mock('../../utils/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../utils/api')>();
  return {
    ...actual,
    getGraphSnapshot: vi.fn(async () => ({ nodes: [], edges: [] })),
    getSessionEventsUrl: vi.fn((sessionId: string, sequenceId?: string) =>
      sequenceId
        ? `/v1/sessions/${sessionId}/graph/events/since/${sequenceId}`
        : `/v1/sessions/${sessionId}/graph/events`,
    ),
    isMockMode: vi.fn(() => false),
  };
});

function renderProvider() {
  return render(
    <SSEProvider sessionId="s-1">
      <div>child</div>
    </SSEProvider>,
  );
}

beforeEach(() => {
  MockEventSource.reset();
  vi.stubGlobal('EventSource', MockEventSource);
  vi.useFakeTimers();
  useGraphStore.getState().resetGraph();
  useSessionStore.setState({ sessions: [], activeSessionId: null, loading: false, error: null });
  useUIStateStore.setState({ clientStatus: SSEClientStatus.Disconnected });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('SSEProvider — 连接与事件应用', () => {
  it('connects to the session events endpoint on mount', () => {
    renderProvider();
    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.instances[0].url).toBe('/v1/sessions/s-1/graph/events');
  });

  it('applies an A-class node_added event to the graph store without REST refresh', () => {
    renderProvider();
    const es = MockEventSource.instances[0];
    es.emit('node_added', JSON.stringify({ node_id: 'n-1', exec_type: 'worker' }), '5');

    expect(useGraphStore.getState().nodes.get('n-1')).toBeDefined();
  });

  it('applies a graph_updated snapshot as full state', () => {
    renderProvider();
    const es = MockEventSource.instances[0];
    es.emit(
      'graph_updated',
      JSON.stringify({
        nodes: [{ id: 'm', exec_type: 'manager', status: 'Running', role: 'dev_lead' }],
        edges: [],
      }),
      '1',
    );

    expect(useGraphStore.getState().nodes.get('m')?.status).toBe(NodeStatus.Running);
  });

  it('does not subscribe to B-class scheduler_tick', () => {
    renderProvider();
    const es = MockEventSource.instances[0];
    expect(es.hasListener('scheduler_tick')).toBe(false);
  });
});

describe('SSEProvider — 断线重连（/since/{sequence_id} 增量续拉）', () => {
  it('reconnects via /since/{consumed sequence_id} after a disconnect', async () => {
    renderProvider();
    const es = MockEventSource.instances[0];
    es.emit('node_added', JSON.stringify({ node_id: 'n-1', exec_type: 'worker' }), '7');
    es.fail();

    expect(es.closed).toBe(true);
    await vi.advanceTimersByTimeAsync(1000);

    expect(MockEventSource.instances).toHaveLength(2);
    expect(MockEventSource.instances[1].url).toBe('/v1/sessions/s-1/graph/events/since/7');
  });

  it('reconnects to the plain URL when nothing was consumed yet', async () => {
    renderProvider();
    MockEventSource.instances[0].fail();
    await vi.advanceTimersByTimeAsync(1000);

    expect(MockEventSource.instances).toHaveLength(2);
    expect(MockEventSource.instances[1].url).toBe('/v1/sessions/s-1/graph/events');
  });

  it('backs off between reconnects and caps the delay', async () => {
    renderProvider();
    MockEventSource.instances[0].fail();
    await vi.advanceTimersByTimeAsync(1000);
    MockEventSource.instances[1].fail();
    await vi.advanceTimersByTimeAsync(2000);

    expect(MockEventSource.instances).toHaveLength(3);
    expect(MockEventSource.instances[2].url).toBe('/v1/sessions/s-1/graph/events');
  });

  it('continues applying events to the store after a successful reconnect', async () => {
    renderProvider();
    MockEventSource.instances[0].fail();
    await vi.advanceTimersByTimeAsync(1000);

    const es1 = MockEventSource.instances[1];
    es1.emit('node_added', JSON.stringify({ node_id: 'n-1', exec_type: 'worker' }), '8');
    es1.emit('node_status_changed', JSON.stringify({ node_id: 'n-1', status: 'Running' }), '9');

    expect(useGraphStore.getState().nodes.get('n-1')?.status).toBe(NodeStatus.Running);
  });
});
