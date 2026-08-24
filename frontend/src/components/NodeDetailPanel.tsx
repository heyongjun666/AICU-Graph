// ============================================================================
// NodeDetailPanel — 节点详情侧边面板（4 个 Tab）
// Task / Artifacts / Logs / Subgraph
// ============================================================================

import React, { useState } from 'react';
import { useGraphStore } from '../stores/GraphStore';
import { useUIStateStore } from '../stores/UIStateStore';
import { useSessionStore } from '../stores/SessionStore';
import { ModalType, NodeStatus } from '../types';
import { continueNode } from '../utils/api';
import TaskTab from './TaskTab';
import ArtifactTab from './ArtifactTab';
import LogsTab from './LogsTab';
import SubgraphTab from './SubgraphTab';

const TABS = ['Task', 'Artifacts', 'Logs', 'Subgraph'] as const;

const API_BASE = (import.meta as any).env?.VITE_API_BASE_URL || '/v1/';
const baseUrl = API_BASE.endsWith('/') ? API_BASE : `${API_BASE}/`;

const panelStyle: React.CSSProperties = {
  position: 'absolute',
  top: 0,
  right: 0,
  width: 480,
  height: '100%',
  backgroundColor: '#FFF',
  borderLeft: '1px solid #E5E7EB',
  boxShadow: '-4px 0 20px rgba(0,0,0,0.08)',
  display: 'flex',
  flexDirection: 'column',
  zIndex: 20,
};

const NodeDetailPanel: React.FC = () => {
  const selectedNodeId = useGraphStore((state) => state.selectedNodeId);
  const sessionId = useSessionStore((state) => state.activeSessionId);
  const selectNode = useGraphStore((state) => state.selectNode);
  const getNode = useGraphStore((state) => state.getNode);
  const openModal = useUIStateStore((state) => state.openModal);
  const addToast = useUIStateStore((state) => state.addToast);
  const [activeTab, setActiveTab] = useState<string>('Task');
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  if (!selectedNodeId) return null;
  if (!sessionId) return null;

  const node = getNode(selectedNodeId);
  if (!node) return null;

  const handleClose = () => selectNode(null);

  // Status-specific action buttons
  const canStop = node.status === NodeStatus.Running || node.status === NodeStatus.Ready;
  const canPause = node.status === NodeStatus.Running;
  const canResume = node.status === NodeStatus.Paused;
  const canEdit = true;

  // Continue: 让任意 FAILED/STOPPED 节点从中断处继续执行
  const canContinue =
    node.status === NodeStatus.Failed || node.status === NodeStatus.Stopped;

  const handleEdit = () => openModal(ModalType.EditTask);

  const executeAction = async (action: string, fn: () => Promise<void>) => {
    setActionLoading(action);
    try {
      await fn();
      addToast({ type: 'success', message: `${action} succeeded` });
    } catch (err: any) {
      addToast({ type: 'error', message: `${action} failed: ${err.message}` });
    } finally {
      setActionLoading(null);
    }
  };

  const handleContinue = () => {
    if (!selectedNodeId) return;
    const nodeId: string = selectedNodeId;
    executeAction('Continue', () => continueNode(sessionId, nodeId));
  };

  const actionBtnStyle = (color: string, disabled: boolean): React.CSSProperties => ({
    padding: '4px 10px',
    borderRadius: 6,
    border: `1px solid ${disabled ? '#E5E7EB' : color}`,
    backgroundColor: disabled ? '#F9FAFB' : '#FFF',
    color: disabled ? '#D1D5DB' : color,
    cursor: disabled ? 'not-allowed' : 'pointer',
    fontSize: 11,
    fontWeight: 500,
  });

  return (
    <div style={panelStyle}>
      {/* Header */}
      <div
        style={{
          padding: '12px 16px',
          borderBottom: '1px solid #E5E7EB',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
            <span
              style={{
                padding: '1px 6px',
                borderRadius: 4,
                backgroundColor:
                  node.exec_type === 'M' ? '#E0E7FF' : '#F3E8FF',
                color: node.exec_type === 'M' ? '#4338CA' : '#7C3AED',
                fontWeight: 700,
                fontSize: 10,
              }}
            >
              {node.exec_type === 'M' ? 'M' : 'W'}
            </span>
            <span style={{ fontWeight: 600, fontSize: 14, color: '#111827' }}>
              {node.label}
            </span>
            <span
              style={{
                padding: '1px 6px',
                borderRadius: 4,
                backgroundColor: '#F3F4F6',
                color: '#6B7280',
                fontSize: 10,
                fontWeight: 500,
              }}
            >
              {node.role || 'N/A'}
            </span>
          </div>
          <div style={{ fontSize: 11, color: '#9CA3AF', wordBreak: 'break-all' }}>
            ID: {node.id}
          </div>
        </div>
        <button
          onClick={handleClose}
          style={{
            border: 'none',
            background: 'none',
            cursor: 'pointer',
            fontSize: 18,
            color: '#9CA3AF',
            padding: 0,
            lineHeight: 1,
          }}
        >
          ×
        </button>
      </div>

      {/* Action buttons */}
      <div
        style={{
          padding: '8px 16px',
          borderBottom: '1px solid #E5E7EB',
          display: 'flex',
          gap: 4,
          flexWrap: 'wrap',
        }}
      >
        <button style={actionBtnStyle('#DC2626', !canStop)} disabled={!canStop}>
          Stop
        </button>
        <button style={actionBtnStyle('#D97706', !canPause)} disabled={!canPause}>
          Pause
        </button>
        <button style={actionBtnStyle('#059669', !canResume)} disabled={!canResume}>
          Resume
        </button>
        {canContinue && (
          <button
            onClick={handleContinue}
            style={actionBtnStyle('#059669', actionLoading === 'continue')}
            disabled={actionLoading === 'continue'}
            title="从失败/停止状态恢复，继续执行之前未完成的任务"
          >
            {actionLoading === 'continue' ? '...' : 'Continue'}
          </button>
        )}
        <button onClick={handleEdit} style={actionBtnStyle('#6366F1', !canEdit)} disabled={!canEdit}>
          Edit
        </button>
      </div>

      {/* Tabs */}
      <div
        style={{
          display: 'flex',
          borderBottom: '1px solid #E5E7EB',
          flexShrink: 0,
        }}
      >
        {TABS.map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            style={{
              flex: 1,
              padding: '8px 0',
              border: 'none',
              background: 'none',
              cursor: 'pointer',
              fontSize: 12,
              fontWeight: activeTab === tab ? 600 : 400,
              color: activeTab === tab ? '#6366F1' : '#6B7280',
              borderBottom: activeTab === tab ? '2px solid #6366F1' : '2px solid transparent',
              transition: 'all 0.15s ease',
            }}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div style={{ flex: 1, overflow: 'auto' }}>
        {activeTab === 'Task' && <TaskTab nodeId={selectedNodeId} />}
        {activeTab === 'Artifacts' && <ArtifactTab nodeId={selectedNodeId} />}
        {activeTab === 'Logs' && <LogsTab nodeId={selectedNodeId} />}
        {activeTab === 'Subgraph' && <SubgraphTab nodeId={selectedNodeId} />}
      </div>
    </div>
  );
};

export default NodeDetailPanel;
