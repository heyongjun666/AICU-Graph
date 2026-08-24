import React, { useState } from 'react';
import {
  FolderOpen,
  Menu,
  Pause,
  Play,
  Plus,
  ShieldAlert,
  Square,
  SquareTerminal,
  Wifi,
  WifiOff,
  Zap,
} from 'lucide-react';
import { SessionSummary, SSEClientStatus } from '../types';
import { controlSession } from '../utils/api';
import { useUIStateStore } from '../stores/UIStateStore';
import { useI18n } from '../i18n';

interface WorkbenchHeaderProps {
  session: SessionSummary;
  clientStatus: SSEClientStatus;
  onOpenSessions: () => void;
  onAddNode: () => void;
  outputsOpen: boolean;
  onToggleOutputs: () => void;
  onOpenEvents: () => void;
  onRefresh: () => void;
  terminalOpen: boolean;
  onToggleTerminal: () => void;
}

const WorkbenchHeader: React.FC<WorkbenchHeaderProps> = ({
  session,
  clientStatus,
  onOpenSessions,
  onAddNode,
  outputsOpen,
  onToggleOutputs,
  onOpenEvents,
  onRefresh,
  terminalOpen,
  onToggleTerminal,
}) => {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const addToast = useUIStateStore((state) => state.addToast);
  const connected = clientStatus === SSEClientStatus.Connected;
  const lifecycleState = session.lifecycle_state || 'running';
  const canPause = lifecycleState === 'running';
  const canResume = lifecycleState === 'paused' || lifecycleState === 'recovery_required' || lifecycleState === 'failed';
  const canStop = !['stopped', 'completed'].includes(lifecycleState);

  const run = async (
    action: 'pause' | 'resume' | 'stop',
    payload?: { mode?: 'drain' | 'immediate' },
  ) => {
    setBusy(true);
    setError('');
    try {
      await controlSession(session.id, action, payload);
      const messages: Record<string, string> = {
        pause: payload?.mode === 'immediate' ? t('header.sessionInterrupted') : t('header.sessionPausing'),
        resume: t('header.sessionResumed'),
        stop: t('header.sessionStopped'),
      };
      addToast({ type: 'success', message: messages[action] });
      await onRefresh();
    } catch (reason) {
      const msg = reason instanceof Error ? reason.message : String(reason);
      setError(msg);
      addToast({ type: 'error', message: msg });
    } finally {
      setBusy(false);
    }
  };

  const pauseImmediately = () => {
    const confirmed = window.confirm(t('header.pauseImmediateConfirm'));
    if (confirmed) void run('pause', { mode: 'immediate' });
  };

  const resumeSession = () => {
    if (session.interrupted_node_ids?.length) {
      const confirmed = window.confirm(
        t('header.resumeInterruptedConfirm', { count: session.interrupted_node_ids.length }),
      );
      if (!confirmed) return;
    }
    void run('resume');
  };

  return (
    <header className="workbench-header">
      <div className="workbench-heading">
        <button className="icon-button mobile-menu" onClick={onOpenSessions} title={t('header.openSessions')}><Menu size={19} /></button>
        <button className={`icon-button header-outputs-toggle ${outputsOpen ? 'is-active' : ''}`} onClick={onToggleOutputs} title={outputsOpen ? t('header.hideSharedWorkspace') : t('header.viewSharedWorkspace')}>
          <FolderOpen size={17} />
        </button>
        <div>
          <h1>{session.title}</h1>
          <div className="header-meta">
            <span className={connected ? 'connected' : 'disconnected'}>
              {connected ? <Wifi size={13} /> : <WifiOff size={13} />}
              {connected ? t('header.live') : t('header.reconnecting')}
            </span>
            <span>{t('header.nodes', { count: session.node_count })}</span>
            <span>{t('header.completed', { count: session.completed_count })}</span>
            <span className={`lifecycle-state lifecycle-${lifecycleState}`}>
              {lifecycleState.replace('_', ' ')}
              {lifecycleState === 'pausing' && ` · ${t('header.running', { count: session.running_count })}`}
            </span>
            {lifecycleState === 'paused' && session.restart_safe && <span className="restart-safe">{t('header.safeToRestart')}</span>}
            {lifecycleState === 'recovery_required' && <span className="recovery-required">{t('header.explicitResumeRequired')}</span>}
            <span>{session.config?.cli_name || 'internal'} · {session.config?.model || t('header.defaultModel')}</span>
            {session.workspace_root && <span className="workspace-meta" title={session.workspace_root}>{session.workspace_root}</span>}
            {session.failed_count > 0 && <span className="failed-count">{t('header.failed', { count: session.failed_count })}</span>}
          </div>
        </div>
      </div>
      <div className="header-actions">
        {error && <span className="header-error" title={error}>{error}</span>}
        <button className={`icon-button ${terminalOpen ? 'header-terminal-toggle is-active' : 'header-terminal-toggle'}`} onClick={onToggleTerminal} title={terminalOpen ? t('header.hideTerminal') : t('header.openTerminal')}>
          <SquareTerminal size={17} />
        </button>
        <button className="icon-button" onClick={onOpenEvents} title={t('header.agentEvents')}><Zap size={17} /></button>
        <button className="secondary-button add-node-button" onClick={onAddNode}><Plus size={16} /> {t('header.addNode')}</button>
        <div className="segmented-actions" aria-label={t('header.sessionControls')}>
          <button onClick={() => run('pause', { mode: 'drain' })} disabled={busy || !canPause} title={t('header.drainPauseSession')}><Pause size={16} /></button>
          <button onClick={pauseImmediately} disabled={busy || !canPause} title={t('header.immediatelyInterruptSession')}><ShieldAlert size={16} /></button>
          <button onClick={resumeSession} disabled={busy || !canResume} title={t('header.resumeSession')}><Play size={16} /></button>
          <button onClick={() => run('stop')} disabled={busy || !canStop} title={t('header.stopSession')}><Square size={15} /></button>
        </div>
      </div>
    </header>
  );
};

export default WorkbenchHeader;
