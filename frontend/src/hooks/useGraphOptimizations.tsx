// ============================================================================
// useGraphOptimizations — 画布优化 Hook
// 节点虚拟化已禁用，仅保留性能降级模式（节点数 >200 时自动收起子树）
// ============================================================================

import { useState, useEffect, useRef } from 'react';
import { NodeProps } from 'reactflow';
import { useGraphStore } from '../stores/GraphStore';
import { useUIStateStore } from '../stores/UIStateStore';

// ============================================================================
// 配置
// ============================================================================

const ENTER_THRESHOLD = 200;
const EXIT_THRESHOLD = 150;
const DEBOUNCE_MS = 500;

// ============================================================================
// Hook 返回类型
// ============================================================================

interface UseGraphOptimizationsOptions {
  containerRef: React.RefObject<HTMLElement | null>;
  originalNodeTypes: Record<string, React.ComponentType<NodeProps>>;
}

interface UseGraphOptimizationsResult {
  virtualizedNodeTypes: Record<string, React.ComponentType<NodeProps>>;
  visibleNodeIds: Set<string>;
  isPerformanceMode: boolean;
}

// ============================================================================
// useGraphOptimizations — 主 Hook
// ============================================================================

export function useGraphOptimizations({
  containerRef: _containerRef,
  originalNodeTypes,
}: UseGraphOptimizationsOptions): UseGraphOptimizationsResult {
  // 节点虚拟化已禁用——直接返回原始 nodeTypes，所有节点始终渲染。
  // 原 IntersectionObserver + React.memo + ref 方案存在可见性更新不触发的 bug。

  // ---- 性能降级模式（原 usePerformanceMode） ----
  const nodes = useGraphStore((state) => state.nodes);
  const collapseAll = useGraphStore((state) => state.collapseAll);
  const performanceMode = useUIStateStore((state) => state.performanceMode);
  const setPerformanceMode = useUIStateStore((state) => state.setPerformanceMode);

  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [isPerfMode, setIsPerfMode] = useState(false);

  useEffect(() => {
    const nodeCount = nodes.size;

    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
    }

    if (!performanceMode && nodeCount > ENTER_THRESHOLD) {
      debounceTimerRef.current = setTimeout(() => {
        setIsPerfMode(true);
        setPerformanceMode(true);
        collapseAll();
      }, DEBOUNCE_MS);
    } else if (performanceMode && nodeCount < EXIT_THRESHOLD) {
      debounceTimerRef.current = setTimeout(() => {
        setIsPerfMode(false);
        setPerformanceMode(false);
      }, DEBOUNCE_MS);
    }

    return () => {
      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current);
      }
    };
  }, [nodes.size, performanceMode, collapseAll, setPerformanceMode]);

  return {
    virtualizedNodeTypes: originalNodeTypes,
    visibleNodeIds: new Set(),
    isPerformanceMode: isPerfMode,
  };
}
