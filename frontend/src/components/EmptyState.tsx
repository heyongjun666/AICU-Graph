// ============================================================================
// EmptyState — 空状态/崩溃恢复状态
// 首次加载无数据时展示欢迎界面，崩溃时展示恢复引导
// ============================================================================

import React from 'react';
import { useUIStateStore } from '../stores/UIStateStore';
import { useGraphStore } from '../stores/GraphStore';
import { useSessionStore } from '../stores/SessionStore';
import { ModalType } from '../types';
import { clearGraph } from '../utils/api';
import { useI18n } from '../i18n';

const containerStyle: React.CSSProperties = {
  position: 'absolute',
  inset: 0,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  backgroundColor: 'rgba(255,255,255,0.9)',
  backdropFilter: 'blur(4px)',
  zIndex: 50,
};

const cardStyle: React.CSSProperties = {
  backgroundColor: '#FFF',
  borderRadius: 16,
  padding: '40px 48px',
  boxShadow: '0 10px 40px rgba(0,0,0,0.12)',
  textAlign: 'center',
  maxWidth: 440,
};

const EmptyState: React.FC = () => {
  const { t } = useI18n();
  const crashed = useGraphStore((state) => state.crashed);
  const crashedAt = useGraphStore((state) => state.crashedAt);
  const affectedNodes = useGraphStore((state) => state.affectedNodes);
  const resetGraph = useGraphStore((state) => state.resetGraph);
  const openModal = useUIStateStore((state) => state.openModal);
  const addToast = useUIStateStore((state) => state.addToast);
  const sessionId = useSessionStore((state) => state.activeSessionId);

  // 崩溃恢复
  if (crashed) {
    const handleContinue = () => {
      useGraphStore.setState({ crashed: false, crashedAt: null, affectedNodes: [] });
      addToast({ type: 'info', message: t('empty.recoveryMode') });
    };

    const handleDiscard = async () => {
      if (sessionId) {
        try {
          await clearGraph(sessionId);
        } catch {
          // ignore
        }
      }
      resetGraph();
      useGraphStore.setState({ crashed: false, crashedAt: null, affectedNodes: [] });
      addToast({ type: 'info', message: t('empty.graphDiscarded') });
    };

    return (
      <div style={containerStyle}>
        <div style={cardStyle}>
          <div
            style={{
              fontSize: 48,
              marginBottom: 12,
              color: '#F59E0B',
            }}
          >
            ⚠
          </div>
          <h2 style={{ fontSize: 20, fontWeight: 600, color: '#374151', margin: '0 0 8px' }}>
            {t('empty.graphCrashed')}
          </h2>
          {crashedAt && (
            <p style={{ fontSize: 13, color: '#6B7280', margin: '0 0 16px' }}>
              {t('empty.crashedAt', { time: new Date(crashedAt).toLocaleString() })}
            </p>
          )}
          {affectedNodes.length > 0 && (
            <div style={{ marginBottom: 16 }}>
              <p style={{ fontSize: 12, color: '#9CA3AF', margin: '0 0 4px' }}>
                {t('empty.affectedNodes', { count: affectedNodes.length })}
              </p>
              <div
                style={{
                  display: 'flex',
                  flexWrap: 'wrap',
                  gap: 4,
                  justifyContent: 'center',
                }}
              >
                {affectedNodes.slice(0, 20).map((id) => (
                  <span
                    key={id}
                    style={{
                      padding: '1px 6px',
                      borderRadius: 4,
                      backgroundColor: '#FEF2F2',
                      color: '#DC2626',
                      fontSize: 11,
                    }}
                  >
                    {id}
                  </span>
                ))}
                {affectedNodes.length > 20 && (
                  <span style={{ fontSize: 11, color: '#9CA3AF' }}>
                    {t('empty.more', { count: affectedNodes.length - 20 })}
                  </span>
                )}
              </div>
            </div>
          )}
          <div style={{ display: 'flex', gap: 8, justifyContent: 'center' }}>
            <button
              onClick={handleContinue}
              style={{
                padding: '8px 20px',
                borderRadius: 8,
                border: '1px solid #D1D5DB',
                backgroundColor: '#FFF',
                cursor: 'pointer',
                fontSize: 13,
                fontWeight: 500,
                color: '#374151',
              }}
            >
              {t('empty.continue')}
            </button>
            <button
              onClick={handleDiscard}
              style={{
                padding: '8px 20px',
                borderRadius: 8,
                border: 'none',
                backgroundColor: '#DC2626',
                cursor: 'pointer',
                fontSize: 13,
                fontWeight: 500,
                color: '#FFF',
              }}
            >
              {t('empty.discardReset')}
            </button>
          </div>
        </div>
      </div>
    );
  }

  // 空状态欢迎
  return (
    <div style={containerStyle}>
      <div style={cardStyle}>
        <div style={{ fontSize: 48, marginBottom: 12 }}>🚀</div>
        <h2 style={{ fontSize: 20, fontWeight: 600, color: '#374151', margin: '0 0 8px' }}>
          {t('empty.welcome')}
        </h2>
        <p style={{ fontSize: 14, color: '#6B7280', margin: '0 0 24px', lineHeight: 1.6 }}>
          {t('empty.welcomeDesc')}
        </p>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'center' }}>
          <button
            onClick={() => openModal(ModalType.SubmitTask)}
            style={{
              padding: '10px 24px',
              borderRadius: 8,
              border: 'none',
              backgroundColor: '#6366F1',
              color: '#FFF',
              cursor: 'pointer',
              fontSize: 14,
              fontWeight: 600,
            }}
          >
            {t('empty.submitNewTask')}
          </button>
        </div>
      </div>
    </div>
  );
};

export default EmptyState;
