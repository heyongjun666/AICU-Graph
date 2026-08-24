// ============================================================================
// EventEdge — 事件边组件
// 仅在选中某个事件时显示：生产节点 → 消费节点，蓝色实线 + 箭头（区别于依赖边的灰色虚线）
// ============================================================================

import React from 'react';
import { BaseEdge, EdgeProps, getBezierPath } from 'reactflow';
import { useI18n } from '../i18n';

interface EventEdgeData {
  eventId?: string;
  highlighted?: boolean;
}

const EventEdgeComponent: React.FC<EdgeProps<EventEdgeData>> = ({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  markerEnd,
  data,
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

  const highlighted = Boolean(data?.highlighted);

  return (
    <>
      <BaseEdge
        id={id}
        path={edgePath}
        markerEnd={markerEnd}
        style={{
          stroke: '#2563EB',
          strokeWidth: highlighted ? 2.5 : 2,
          opacity: highlighted ? 1 : 0.85,
        }}
      />
      {highlighted && (
        <text style={{ fontSize: 10, fill: '#1D4ED8', fontWeight: 600 }}>
          <textPath href={`#${id}`} startOffset="50%" textAnchor="middle">
            {t('edge.event')}
          </textPath>
        </text>
      )}
    </>
  );
};

const EventEdge = React.memo(EventEdgeComponent);
export default EventEdge;
