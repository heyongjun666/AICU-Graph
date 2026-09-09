// ============================================================================
// SubmitTaskModal — 提交任务弹窗
// ============================================================================

import React, { useState, useEffect, useRef } from 'react';
import { useUIStateStore } from '../stores/UIStateStore';
import { useSessionStore } from '../stores/SessionStore';
import { ModalType, NodeConfig } from '../types';
import { createManager, getRoles, RoleInfo } from '../utils/api';
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

const labelStyle: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: '#374151',
  marginBottom: 4,
  display: 'block',
};

const inputStyle: React.CSSProperties = {
  width: '100%',
  padding: '8px 12px',
  borderRadius: 8,
  border: '1px solid #D1D5DB',
  fontSize: 13,
  outline: 'none',
  boxSizing: 'border-box',
};

const SubmitTaskModal: React.FC = () => {
  const { t } = useI18n();
  const isOpen = useUIStateStore((state) => state.modals[ModalType.SubmitTask]);
  const closeModal = useUIStateStore((state) => state.closeModal);
  const addToast = useUIStateStore((state) => state.addToast);
  const setCurrentTaskText = useUIStateStore((state) => state.setCurrentTaskText);
  const sessionId = useSessionStore((state) => state.activeSessionId);

  const [task, setTask] = useState('');
  const [role, setRole] = useState('');
  const [roles, setRoles] = useState<RoleInfo[]>([]);
  const [showRoleDropdown, setShowRoleDropdown] = useState(false);
  const [roleSearch, setRoleSearch] = useState('');
  const [numCCs, setNumCCs] = useState(3);
  // model 默认留空 = 跟随系统设置（会话配置 _session_model）。
  // 解析优先级：这里显式填写 > 角色 yaml 显式 > 系统设置 > CLI 自身默认。
  const [model, setModel] = useState('');
  const [maxIterations, setMaxIterations] = useState(5);
  const [submitting, setSubmitting] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // 打开时加载角色列表并聚焦
  useEffect(() => {
    if (isOpen) {
      setTask('');
      setRole('');
      setRoleSearch('');
      setNumCCs(3);
      setModel('');
      setMaxIterations(5);
      setSubmitting(false);

      getRoles().then(setRoles).catch(() => {
        setRoles([
          { id: 'dev_lead', display_name: 'Dev Lead' },
          { id: 'dev_member', display_name: 'Dev Member' },
          { id: 'acceptance', display_name: 'Acceptance' },
          { id: 'architecture', display_name: 'Architecture' },
          { id: 'test', display_name: 'Test' },
        ]);
      });

      setTimeout(() => inputRef.current?.focus(), 100);
    }
  }, [isOpen]);

  // Esc 关闭
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) closeModal(ModalType.SubmitTask);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [isOpen, closeModal]);

  if (!isOpen) return null;

  const filteredRoles = roles.filter(
    (r) =>
      r.display_name.toLowerCase().includes(roleSearch.toLowerCase()) ||
      r.id.toLowerCase().includes(roleSearch.toLowerCase()),
  );

  const handleSubmit = async () => {
    if (!task.trim()) {
      addToast({ type: 'warning', message: t('submit.enterTask') });
      return;
    }
    if (!role) {
      addToast({ type: 'warning', message: t('submit.selectRoleToast') });
      return;
    }

    setSubmitting(true);
    try {
      if (!sessionId) {
        addToast({ type: 'warning', message: t('submit.noActiveSession') });
        return;
      }
      const config: NodeConfig = {
        num_ccs: numCCs,
        max_iterations: maxIterations,
      };
      // 仅当显式填写了模型才作为「节点显式 model」下发；
      // 留空则交给服务端按 系统设置(_session_model) 解析，避免写死的旧模型压过系统设置。
      if (model.trim()) config.model = model.trim();

      await createManager(sessionId, { task: task.trim(), role, config });
      setCurrentTaskText(task.trim());
      addToast({ type: 'success', message: t('submit.submitted') });
      closeModal(ModalType.SubmitTask);
    } catch (err) {
      addToast({ type: 'error', message: t('submit.failed') });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={overlayStyle} onClick={() => closeModal(ModalType.SubmitTask)}>
      <div style={modalStyle} onClick={(e) => e.stopPropagation()}>
        <h2 style={{ fontSize: 18, fontWeight: 600, color: '#111827', margin: '0 0 16px' }}>
          {t('submit.title')}
        </h2>

        {/* Task 文本 */}
        <div style={{ marginBottom: 16 }}>
          <label style={labelStyle}>{t('submit.taskDescription')}</label>
          <textarea
            ref={inputRef}
            value={task}
            onChange={(e) => setTask(e.target.value.slice(0, MAX_TASK_LENGTH))}
            placeholder={t('submit.describeTask')}
            rows={6}
            style={{
              ...inputStyle,
              resize: 'vertical',
              fontFamily: 'monospace',
              fontSize: 12,
              lineHeight: 1.5,
            }}
          />
          <div style={{ textAlign: 'right', fontSize: 11, color: '#9CA3AF', marginTop: 2 }}>
            {task.length}/{MAX_TASK_LENGTH}
          </div>
        </div>

        {/* Role 选择 */}
        <div style={{ marginBottom: 16, position: 'relative' }}>
          <label style={labelStyle}>{t('node.role')}</label>
          <input
            value={roleSearch || role}
            onChange={(e) => {
              setRoleSearch(e.target.value);
              setRole('');
              setShowRoleDropdown(true);
            }}
            onFocus={() => setShowRoleDropdown(true)}
            placeholder={t('submit.selectRole')}
            style={inputStyle}
          />
          {showRoleDropdown && (
            <div
              style={{
                position: 'absolute',
                top: '100%',
                left: 0,
                right: 0,
                backgroundColor: '#FFF',
                border: '1px solid #E5E7EB',
                borderRadius: 8,
                boxShadow: '0 4px 12px rgba(0,0,0,0.1)',
                maxHeight: 200,
                overflow: 'auto',
                zIndex: 10,
              }}
            >
              {filteredRoles.map((r) => (
                <div
                  key={r.id}
                  onClick={() => {
                    setRole(r.id);
                    setRoleSearch(r.display_name);
                    setShowRoleDropdown(false);
                  }}
                  style={{
                    padding: '8px 12px',
                    cursor: 'pointer',
                    fontSize: 13,
                    color: '#374151',
                    borderBottom: '1px solid #F3F4F6',
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = '#F9FAFB')}
                  onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = 'transparent')}
                >
                  {r.display_name}
                  <span style={{ color: '#9CA3AF', marginLeft: 8, fontSize: 11 }}>
                    ({r.id})
                  </span>
                </div>
              ))}
              {filteredRoles.length === 0 && (
                <div style={{ padding: 8, color: '#9CA3AF', fontSize: 12 }}>
                  {t('submit.noRolesFound')}
                </div>
              )}
            </div>
          )}
        </div>

        {/* 高级配置 */}
        <div style={{ marginBottom: 20 }}>
          <details>
            <summary style={{ cursor: 'pointer', fontSize: 13, color: '#6B7280' }}>
              {t('submit.advancedConfig')}
            </summary>
            <div style={{ marginTop: 8, display: 'flex', gap: 12, flexDirection: 'column' }}>
              <div style={{ display: 'flex', gap: 12 }}>
                <div style={{ flex: 1 }}>
                  <label style={labelStyle}>{t('submit.numCCs')}</label>
                  <input
                    type="number"
                    min={1}
                    max={10}
                    value={numCCs}
                    onChange={(e) => setNumCCs(Number(e.target.value))}
                    style={{ ...inputStyle, width: '100%' }}
                  />
                </div>
                <div style={{ flex: 1 }}>
                  <label style={labelStyle}>{t('dialog.model')}</label>
                  <input
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                    placeholder={t('submit.modelPlaceholder')}
                    style={{ ...inputStyle, width: '100%' }}
                  />
                </div>
              </div>
              <div>
                <label style={labelStyle}>{t('submit.maxIterations')}</label>
                <input
                  type="number"
                  min={1}
                  max={100}
                  value={maxIterations}
                  onChange={(e) => setMaxIterations(Number(e.target.value))}
                  style={{ ...inputStyle, width: '100%' }}
                />
              </div>
            </div>
          </details>
        </div>

        {/* 按钮 */}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button
            onClick={() => closeModal(ModalType.SubmitTask)}
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
            onClick={handleSubmit}
            disabled={submitting}
            style={{
              padding: '8px 20px',
              borderRadius: 8,
              border: 'none',
              backgroundColor: submitting ? '#9CA3AF' : '#6366F1',
              color: '#FFF',
              cursor: submitting ? 'not-allowed' : 'pointer',
              fontSize: 13,
              fontWeight: 600,
            }}
          >
            {submitting ? t('submit.submitting') : t('submit.submit')}
          </button>
        </div>
      </div>
    </div>
  );
};

export default SubmitTaskModal;
