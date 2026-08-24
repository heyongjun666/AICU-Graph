// ============================================================================
// DependencyEdge — 自定义依赖边组件
// 贝塞尔曲线、虚线、箭头标记
// ============================================================================

import React from 'react';
import { BaseEdge, EdgeProps, getBezierPath } from 'reactflow';
import { useI18n } from '../i18n';

const DependencyEdgeComponent: React.FC<EdgeProps> = ({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  selected,
}) => {
  const { t } = useI18n();
  const [edgePath] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });

  return (
    <>
      <BaseEdge
        id={id}
        path={edgePath}
        style={{
          stroke: selected ? '#6366F1' : '#9CA3AF',
          strokeWidth: selected ? 2 : 1.5,
          strokeDasharray: '5,5',
          opacity: 0.6,
        }}
      />
      {selected && (
        <text
          style={{ fontSize: 10, fill: '#6366F1', fontWeight: 600 }}
        >
          <textPath href={`#${id}`} startOffset="50%" textAnchor="middle">
            {t('edge.dependency')}
          </textPath>
        </text>
      )}
    </>
  );
};

const DependencyEdge = React.memo(DependencyEdgeComponent);
export default DependencyEdge;
