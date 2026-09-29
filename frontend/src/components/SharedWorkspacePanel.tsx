import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowLeft,
  ChevronRight,
  Copy,
  FolderOpen,
  RefreshCw,
  X,
} from 'lucide-react';
import { OutputEntry, OutputFile, OutputListing } from '../types';
import { listSessionShared, readSessionSharedFile } from '../utils/api';
import { formatBytes } from '../utils/format';
import { useWorkspaceDrop } from '../hooks/useWorkspaceDrop';
import WorkspaceEntryRow from './WorkspaceEntryRow';
import { useI18n } from '../i18n';

interface SharedWorkspacePanelProps {
  sessionId: string | null;
  open: boolean;
  width: number;
  onToggle: () => void;
  onResize: (width: number) => void;
}

const MIN_WIDTH = 240;
const MAX_WIDTH = 720;

/**
 * SharedWorkspacePanel — 会话级「共享 workspace」停靠面板。
 * 紧贴左侧会话栏；可收缩（折叠为一条竖向按钮栏）、展开，右缘可拖拽拉宽。
 * 内容复用 SessionOutputsDialog 的文件树浏览（shared 根 → 分区 → 文件预览）。
 */
const SharedWorkspacePanel: React.FC<SharedWorkspacePanelProps> = ({
  sessionId,
  open,
  width,
  onToggle,
  onResize,
}) => {
  const { t } = useI18n();
  const [currentPath, setCurrentPath] = useState('');
  const [listing, setListing] = useState<OutputListing | null>(null);
  const [selectedFile, setSelectedFile] = useState<OutputFile | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const dragState = useRef<{ startX: number; startWidth: number } | null>(null);

  const load = useCallback((path: string) => {
    if (!sessionId) return;
    setLoading(true);
    setError('');
    setSelectedFile(null);
    listSessionShared(sessionId, path)
      .then(setListing)
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setLoading(false));
  }, [sessionId]);

  const drop = useWorkspaceDrop({
    sessionId: sessionId ?? '',
    scope: 'shared',
    currentDir: currentPath,
    // 上传完必须重列，否则刚拖进去的东西在界面上看不见，用户会以为没传成功。
    onUploaded: () => load(currentPath),
  });

  useEffect(() => {
    if (open && sessionId) {
      setCurrentPath('');
      setListing(null);
      setSelectedFile(null);
      setError('');
      load('');
    }
  }, [open, sessionId, load]);

  const onDragStart = (event: React.MouseEvent) => {
    event.preventDefault();
    dragState.current = { startX: event.clientX, startWidth: width };
    const onMove = (ev: MouseEvent) => {
      if (!dragState.current) return;
      const delta = ev.clientX - dragState.current.startX;
      const next = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, dragState.current.startWidth + delta));
      onResize(next);
    };
    const onUp = () => {
      dragState.current = null;
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  };

  if (!open) return null;

  const pathSegments = currentPath ? currentPath.split('/').filter(Boolean) : [];

  const openEntry = (entry: OutputEntry) => {
    if (entry.type === 'directory') {
      const next = entry.path === '.' ? '' : entry.path;
      setCurrentPath(next);
      load(next);
      return;
    }
    if (!sessionId) return;
    setLoading(true);
    setError('');
    readSessionSharedFile(sessionId, entry.path)
      .then(setSelectedFile)
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setLoading(false));
  };

  const goToPath = (index: number) => {
    const next = pathSegments.slice(0, index + 1).join('/');
    setCurrentPath(next);
    load(next);
  };

  const goHome = () => {
    setCurrentPath('');
    load('');
  };

  /**
   * 拖到某一行目录上。`items` 已由行组件**同步**取出（DataTransferItemList 在事件
   * 处理器返回后即失效），这里直接转发给 hook。
   */
  const uploadInto = (targetDir: string, items: DataTransferItem[]) => {
    drop.upload(targetDir, items);
  };

  return (
    <aside className="shared-workspace-panel" aria-label={t('sharedWorkspace.browser')}>
      <div className="shared-panel-header">
        <div className="shared-panel-title">
          <FolderOpen size={15} />
          <span>{t('sharedWorkspace.title')}</span>
        </div>
        <div className="dialog-header-actions">
          {sessionId && (
            <button className="icon-button" onClick={() => load(currentPath)} disabled={loading} title={t('common.refresh')}>
              <RefreshCw size={14} className={loading ? 'spin' : ''} />
            </button>
          )}
          <button className="icon-button" onClick={onToggle} title={t('sharedWorkspace.collapse')}>
            <X size={16} />
          </button>
        </div>
      </div>

      <div className="workspace-browser outputs-browser shared-panel-body">
        {!sessionId ? (
          <div className="detail-empty">{t('sharedWorkspace.selectSession')}</div>
        ) : (
          <>
            <div className="workspace-breadcrumbs">
              <button onClick={goHome} disabled={!currentPath && !selectedFile}>{t('outputs.outputs')}</button>
              {pathSegments.map((segment, index) => (
                <React.Fragment key={`${segment}-${index}`}>
                  <ChevronRight size={13} />
                  <button onClick={() => goToPath(index)}>{segment}</button>
                </React.Fragment>
              ))}
              <button
                className="icon-button workspace-breadcrumb-action"
                title={t('files.copyPath')}
                aria-label={t('files.copyPath')}
                onClick={() => drop.onCopyPath(currentPath)}
              >
                <Copy size={12} />
              </button>
            </div>
            {drop.progress && (
              <div className="workspace-drop-status">
                {t('files.uploading', { done: drop.progress.done, total: drop.progress.total })}
              </div>
            )}
            {!!drop.failures.length && (
              <div className="workspace-drop-failures">
                {drop.failures.slice(0, 3).map((item) => (
                  <div key={item.name}>{item.name}: {item.reason}</div>
                ))}
                {drop.failures.length > 3 && (
                  <div>+{drop.failures.length - 3}</div>
                )}
              </div>
            )}
            {error && <div className="inspector-error">{error}</div>}
            {loading && !selectedFile && <div className="detail-empty"><RefreshCw size={15} className="spin" /> {t('common.loading')}</div>}
            {!loading && selectedFile && (
              <div className="workspace-preview">
                <div className="workspace-preview-header">
                  <button className="secondary-button" onClick={() => setSelectedFile(null)}><ArrowLeft size={14} /> {t('common.back')}</button>
                  <span>{selectedFile.path}</span>
                  <small>{formatBytes(selectedFile.size)}</small>
                  <button
                    className="icon-button"
                    title={t('files.copyPath')}
                    aria-label={t('files.copyPath')}
                    onClick={() => drop.onCopyPath(selectedFile.path)}
                  >
                    <Copy size={13} />
                  </button>
                </div>
                {selectedFile.previewable ? (
                  <>
                    <pre>{selectedFile.content}</pre>
                    {selectedFile.truncated && <div className="detail-empty">{t('outputs.previewTruncated')}</div>}
                  </>
                ) : (
                  <div className="detail-empty">{t('outputs.binaryFile')}</div>
                )}
              </div>
            )}
            {!loading && !selectedFile && listing && (
              <div
                className={`workspace-list${drop.dropTarget === currentPath ? ' is-drop-target' : ''}`}
                {...drop.containerProps}
              >
                {!listing.entries.length && (
                  <div className="detail-empty">
                    {currentPath ? t('outputs.emptyDir') : t('outputs.noOutputs')}
                    <div className="workspace-drop-hint">{t('files.dropHint')}</div>
                  </div>
                )}
                {listing.entries.map((entry) => (
                  <WorkspaceEntryRow
                    key={entry.path}
                    entry={entry}
                    sessionId={sessionId as string}
                    scope="shared"
                    onOpen={openEntry}
                    onCopyPath={(target) => drop.onCopyPath(target.path)}
                    onReveal={(target) => drop.onReveal(target.path)}
                    dropTarget={drop.dropTarget}
                    onDropTargetChange={drop.setDropTarget}
                    onDrop={uploadInto}
                  />
                ))}
                {listing.truncated && <div className="detail-empty">{t('outputs.listingTruncated')}</div>}
              </div>
            )}
          </>
        )}
      </div>

      <div className="shared-panel-resizer" onMouseDown={onDragStart} title={t('sharedWorkspace.dragToResize')} />
    </aside>
  );
};

export default SharedWorkspacePanel;
