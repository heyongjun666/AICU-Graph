// @vitest-environment jsdom
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import EventsDialog from '../EventsDialog';
import * as api from '../../utils/api';

vi.mock('../../utils/api', () => ({
  getAgentEvents: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(api.getAgentEvents).mockReset();
});

afterEach(cleanup);

const sampleEvents = [
  {
    event_id: 'evt_aaa111',
    producer: 'node_1',
    producer_label: 'dev_member node_1',
    objective: 'backend 基础层已就绪',
    created_at: '2026-08-17T01:00:00',
    status: 'delivered',
    routed_to: ['node_2', 'node_3'],
    targets: [
      { node_id: 'node_2', label: 'engineering_coord node_2' },
      { node_id: 'node_3', label: 'test node_3' },
    ],
  },
  {
    event_id: 'evt_bbb222',
    producer: 'node_2',
    producer_label: 'engineering_coord node_2',
    objective: '需求已确认',
    created_at: '2026-08-17T01:05:00',
    status: 'consumed',
    routed_to: [],
    targets: [],
  },
];

describe('EventsDialog', () => {
  it('renders event rows with producer, status, targets and content', async () => {
    vi.mocked(api.getAgentEvents).mockResolvedValue(sampleEvents);

    render(<EventsDialog open sessionId="s1" onClose={() => undefined} />);

    expect(await screen.findByText('evt_aaa111')).toBeDefined();
    expect(screen.getByText('dev_member node_1')).toBeDefined();
    expect(screen.getByText('DELIVERED')).toBeDefined();
    // 该标签同时出现在目标 chip 与第二条事件的 producer → 使用 getAllByText
    expect(screen.getAllByText('engineering_coord node_2').length).toBeGreaterThan(0);
    expect(screen.getByText('test node_3')).toBeDefined();
    expect(screen.getByText('backend 基础层已就绪')).toBeDefined();
    expect(api.getAgentEvents).toHaveBeenCalledWith('s1');
  });

  it('renders consumed event with empty-target fallback', async () => {
    vi.mocked(api.getAgentEvents).mockResolvedValue(sampleEvents);

    render(<EventsDialog open sessionId="s1" onClose={() => undefined} />);

    expect(await screen.findByText('CONSUMED')).toBeDefined();
    expect(screen.getByText('（无）')).toBeDefined();
  });

  it('refresh re-fetches the event list', async () => {
    vi.mocked(api.getAgentEvents).mockResolvedValue(sampleEvents);

    render(<EventsDialog open sessionId="s1" onClose={() => undefined} />);

    await screen.findByText('evt_aaa111');
    fireEvent.click(screen.getByText('刷新'));
    await waitFor(() => {
      expect(api.getAgentEvents).toHaveBeenCalledTimes(2);
    });
  });

  it('prompts to select a session when none is active', () => {
    render(<EventsDialog open sessionId={null} onClose={() => undefined} />);
    expect(screen.getByText('选择会话以查看其 Agent 事件。')).toBeDefined();
    expect(api.getAgentEvents).not.toHaveBeenCalled();
  });
});
