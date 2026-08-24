// ============================================================================
// SubmitTaskButton — 提交任务按钮
// ============================================================================

import React from 'react';
import { useUIStateStore } from '../stores/UIStateStore';
import { ModalType } from '../types';
import { useI18n } from '../i18n';

const SubmitTaskButton: React.FC = () => {
  const { t } = useI18n();
  const openModal = useUIStateStore((state) => state.openModal);

  const handleClick = () => {
    openModal(ModalType.SubmitTask);
  };

  return (
    <button
      onClick={handleClick}
      style={{
        padding: '6px 16px',
        borderRadius: 6,
        border: 'none',
        backgroundColor: '#6366F1',
        color: '#FFF',
        cursor: 'pointer',
        fontSize: 13,
        fontWeight: 600,
        display: 'flex',
        alignItems: 'center',
        gap: 4,
      }}
    >
      + {t('submit.newTask')}
    </button>
  );
};

export default SubmitTaskButton;
