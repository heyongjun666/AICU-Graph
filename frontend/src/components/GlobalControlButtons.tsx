// ============================================================================
// GlobalControlButtons — 全局控制按钮（暂停/恢复/停止）
// ============================================================================

import React, { useState } from 'react';
import { useGraphStore } from '../stores/GraphStore';
import { useUIStateStore } from '../stores/UIStateStore';
import { useSessionStore } from '../stores/SessionStore';
import { ModalType, NodeStatus } from '../types';
import { pauseGraph, resumeGraph } from '../utils/api';
import { useI18n } from '../i18n';

const btnBase: React.CSSProperties = {
  padding: '6px 12px',
  borderRadius: 6,
  border: '1px solid #D1D5DB',
  backgroundColor: '#FFF',
  cursor: 'pointer',
  fontSize: 13,
  fontWeight: 500,
  display: 'flex',
  alignItems: 'center',
  gap: 4,
};

const GlobalControlButtons: React.FC = () => {
  const { t } = useI18n();
  const nodes = useGraphStore((state) => state.nodes);
  const openModal = useUIStateStore((state) => state.openModal);
  const addToast = useUIStateStore((state) => state.addToast);
  const sessionId = useSessionStore((state) => state.activeSessionId);
  const [paused, setPaused] = useState(false);

  // 检查是否有 running 节点
  const hasRunningNodes = Array.from(nodes.values()).some(
    (n) => n.status === NodeStatus.Running,
  );
  // 检查是否有 active 节点（非 terminal）
  const hasActiveNodes = Array.from(nodes.values()).some(
    (n) =>
      n.status !== NodeStatus.Completed &&
      n.status !== NodeStatus.Failed &&
      n.status !== NodeStatus.Stopped,
  );

  const handlePauseResume = async () => {
    if (!sessionId) {
      addToast({ type: 'warning', message: t('control.noActiveSession') });
      return;
    }
    try {
      if (paused) {
        await resumeGraph(sessionId);
        setPaused(false);
        addToast({ type: 'info', message: t('control.graphResumed') });
      } else {
        await pauseGraph(sessionId);
        setPaused(true);
        addToast({ type: 'info', message: t('control.graphPaused') });
      }
    } catch (err) {
      addToast({ type: 'error', message: paused ? t('control.failedResume') : t('control.failedPause') });
    }
  };

  const handleStop = () => {
    openModal(ModalType.StopConfirm);
  };

  return (
    <>
      <button
        onClick={handlePauseResume}
        style={{
          ...btnBase,
          color: paused ? '#059669' : '#D97706',
          opacity: hasActiveNodes ? 1 : 0.5,
        }}
        disabled={!hasActiveNodes}
        title={paused ? t('control.resume') : t('control.pause')}
      >
        {paused ? `▶ ${t('control.resume')}` : `⏸ ${t('control.pause')}`}
      </button>
      <button
        onClick={handleStop}
        style={{
          ...btnBase,
          color: '#DC2626',
          opacity: hasRunningNodes ? 1 : 0.5,
        }}
        disabled={!hasRunningNodes}
        title={t('common.stop')}
      >
        ⏹ {t('common.stop')}
      </button>
    </>
  );
};

export default GlobalControlButtons;
