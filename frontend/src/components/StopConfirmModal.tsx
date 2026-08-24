// ============================================================================
// StopConfirmModal — 停止确认弹窗
// 列出所有非终止态节点，确认后停止整个图
// ============================================================================

import React, { useMemo } from 'react';
import { useUIStateStore } from '../stores/UIStateStore';
import { useGraphStore } from '../stores/GraphStore';
import { useSessionStore } from '../stores/SessionStore';
import { ModalType, NodeStatus } from '../types';
import { stopGraph } from '../utils/api';
import { useI18n } from '../i18n';

const overlayStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  backgroundColor: 'rgba(0,0,0,0.4)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  zIndex: 100,
};

const modalStyle: React.CSSProperties = {
  backgroundColor: '#FFF',
  borderRadius: 16,
  padding: 24,
  width: 480,
  maxHeight: '70vh',
  display: 'flex',
  flexDirection: 'column',
  boxShadow: '0 20px 60px rgba(0,0,0,0.2)',
};

const StopConfirmModal: React.FC = () => {
  const { t } = useI18n();
  const isOpen = useUIStateStore((state) => state.modals[ModalType.StopConfirm]);
  const closeModal = useUIStateStore((state) => state.closeModal);
  const addToast = useUIStateStore((state) => state.addToast);
  const nodes = useGraphStore((state) => state.nodes);
  const updateNode = useGraphStore((state) => state.updateNode);
  const sessionId = useSessionStore((state) => state.activeSessionId);

  const nonTerminalNodes = useMemo(
    () =>
      Array.from(nodes.values()).filter(
        (n) =>
          n.status !== NodeStatus.Completed &&
          n.status !== NodeStatus.Failed &&
          n.status !== NodeStatus.Stopped,
      ),
    [nodes],
  );

  if (!isOpen) return null;

  const handleConfirm = async () => {
    if (!sessionId) {
      addToast({ type: 'warning', message: t('control.noActiveSession') });
      return;
    }
    try {
      await stopGraph(sessionId);

      // Optimistic update
      for (const node of nonTerminalNodes) {
        updateNode(node.id, { status: NodeStatus.Stopped });
      }

      addToast({ type: 'success', message: t('stop.graphStopped', { count: nonTerminalNodes.length }) });
      closeModal(ModalType.StopConfirm);
    } catch (err) {
      addToast({ type: 'error', message: t('control.failedStop') });
    }
  };

  return (
    <div style={overlayStyle} onClick={() => closeModal(ModalType.StopConfirm)}>
      <div style={modalStyle} onClick={(e) => e.stopPropagation()}>
        <h2 style={{ fontSize: 18, fontWeight: 600, color: '#111827', margin: '0 0 8px' }}>
          {t('stop.title')}
        </h2>
        <p style={{ fontSize: 13, color: '#6B7280', margin: '0 0 16px' }}>
          {t('stop.desc')}
        </p>

        {/* Node list */}
        <div style={{ flex: 1, overflow: 'auto', marginBottom: 16 }}>
          {nonTerminalNodes.length === 0 ? (
            <div style={{ textAlign: 'center', color: '#9CA3AF', padding: 20, fontSize: 13 }}>
              {t('stop.noActiveNodes')}
            </div>
          ) : (
            nonTerminalNodes.map((node) => (
              <div
                key={node.id}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  padding: '6px 8px',
                  borderBottom: '1px solid #F3F4F6',
                  fontSize: 12,
                }}
              >
                <span style={{ fontWeight: 500, color: '#374151' }}>
                  {node.label}
                </span>
                <span style={{ color: '#6B7280' }}>
                  {node.status}
                </span>
              </div>
            ))
          )}
        </div>

        {/* Buttons */}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button
            onClick={() => closeModal(ModalType.StopConfirm)}
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
            {t('common.cancel')}
          </button>
          <button
            onClick={handleConfirm}
            disabled={nonTerminalNodes.length === 0}
            style={{
              padding: '8px 20px',
              borderRadius: 8,
              border: 'none',
              backgroundColor: nonTerminalNodes.length === 0 ? '#9CA3AF' : '#DC2626',
              color: '#FFF',
              cursor: nonTerminalNodes.length === 0 ? 'not-allowed' : 'pointer',
              fontSize: 13,
              fontWeight: 600,
            }}
          >
            {t('stop.stopNodes', { count: nonTerminalNodes.length })}
          </button>
        </div>
      </div>
    </div>
  );
};

export default StopConfirmModal;
