// ============================================================================
// layoutCache —  dagre 布局结果的 LRU 缓存
// 避免在节点/边未变化时重复计算布局
// ============================================================================

import { GraphNode, GraphEdge, RFNode, RFEdge } from '../types';

interface LayoutResult {
  rfNodes: RFNode[];
  rfEdges: RFEdge[];
}

/**
 * LayoutCache — 基于 hash signature 的 LRU 缓存
 * 缓存 key 由节点 ID/status、边 ID/type、showDependencyEdges 共同决定
 */
export class LayoutCache {
  private cache: Map<string, LayoutResult>;
  private maxEntries: number;

  constructor(maxEntries: number = 10) {
    this.cache = new Map();
    this.maxEntries = maxEntries;
  }

  /**
   * 计算签名 hash
   */
  private computeHash(
    nodes: GraphNode[],
    edges: GraphEdge[],
    showDependencyEdges: boolean,
  ): string {
    // 节点签名：id + status
    const nodeSignature = nodes
      .map((n) => `${n.id}:${n.status}`)
      .sort()
      .join('|');
    // 边签名：id + type + source + target
    const edgeSignature = edges
      .map((e) => `${e.id}:${e.type}:${e.source}:${e.target}`)
      .sort()
      .join('|');
    return `${nodeSignature}||${edgeSignature}||${showDependencyEdges}`;
  }

  /**
   * 获取缓存
   */
  get(nodes: GraphNode[], edges: GraphEdge[], showDependencyEdges: boolean): LayoutResult | null {
    const hash = this.computeHash(nodes, edges, showDependencyEdges);
    const cached = this.cache.get(hash);
    if (cached) {
      // LRU: 移到末尾
      this.cache.delete(hash);
      this.cache.set(hash, cached);
      return cached;
    }
    return null;
  }

  /**
   * 设置缓存
   */
  set(
    nodes: GraphNode[],
    edges: GraphEdge[],
    showDependencyEdges: boolean,
    result: LayoutResult,
  ): void {
    const hash = this.computeHash(nodes, edges, showDependencyEdges);

    // 淘汰最旧条目
    if (this.cache.size >= this.maxEntries) {
      const firstKey = this.cache.keys().next().value;
      if (firstKey !== undefined) {
        this.cache.delete(firstKey);
      }
    }

    this.cache.set(hash, result);
  }

  /**
   * 清空缓存
   */
  clear(): void {
    this.cache.clear();
  }

  /**
   * 当前缓存大小
   */
  get size(): number {
    return this.cache.size;
  }
}
