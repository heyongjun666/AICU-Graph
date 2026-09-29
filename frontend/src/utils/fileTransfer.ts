// ============================================================================
// fileTransfer.ts — 可浏览根的拖入 / 拖出 / 复制路径
// ============================================================================
//
// 两个面板（SharedWorkspacePanel 的共享区、NodeInspector 的节点产出）今天各抄了一份
// 文件树渲染，`format.ts` 的注释里记着这类重复的历史代价。所以拖拽逻辑只写这一份：
// 组件传根（scope + nodeId），这里负责其余全部。
//
// 这里的东西**全部是纯函数或纯编排**（fetch 走 utils/api，不直接碰 DOM），
// 所以能在 jsdom 里把「DataTransfer 的坑」一次踩完，组件测试只管接线对不对。

import {
  BrowseScope,
  UploadResult,
} from '../types';
import { resolveBrowsePath, revealBrowsePath, uploadBrowseFile } from './api';

/** 一次拖入的文件上限。防的是误拖一个 .app 包（会被递归成几百个文件）。 */
export const MAX_DROP_FILES = 2000;

/** 单文件大小上限，与后端 `runtime/file_transfer.py` 的 MAX_UPLOAD_BYTES 同源。 */
export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

/** 并发上传数。1 太慢（大量小文件时往返延迟占满），再高也只是在后端那把改名锁前排队。 */
const UPLOAD_CONCURRENCY = 3;

/**
 * 一个待上传的文件：`relPath` 是它相对**拖入落点**的路径，含顶层文件夹名，
 * 用 `/` 分隔。拖 `docs/` 进 `backend/` 得到 `docs/readme.md` → 落到
 * `backend/docs/readme.md`，与所有系统文件管理器一致。
 */
export interface DroppedFile {
  file: File;
  relPath: string;
}

export interface UploadFailure {
  name: string;
  reason: string;
}

export interface UploadOutcome {
  uploaded: UploadResult[];
  failed: UploadFailure[];
  /** 被 MAX_DROP_FILES 截掉的数量（>0 时界面要如实说，不能装作全传完了）。 */
  skipped: number;
}

/** 拖的是文件而不是文本/内部元素。 */
export function isFileDrag(dataTransfer: DataTransfer | null | undefined): boolean {
  return Boolean(dataTransfer?.types?.includes('Files'));
}

export function joinPath(...parts: string[]): string {
  return parts
    .filter((part) => part !== '' && part !== '.' && part !== undefined)
    .join('/')
    .replace(/\/+/g, '/');
}

export function dirnameOf(relPath: string): string {
  const cut = relPath.lastIndexOf('/');
  return cut < 0 ? '' : relPath.slice(0, cut);
}

export function basenameOf(relPath: string): string {
  const cut = relPath.lastIndexOf('/');
  return cut < 0 ? relPath : relPath.slice(cut + 1);
}

/**
 * 把一次 drop 里的东西展开成一个扁平的文件列表，文件夹递归展开。
 *
 * 两个必须踩对的点：
 *
 * 1. **`dataTransfer.items` 必须在 drop 处理器里同步读完**（所以参数是 item 数组而不是
 *    dataTransfer 本身）。事件处理器一返回，浏览器就把 DataTransferItemList 标为失效，
 *    之后再调 `webkitGetAsEntry()` 只会拿到 null —— 而且是**静默**的，表现为「拖了没反应」。
 * 2. **`readEntries` 每次最多回 100 条**，必须反复调到返回空数组为止。只调一次的话，
 *    超过 100 个条目的目录会被**静默截断**成 100 个。
 *
 * 没有 `webkitGetAsEntry`（Firefox 等）时退回 `getAsFile()` —— 退化结果是没有文件夹
 * 支持，但仍能把文件传上去，不报错。
 */
export async function collectDroppedFiles(
  items: DataTransferItem[],
  limit = MAX_DROP_FILES,
): Promise<{ files: DroppedFile[]; truncated: boolean; failed: UploadFailure[] }> {
  const files: DroppedFile[] = [];
  const failed: UploadFailure[] = [];
  // true 表示还有未遍历的条目，不为统计漏掉的数量继续读整棵树。
  let truncated = false;
  const roots: Array<{ entry: FileSystemEntry } | { file: File }> = [];
  const fail = (name: string, error: unknown) => {
    failed.push({ name, reason: error instanceof Error ? error.message : String(error) });
  };

  // 必须在第一次 await 之前捕获所有句柄（含 getAsFile 的退化路径）。
  for (const item of items) {
    if (item.kind !== 'file') continue;
    try {
      const entry = typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null;
      if (entry) roots.push({ entry });
      else {
        const file = item.getAsFile();
        if (file) roots.push({ file });
      }
    } catch (error) {
      fail('', error);
    }
  }

  const walk = async (entry: FileSystemEntry, prefix: string): Promise<void> => {
    const path = joinPath(prefix, entry.name);
    try {
      if (entry.isFile) {
        const file = await new Promise<File>((resolve, reject) =>
          (entry as FileSystemFileEntry).file(resolve, reject),
        );
        files.push({ file, relPath: path });
      } else if (entry.isDirectory) {
        const reader = (entry as FileSystemDirectoryEntry).createReader();
        for (;;) {
          const batch = await new Promise<FileSystemEntry[]>((resolve, reject) =>
            reader.readEntries(resolve, reject),
          );
          if (!batch.length) return;
          for (let index = 0; index < batch.length; index += 1) {
            if (files.length >= limit) {
              truncated = true;
              return;
            }
            await walk(batch[index], path);
            if (truncated) return;
          }
          // A full final batch is not necessarily truncated. Read one bounded
          // look-ahead batch so an exact-limit directory is not reported falsely.
          if (files.length >= limit) {
            const nextBatch = await new Promise<FileSystemEntry[]>((resolve, reject) =>
              reader.readEntries(resolve, reject),
            );
            if (!nextBatch.length) return;
            truncated = true;
            return;
          }
        }
      }
    } catch (error) {
      // 一个损坏的文件/目录不影响已收集的文件及兄弟条目。
      fail(path, error);
    }
  };

  for (const root of roots) {
    if (files.length >= limit) {
      truncated = true;
      break;
    }
    if ('entry' in root) await walk(root.entry, '');
    else files.push({ file: root.file, relPath: root.file.name });
  }
  return { files, truncated, failed };
}

/**
 * 逐个上传，最多 `concurrency` 路并发。`baseDir` 是拖入的落点（相对 scope 根）。
 *
 * **一个失败不中断其余的** —— 拖 20 个文件进去，其中一个超限，另外 19 个该照常落地。
 * 本地先按大小拦一道，省掉一次注定 413 的 100MB 传输。
 */
export async function uploadFiles(
  sessionId: string,
  scope: BrowseScope,
  nodeId: string | undefined,
  baseDir: string,
  files: DroppedFile[],
  options: {
    concurrency?: number;
    maxBytes?: number;
    onProgress?: (done: number, total: number) => void;
  } = {},
): Promise<UploadOutcome> {
  const concurrency = options.concurrency ?? UPLOAD_CONCURRENCY;
  const maxBytes = options.maxBytes ?? MAX_UPLOAD_BYTES;
  const uploaded: UploadResult[] = [];
  const failed: UploadFailure[] = [];
  let done = 0;
  let cursor = 0;

  const worker = async () => {
    for (;;) {
      const index = cursor++;
      if (index >= files.length) return;
      const item = files[index];
      if (item.file.size > maxBytes) {
        failed.push({ name: item.relPath, reason: 'too-large' });
      } else {
        try {
          uploaded.push(
            await uploadBrowseFile(
              sessionId, scope,
              joinPath(baseDir, dirnameOf(item.relPath)),
              basenameOf(item.relPath),
              item.file, nodeId,
            ),
          );
        } catch (error) {
          failed.push({
            name: item.relPath,
            reason: error instanceof Error ? error.message : String(error),
          });
        }
      }
      done += 1;
      options.onProgress?.(done, files.length);
    }
  };

  await Promise.all(
    Array.from({ length: Math.min(concurrency, files.length) }, () => worker()),
  );
  return { uploaded, failed, skipped: 0 };
}

/**
 * 写剪贴板。**不能直接用 `navigator.clipboard`** —— 它在非安全上下文里根本不存在。
 *
 * 服务绑的是 `0.0.0.0`，用 `http://<局域网IP>:8080` 打开就不是 secure context
 * （只有 https 和 localhost 算），此时 `navigator.clipboard` 是 `undefined`，
 * `navigator.clipboard.writeText(...)` 在**属性访问**那一步就抛 TypeError，连
 * 请求都没发出去，界面只显示「复制失败」而用户什么都没做错。
 *
 * 所以：能用就用（异步、支持富文本、不被弹窗拦截），不能用就退回
 * `document.execCommand('copy')` + 临时 textarea —— 这条老路在非安全上下文里照样有效。
 */
export async function writeClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // 有 API 也可能被拒：权限被拒、文档没聚焦（"Document is not focused"）、
      // 嵌在 iframe 里。这些都不该直接判死，继续走老路。
    }
  }
  if (!copyViaExecCommand(text)) {
    // 两条路都不通时，把**要复制的文本本身**当错误消息抛出去：调用方的提示是
    // 「复制失败，请手动复制」，后面跟一串 JS 报错毫无用处，跟路径才让人能手动复制。
    throw new Error(text);
  }
}

function copyViaExecCommand(text: string): boolean {
  const area = document.createElement('textarea');
  area.value = text;
  // 必须能被 select()，但不能影响布局，也不能引起滚动跳动 ——
  // 用 fixed + 移出视口，比 display:none（选不中）和 opacity 单独用都稳。
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.top = '-1000px';
  area.style.opacity = '0';
  document.body.appendChild(area);
  try {
    area.select();
    // iOS Safari 只认 setSelectionRange 选中的内容
    area.setSelectionRange(0, text.length);
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    document.body.removeChild(area);
  }
}

export async function copyPathToClipboard(
  sessionId: string,
  scope: BrowseScope,
  path: string,
  nodeId?: string,
): Promise<string> {
  const info = await resolveBrowsePath(sessionId, scope, path, nodeId);
  await writeClipboard(info.abs_path);
  return info.abs_path;
}

export async function revealPath(
  sessionId: string,
  scope: BrowseScope,
  path: string,
  nodeId?: string,
): Promise<void> {
  await revealBrowsePath(sessionId, scope, path, nodeId);
}

/**
 * Drag-out 的载荷：`<mime>:<filename>:<absolute url>`。
 *
 * 这不是「下载」：文件不经过下载目录，直接落到松手的位置。所以「拖到桌面看一眼」
 * 之后共享区里那份还在 —— 全程是复制语义，没有移动。
 *
 * mime 默认 octet-stream：真实类型由服务端随响应给出，而写一个「可渲染」的类型
 * （text/plain、image/png）反而会让 Chromium 倾向落一个快捷方式而不是文件。
 */
export function downloadUrlPayload(
  name: string,
  url: string,
  mime = 'application/octet-stream',
): string {
  return `${mime}:${name}:${url}`;
}
