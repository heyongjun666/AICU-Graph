// ============================================================================
// EditTaskModal — 编辑任务弹窗
// ============================================================================

import React, { useState, useEffect } from 'react';
import { useUIStateStore } from '../stores/UIStateStore';
import { useGraphStore } from '../stores/GraphStore';
import { useSessionStore } from '../stores/SessionStore';
import { ModalType, NodeConfig } from '../types';
import { updateNodeTask } from '../utils/api';
import { MAX_TASK_LENGTH } from '../constants';
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
  width: 560,
  maxHeight: '80vh',
  overflow: 'auto',
  boxShadow: '0 20px 60px rgba(0,0,0,0.2)',
};

const EditTaskModal: React.FC = () => {
  const { t } = useI18n();
  const isOpen = useUIStateStore((state) => state.modals[ModalType.EditTask]);
  const closeModal = useUIStateStore((state) => state.closeModal);
  const addToast = useUIStateStore((state) => state.addToast);
  const selectedNodeId = useGraphStore((state) => state.selectedNodeId);
  const getNode = useGraphStore((state) => state.getNode);
  const getNodeDependents = useGraphStore((state) => state.getNodeDependents);
  const updateNode = useGraphStore((state) => state.updateNode);
  const sessionId = useSessionStore((state) => state.activeSessionId);

  const [task, setTask] = useState('');
  const [configText, setConfigText] = useState('');
  const [configError, setConfigError] = useState('');
  const [maxIterations, setMaxIterations] = useState<number>(5);
  const [saving, setSaving] = useState(false);

  const node = selectedNodeId ? getNode(selectedNodeId) : null;
  const dependents = selectedNodeId ? getNodeDependents(selectedNodeId) : [];

  // Initialize form
  useEffect(() => {
    if (isOpen && node) {
      setTask(node.task || '');
      setMaxIterations(node.max_iterations || 5);
      setConfigText(JSON.stringify(node.config || {}, null, 2));
      setConfigError('');
    }
  }, [isOpen, node]);

  // Esc
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) closeModal(ModalType.EditTask);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [isOpen, closeModal]);

  if (!isOpen || !node) return null;

  const isManager = node.exec_type === 'M';

  const handleConfigChange = (value: string) => {
    setConfigText(value);
    try {
      JSON.parse(value);
      setConfigError('');
    } catch {
      setConfigError(t('editTask.invalidJson'));
    }
  };

  // Collect all descendant IDs for manager subtree clearing
  const collectDescendantIds = (nodeId: string): string[] => {
    const { nodes, edges } = useGraphStore.getState();
    const ids: string[] = [];
    for (const edge of edges.values()) {
      if (edge.type === 'parent' && edge.source === nodeId) {
        ids.push(edge.target);
        ids.push(...collectDescendantIds(edge.target));
      }
    }
    return ids;
  };

  const handleSave = async () => {
    if (configError) {
      addToast({ type: 'warning', message: t('editTask.fixJson') });
      return;
    }

    setSaving(true);
    try {
      if (!sessionId) {
        addToast({ type: 'warning', message: t('control.noActiveSession') });
        return;
      }
      let parsedConfig: NodeConfig | undefined;
      if (configText.trim()) {
        parsedConfig = JSON.parse(configText);
        if (isManager && parsedConfig) {
          parsedConfig.max_iterations = maxIterations;
        }
      }

      await updateNodeTask(sessionId, node.id, {
        task: task.trim() || undefined,
        config: parsedConfig,
      });

      // If manager, clear subtree
      if (isManager) {
        const descendants = collectDescendantIds(node.id);
        for (const descId of descendants) {
          updateNode(descId, { status: 'Waiting' as any });
        }
      }

      // Also update local store
      const updates: any = {};
      if (task.trim()) updates.task = task.trim();
      if (parsedConfig) updates.config = parsedConfig;
      updateNode(node.id, updates);

      addToast({ type: 'success', message: t('editTask.updated') });

      closeModal(ModalType.EditTask);
    } catch (err) {
      addToast({ type: 'error', message: t('editTask.failed') });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={overlayStyle} onClick={() => closeModal(ModalType.EditTask)}>
      <div style={modalStyle} onClick={(e) => e.stopPropagation()}>
        <h2 style={{ fontSize: 18, fontWeight: 600, color: '#111827', margin: '0 0 4px' }}>
          {t('editTask.title')}
        </h2>
        <p style={{ fontSize: 12, color: '#6B7280', margin: '0 0 16px' }}>
          {t('editTask.node', { id: node.id, label: node.label })}
          {isManager ? ` — ${t('dialog.manager')}` : ''}
        </p>

        {/* Task 文本 */}
        <div style={{ marginBottom: 16 }}>
          <label style={{ fontSize: 13, fontWeight: 600, color: '#374151', marginBottom: 4, display: 'block' }}>
            {t('submit.taskDescription')}
          </label>
          <textarea
            value={task}
            onChange={(e) => setTask(e.target.value.slice(0, MAX_TASK_LENGTH))}
            rows={5}
            style={{
              width: '100%',
              padding: '8px 12px',
              borderRadius: 8,
              border: '1px solid #D1D5DB',
              fontSize: 12,
              fontFamily: 'monospace',
              lineHeight: 1.5,
              resize: 'vertical',
              outline: 'none',
              boxSizing: 'border-box',
            }}
          />
          <div style={{ textAlign: 'right', fontSize: 11, color: '#9CA3AF', marginTop: 2 }}>
            {task.length}/{MAX_TASK_LENGTH}
          </div>
        </div>

        {/* Manager: max_iterations */}
        {isManager && (
          <div style={{ marginBottom: 16 }}>
            <label style={{ fontSize: 13, fontWeight: 600, color: '#374151', marginBottom: 4, display: 'block' }}>
              {t('submit.maxIterations')}
            </label>
            <input
              type="number"
              min={1}
              max={100}
              value={maxIterations}
              onChange={(e) => setMaxIterations(Number(e.target.value))}
              style={{
                width: '100%',
                padding: '8px 12px',
                borderRadius: 8,
                border: '1px solid #D1D5DB',
                fontSize: 13,
                outline: 'none',
                boxSizing: 'border-box',
              }}
            />
          </div>
        )}

        {/* Config JSON */}
        <div style={{ marginBottom: 16 }}>
          <label style={{ fontSize: 13, fontWeight: 600, color: '#374151', marginBottom: 4, display: 'block' }}>
            {t('editTask.configJson')}
          </label>
          <textarea
            value={configText}
            onChange={(e) => handleConfigChange(e.target.value)}
            rows={6}
            style={{
              width: '100%',
              padding: '8px 12px',
              borderRadius: 8,
              border: `1px solid ${configError ? '#EF4444' : '#D1D5DB'}`,
              fontSize: 11,
              fontFamily: 'monospace',
              lineHeight: 1.5,
              resize: 'vertical',
              outline: 'none',
              boxSizing: 'border-box',
            }}
          />
          {configError && (
            <div style={{ color: '#EF4444', fontSize: 11, marginTop: 2 }}>{configError}</div>
          )}
        </div>

        {/* Dependent node warning */}
        {dependents.length > 0 && (
          <div
            style={{
              padding: '8px 12px',
              borderRadius: 8,
              backgroundColor: '#FEFCE8',
              border: '1px solid #FDE68A',
              fontSize: 12,
              color: '#92400E',
              marginBottom: 16,
            }}
          >
            <strong>{t('editTask.dependentNodes', { count: dependents.length })}</strong> {t('editTask.changesMayAffect')}
          </div>
        )}

        {/* Buttons */}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button
            onClick={() => closeModal(ModalType.EditTask)}
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
            onClick={handleSave}
            disabled={saving || !!configError}
            style={{
              padding: '8px 20px',
              borderRadius: 8,
              border: 'none',
              backgroundColor: saving || configError ? '#9CA3AF' : '#6366F1',
              color: '#FFF',
              cursor: saving || configError ? 'not-allowed' : 'pointer',
              fontSize: 13,
              fontWeight: 600,
            }}
          >
            {saving ? t('workspace.saving') : t('common.save')}
          </button>
        </div>
      </div>
    </div>
  );
};

export default EditTaskModal;
