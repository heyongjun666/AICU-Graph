// ============================================================================
// ToastContainer — Toast 通知容器
// 固定在右上角，自动消失，支持 4 种类型
// ============================================================================

import React, { useEffect, useRef } from 'react';
import { useUIStateStore } from '../stores/UIStateStore';
import { Toast } from '../types';

// ============================================================================
// Toast 样式配置
// ============================================================================

const TOAST_STYLES: Record<
  Toast['type'],
  { bg: string; border: string; icon: string }
> = {
  success: { bg: '#F0FDF4', border: '#86EFAC', icon: '✓' },
  error: { bg: '#FEF2F2', border: '#FCA5A5', icon: '✕' },
  info: { bg: '#EFF6FF', border: '#93C5FD', icon: 'ℹ' },
  warning: { bg: '#FEFCE8', border: '#FDE68A', icon: '⚠' },
};

// ============================================================================
// ToastItem 组件
// ============================================================================

const ToastItem: React.FC<{ toast: Toast; onRemove: (id: string) => void }> = ({
  toast,
  onRemove,
}) => {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const style = TOAST_STYLES[toast.type];

  useEffect(() => {
    const duration = toast.duration ?? 3000;
    timerRef.current = setTimeout(() => {
      onRemove(toast.id);
    }, duration);

    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [toast.id, toast.duration, onRemove]);

  return (
    <div
      role={toast.type === 'error' ? 'alert' : 'status'}
      aria-live={toast.type === 'error' ? 'assertive' : 'polite'}
      style={{
        padding: '8px 14px',
        borderRadius: 8,
        backgroundColor: style.bg,
        border: `1px solid ${style.border}`,
        boxShadow: '0 4px 12px rgba(0,0,0,0.1)',
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        fontSize: 13,
        color: '#374151',
        animation: 'slideIn 0.3s ease',
        maxWidth: 360,
      }}
    >
      <span style={{ fontWeight: 700 }}>{style.icon}</span>
      <span style={{ flex: 1 }}>{toast.message}</span>
      <button
        onClick={() => onRemove(toast.id)}
        style={{
          border: 'none',
          background: 'none',
          cursor: 'pointer',
          color: '#9CA3AF',
          fontSize: 14,
          padding: 0,
          lineHeight: 1,
        }}
      >
        ×
      </button>
    </div>
  );
};

// ============================================================================
// ToastContainer
// ============================================================================

const ToastContainer: React.FC = () => {
  const toasts = useUIStateStore((state) => state.toasts);
  const removeToast = useUIStateStore((state) => state.removeToast);

  if (toasts.length === 0) return null;

  return (
    <div
      aria-live="polite"
      style={{
        position: 'fixed',
        top: 16,
        right: 16,
        zIndex: 60,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      {toasts.map((toast) => (
        <ToastItem key={toast.id} toast={toast} onRemove={removeToast} />
      ))}
    </div>
  );
};

export default ToastContainer;
