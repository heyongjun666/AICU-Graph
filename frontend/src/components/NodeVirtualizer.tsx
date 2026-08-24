// ============================================================================
// NodeVirtualizer — 基于 IntersectionObserver 的节点虚拟化渲染
// 屏幕外节点渲染占位元素（固定高度 div）
// ============================================================================

import React, { useMemo } from 'react';
import { NodeProps } from 'reactflow';
import { useNodeVisibility } from '../hooks/useNodeVisibility';

// ============================================================================
// PlaceholderNode — 屏幕外节点的占位元素
// ============================================================================

const PLACEHOLDER_HEIGHT = 80;

const PlaceholderNode: React.FC<{ nodeId: string }> = ({ nodeId }) => {
  return (
    <div
      data-node-id={nodeId}
      data-placeholder="true"
      style={{
        width: 180,
        height: PLACEHOLDER_HEIGHT,
        border: '1px dashed #D1D5DB',
        borderRadius: 10,
        backgroundColor: '#F9FAFB',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: 11,
        color: '#9CA3AF',
      }}
    >
      <span>···</span>
    </div>
  );
};

// ============================================================================
// VirtualizedNode — 根据可见性选择渲染真实节点或占位元素
// ============================================================================

interface VirtualizedNodeProps extends NodeProps {
  renderOriginal: React.ComponentType<NodeProps>;
  visibleNodeIds: Set<string>;
}

const VirtualizedNodeComponent: React.FC<VirtualizedNodeProps> = (props) => {
  const { id, renderOriginal: Original, visibleNodeIds, ...rest } = props;
  const isVisible = visibleNodeIds.has(id);

  if (!isVisible) {
    return <PlaceholderNode nodeId={id} />;
  }

  return <Original id={id} {...(rest as any)} />;
};

const VirtualizedNode = React.memo(VirtualizedNodeComponent);

// ============================================================================
// useNodeVirtualizer — 创建虚拟化 nodeTypes 的 hook
// ============================================================================

interface UseNodeVirtualizerOptions {
  containerRef: React.RefObject<HTMLElement | null>;
  originalNodeTypes: Record<string, React.ComponentType<NodeProps>>;
}

interface UseNodeVirtualizerResult {
  virtualizedNodeTypes: Record<string, React.ComponentType<NodeProps>>;
  visibleNodeIds: Set<string>;
}

export function useNodeVirtualizer({
  containerRef,
  originalNodeTypes,
}: UseNodeVirtualizerOptions): UseNodeVirtualizerResult {
  const visibleNodeIds = useNodeVisibility(containerRef);

  // 将 visibleNodeIds 直接作为 prop 传给 VirtualizedNode（而非 ref），
  // React.memo 通过浅比较检测 Set 引用变化 → 正确触发重渲染。
  const virtualizedNodeTypes = useMemo(() => {
    const result: Record<string, React.ComponentType<NodeProps>> = {};

    for (const [type, OriginalComponent] of Object.entries(originalNodeTypes)) {
      const WrappedComponent: React.FC<NodeProps> = (props) => (
        <VirtualizedNode
          {...props}
          renderOriginal={OriginalComponent}
          visibleNodeIds={visibleNodeIds}
        />
      );
      WrappedComponent.displayName = `Virtualized_${type}`;
      result[type] = WrappedComponent;
    }

    return result;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [originalNodeTypes, visibleNodeIds]);

  return { virtualizedNodeTypes, visibleNodeIds };
}
