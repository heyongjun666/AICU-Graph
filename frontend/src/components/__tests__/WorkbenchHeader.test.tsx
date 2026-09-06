// @vitest-environment jsdom
// ============================================================================
// 问题 16 扩展回归测试：WorkbenchHeader 会话控制失败时的 header-error 提示生命周期
//
// 复现：后端不可用 / controlSession 返回 4xx → 点击会话控制按钮（如 Stop），
// 顶栏出现 header-error 内联错误。该提示必须满足（与 NodeInspector 主 error 一致）：
//   1) 正常弹出（可见）
//   2) 具备明确关闭入口（关闭按钮可手动关闭）
//   3) 在应消失时机自动消失（定时自动关闭，不无限期挂起）
// 修复前：纯文本渲染、无关闭按钮、无自动消失（常驻页面）。
// ============================================================================

import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import WorkbenchHeader from '../WorkbenchHeader';
import { I18nProvider } from '../../i18n';
import { useUIStateStore } from '../../stores/UIStateStore';
import { GraphNode, NodeStatus, SSEClientStatus } from '../../types';
import * as api from '../../utils/api';

vi.mock('../../utils/api', () => ({
  controlSession: vi.fn(),
}));

function makeSession(): GraphNode & Record<string, unknown> {
  return {
    id: 'session-1',
    title: 'S',
    created_at: '',
    updated_at: '',
    status: NodeStatus.Running,
    lifecycle_state: 'running',
    desired_state: 'running',
    restart_safe: true,
    recovery_required: false,
    interrupted_node_ids: [],
    node_count: 0,
    running_count: 0,
    completed_count: 0,
    failed_count: 0,
  };
}

beforeEach(() => {
  useUIStateStore.getState().reset();
  useUIStateStore.getState().setLanguage('en');
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
  useUIStateStore.getState().reset();
});

function renderHeader() {
  render(
    <I18nProvider>
      <WorkbenchHeader
        session={makeSession() as never}
        clientStatus={SSEClientStatus.Connected}
        onOpenSessions={() => {}}
        onAddNode={() => {}}
        outputsOpen={false}
        onToggleOutputs={() => {}}
        onOpenEvents={() => {}}
        onRefresh={async () => {}}
        terminalOpen={false}
        onToggleTerminal={() => {}}
      />
    </I18nProvider>,
  );
}

function headerError(): HTMLElement | null {
  return document.querySelector('.header-error');
}

describe('WorkbenchHeader 会话控制失败 header-error 提示生命周期（问题16 扩展）', () => {
  it('会话控制失败 → header-error 弹出且具备明确关闭入口（关闭按钮可手动关闭）', async () => {
    vi.mocked(api.controlSession).mockRejectedValue(new Error('session control failed'));
    renderHeader();

    fireEvent.click(screen.getByTitle('Stop session'));
    const textEl = await screen.findByText(/session control failed/);
    const box = textEl.closest('.header-error');
    expect(box).not.toBeNull();
    // 明确关闭入口：与 NodeInspector 主 error 一致，错误框内具备关闭按钮
    const closeButton = box!.querySelector('button');
    expect(closeButton).not.toBeNull();
    fireEvent.click(closeButton as HTMLButtonElement);
    expect(headerError()).toBeNull();
  });

  it('会话控制失败 → header-error 在应消失时机自动消失（不无限期挂起）', async () => {
    vi.mocked(api.controlSession).mockRejectedValue(new Error('session control failed'));
    vi.useFakeTimers();
    renderHeader();

    fireEvent.click(screen.getByTitle('Stop session'));
    // 错误经异步 reject 设置；flush 微任务后 header-error 已弹出
    await act(async () => {});
    expect(headerError()).not.toBeNull();

    // 未到超时时限前仍可见
    act(() => { vi.advanceTimersByTime(2999); });
    expect(headerError()).not.toBeNull();

    // 到达自动关闭时限后消失，不再无限期挂起
    act(() => { vi.advanceTimersByTime(1); });
    expect(headerError()).toBeNull();
  });
});
