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
  resolveBrowsePath: vi.fn(),
  revealBrowsePath: vi.fn(),
  uploadBrowseFile: vi.fn(),
  browseDownloadUrl: vi.fn(),
  getGlobalConfig: vi.fn(),
  getRole: vi.fn(),
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

// ---------------------------------------------------------------------------
// Worker 节点 Revise 入口：已完成 Worker 也能增量修订（后端 revise_worker_node）
// ---------------------------------------------------------------------------
describe('NodeInspector Worker Revise 按钮', () => {
  const reviseButton = () => screen.queryByRole('button', { name: /Revise/i });

  function renderWorkerInspector(status: NodeStatus): HTMLButtonElement | null {
    const worker = makeNode('node-w', 'node-1', 'W');
    worker.status = status;
    useGraphStore.setState({ nodes: new Map([['node-w', worker]]) });
    useGraphStore.getState().selectNode('node-w');
    render(
      <I18nProvider>
        <NodeInspector />
      </I18nProvider>,
    );
    return reviseButton();
  }

  it('已完成 Worker 显示 Revise 按钮（不再仅限 Manager）', () => {
    expect(renderWorkerInspector(NodeStatus.Completed)).not.toBeNull();
  });

  it('FAILED / STOPPED / PAUSED Worker 同样显示 Revise 按钮', () => {
    for (const status of [NodeStatus.Failed, NodeStatus.Stopped, NodeStatus.Paused]) {
      cleanup();
      useGraphStore.getState().resetGraph();
      expect(renderWorkerInspector(status)).not.toBeNull();
    }
  });

  it('非终态 Worker（WAITING）不显示 Revise 按钮', () => {
    expect(renderWorkerInspector(NodeStatus.Waiting)).toBeNull();
  });

  it('点击后打开 Worker 专用修订弹窗并提交增量任务', async () => {
    vi.mocked(api.getSessionNode).mockResolvedValue(
      (() => { const n = makeNode('node-w', 'node-1', 'W'); n.status = NodeStatus.Completed; return n; })(),
    );
    renderWorkerInspector(NodeStatus.Completed);

    fireEvent.click(reviseButton() as HTMLButtonElement);
    expect(screen.getByText('Revise worker')).not.toBeNull();

    const textarea = document.querySelector('.inspector-editor textarea') as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: 'add pagination' } });
    fireEvent.click(document.querySelector('.inspector-editor .primary-button') as HTMLButtonElement);

    await waitFor(() => {
      expect(api.reviseSessionNode).toHaveBeenCalledWith('session-1', 'node-w', 'add pagination');
    });
  });
});

// ============================================================================
// 「角色声明了 mcp，而这个 CLI 接不了」的行内提示
//
// 判据落在**后端那句原因原样出现**上，而不是文案长什么样：成因是要随后端变的（pi 今天
// 是「装个扩展就行」，agy / grok 是「AICU 还没接」），前端若自己拼一份，后端改了口径这里
// 不会跟着变。这一组顺带守住 JSX 那根线 —— hook 单测绿了、提示却没接进面板，是完全可能的。
// ============================================================================
describe('NodeInspector 的 MCP 接线提示', () => {
  const PI_REASON = '需要先安装 pi-mcp-adapter 扩展（pi install npm:pi-mcp-adapter）';

  /** 渲染一个选中 node-1 的 Inspector（CLI = pi，角色声明了 mcp 由各用例自己定）。 */
  function openEditorOnPiNode(): void {
    const node = { ...makeNode('node-1', null, 'W'), role: 'dev', config: { cli_name: 'pi' } };
    vi.mocked(api.getSessionNode).mockResolvedValue(node);
    vi.mocked(api.getGlobalConfig).mockResolvedValue({
      clis: [{
        id: 'pi', display_name: 'Pi', installed: true, priority: 0, models: [],
        supports_mcp: false, mcp_unwired_reason: PI_REASON,
      }],
      default_cli: null,
    } as never);
    useGraphStore.setState({ nodes: new Map([['node-1', node]]) });
    useGraphStore.getState().selectNode('node-1');
    render(<I18nProvider><NodeInspector /></I18nProvider>);
    fireEvent.click(screen.getByTitle(t_editNode));
  }

  const t_editNode = 'Edit node'; // 本文件把语言钉在 en（见 beforeEach）

  function warningNotice(): HTMLElement | null {
    return document.querySelector('.form-notice.is-warning');
  }

  it('角色声明了 mcp 且 CLI 接不了 → 给出后端那句原因', async () => {
    vi.mocked(api.getRole).mockResolvedValue(
      { id: 'dev', type: 'worker', effective_config: { mcp: ['echo-probe'] } } as never,
    );

    openEditorOnPiNode();

    await waitFor(() => expect(warningNotice()).not.toBeNull());
    expect(warningNotice()!.textContent).toContain(PI_REASON);
    expect(warningNotice()!.textContent).toContain('Pi');
  });

  it('角色没声明 mcp → 不提示（大多数节点的常态，不该被打扰）', async () => {
    vi.mocked(api.getRole).mockResolvedValue(
      { id: 'dev', type: 'worker', effective_config: {} } as never,
    );

    openEditorOnPiNode();

    await waitFor(() => expect(api.getRole).toHaveBeenCalled());
    expect(warningNotice()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// V3-2.3：节点产出页的「拖入 / 复制路径 / 拖出」
//
// 节点侧与会话共享区共用 WorkspaceEntryRow 与 useWorkspaceDrop，接线在共享区那侧
// 已经测过。这里只测**节点侧独有的两个边界**：
//   1. 落点是 `/nodes/{nid}/results`，不是共享区 —— 传错 scope 会把参考资料塞进
//      共享区，界面上看不出来（两边长得一样），只有请求参数能区分；
//   2. 没有产出目录时**不接受拖入** —— 落到别处就是所见非所得。
// ---------------------------------------------------------------------------
describe('NodeInspector 产出页的文件拖入 / 拖出（V3-2.3）', () => {
  const artifactTab = (): HTMLButtonElement =>
    document.querySelectorAll('.inspector-tabs button')[2] as HTMLButtonElement;

  const FILE_LISTING = {
    path: '.',
    artifact_path: '/tmp/artifacts/node-1',
    artifact_kind: 'directory',
    result_root: 'node-1',
    entries: [{ name: 'report.md', path: 'report.md', type: 'file', size: 20, modified_at: 0 }],
  };

  function openArtifactTab(listing: unknown) {
    vi.mocked(api.listNodeResults).mockResolvedValue(listing as never);
    renderInspectorWithChildAndGetDeleteButton();
    fireEvent.click(artifactTab());
  }

  function makeFileEntry(name: string, size = 4) {
    return {
      isFile: true,
      isDirectory: false,
      name,
      file: (resolve: (file: File) => void) =>
        resolve(new File([new Uint8Array(size)], name, { type: 'text/plain' })),
    };
  }

  function fileDrop(names: string[]) {
    return {
      types: ['Files'],
      dropEffect: '',
      items: names.map((name) => ({ kind: 'file', webkitGetAsEntry: () => makeFileEntry(name) })),
    };
  }

  beforeEach(() => {
    vi.mocked(api.uploadBrowseFile).mockResolvedValue({
      name: 'ref.md', path: 'ref.md', size: 4, renamed: false,
    });
    vi.mocked(api.resolveBrowsePath).mockResolvedValue({
      path: 'report.md', abs_path: '/tmp/artifacts/node-1/report.md',
      name: 'report.md', type: 'file',
    });
    vi.mocked(api.browseDownloadUrl).mockReturnValue('http://localhost/v1/n/download?path=report.md');
    Object.assign(navigator, { clipboard: { writeText: vi.fn(async () => undefined) } });
  });

  it('拖进产出目录 → 落在 results 作用域的这个节点上', async () => {
    openArtifactTab(FILE_LISTING);
    await screen.findByText('report.md');

    fireEvent.drop(document.querySelector('.workspace-list') as HTMLElement, {
      dataTransfer: fileDrop(['ref.md']),
    });

    await waitFor(() => {
      expect(api.uploadBrowseFile).toHaveBeenCalledWith(
        'session-1', 'results', '', 'ref.md', expect.anything(), 'node-1',
      );
    });
  });

  it('上传完重列产出目录', async () => {
    openArtifactTab(FILE_LISTING);
    await screen.findByText('report.md');
    const before = vi.mocked(api.listNodeResults).mock.calls.length;

    fireEvent.drop(document.querySelector('.workspace-list') as HTMLElement, {
      dataTransfer: fileDrop(['ref.md']),
    });

    await waitFor(() => {
      expect(vi.mocked(api.listNodeResults).mock.calls.length).toBeGreaterThan(before);
    });
  });

  it('复制路径走 results 作用域（不是共享区）', async () => {
    openArtifactTab(FILE_LISTING);
    const row = (await screen.findByText('report.md')).closest('.workspace-entry') as HTMLElement;

    fireEvent.click(within(row).getByRole('button', { name: 'Copy path' }));

    await waitFor(() => {
      expect(api.resolveBrowsePath).toHaveBeenCalledWith('session-1', 'results', 'report.md', 'node-1');
    });
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('/tmp/artifacts/node-1/report.md');
  });

  it('文件行拖出设的是 DownloadURL', async () => {
    openArtifactTab(FILE_LISTING);
    const row = (await screen.findByText('report.md')).closest('.workspace-entry') as HTMLElement;
    const setData = vi.fn();

    fireEvent.dragStart(row, { dataTransfer: { setData, effectAllowed: '' } });

    expect(setData).toHaveBeenCalledWith(
      'DownloadURL',
      'application/octet-stream:report.md:http://localhost/v1/n/download?path=report.md',
    );
  });

  it('节点还没产出目录 → 不接受拖入（没有「产出目录」这个落点）', async () => {
    openArtifactTab({
      path: '.', artifact_path: null, artifact_kind: null, result_root: null, entries: [],
    });
    await screen.findByText(/no declared output/i);

    const list = document.querySelector('.workspace-list') as HTMLElement;
    fireEvent.dragOver(list, { dataTransfer: { types: ['Files'], dropEffect: '' } });
    expect(list.className).not.toContain('is-drop-target');

    fireEvent.drop(list, { dataTransfer: fileDrop(['ref.md']) });
    expect(api.uploadBrowseFile).not.toHaveBeenCalled();
  });

  it('产出是单个文件 → 同样不接受拖入（父子落点都不存在）', async () => {
    openArtifactTab({
      path: '.', artifact_path: '/tmp/artifacts/node-1/out.md', artifact_kind: 'file',
      result_root: 'out.md', entries: [],
    });
    // 有产出，所以显示的是「目录为空」而不是「没有声明产出」
    await screen.findByText(/output directory is empty/i);
    // 不能落的话就别提「可以拖进来」——说了却落不下比不说更糟
    expect(document.querySelector('.workspace-drop-hint')).toBeNull();

    fireEvent.drop(document.querySelector('.workspace-list') as HTMLElement, {
      dataTransfer: fileDrop(['ref.md']),
    });
    expect(api.uploadBrowseFile).not.toHaveBeenCalled();
  });

  it('空产出目录 → 提示可以拖进来', async () => {
    openArtifactTab({
      path: '.', artifact_path: '/tmp/artifacts/node-1', artifact_kind: 'directory',
      result_root: 'node-1', entries: [],
    });
    await screen.findByText(/output directory is empty/i);
    expect(document.querySelector('.workspace-drop-hint')).not.toBeNull();
  });
});

