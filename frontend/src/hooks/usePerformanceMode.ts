// ============================================================================
// usePerformanceMode — 自动检测节点数量，触发/退出降级模式
// 自动检测节点数量：GraphStore.nodes.size > 200 时触发降级模式
// 节点数回到 150 以下时自动退出降级模式（迟滞窗口防抖）
// ============================================================================

import { useState, useEffect, useRef } from 'react';
import { useGraphStore } from '../stores/GraphStore';
import { useUIStateStore } from '../stores/UIStateStore';

const ENTER_THRESHOLD = 200;
const EXIT_THRESHOLD = 150;
const DEBOUNCE_MS = 500;

interface UsePerformanceModeResult {
  isPerformanceMode: boolean;
}

/**
 * usePerformanceMode hook
 * 自动检测节点数量，超过阈值时触发降级模式
 */
export function usePerformanceMode(): UsePerformanceModeResult {
  const nodes = useGraphStore((state) => state.nodes);
  const collapseAll = useGraphStore((state) => state.collapseAll);
  const performanceMode = useUIStateStore((state) => state.performanceMode);
  const setPerformanceMode = useUIStateStore((state) => state.setPerformanceMode);

  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [isPerfMode, setIsPerfMode] = useState(false);

  useEffect(() => {
    const nodeCount = nodes.size;

    // 清除上一次防抖定时器
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = null;
    }

    if (!performanceMode && nodeCount > ENTER_THRESHOLD) {
      // 进入降级模式（带防抖）
      debounceTimerRef.current = setTimeout(() => {
        setIsPerfMode(true);
        setPerformanceMode(true);
        collapseAll();
      }, DEBOUNCE_MS);
    } else if (performanceMode && nodeCount < EXIT_THRESHOLD) {
      // 退出降级模式（带防抖）
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

  return { isPerformanceMode: isPerfMode };
}
