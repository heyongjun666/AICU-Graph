// ============================================================================
// TreeNode — 自定义树节点组件（ReactFlow）
// 7 种状态颜色/形状编码、色盲模式（形状标记）、脉冲动画
// ============================================================================

import React from 'react';
import { Handle, Position, NodeProps } from 'reactflow';
import { RFNodeData, NodeStatus } from '../types';
import { useI18n } from '../i18n';

// ============================================================================
// 状态颜色配置
// ============================================================================

const STATUS_STYLES: Record<
  NodeStatus,
  { bg: string; border: string; text: string; badge: string }
> = {
  [NodeStatus.Waiting]: {
    bg: '#F3F4F6',
    border: '#D1D5DB',
    text: '#6B7280',
    badge: '#9CA3AF',
  },
  [NodeStatus.Ready]: {
    bg: '#EFF6FF',
    border: '#93C5FD',
    text: '#1D4ED8',
    badge: '#3B82F6',
  },
  [NodeStatus.Running]: {
    bg: '#FEFCE8',
    border: '#FDE68A',
    text: '#92400E',
    badge: '#F59E0B',
  },
  [NodeStatus.Completed]: {
    bg: '#F0FDF4',
    border: '#86EFAC',
    text: '#166534',
    badge: '#10B981',
  },
  [NodeStatus.Failed]: {
    bg: '#FEF2F2',
    border: '#FCA5A5',
    text: '#991B1B',
    badge: '#EF4444',
  },
  [NodeStatus.Stopped]: {
    bg: '#FFF7ED',
    border: '#FDBA74',
    text: '#9A3412',
    badge: '#F97316',
  },
  [NodeStatus.Paused]: {
    bg: '#F5F3FF',
    border: '#C4B5FD',
    text: '#5B21B6',
    badge: '#8B5CF6',
  },
};

// ============================================================================
// ActivityIndicator — 三点点阵动画（仅 Running 状态显示）
// ============================================================================

const ActivityIndicator: React.FC<{ hidden?: boolean }> = ({ hidden }) => {
  if (hidden) return null;
  return (
    <span style={{ display: 'inline-flex', gap: 2, marginLeft: 4 }}>
      <span style={{
        width: 4, height: 4, borderRadius: '50%', backgroundColor: '#F59E0B',
        animation: 'pulse 1.5s ease-in-out infinite',
      }} />
      <span style={{
        width: 4, height: 4, borderRadius: '50%', backgroundColor: '#F59E0B',
        animation: 'pulse 1.5s ease-in-out infinite', animationDelay: '0.3s',
      }} />
      <span style={{
        width: 4, height: 4, borderRadius: '50%', backgroundColor: '#F59E0B',
        animation: 'pulse 1.5s ease-in-out infinite', animationDelay: '0.6s',
      }} />
    </span>
  );
};

// ============================================================================
// TreeNode 组件
// ============================================================================

const TreeNodeComponent: React.FC<NodeProps<RFNodeData>> = ({ id, data, selected }) => {
  const { t } = useI18n();
  const {
    label,
    status,
    exec_type,
    role,
    performanceMode,
    iteration,
    max_iterations,
    highlighted,
  } = data;

  const style = STATUS_STYLES[status] || STATUS_STYLES[NodeStatus.Waiting];
  const statusLabel = t(`status.${status.toLowerCase()}`);

  const highlightShadow = highlighted
    ? '0 0 0 3px rgba(37,99,235,0.30), 0 6px 18px rgba(37,99,235,0.20)'
    : undefined;

  return (
    <div
      data-node-id={id}
      className="graph-node-card"
      style={{
        width: 210,
        minHeight: 68,
        border: `1px solid ${selected ? '#176B55' : highlighted ? '#2563EB' : style.border}`,
        borderRadius: 8,
        backgroundColor: style.bg,
        padding: '6px 10px',
        boxShadow: selected
          ? '0 0 0 3px rgba(23,107,85,0.16), 0 4px 12px rgba(0,0,0,0.08)'
          : highlightShadow || '0 2px 7px rgba(26,32,38,0.08)',
        cursor: 'pointer',
        transition: performanceMode ? 'none' : 'box-shadow 0.15s ease',
        fontFamily: 'system-ui, sans-serif',
        fontSize: 12,
        position: 'relative',
      }}
    >
      {/* Handle */}
      <Handle type="target" position={Position.Top} className="graph-node-handle" />
      <Handle type="source" position={Position.Bottom} className="graph-node-handle" />

      {/* 头部：标签 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 2 }}>
        <span
          style={{
            fontWeight: 600,
            color: style.text,
            fontSize: 13,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            flex: 1,
          }}
        >
          {label}
        </span>

        {/* Running 脉冲动画 */}
        {status === NodeStatus.Running && !performanceMode && <ActivityIndicator />}
      </div>

      {/* 状态 Badge + 执行类型 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginTop: 2 }}>
        <span
          style={{
            display: 'inline-block',
            padding: '0 6px',
            borderRadius: 4,
            backgroundColor: style.badge,
            color: '#FFF',
            fontWeight: 600,
            fontSize: 10,
            lineHeight: '18px',
          }}
        >
          {status === NodeStatus.Running && iteration != null
            ? `${statusLabel} ${iteration}/${max_iterations || '-'}`
            : statusLabel}
        </span>
        <span
          style={{
            padding: '0 4px',
            borderRadius: 3,
            backgroundColor: exec_type === 'M' ? '#E0E7FF' : '#F3E8FF',
            color: exec_type === 'M' ? '#4338CA' : '#7C3AED',
            fontWeight: 600,
            fontSize: 9,
            lineHeight: '16px',
          }}
        >
          {exec_type === 'M' ? 'M' : 'W'}
        </span>
        {role && (
          <span style={{ color: '#9CA3AF', fontSize: 9, marginLeft: 2 }}>
            {role}
          </span>
        )}
      </div>
    </div>
  );
};

const TreeNode = React.memo(TreeNodeComponent);
export default TreeNode;
