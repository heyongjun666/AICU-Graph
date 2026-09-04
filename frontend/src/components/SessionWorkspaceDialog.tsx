import React, { useEffect, useState } from 'react';
import { ChevronDown, X } from 'lucide-react';
import { SessionSummary } from '../types';
import { updateSessionSettings } from '../utils/api';
import { useI18n } from '../i18n';
import { useUIStateStore } from '../stores/UIStateStore';

interface SessionWorkspaceDialogProps {
  open: boolean;
  session: SessionSummary | null;
  onClose: () => void;
  onSaved: () => void | Promise<void>;
}

const SessionWorkspaceDialog: React.FC<SessionWorkspaceDialogProps> = ({
  open,
  session,
  onClose,
  onSaved,
}) => {
  const { t } = useI18n();
  // 本地 state 独立：仅在打开时由 session 初始化，编辑期间不触碰全局状态。
  const [workspaceRoot, setWorkspaceRoot] = useState('');
  const [sharedWorkspace, setSharedWorkspace] = useState('');
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const addToast = useUIStateStore((state) => state.addToast);

  const refreshAfterSave = async () => {
    try {
      await onSaved();
    } catch {
      addToast({ type: 'warning', message: t('settings.sessionRefreshFailed') });
    }
  };

  useEffect(() => {
    if (open && session) {
      setWorkspaceRoot(session.workspace_root || '');
      setSharedWorkspace(session.shared_workspace || '');
      setAdvancedOpen(false);
      setError('');
    }
  }, [open, session]);

  if (!open || !session) return null;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      // 项目目录三态：空串 → null → 清除配置恢复默认；工作区根空串 → undefined（不发送）。
      await updateSessionSettings(
        session.id,
        {},
        workspaceRoot.trim() || undefined,
        sharedWorkspace.trim() || null,
      );
      onClose();
      await refreshAfterSave();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="dialog-backdrop" onMouseDown={onClose}>
      <form className="workbench-dialog compact" onSubmit={submit} onMouseDown={(event) => event.stopPropagation()}>
        <div className="dialog-header">
          <div><h2>{t('workspace.title')}</h2><p>{t('workspace.desc')}</p></div>
          <button type="button" className="icon-button" onClick={onClose} title={t('common.close')}><X size={18} /></button>
        </div>
        <label>{t('workspace.project')}<input autoFocus value={sharedWorkspace} onChange={(event) => setSharedWorkspace(event.target.value)} placeholder={t('workspace.absolutePath')} /></label>
        <small className="form-hint">{t('workspace.projectWarn')}</small>
        {session.workspace_root && <small className="form-hint">{t('workspace.projectDefault', { path: `${session.workspace_root}/shared` })}</small>}
        <div className="advanced-config">
          <button type="button" className="advanced-config-toggle" onClick={() => setAdvancedOpen((value) => !value)}>
            <ChevronDown size={14} className={advancedOpen ? 'is-open' : ''} />
            {t('workspace.advanced')}
          </button>
          {advancedOpen && (
            <div className="advanced-config-body">
              <label>{t('workspace.runDir')}<input value={workspaceRoot} onChange={(event) => setWorkspaceRoot(event.target.value)} placeholder={t('workspace.absolutePath')} /></label>
              <small className="form-hint">{t('workspace.runDirDesc')}</small>
              {session.workspace_root && <small className="form-hint">{t('workspace.current', { path: session.workspace_root })}</small>}
            </div>
          )}
        </div>
        {error && <div className="form-error">{error}</div>}
        <div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>{t('common.cancel')}</button><button className="primary-button" disabled={busy}>{busy ? t('workspace.saving') : t('workspace.save')}</button></div>
      </form>
    </div>
  );
};

export default SessionWorkspaceDialog;
