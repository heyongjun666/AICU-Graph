import React, { useCallback, useEffect, useState } from 'react';
import { ChevronLeft, File, Folder, RefreshCw } from 'lucide-react';
import { useSessionStore } from '../stores/SessionStore';
import { WorkspaceFile, WorkspaceListing } from '../types';
import { listNodeResults, readNodeResultFile } from '../utils/api';
import { useI18n } from '../i18n';

interface ArtifactTabProps {
  nodeId: string;
}

function parentPath(path: string): string {
  if (!path || path === '.') return '';
  const parts = path.split('/').filter(Boolean);
  parts.pop();
  return parts.join('/');
}

const ArtifactTab: React.FC<ArtifactTabProps> = ({ nodeId }) => {
  const { t } = useI18n();
  const activeSessionId = useSessionStore((state) => state.activeSessionId);
  const [currentPath, setCurrentPath] = useState('');
  const [listing, setListing] = useState<WorkspaceListing | null>(null);
  const [selectedFile, setSelectedFile] = useState<WorkspaceFile | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadResults = useCallback(async (path: string) => {
    if (!activeSessionId) return;
    setLoading(true);
    setError(null);
    try {
      const result = await listNodeResults(activeSessionId, nodeId, path);
      setListing(result);
      setCurrentPath(result.path === '.' ? '' : result.path);
      setSelectedFile(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('artifact.unableToLoad'));
    } finally {
      setLoading(false);
    }
  }, [activeSessionId, nodeId]);

  useEffect(() => {
    setCurrentPath('');
    setListing(null);
    setSelectedFile(null);
    if (activeSessionId) void loadResults('');
  }, [activeSessionId, nodeId, loadResults]);

  const openFile = async (path: string) => {
    if (!activeSessionId) return;
    setLoading(true);
    setError(null);
    try {
      setSelectedFile(await readNodeResultFile(activeSessionId, nodeId, path));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : t('artifact.unableToRead'));
    } finally {
      setLoading(false);
    }
  };

  if (!activeSessionId) {
    return <div className="artifact-empty">{t('artifact.selectSession')}</div>;
  }

  return (
    <div className="artifact-tab">
      <div className="artifact-toolbar">
        <button
          type="button"
          className="icon-button"
          disabled={!currentPath || loading}
          title={t('artifact.parentDirectory')}
          aria-label={t('artifact.parentDirectory')}
          onClick={() => void loadResults(parentPath(currentPath))}
        >
          <ChevronLeft size={15} />
        </button>
        <span className="artifact-path">{t('artifact.output')} / {currentPath || ''}</span>
        <button
          type="button"
          className="icon-button"
          disabled={loading}
          title={t('artifact.refreshOutput')}
          aria-label={t('artifact.refreshOutput')}
          onClick={() => void loadResults(currentPath)}
        >
          <RefreshCw size={14} />
        </button>
      </div>

      {error && <div className="inline-error">{error}</div>}
      {loading && !listing && <div className="artifact-empty">{t('artifact.loadingOutput')}</div>}
      {!loading && listing?.artifact_path === null && (
        <div className="artifact-empty">{t('inspector.noDeclaredOutput')}</div>
      )}
      {listing?.artifact_path && (
        <div className="artifact-browser">
          <div className="artifact-files" role="list" aria-label={t('artifact.nodeOutputFiles')}>
            {listing.entries.length === 0 && (
              <div className="artifact-empty">{t('artifact.emptyOutputDir')}</div>
            )}
            {listing.entries.map((entry) => (
              <button
                type="button"
                className={`artifact-entry ${selectedFile?.path === entry.path ? 'selected' : ''}`}
                key={entry.path}
                onClick={() => {
                  if (entry.type === 'directory') void loadResults(entry.path);
                  else void openFile(entry.path);
                }}
              >
                {entry.type === 'directory' ? <Folder size={15} /> : <File size={15} />}
                <span>{entry.name}</span>
              </button>
            ))}
          </div>
          <div className="artifact-preview">
            {selectedFile ? (
              <>
                <div className="artifact-preview-title">{selectedFile.path}</div>
                <pre>{selectedFile.content}</pre>
              </>
            ) : (
              <div className="artifact-empty">{t('artifact.selectFilePreview')}</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default ArtifactTab;
