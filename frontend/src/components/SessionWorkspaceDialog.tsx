import React, { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import { SessionSummary } from '../types';
import { updateSessionSettings } from '../utils/api';
import { useI18n } from '../i18n';

interface SessionWorkspaceDialogProps {
  open: boolean;
  session: SessionSummary | null;
  onClose: () => void;
  onSaved: () => void;
}

const SessionWorkspaceDialog: React.FC<SessionWorkspaceDialogProps> = ({
  open,
  session,
  onClose,
  onSaved,
}) => {
  const { t } = useI18n();
  const [workspaceRoot, setWorkspaceRoot] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (open && session) {
      setWorkspaceRoot(session.workspace_root || '');
      setError('');
    }
  }, [open, session]);

  if (!open || !session) return null;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      await updateSessionSettings(session.id, {}, workspaceRoot.trim() || undefined);
      onClose();
      onSaved();
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
        <label>{t('workspace.root')}<input autoFocus value={workspaceRoot} onChange={(event) => setWorkspaceRoot(event.target.value)} placeholder={t('workspace.absolutePath')} /></label>
        {session.workspace_root && <small className="form-hint">{t('workspace.current', { path: session.workspace_root })}</small>}
        {error && <div className="form-error">{error}</div>}
        <div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>{t('common.cancel')}</button><button className="primary-button" disabled={busy}>{busy ? t('workspace.saving') : t('workspace.save')}</button></div>
      </form>
    </div>
  );
};

export default SessionWorkspaceDialog;
