// ============================================================================
// StatusBar — 底部状态栏
// 显示 7 种状态节点计数 + 总数
// ============================================================================

import React, { useMemo } from 'react';
import { useGraphStore } from '../stores/GraphStore';
import { useI18n } from '../i18n';

const STATUS_ITEMS = [
  { key: 'waiting', label: 'status.waiting', color: '#9CA3AF' },
  { key: 'ready', label: 'status.ready', color: '#3B82F6' },
  { key: 'running', label: 'status.running', color: '#F59E0B' },
  { key: 'completed', label: 'status.completed', color: '#10B981' },
  { key: 'failed', label: 'status.failed', color: '#EF4444' },
  { key: 'stopped', label: 'status.stopped', color: '#F97316' },
  { key: 'paused', label: 'status.paused', color: '#8B5CF6' },
] as const;

const StatusBar: React.FC = () => {
  const { t } = useI18n();
  const stats = useGraphStore((state) => state.stats);

  // 确保 stats 是最新的
  const items = useMemo(
    () =>
      STATUS_ITEMS.map((item) => ({
        ...item,
        count: stats[item.key as keyof typeof stats] as number,
      })),
    [stats],
  );

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '4px 16px',
        backgroundColor: '#F9FAFB',
        borderTop: '1px solid #E5E7EB',
        fontSize: 12,
        flexShrink: 0,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        {items.map((item) => (
          <span
            key={item.key}
            style={{ display: 'flex', alignItems: 'center', gap: 4 }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                backgroundColor: item.color,
                display: 'inline-block',
              }}
            />
            <span style={{ color: '#6B7280' }}>{t(item.label)}</span>
            <span style={{ fontWeight: 600, color: '#374151' }}>{item.count}</span>
          </span>
        ))}
      </div>
      <div style={{ color: '#9CA3AF' }}>
        {t('common.total')}: <strong style={{ color: '#374151' }}>{stats.total}</strong>
      </div>
    </div>
  );
};

export default React.memo(StatusBar);
