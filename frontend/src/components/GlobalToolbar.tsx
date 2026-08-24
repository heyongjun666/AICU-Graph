// ============================================================================
// GlobalToolbar — 全局工具栏
// 包含：提交任务按钮、全局控制按钮、导出、搜索日志、并发度滑块
// ============================================================================

import React, { useState } from 'react';
import { useUIStateStore } from '../stores/UIStateStore';
import { useGraphStore } from '../stores/GraphStore';
import { useSessionStore } from '../stores/SessionStore';
import { ModalType } from '../types';
import { clearGraph } from '../utils/api';
import { useI18n } from '../i18n';
import GlobalControlButtons from './GlobalControlButtons';
import SubmitTaskButton from './SubmitTaskButton';
import ConcurrencySlider from './ConcurrencySlider';

const toolbarStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  padding: '8px 16px',
  backgroundColor: '#FFF',
  borderBottom: '1px solid #E5E7EB',
  gap: 12,
  flexShrink: 0,
};

const leftStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
};

const rightStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
};

const btnBase: React.CSSProperties = {
  padding: '6px 12px',
  borderRadius: 6,
  border: '1px solid #D1D5DB',
  backgroundColor: '#FFF',
  cursor: 'pointer',
  fontSize: 13,
  fontWeight: 500,
  color: '#374151',
  display: 'flex',
  alignItems: 'center',
  gap: 4,
};

const selectStyle: React.CSSProperties = {
  padding: '6px 8px',
  borderRadius: 6,
  border: '1px solid #D1D5DB',
  backgroundColor: '#FFF',
  fontSize: 13,
  color: '#374151',
  cursor: 'pointer',
};

const GlobalToolbar: React.FC = () => {
  const { t } = useI18n();
  const openModal = useUIStateStore((state) => state.openModal);
  const addToast = useUIStateStore((state) => state.addToast);
  const clientStatus = useUIStateStore((state) => state.clientStatus);
  const resetGraph = useGraphStore((state) => state.resetGraph);
  const sessionId = useSessionStore((state) => state.activeSessionId);
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [typeFilter, setTypeFilter] = useState<string>('all');
  const [clearing, setClearing] = useState(false);

  const handleExport = () => openModal(ModalType.Export);
  const handleSearchLogs = () => openModal(ModalType.GlobalLogSearch);

  const handleClearGraph = async () => {
    if (!window.confirm(t('toolbar.clearGraphConfirm'))) {
      return;
    }
    setClearing(true);
    try {
      if (!sessionId) {
        addToast({ type: 'warning', message: t('toolbar.noActiveSession') });
        return;
      }
      await clearGraph(sessionId);
      resetGraph();
      addToast({ type: 'success', message: t('toolbar.graphCleared') });
    } catch (err) {
      addToast({ type: 'error', message: t('toolbar.failedClearGraph') });
    } finally {
      setClearing(false);
    }
  };

  return (
    <div style={toolbarStyle}>
      <div style={leftStyle}>
        <SubmitTaskButton />
        <GlobalControlButtons />
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          style={selectStyle}
          title={t('toolbar.filterByStatus')}
        >
          <option value="all">{t('toolbar.allStatus')}</option>
          <option value="Waiting">{t('status.waiting')}</option>
          <option value="Ready">{t('status.ready')}</option>
          <option value="Running">{t('status.running')}</option>
          <option value="Completed">{t('status.completed')}</option>
          <option value="Failed">{t('status.failed')}</option>
          <option value="Stopped">{t('status.stopped')}</option>
          <option value="Paused">{t('status.paused')}</option>
        </select>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          style={selectStyle}
          title={t('toolbar.filterByType')}
        >
          <option value="all">{t('toolbar.allTypes')}</option>
          <option value="manager">{t('dialog.manager')}</option>
          <option value="worker">{t('dialog.worker')}</option>
        </select>
        <button
          onClick={handleSearchLogs}
          style={btnBase}
          title={t('toolbar.searchLogs')}
        >
          {t('toolbar.searchLogs')}
        </button>
        <button
          onClick={handleExport}
          style={btnBase}
          title={t('toolbar.export')}
        >
          {t('toolbar.export')}
        </button>
        <button
          onClick={handleClearGraph}
          disabled={clearing}
          style={{
            ...btnBase,
            color: '#DC2626',
            borderColor: '#FCA5A5',
            cursor: clearing ? 'not-allowed' : 'pointer',
            opacity: clearing ? 0.5 : 1,
          }}
          title={t('toolbar.clearGraph')}
        >
          {clearing ? t('toolbar.clearing') : t('toolbar.clearGraph')}
        </button>
        <ConcurrencySlider />
        <span
          style={{
            fontSize: 11,
            color: '#9CA3AF',
            padding: '2px 6px',
            backgroundColor: '#F3F4F6',
            borderRadius: 4,
          }}
        >
          {clientStatus}
        </span>
      </div>
    </div>
  );
};

export default GlobalToolbar;
