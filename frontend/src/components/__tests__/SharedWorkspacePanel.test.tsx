// @vitest-environment jsdom
// ============================================================================
// SharedWorkspacePanel —— 复制路径 / 拖入 / 拖出
//
// 这个组件此前零测试。加这轮功能时补上，理由是**落点是这个功能唯一会错的地方**：
// 拖到空白处要落当前目录、拖到目录行上要落那个子目录 —— 两者都只体现在传给
// uploadBrowseFile 的 `dirPath` 参数上，浏览器里肉眼很难分辨，只能在这里钉住。
//
// 纯逻辑（DataTransfer 的坑、分批读目录、并发）已经在 fileTransfer.test.ts 里测过，
// 这里只管**接线**：组件的哪个手势接到了哪个参数上。
// ============================================================================

import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import SharedWorkspacePanel from '../SharedWorkspacePanel';
import { I18nProvider } from '../../i18n';
import { useUIStateStore } from '../../stores/UIStateStore';
import * as api from '../../utils/api';

vi.mock('../../utils/api', () => ({
  listSessionShared: vi.fn(),
  readSessionSharedFile: vi.fn(),
  resolveBrowsePath: vi.fn(),
  revealBrowsePath: vi.fn(),
  uploadBrowseFile: vi.fn(),
  browseDownloadUrl: vi.fn(),
}));

const SESSION = 'session-1';

const LISTING = {
  path: '',
  truncated: false,
  entries: [
    { name: 'refs', path: 'refs', type: 'directory' as const, size: 0, modified_at: 0 },
    { name: 'a.txt', path: 'a.txt', type: 'file' as const, size: 12, modified_at: 0 },
  ],
};

/** 一个「拖文件」的 dataTransfer。`types` 决定它是不是文件拖拽。 */
function fileDrop(entries: any[] = [makeFileEntry('dropped.txt')], types = ['Files']) {
  return {
    types,
    dropEffect: '',
    items: entries.map((entry) => ({
      kind: 'file',
      webkitGetAsEntry: () => entry,
    })),
  };
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

function renderPanel(sessionId = SESSION) {
  return render(
    <I18nProvider>
      <SharedWorkspacePanel
        sessionId={sessionId}
        open
        width={320}
        onToggle={() => undefined}
        onResize={() => undefined}
      />
    </I18nProvider>,
  );
}

beforeEach(() => {
  useUIStateStore.getState().reset();
  useUIStateStore.getState().setLanguage('en');
  vi.mocked(api.listSessionShared).mockResolvedValue(LISTING);
  vi.mocked(api.resolveBrowsePath).mockResolvedValue({
    path: 'a.txt', abs_path: '/tmp/shared/a.txt', name: 'a.txt', type: 'file',
  });
  vi.mocked(api.uploadBrowseFile).mockResolvedValue({
    name: 'dropped.txt', path: 'dropped.txt', size: 4, renamed: false,
  });
  vi.mocked(api.browseDownloadUrl).mockReturnValue('http://localhost/v1/x/download?path=a.txt');
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn(async () => undefined) },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useUIStateStore.getState().reset();
});

describe('SharedWorkspacePanel —— 列表与复制路径', () => {
  it('列出当前目录的条目', async () => {
    renderPanel();
    expect(await screen.findByText('a.txt')).toBeTruthy();
    expect(screen.getByText('refs')).toBeTruthy();
    expect(api.listSessionShared).toHaveBeenCalledWith(SESSION, '');
  });

  it('点行上的复制按钮，写进剪贴板的是后端给的绝对路径', async () => {
    renderPanel();
    const row = (await screen.findByText('a.txt')).closest('.workspace-entry') as HTMLElement;
    // 必须 scope 到行内：面包屑尾部也有一个同名的「复制路径」按钮。
    fireEvent.click(within(row).getByRole('button', { name: 'Copy path' }));

    await waitFor(() => {
      expect(api.resolveBrowsePath).toHaveBeenCalledWith(SESSION, 'shared', 'a.txt', undefined);
    });
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('/tmp/shared/a.txt');
  });

  it('「在访达中显示」调 reveal，而不是自己拼路径', async () => {
    renderPanel();
    const row = (await screen.findByText('a.txt')).closest('.workspace-entry') as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: 'Reveal in Finder' }));

    await waitFor(() => {
      expect(api.revealBrowsePath).toHaveBeenCalledWith(SESSION, 'shared', 'a.txt', undefined);
    });
  });

  it('面包屑尾部的复制按钮复制的是**当前目录**，不是某个条目', async () => {
    renderPanel();
    await screen.findByText('a.txt');
    const button = document.querySelector('.workspace-breadcrumb-action') as HTMLElement;
    fireEvent.click(button);

    await waitFor(() => {
      expect(api.resolveBrowsePath).toHaveBeenCalledWith(SESSION, 'shared', '', undefined);
    });
  });
});

describe('SharedWorkspacePanel —— 拖入', () => {
  it('拖到空白处 → 落在当前浏览目录', async () => {
    renderPanel();
    await screen.findByText('a.txt');
    const list = document.querySelector('.workspace-list') as HTMLElement;

    fireEvent.drop(list, { dataTransfer: fileDrop() });

    await waitFor(() => {
      expect(api.uploadBrowseFile).toHaveBeenCalledWith(
        SESSION, 'shared', '', 'dropped.txt', expect.anything(), undefined,
      );
    });
  });

  it('拖到目录行上 → 落在那个子目录，而不是当前目录', async () => {
    renderPanel();
    await screen.findByText('refs');
    const row = screen.getByText('refs').closest('.workspace-entry') as HTMLElement;

    fireEvent.drop(row, { dataTransfer: fileDrop() });

    await waitFor(() => {
      expect(api.uploadBrowseFile).toHaveBeenCalledWith(
        SESSION, 'shared', 'refs', 'dropped.txt', expect.anything(), undefined,
      );
    });
  });

  it('上传完重列当前目录 —— 否则刚拖进去的东西在界面上看不见', async () => {
    renderPanel();
    await screen.findByText('a.txt');
    const callsBefore = vi.mocked(api.listSessionShared).mock.calls.length;
    fireEvent.drop(document.querySelector('.workspace-list') as HTMLElement, {
      dataTransfer: fileDrop(),
    });

    await waitFor(() => {
      expect(vi.mocked(api.listSessionShared).mock.calls.length).toBeGreaterThan(callsBefore);
    });
  });

  it('拖文字 / 拖内部元素时不点亮落点（types 里没有 Files）', async () => {
    renderPanel();
    await screen.findByText('a.txt');
    const list = document.querySelector('.workspace-list') as HTMLElement;

    fireEvent.dragOver(list, { dataTransfer: { types: ['text/plain'], dropEffect: '' } });
    expect(list.className).not.toContain('is-drop-target');

    fireEvent.dragOver(list, { dataTransfer: { types: ['Files'], dropEffect: '' } });
    expect(list.className).toContain('is-drop-target');
  });

  it('一个文件失败不影响其余，且失败的会留在面板上（toast 会消失）', async () => {
    vi.mocked(api.uploadBrowseFile)
      .mockRejectedValueOnce(new Error('文件超出上限'))
      .mockResolvedValue({ name: 'ok.txt', path: 'ok.txt', size: 4, renamed: false });
    renderPanel();
    await screen.findByText('a.txt');

    fireEvent.drop(document.querySelector('.workspace-list') as HTMLElement, {
      dataTransfer: fileDrop([makeFileEntry('bad.txt'), makeFileEntry('ok.txt')]),
    });

    expect(await screen.findByText(/bad\.txt/)).toBeTruthy();
    expect(vi.mocked(api.uploadBrowseFile)).toHaveBeenCalledTimes(2);
  });

  it('切换会话后，旧上传完成不刷新新会话的共享区', async () => {
    let finishUpload!: (result: { name: string; path: string; size: number; renamed: boolean }) => void;
    vi.mocked(api.uploadBrowseFile).mockImplementation(() => new Promise((resolve) => {
      finishUpload = resolve;
    }));
    const view = renderPanel('session-1');
    await screen.findByText('a.txt');
    fireEvent.drop(document.querySelector('.workspace-list') as HTMLElement, {
      dataTransfer: fileDrop(),
    });
    await waitFor(() => expect(api.uploadBrowseFile).toHaveBeenCalled());

    view.rerender(
      <I18nProvider>
        <SharedWorkspacePanel
          sessionId="session-2"
          open
          width={320}
          onToggle={() => undefined}
          onResize={() => undefined}
        />
      </I18nProvider>,
    );
    await waitFor(() => expect(api.listSessionShared).toHaveBeenCalledWith('session-2', ''));
    const callsAfterSwitch = vi.mocked(api.listSessionShared).mock.calls.length;
    finishUpload({ name: 'dropped.txt', path: 'dropped.txt', size: 4, renamed: false });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(vi.mocked(api.listSessionShared).mock.calls.length).toBe(callsAfterSwitch);
  });
});

describe('SharedWorkspacePanel —— 拖出', () => {
  it('文件行 dragStart 设的是 DownloadURL:mime:名字:绝对URL', async () => {
    renderPanel();
    const row = (await screen.findByText('a.txt')).closest('.workspace-entry') as HTMLElement;
    const setData = vi.fn();

    fireEvent.dragStart(row, { dataTransfer: { setData, effectAllowed: '' } });

    expect(setData).toHaveBeenCalledWith(
      'DownloadURL',
      'application/octet-stream:a.txt:http://localhost/v1/x/download?path=a.txt',
    );
    // 目标程序不认 DownloadURL 时，至少还能拿到一个链接
    expect(setData).toHaveBeenCalledWith('text/uri-list', 'http://localhost/v1/x/download?path=a.txt');
  });

  it('目录行不可拖出 —— DownloadURL 只能带文件', async () => {
    renderPanel();
    const row = (await screen.findByText('refs')).closest('.workspace-entry') as HTMLElement;

    expect(row.getAttribute('draggable')).toBe('false');
  });
});
