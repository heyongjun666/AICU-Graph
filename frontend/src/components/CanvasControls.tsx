// ============================================================================
// CanvasControls — 画布悬浮控制按钮组
// 缩放、适应视图、折叠/展开全部、切换依赖边
// ============================================================================

import React, { useState } from 'react';
import { useReactFlow } from 'reactflow';
import { useUIStateStore } from '../stores/UIStateStore';
import { useGraphStore } from '../stores/GraphStore';
import { useI18n } from '../i18n';

const buttonStyle: React.CSSProperties = {
  width: 32,
  height: 32,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  border: '1px solid #E5E7EB',
  borderRadius: 6,
  backgroundColor: '#FFF',
  cursor: 'pointer',
  fontSize: 16,
  fontWeight: 600,
  color: '#374151',
  boxShadow: '0 1px 3px rgba(0,0,0,0.1)',
};

const activeButtonStyle: React.CSSProperties = {
  ...buttonStyle,
  backgroundColor: '#EEF2FF',
  borderColor: '#6366F1',
  color: '#6366F1',
};

const CanvasControls: React.FC = () => {
  const { t } = useI18n();
  const reactFlowInstance = useReactFlow();
  const showDependencyEdges = useUIStateStore((state) => state.showDependencyEdges);
  const toggleDependencyEdges = useUIStateStore((state) => state.toggleDependencyEdges);
  const [collapsed, setCollapsed] = useState(false);

  const handleZoomIn = () => {
    reactFlowInstance.zoomIn({ duration: 200 });
  };

  const handleZoomOut = () => {
    reactFlowInstance.zoomOut({ duration: 200 });
  };

  const handleFitView = () => {
    reactFlowInstance.fitView({ padding: 0.2, duration: 300 });
  };

  const handleToggleCollapse = () => {
    const { expandAll, collapseAll } = useGraphStore.getState();
    if (collapsed) {
      expandAll();
    } else {
      collapseAll();
    }
    setCollapsed(!collapsed);
  };

  return (
    <div
      style={{
        position: 'absolute',
        top: 10,
        right: 10,
        zIndex: 10,
        display: 'flex',
        flexDirection: 'column',
        gap: 4,
      }}
    >
      <button
        onClick={handleZoomIn}
        style={buttonStyle}
        title={t('canvas.zoomIn')}
      >
        +
      </button>
      <button
        onClick={handleZoomOut}
        style={buttonStyle}
        title={t('canvas.zoomOut')}
      >
        −
      </button>
      <button
        onClick={handleFitView}
        style={buttonStyle}
        title={t('canvas.fitView')}
      >
        ⊞
      </button>
      <button
        onClick={handleToggleCollapse}
        style={collapsed ? activeButtonStyle : buttonStyle}
        title={collapsed ? t('canvas.expandAll') : t('canvas.collapseAll')}
      >
        {collapsed ? '⊕' : '⊖'}
      </button>
      <button
        onClick={toggleDependencyEdges}
        style={showDependencyEdges ? activeButtonStyle : buttonStyle}
        title={showDependencyEdges ? t('canvas.hideDependencyEdges') : t('canvas.showDependencyEdges')}
      >
        ↹
      </button>
    </div>
  );
};

export default CanvasControls;
