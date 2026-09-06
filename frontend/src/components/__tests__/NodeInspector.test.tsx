// @vitest-environment jsdom
// ============================================================================
// 问题 16 回归测试：NodeInspector 的「删除有子节点节点」错误提示生命周期
//
// 复现：选中一个有子节点的节点并点击删除 → 前端拦截（hasChildren），
// 显示报错提示。该提示必须满足：
//   1) 正常弹出（可见）
//   2) 具备明确关闭入口（关闭按钮可手动关闭）
//   3) 在应消失时机自动消失（定时自动关闭，不无限期挂起）
//   4) 不在应关闭后残留（切换节点时清理旧错误）
// ============================================================================

import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import NodeInspector from '../NodeInspector';
import { I18nProvider } from '../../i18n';
import { useGraphStore } from '../../stores/GraphStore';
import { useSessionStore } from '../../stores/SessionStore';
import { useUIStateStore } from '../../stores/UIStateStore';
import { useEventStore } from '../../stores/EventStore';
import { GraphNode, NodeStatus } from '../../types';
import * as api from '../../utils/api';

vi.mock('../../utils/api', () => ({
  getSessionNode: vi.fn(),
  getSessionNodeLogs: vi.fn(),
  updateSessionNode: vi.fn(),
  deleteSessionNode: vi.fn(),
  continueNode: vi.fn(),
  controlSessionNode: vi.fn(),
  reviseSessionNode: vi.fn(),
  listNodeResults: vi.fn(),
  readNodeResultFile: vi.fn(),
  getGlobalConfig: vi.fn(),
}));

function makeNode(id: string, parentId: string | null, execType: 'M' | 'W'): GraphNode {
  return {
    id,
    label: `${execType} ${id}`,
    status: NodeStatus.Waiting,
    exec_type: execType,
    parent_id: parentId,
    role: 'default',
    task: `task of ${id}`,
  };
}

beforeEach(() => {
  useUIStateStore.getState().reset();
  useUIStateStore.getState().setLanguage('en');
  useGraphStore.getState().resetGraph();
  useSessionStore.setState({ activeSessionId: 'session-1' });
  useEventStore.setState({ events: [], loading: false, highlightedEventId: null });

  // 真实 API 模块：删除有子节点时不发请求（前端拦截），其余接口给静默成功。
  vi.mocked(api.getSessionNode).mockResolvedValue(makeNode('node-1', null, 'M'));
  vi.mocked(api.getSessionNodeLogs).mockResolvedValue([]);
  vi.mocked(api.getGlobalConfig).mockResolvedValue({ clis: [], default_cli: null });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
  useUIStateStore.getState().reset();
  useGraphStore.getState().resetGraph();
  useSessionStore.setState({ activeSessionId: null });
});

/**
 * 渲染一个带子节点的 Inspector：node-1（选中，M）有子节点 node-2（W）。
 * 返回删除按钮（.danger-action，无文字仅有图标）。
 */
function renderInspectorWithChildAndGetDeleteButton(): HTMLButtonElement {
  const parent = makeNode('node-1', null, 'M');
  const child = makeNode('node-2', 'node-1', 'W');
  useGraphStore.setState({ nodes: new Map([['node-1', parent], ['node-2', child]]) });
  useGraphStore.getState().selectNode('node-1');
  render(
    <I18nProvider>
      <NodeInspector />
    </I18nProvider>,
  );
  const deleteButton = document.querySelector('.danger-action') as HTMLButtonElement;
  expect(deleteButton).not.toBeNull();
  return deleteButton;
}

function errorBox(): HTMLElement | null {
  return document.querySelector('.inspector-error');
}

describe('NodeInspector 删除有子节点节点的错误提示生命周期', () => {
  it('弹出报错提示并具备明确关闭入口（关闭按钮可手动关闭）', () => {
    const deleteButton = renderInspectorWithChildAndGetDeleteButton();

    fireEvent.click(deleteButton);

    const box = errorBox();
    expect(box).not.toBeNull();
    expect(box!.textContent).toContain('child nodes');

    // 明确关闭入口：错误框内的关闭按钮可手动关闭提示
    const closeButton = within(box!).getByRole('button');
    fireEvent.click(closeButton);
    expect(errorBox()).toBeNull();
  });

  it('报错提示在应消失时机自动消失（不无限期挂起）', () => {
    vi.useFakeTimers();
    const deleteButton = renderInspectorWithChildAndGetDeleteButton();

    fireEvent.click(deleteButton);
    expect(errorBox()).not.toBeNull();

    // 未到超时时限前仍可见
    act(() => { vi.advanceTimersByTime(2999); });
    expect(errorBox()).not.toBeNull();

    // 到达自动关闭时限后消失，不再无限期挂起
    act(() => { vi.advanceTimersByTime(1); });
    expect(errorBox()).toBeNull();
  });

  it('切换节点时清理旧错误提示（不在应关闭后残留）', () => {
    const deleteButton = renderInspectorWithChildAndGetDeleteButton();

    fireEvent.click(deleteButton);
    expect(errorBox()).not.toBeNull();

    // 切到另一个节点，旧错误必须被清掉，不能残留到其它节点
    act(() => { useGraphStore.getState().selectNode('node-2'); });
    expect(errorBox()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 问题16 扩展：Artifact 页 workspaceError（同类 .inspector-error 内联错误）
// 与主 error 共享同一生命周期：明确关闭入口 + 超时自动消失 + 下次成功清理。
// 修复前：纯文本渲染、无关闭按钮、无自动消失（一直挂起）。
// ---------------------------------------------------------------------------
describe('NodeInspector Artifact 页 workspaceError 提示生命周期（问题16 扩展）', () => {
  const artifactTab = (): HTMLButtonElement =>
    document.querySelectorAll('.inspector-tabs button')[2] as HTMLButtonElement;

  it('Artifact 浏览失败 → 错误弹出且具备明确关闭入口（关闭按钮可手动关闭）', async () => {
    vi.mocked(api.listNodeResults).mockRejectedValue(new Error('artifact missing (404)'));
    renderInspectorWithChildAndGetDeleteButton();

    fireEvent.click(artifactTab());
    await screen.findByText(/artifact missing/);

    const box = errorBox();
    expect(box).not.toBeNull();
    // 明确关闭入口：与主 error 一致，错误框内具备关闭按钮
    const closeButton = box!.querySelector('.inspector-error-close');
    expect(closeButton).not.toBeNull();
    fireEvent.click(closeButton as HTMLButtonElement);
    expect(errorBox()).toBeNull();
  });

  it('Artifact 浏览失败 → 在应消失时机自动消失（不无限期挂起）', async () => {
    vi.mocked(api.listNodeResults).mockRejectedValue(new Error('artifact missing (404)'));
    vi.useFakeTimers();
    renderInspectorWithChildAndGetDeleteButton();

    fireEvent.click(artifactTab());
    // 错误经异步 reject 设置；flush 微任务后 workspaceError 已弹出
    await act(async () => {});
    expect(errorBox()).not.toBeNull();

    // 未到超时时限前仍可见
    act(() => { vi.advanceTimersByTime(2999); });
    expect(errorBox()).not.toBeNull();

    // 到达自动关闭时限后消失，不再无限期挂起
    act(() => { vi.advanceTimersByTime(1); });
    expect(errorBox()).toBeNull();
  });

  it('再次成功浏览时清理旧错误（不在应关闭后残留）', async () => {
    vi.mocked(api.listNodeResults).mockRejectedValue(new Error('artifact missing (404)'));
    renderInspectorWithChildAndGetDeleteButton();

    fireEvent.click(artifactTab());
    await screen.findByText(/artifact missing/);
    expect(errorBox()).not.toBeNull();

    // 后端恢复：重新进入 Artifact 页触发成功浏览 → 旧错误被清掉
    vi.mocked(api.listNodeResults).mockResolvedValue({ artifact_path: null, entries: [] });
    fireEvent.click(document.querySelectorAll('.inspector-tabs button')[0] as HTMLButtonElement);
    fireEvent.click(artifactTab());
    await waitFor(() => expect(errorBox()).toBeNull());
  });
});
