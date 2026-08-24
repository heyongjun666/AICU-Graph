// ============================================================================
// useNodeVisibility — IntersectionObserver 检测节点可视区域
// 接收容器 ref，返回可视区域内的 node ID 集合
// ============================================================================

import { useState, useEffect, useRef, useCallback } from 'react';

/**
 * useNodeVisibility hook
 * @param containerRef - 容器 Ref，用于绑定 MutationObserver 和 IntersectionObserver
 * @returns 可视区域内的 node ID 集合
 */
export function useNodeVisibility(
  containerRef: React.RefObject<HTMLElement | null>,
): Set<string> {
  const [visibleNodeIds, setVisibleNodeIds] = useState<Set<string>>(new Set());
  const intersectionObserverRef = useRef<IntersectionObserver | null>(null);
  const observedElementsRef = useRef<Map<string, Element>>(new Map());

  // IntersectionObserver 回调：更新可见集合
  const updateVisibility = useCallback((entries: IntersectionObserverEntry[]) => {
    setVisibleNodeIds((prev) => {
      const next = new Set(prev);

      for (const entry of entries) {
        const nodeId = (entry.target as HTMLElement).dataset.nodeId;
        if (!nodeId) continue;

        if (entry.isIntersecting) {
          next.add(nodeId);
        } else {
          next.delete(nodeId);
        }
      }

      return next;
    });
  }, []);

  // 初始化 IntersectionObserver（阈值 0）
  useEffect(() => {
    const observer = new IntersectionObserver(updateVisibility, {
      threshold: 0,
    });
    intersectionObserverRef.current = observer;

    // 将已存在的 [data-node-id] 元素加入观察
    const container = containerRef.current;
    if (container) {
      const elements = container.querySelectorAll<HTMLElement>('[data-node-id]');
      elements.forEach((el) => {
        const nodeId = el.dataset.nodeId;
        if (nodeId) {
          observedElementsRef.current.set(nodeId, el);
          observer.observe(el);
        }
      });
    }

    return () => {
      observer.disconnect();
      intersectionObserverRef.current = null;
      observedElementsRef.current.clear();
    };
  }, [containerRef, updateVisibility]);

  // MutationObserver 监听子元素的增删
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const observer = new MutationObserver((mutations) => {
      const io = intersectionObserverRef.current;
      if (!io) return;

      for (const mutation of mutations) {
        // 处理新增节点
        for (const node of mutation.addedNodes) {
          if (node instanceof HTMLElement) {
            if (node.dataset.nodeId) {
              const nodeId = node.dataset.nodeId;
              observedElementsRef.current.set(nodeId, node);
              io.observe(node);
            }
            const children = node.querySelectorAll<HTMLElement>('[data-node-id]');
            children.forEach((el) => {
              const nodeId = el.dataset.nodeId;
              if (nodeId && !observedElementsRef.current.has(nodeId)) {
                observedElementsRef.current.set(nodeId, el);
                io.observe(el);
              }
            });
          }
        }

        // 处理移除节点
        for (const node of mutation.removedNodes) {
          if (node instanceof HTMLElement) {
            const cleanupId = (id: string) => {
              observedElementsRef.current.delete(id);
            };
            if (node.dataset.nodeId) {
              cleanupId(node.dataset.nodeId);
            }
            node.querySelectorAll<HTMLElement>('[data-node-id]').forEach((el) => {
              if (el.dataset.nodeId) cleanupId(el.dataset.nodeId);
            });
          }
        }
      }
    });

    observer.observe(container, {
      childList: true,
      subtree: true,
    });

    return () => {
      observer.disconnect();
    };
  }, [containerRef]);

  return visibleNodeIds;
}
