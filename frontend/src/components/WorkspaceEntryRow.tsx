import React from 'react';
import { Copy, File, Folder, FolderOpen } from 'lucide-react';
import { BrowseScope, WorkspaceEntry } from '../types';
import { browseDownloadUrl } from '../utils/api';
import { downloadUrlPayload } from '../utils/fileTransfer';
import { formatBytes } from '../utils/format';
import { useI18n } from '../i18n';

interface WorkspaceEntryRowProps {
  entry: WorkspaceEntry;
  sessionId: string;
  scope: BrowseScope;
  nodeId?: string;
  onOpen: (entry: WorkspaceEntry) => void;
  onCopyPath: (entry: WorkspaceEntry) => void;
  onReveal: (entry: WorkspaceEntry) => void;
  /** 当前高亮的落点（父组件持有 —— 容器也是一个落点，两边必须是同一份状态）。 */
  dropTarget: string | null;
  onDropTargetChange: (path: string | null) => void;
  onDrop: (targetDir: string, items: DataTransferItem[]) => void;
}

/**
 * WorkspaceEntryRow — 两个文件浏览面板共用的一行条目。
 *
 * 抽出来的理由与 `format.ts` 里记的那笔是同一笔：同一行东西在会话共享区和节点产出
 * 里各写一份，迟早分叉。加上拖拽之后分叉的代价更大了 —— DownloadURL 的写法错一处，
 * 表现是「某个面板里拖出去没反应」，很难查。
 *
 * **这一行是 `<div role="button">` 而不是 `<button>`。** 原来的实现用的是 `<button>`，
 * 而这里要往里放「复制路径 / 在访达中显示」两个按钮 —— HTML 不允许 button 套 button，
 * 浏览器虽然会渲染出来，但内层按钮的点击会同时触发外层的打开动作（点一下复制，
 * 顺手把文件也打开了）。所以外层降级成带键盘处理的 div。
 */
const WorkspaceEntryRow: React.FC<WorkspaceEntryRowProps> = ({
  entry,
  sessionId,
  scope,
  nodeId,
  onOpen,
  onCopyPath,
  onReveal,
  dropTarget,
  onDropTargetChange,
  onDrop,
}) => {
  const { t } = useI18n();
  const isDirectory = entry.type === 'directory';
  const active = isDirectory && dropTarget === entry.path;

  const onDragStart = (event: React.DragEvent) => {
    // 目录拖不出去：DownloadURL 只能带文件，没有「打包下载」这回事。
    // 目录的出口是「在访达中显示」。
    if (isDirectory || !event.dataTransfer) return;
    const url = browseDownloadUrl(sessionId, scope, entry.path, nodeId);
    event.dataTransfer.effectAllowed = 'copy';
    // 一律 octet-stream：真实的 Content-Type 由服务端随响应给出，而这里写死一个
    // 「可渲染」的类型（text/plain、image/png）反而会让 Chromium 倾向落一个快捷方式
    // 而不是文件。服务端那侧同样是 attachment，两边一致。
    event.dataTransfer.setData('DownloadURL', downloadUrlPayload(entry.name, url));
    // 同时给一个纯文本 URL 兜底：目标程序不认 DownloadURL 时至少拿得到一个链接。
    event.dataTransfer.setData('text/uri-list', url);
  };

  const onRowDragOver = (event: React.DragEvent) => {
    if (!isDirectory) return; // 文件行不拦截，让它冒泡到容器 → 落到当前目录
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = 'copy';
    onDropTargetChange(entry.path);
  };

  const onRowDrop = (event: React.DragEvent) => {
    if (!isDirectory) return;
    event.preventDefault();
    event.stopPropagation();
    onDropTargetChange(null);
    // 必须**同步**取出 item：事件处理器一返回，DataTransferItemList 就失效了，
    // 之后再调 webkitGetAsEntry() 只会拿到 null（表现为「拖了没反应」）。
    onDrop(entry.path, Array.from(event.dataTransfer.items));
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      onOpen(entry);
    }
  };

  return (
    <div
      className={`workspace-entry${active ? ' is-drop-target' : ''}`}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(entry)}
      onKeyDown={onKeyDown}
      draggable={!isDirectory}
      onDragStart={onDragStart}
      onDragOver={onRowDragOver}
      onDragLeave={(event) => {
        if (isDirectory && !event.currentTarget.contains(event.relatedTarget as Node)) {
          onDropTargetChange(null);
        }
      }}
      onDrop={onRowDrop}
    >
      <span className="workspace-entry-icon">
        {isDirectory ? <Folder size={16} /> : <File size={16} />}
      </span>
      <span className="workspace-entry-name">{entry.name}</span>
      <span className="workspace-entry-meta">
        <small>{isDirectory ? t('outputs.folder') : formatBytes(entry.size)}</small>
        <button
          type="button"
          className="icon-button"
          title={t('files.copyPath')}
          aria-label={t('files.copyPath')}
          onClick={(event) => { event.stopPropagation(); onCopyPath(entry); }}
        >
          <Copy size={13} />
        </button>
        <button
          type="button"
          className="icon-button"
          title={t('files.revealInFinder')}
          aria-label={t('files.revealInFinder')}
          onClick={(event) => { event.stopPropagation(); onReveal(entry); }}
        >
          <FolderOpen size={13} />
        </button>
      </span>
    </div>
  );
};

export default WorkspaceEntryRow;
