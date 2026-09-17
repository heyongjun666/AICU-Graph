import React, { useCallback, useEffect, useState } from 'react';
import {
  ArrowLeft,
  ChevronRight,
  File,
  Folder,
  RefreshCw,
  X,
} from 'lucide-react';
import { OutputEntry, OutputFile, OutputListing } from '../types';
import { listSessionShared, readSessionSharedFile } from '../utils/api';
import { formatBytes } from '../utils/format';
import { useI18n } from '../i18n';

interface SessionOutputsDialogProps {
  open: boolean;
  sessionId: string | null;
  onClose: () => void;
}

/**
 * SessionOutputsDialog — 会话级「查看产出」文件树浏览器（V3-2.2）。
 * 打开即加载 shared 根；面包屑 Outputs / {partition} / {subpath}。
 * 文件预览走 previewable 分支（否则「二进制/不可预览文件」）。
 */
const SessionOutputsDialog: React.FC<SessionOutputsDialogProps> = ({ open, sessionId, onClose }) => {
  const { t } = useI18n();
  const [currentPath, setCurrentPath] = useState('');
  const [listing, setListing] = useState<OutputListing | null>(null);
  const [selectedFile, setSelectedFile] = useState<OutputFile | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

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

  useEffect(() => {
    if (open) {
      setCurrentPath('');
      setListing(null);
      setSelectedFile(null);
      setError('');
      if (sessionId) load('');
    }
  }, [open, sessionId, load]);

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

  return (
    <div className="dialog-backdrop" onMouseDown={onClose}>
      <div className="workbench-dialog outputs-dialog" onMouseDown={(event) => event.stopPropagation()}>
        <div className="dialog-header">
          <div><h2>{t('outputs.title')}</h2><p>{t('outputs.desc')}</p></div>
          <div className="dialog-header-actions">
            {sessionId && (
              <button className="secondary-button" onClick={() => load(currentPath)} disabled={loading} title={t('common.refresh')}>
                <RefreshCw size={14} className={loading ? 'spin' : ''} /> {t('common.refresh')}
              </button>
            )}
            <button className="icon-button" onClick={onClose} title={t('common.close')}><X size={18} /></button>
          </div>
        </div>

        {!sessionId ? (
          <div className="detail-empty">{t('outputs.selectSession')}</div>
        ) : (
          <div className="workspace-browser outputs-browser">
            <div className="workspace-breadcrumbs">
              <button onClick={goHome} disabled={!currentPath && !selectedFile}>{t('outputs.outputs')}</button>
              {pathSegments.map((segment, index) => (
                <React.Fragment key={`${segment}-${index}`}>
                  <ChevronRight size={13} />
                  <button onClick={() => goToPath(index)}>{segment}</button>
                </React.Fragment>
              ))}
            </div>
            {error && <div className="inspector-error">{error}</div>}
            {loading && !selectedFile && <div className="detail-empty"><RefreshCw size={15} className="spin" /> {t('outputs.loadingOutputs')}</div>}
            {!loading && selectedFile && (
              <div className="workspace-preview">
                <div className="workspace-preview-header">
                  <button className="secondary-button" onClick={() => setSelectedFile(null)}><ArrowLeft size={14} /> {t('common.back')}</button>
                  <span>{selectedFile.path}</span>
                  <small>{formatBytes(selectedFile.size)}</small>
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
              <div className="workspace-list">
                {!listing.entries.length && <div className="detail-empty">{currentPath ? t('outputs.emptyDir') : t('outputs.noOutputs')}</div>}
                {listing.entries.map((entry) => (
                  <button className="workspace-entry outputs-entry" key={entry.path} onClick={() => openEntry(entry)}>
                    <span className="workspace-entry-icon">{entry.type === 'directory' ? <Folder size={16} /> : <File size={16} />}</span>
                    <span className="workspace-entry-name">
                      <span className="outputs-entry-title">{entry.name}</span>
                    </span>
                    <small>{entry.type === 'directory' ? t('outputs.folder') : formatBytes(entry.size)}</small>
                  </button>
                ))}
                {listing.truncated && <div className="detail-empty">{t('outputs.listingTruncated')}</div>}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
};

export default SessionOutputsDialog;
