// ============================================================================
// TaskTab — 节点任务详情 Tab
// ============================================================================

import React from 'react';
import { useGraphStore } from '../stores/GraphStore';
import { useI18n } from '../i18n';

interface TaskTabProps {
  nodeId: string;
}

const TaskTab: React.FC<TaskTabProps> = ({ nodeId }) => {
  const { t } = useI18n();
  const getNode = useGraphStore((state) => state.getNode);
  const node = getNode(nodeId);

  if (!node) return null;

  const sectionStyle: React.CSSProperties = {
    padding: '12px 16px',
    borderBottom: '1px solid #F3F4F6',
  };

  const labelStyle: React.CSSProperties = {
    fontSize: 11,
    fontWeight: 600,
    color: '#9CA3AF',
    textTransform: 'uppercase',
    marginBottom: 4,
  };

  const valueStyle: React.CSSProperties = {
    fontSize: 13,
    color: '#374151',
    lineHeight: 1.5,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
  };

  return (
    <div>
      {/* Task */}
      <div style={sectionStyle}>
        <div style={labelStyle}>{t('node.task')}</div>
        <div style={valueStyle}>{node.task || t('inspector.noTaskDescription')}</div>
      </div>

      {/* Role */}
      <div style={sectionStyle}>
        <div style={labelStyle}>{t('node.role')}</div>
        <div style={valueStyle}>{node.role || 'N/A'}</div>
      </div>

      {/* Config */}
      {node.config && Object.keys(node.config).length > 0 && (
        <div style={sectionStyle}>
          <div style={labelStyle}>{t('node.config')}</div>
          <pre
            style={{
              fontSize: 11,
              color: '#374151',
              backgroundColor: '#F9FAFB',
              padding: 8,
              borderRadius: 6,
              overflow: 'auto',
              margin: 0,
              lineHeight: 1.4,
            }}
          >
            {JSON.stringify(node.config, null, 2)}
          </pre>
        </div>
      )}

      {/* Timeline */}
      <div style={sectionStyle}>
        <div style={labelStyle}>{t('taskTab.timeline')}</div>
        <div style={{ fontSize: 12, color: '#6B7280', lineHeight: 1.8 }}>
          {node.created_at && (
            <div>{t('taskTab.created')} {new Date(node.created_at).toLocaleString()}</div>
          )}
          {node.started_at && (
            <div>{t('taskTab.started')} {new Date(node.started_at).toLocaleString()}</div>
          )}
          {node.finished_at && (
            <div>{t('taskTab.finished')} {new Date(node.finished_at).toLocaleString()}</div>
          )}
          {!node.created_at && !node.started_at && !node.finished_at && (
            <div>{t('taskTab.noTimeline')}</div>
          )}
        </div>
      </div>

      {/* Iteration (for Manager) */}
      {node.exec_type === 'M' && (
        <div style={sectionStyle}>
          <div style={labelStyle}>{t('taskTab.iterationProgress')}</div>
          <div style={{ fontSize: 13, color: '#374151' }}>
            {node.iteration != null ? (
              <span>
                {t('taskTab.iteration', { n: node.iteration })}
                {node.max_iterations != null ? ` / ${node.max_iterations}` : ''}
              </span>
            ) : (
              'N/A'
            )}
          </div>
        </div>
      )}

      {/* Error */}
      {node.error && (
        <div style={{ ...sectionStyle, backgroundColor: '#FEF2F2' }}>
          <div style={{ ...labelStyle, color: '#DC2626' }}>{t('common.error')}</div>
          <div style={{ ...valueStyle, color: '#DC2626' }}>{node.error}</div>
        </div>
      )}
    </div>
  );
};

export default TaskTab;
