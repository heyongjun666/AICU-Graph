// @vitest-environment jsdom
// ============================================================================
// fileTransfer.test.ts — 拖入展开 / 上传编排 / 拖出载荷
// ============================================================================
//
// 这个模块的价值全在「把 DataTransfer 的坑一次踩完」。所以这里的假 entry 树是**故意
// 按真实浏览器的怪癖造的**：readEntries 分批返回、item 在事件处理器返回后失效。
// 用顺手的假实现（一次读完、item 永不过期）测，等于什么都没测。

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../utils/api', () => ({
  uploadBrowseFile: vi.fn(),
  resolveBrowsePath: vi.fn(),
  revealBrowsePath: vi.fn(),
}));

import { resolveBrowsePath, uploadBrowseFile } from '../api';
import {
  basenameOf,
  collectDroppedFiles,
  copyPathToClipboard,
  dirnameOf,
  downloadUrlPayload,
  isFileDrag,
  joinPath,
  uploadFiles,
  writeClipboard,
} from '../fileTransfer';

const makeFile = (name: string, size = 4) =>
  new File([new Uint8Array(size)], name, { type: 'text/plain' });

/** 造一个真的 File 对象给 entry.file() 回调 —— jsdom 的 File 够用。 */
function fileEntry(name: string, size = 4) {
  return {
    isFile: true,
    isDirectory: false,
    name,
    file: (resolve: (file: File) => void) => resolve(makeFile(name, size)),
  };
}

/**
 * 目录 entry。`batches` 是 readEntries **依次**返回的批次 —— 每个目录都必须在返回
 * 空数组之前被反复调用。真实的 Chromium 每次最多回 100 条，所以造一个「两批非空、
 * 第三批才空」的目录是常规情况，不是极端情况。
 */
function dirEntry(name: string, batches: any[][]) {
  let call = 0;
  return {
    isDirectory: true,
    isFile: false,
    name,
    createReader: () => ({
      readEntries: (resolve: (entries: any[]) => void) => {
        resolve(batches[call] ?? []);
        call += 1;
      },
    }),
    __readCount: () => call,
  };
}

function makeItems(entries: any[], options: { dropItemsAfterUse?: boolean } = {}) {
  let consumed = false;
  const items = entries.map((entry) => ({
    kind: 'file',
    webkitGetAsEntry: () => {
      if (options.dropItemsAfterUse && consumed) return null;
      return entry;
    },
    getAsFile: () => makeFile(entry.name),
  }));
  return {
    items,
    /** 模拟「事件处理器返回后 DataTransferItemList 失效」。 */
    consume: () => { consumed = true; },
  };
}

/** 换掉 navigator.clipboard；传 undefined 模拟「非安全上下文里它压根不存在」。 */
function setClipboard(value: unknown) {
  if (value === undefined) delete (navigator as any).clipboard;
  else Object.assign(navigator, { clipboard: value });
}

/** jsdom 不实现 execCommand，得自己造，并记录被复制出去的文本。 */
function stubExecCommand(result = true) {
  const calls: string[] = [];
  (document as any).execCommand = vi.fn(() => {
    // 复制的内容从当时挂在 DOM 上那个 textarea 读
    const area = document.querySelector('textarea');
    calls.push(area ? (area as HTMLTextAreaElement).value : '');
    return result;
  });
  return calls;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('路径小工具', () => {
  it('joinPath 跳过空段与 . 段，并把重复斜杠压平', () => {
    expect(joinPath('refs', '', 'a.txt')).toBe('refs/a.txt');
    expect(joinPath('.', 'a.txt')).toBe('a.txt');
    expect(joinPath('', 'a.txt')).toBe('a.txt');
    expect(joinPath('refs/', '/deep/', 'a.txt')).toBe('refs/deep/a.txt');
  });

  it('dirname / basename 只切最后一段', () => {
    expect(dirnameOf('a/b/c.txt')).toBe('a/b');
    expect(basenameOf('a/b/c.txt')).toBe('c.txt');
    expect(dirnameOf('c.txt')).toBe('');
    expect(basenameOf('c.txt')).toBe('c.txt');
  });
});

describe('isFileDrag', () => {
  it('只有拖文件才算，拖选中文字 / 内部元素不算', () => {
    // 这条是防劫持的：NodeInspector 里 reactflow 的节点拖动也是 HTML5 DnD，
    // 不门禁的话两边会互相点亮对方的落点提示。
    expect(isFileDrag({ types: ['Files'] } as any)).toBe(true);
    expect(isFileDrag({ types: ['text/plain'] } as any)).toBe(false);
    expect(isFileDrag(null)).toBe(false);
    expect(isFileDrag(undefined)).toBe(false);
  });
});

describe('collectDroppedFiles', () => {
  it('平铺的文件直接用 getAsFile 的退化路径', async () => {
    const { items } = makeItems([
      { kind: 'file', name: 'a.txt', webkitGetAsEntry: () => null },
    ]);
    // 没有 webkitGetAsEntry 的项走另一条分支
    (items[0] as any).webkitGetAsEntry = () => null;
    const { files } = await collectDroppedFiles(items as any);
    expect(files.map((f) => f.relPath)).toEqual(['a.txt']);
  });

  it('递归展开文件夹，保留顶层文件夹名，用 / 分隔', async () => {
    const tree = dirEntry('docs', [
      [fileEntry('readme.md'), dirEntry('sub', [[fileEntry('deep.txt')], []])],
      [],
    ]);
    const { items } = makeItems([tree]);
    const { files } = await collectDroppedFiles(items as any);
    expect(files.map((f) => f.relPath).sort()).toEqual([
      'docs/readme.md',
      'docs/sub/deep.txt',
    ]);
  });

  it('readEntries 必须反复调到空数组 —— 只读一批会静默丢条目', async () => {
    // Chromium 每次最多回 100 条。造两批非空 + 一批空：只读一次的实现在这里会少 1 个文件。
    const tree = dirEntry('many', [[fileEntry('a.txt')], [fileEntry('b.txt')], []]);
    const { items } = makeItems([tree]);
    const { files } = await collectDroppedFiles(items as any);
    expect(files.map((f) => f.relPath).sort()).toEqual(['many/a.txt', 'many/b.txt']);
    expect(tree.__readCount()).toBe(3);
  });

  it('webkitGetAsEntry 必须在 handler 里同步取完', async () => {
    // 事件处理器一返回 DataTransferItemList 就失效，之后再取只会拿到 null（静默丢文件）。
    // collectDroppedFiles 收的是**已取出的 item 数组**，正是为了把这个前提钉进签名。
    const tree = fileEntry('a.txt');
    const { items, consume } = makeItems([tree], { dropItemsAfterUse: true });
    const collected = collectDroppedFiles(items as any);
    consume(); // 模拟 handler 已返回
    const { files } = await collected;
    expect(files.map((f) => f.relPath)).toEqual(['a.txt']);
  });

  it('超过上限时截断，并如实报出漏掉了几个', async () => {
    const tree = dirEntry('big', [
      [fileEntry('a.txt'), fileEntry('b.txt'), fileEntry('c.txt')],
      [],
    ]);
    const { items } = makeItems([tree]);
    const { files, truncated } = await collectDroppedFiles(items as any, 2);
    expect(files).toHaveLength(2);
    expect(truncated).toBe(true);
  });

  it('达到上限后停止读取目录，不再枚举整棵树', async () => {
    const tree = dirEntry('big', [
      [fileEntry('a.txt'), fileEntry('b.txt'), fileEntry('c.txt')],
      [fileEntry('d.txt')],
      [],
    ]);
    const { items } = makeItems([tree]);
    const result = await collectDroppedFiles(items as any, 2);
    expect(result.truncated).toBe(true);
    expect(tree.__readCount()).toBe(1);
  });

  it('刚好达到上限但目录已读完时不误报截断', async () => {
    const tree = dirEntry('exact', [[fileEntry('a.txt'), fileEntry('b.txt')], []]);
    const { items } = makeItems([tree]);
    const result = await collectDroppedFiles(items as any, 2);
    expect(result.truncated).toBe(false);
    expect(tree.__readCount()).toBe(2);
  });

  it('单个 entry 读取失败时保留有效兄弟文件并返回失败清单', async () => {
    const broken = {
      isFile: true,
      isDirectory: false,
      name: 'broken.txt',
      file: (_resolve: (file: File) => void, reject: (error: Error) => void) => reject(new Error('read failed')),
    };
    const tree = dirEntry('mixed', [[fileEntry('ok.txt'), broken], []]);
    const { items } = makeItems([tree]);
    const result = await collectDroppedFiles(items as any);
    expect(result.files.map((item) => item.relPath)).toEqual(['mixed/ok.txt']);
    expect(result.failed).toEqual([{ name: 'mixed/broken.txt', reason: 'read failed' }]);
  });

  it('非 file 类型的 item 直接跳过', async () => {
    const { items } = makeItems([]);
    (items as any).push({ kind: 'string', type: 'text/plain' });
    const { files } = await collectDroppedFiles(items as any);
    expect(files).toEqual([]);
  });
});

describe('uploadFiles', () => {
  const dropped = (relPath: string, size = 4) => ({ file: makeFile('x', size), relPath });

  it('按相对路径拆出目录与文件名，基准是落点目录', async () => {
    vi.mocked(uploadBrowseFile).mockResolvedValue({
      name: 'a.txt', path: 'refs/a.txt', size: 4, renamed: false,
    });
    await uploadFiles('s1', 'shared', undefined, 'refs', [dropped('docs/a.txt')]);
    expect(uploadBrowseFile).toHaveBeenCalledWith(
      's1', 'shared', 'refs/docs', 'a.txt', expect.anything(), undefined,
    );
  });

  it('并发不超过上限', async () => {
    let inFlight = 0;
    let peak = 0;
    vi.mocked(uploadBrowseFile).mockImplementation(async () => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await new Promise((resolve) => setTimeout(resolve, 1));
      inFlight -= 1;
      return { name: 'x', path: 'x', size: 1, renamed: false };
    });
    await uploadFiles(
      's1', 'shared', undefined, '', Array.from({ length: 10 }, () => dropped('a.txt')),
      { concurrency: 3 },
    );
    expect(peak).toBeLessThanOrEqual(3);
  });

  it('一个失败不拖垮其余 —— 拖 20 个进去，坏的那个不该连坐', async () => {
    vi.mocked(uploadBrowseFile)
      .mockResolvedValueOnce({ name: 'a', path: 'a', size: 1, renamed: false })
      .mockRejectedValueOnce(new Error('文件超出上限'))
      .mockResolvedValue({ name: 'c', path: 'c', size: 1, renamed: false });
    const outcome = await uploadFiles(
      's1', 'shared', undefined, '',
      [dropped('a.txt'), dropped('b.txt'), dropped('c.txt')],
      { concurrency: 1 },
    );
    expect(outcome.uploaded).toHaveLength(2);
    expect(outcome.failed).toEqual([{ name: 'b.txt', reason: '文件超出上限' }]);
  });

  it('超限的文件在本地就拦下，不发请求', async () => {
    vi.mocked(uploadBrowseFile).mockResolvedValue({
      name: 'a', path: 'a', size: 1, renamed: false,
    });
    const outcome = await uploadFiles(
      's1', 'shared', undefined, '',
      [dropped('big.bin', 2048), dropped('small.txt', 4)],
      { maxBytes: 1024 },
    );
    expect(outcome.failed).toEqual([{ name: 'big.bin', reason: 'too-large' }]);
    expect(uploadBrowseFile).toHaveBeenCalledTimes(1);
    expect(vi.mocked(uploadBrowseFile).mock.calls[0][3]).toBe('small.txt');
  });

  it('逐条上报进度', async () => {
    vi.mocked(uploadBrowseFile).mockResolvedValue({
      name: 'a', path: 'a', size: 1, renamed: false,
    });
    const seen: number[] = [];
    await uploadFiles(
      's1', 'shared', undefined, '',
      [dropped('a.txt'), dropped('b.txt')],
      { concurrency: 1, onProgress: (done, total) => seen.push(`${done}/${total}` as any) },
    );
    expect(seen).toEqual(['1/2', '2/2']);
  });
});

describe('复制路径 / 拖出载荷', () => {
  it('复制的是后端给的绝对路径，不是列表里的相对路径', async () => {
    vi.mocked(resolveBrowsePath).mockResolvedValue({
      path: 'refs/a.txt', abs_path: '/Users/me/shared/refs/a.txt',
      name: 'a.txt', type: 'file',
    });
    const written: string[] = [];
    setClipboard({ writeText: vi.fn(async (text: string) => { written.push(text); }) });
    const result = await copyPathToClipboard('s1', 'shared', 'refs/a.txt');
    expect(result).toBe('/Users/me/shared/refs/a.txt');
    expect(written).toEqual(['/Users/me/shared/refs/a.txt']);
  });

  it('拖出载荷是 mime:filename:绝对URL', () => {
    expect(downloadUrlPayload('a.pdf', 'http://x/v1/download', 'application/pdf')).toBe(
      'application/pdf:a.pdf:http://x/v1/download',
    );
  });
});

// ---------------------------------------------------------------------------
// 非安全上下文下的剪贴板
//
// 服务绑到局域网（HOST=0.0.0.0），用 http://<局域网IP>:8080 打开时 navigator.clipboard 是
// **undefined**（只有 https 与 localhost 是 secure context）。原来的写法
// `navigator.clipboard.writeText(...)` 会在属性访问那一步抛 TypeError，
// 界面显示「复制失败」而用户什么都没做错 —— 这是真机上报回来的 bug。
// ---------------------------------------------------------------------------
describe('writeClipboard 在非安全上下文下的退路', () => {
  afterEach(() => {
    delete (document as any).execCommand;
    setClipboard(undefined);
  });

  it('navigator.clipboard 不存在时退回 execCommand，文本照样进剪贴板', async () => {
    setClipboard(undefined);
    const copied = stubExecCommand();

    await writeClipboard('/tmp/shared/a.txt');

    expect(copied).toEqual(['/tmp/shared/a.txt']);
  });

  it('有 API 但被拒（权限 / 文档未聚焦）时也退回 execCommand', async () => {
    setClipboard({ writeText: vi.fn(async () => { throw new Error('Document is not focused'); }) });
    const copied = stubExecCommand();

    await writeClipboard('/tmp/shared/a.txt');

    expect(copied).toEqual(['/tmp/shared/a.txt']);
  });

  it('能用原生 API 时不去碰 execCommand', async () => {
    const writeText = vi.fn(async () => undefined);
    setClipboard({ writeText });
    const copied = stubExecCommand();

    await writeClipboard('/tmp/shared/a.txt');

    expect(writeText).toHaveBeenCalledWith('/tmp/shared/a.txt');
    expect(copied).toEqual([]);
  });

  it('两条路都不通时，抛出的消息是**路径本身**而不是 JS 报错', async () => {
    // 调用方的提示是「复制失败，请手动复制」，后面只该跟能照着复制的路径，
    // 跟一串 "Cannot read properties of undefined" 对用户毫无用处。
    setClipboard(undefined);
    stubExecCommand(false);

    await expect(writeClipboard('/tmp/shared/a.txt')).rejects.toThrow('/tmp/shared/a.txt');
  });

  it('临时 textarea 用完就摘掉，不留 DOM 垃圾', async () => {
    setClipboard(undefined);
    stubExecCommand();

    await writeClipboard('/tmp/shared/a.txt');

    expect(document.querySelector('textarea')).toBeNull();
  });

  it('execCommand 自己抛异常时也不把 textarea 留在页面上', async () => {
    setClipboard(undefined);
    (document as any).execCommand = vi.fn(() => { throw new Error('not supported'); });

    await expect(writeClipboard('/x')).rejects.toThrow('/x');
    expect(document.querySelector('textarea')).toBeNull();
  });
});
