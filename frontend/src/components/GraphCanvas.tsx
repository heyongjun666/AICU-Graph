// ============================================================================
// GraphCanvas — ReactFlow 画布组件
// 集成 dagre 自动布局、节点点击选中、拖拽平移、滚轮缩放
// 集成性能优化：LayoutCache、useGraphOptimizations、CanvasControls
// ============================================================================

import React, { useMemo, useCallback, useEffect, useRef, useState } from 'react';
import ReactFlow, {
  Node,
  Edge,
  Background,
  MiniMap,
  BackgroundVariant,
  useReactFlow,
  ReactFlowProvider,
  SelectionMode,
  Panel,
  MarkerType,
  Connection,
} from 'reactflow';
import 'reactflow/dist/style.css';

import { useGraphStore } from '../stores/GraphStore';
import { useUIStateStore } from '../stores/UIStateStore';
import { useSessionStore } from '../stores/SessionStore';
import { useEventStore } from '../stores/EventStore';
import { createDependency, deleteDependency } from '../utils/api';
import TreeNode from './TreeNode';
import DependencyEdge from './DependencyEdge';
import EventEdge from './EventEdge';
import CanvasControls from './CanvasControls';
import { computeLayout } from '../utils/dagreLayout';
import { LayoutCache } from '../utils/layoutCache';
import { useGraphOptimizations } from '../hooks/useGraphOptimizations';
import { GraphNode, RFEdge } from '../types';
import { useI18n } from '../i18n';

// ============================================================================
// 布局缓存实例（模块级单例）
// ============================================================================

const layoutCache = new LayoutCache(10);

// ============================================================================
// 自定义节点和边类型注册（基础类型）
// ============================================================================

const baseNodeTypes = {
  treeNode: TreeNode,
};

const edgeTypes = {
  dependencyEdge: DependencyEdge,
  eventEdge: EventEdge,
};

// ============================================================================
// GraphCanvasInner — 内部组件（需在 ReactFlowProvider 内）
// ============================================================================

const GraphCanvasInner: React.FC = () => {
  const { t } = useI18n();
  const graphNodes = useGraphStore((state) => state.nodes);
  const graphEdges = useGraphStore((state) => state.edges);
  const selectedNodeId = useGraphStore((state) => state.selectedNodeId);
  const selectNode = useGraphStore((state) => state.selectNode);
  const sessionId = useSessionStore((state) => state.activeSessionId);
  const events = useEventStore((state) => state.events);
  const highlightedEventId = useEventStore((state) => state.highlightedEventId);
  const highlightedNodeIds = useEventStore((state) => state.highlightedNodeIds);
  const clearHighlight = useEventStore((state) => state.clearHighlight);
  const {
    colorBlindMode,
    highContrastMode,
    showDependencyEdges,
    performanceMode,
  } = useUIStateStore();
  const reactFlowInstance = useReactFlow();
  const containerRef = useRef<HTMLDivElement>(null);

  // Performance degradation + node virtualization (merged optimization)
  const { virtualizedNodeTypes } = useGraphOptimizations({
    containerRef: containerRef as React.RefObject<HTMLElement | null>,
    originalNodeTypes: baseNodeTypes,
  });

  // Whether first layout is ready
  const [layoutReady, setLayoutReady] = useState(false);

  // 1. 使用 dagre 计算布局——通过 LayoutCache 缓存
  const { rfNodes, rfEdges } = useMemo(() => {
    const nodesArray = Array.from(graphNodes.values());
    const edgesArray = Array.from(graphEdges.values());

    // 尝试从缓存获取
    const cached = layoutCache.get(nodesArray, edgesArray, showDependencyEdges);
    if (cached) {
      const enrichedNodes = cached.rfNodes.map((n: any) => ({
        ...n,
        data: {
          ...n.data,
          colorBlindMode,
          highContrastMode,
          performanceMode,
          highlighted: highlightedNodeIds.includes(n.id),
        },
      }));
      return { rfNodes: enrichedNodes, rfEdges: cached.rfEdges };
    }

    // 缓存未命中，重新计算
    const [computedNodes, computedEdges] = computeLayout(
      nodesArray,
      edgesArray,
      showDependencyEdges,
    );

    // 缓存计算结果
    layoutCache.set(nodesArray, edgesArray, showDependencyEdges, {
      rfNodes: computedNodes,
      rfEdges: computedEdges,
    });

    // 注入视图模式到 data
    const enrichedNodes = computedNodes.map((n) => ({
      ...n,
      data: {
        ...n.data,
        colorBlindMode,
        highContrastMode,
        performanceMode,
        highlighted: highlightedNodeIds.includes(n.id),
      },
    }));

    return { rfNodes: enrichedNodes, rfEdges: computedEdges };
  }, [graphNodes, graphEdges, showDependencyEdges, colorBlindMode, highContrastMode, performanceMode, highlightedNodeIds]);

  // 事件边：仅在选中某个事件时，绘制该事件「生产节点 → 各消费节点」的蓝色实线箭头
  const eventEdges = useMemo(() => {
    const result: RFEdge[] = [];
    if (!highlightedEventId) return result;
    const ev = events.find((e) => e.event_id === highlightedEventId);
    if (!ev || !ev.producer || !graphNodes.has(ev.producer)) return result;
    for (const target of ev.targets) {
      if (!target.node_id || !graphNodes.has(target.node_id)) continue;
      result.push({
        id: `event:${ev.event_id}:${target.node_id}`,
        source: ev.producer,
        target: target.node_id,
        type: 'eventEdge',
        markerEnd: { type: MarkerType.ArrowClosed, color: '#2563EB', width: 20, height: 20 },
        data: { eventId: ev.event_id, highlighted: true },
      } as unknown as RFEdge);
    }
    return result;
  }, [events, graphNodes, highlightedEventId]);

  const allEdges = useMemo(() => [...rfEdges, ...eventEdges], [rfEdges, eventEdges]);

  // Fit view after first layout

  useEffect(() => {
    if (rfNodes.length > 0 && !layoutReady) {
      setLayoutReady(true);
      const timer = setTimeout(() => {
        reactFlowInstance.fitView({ padding: 0.2, duration: 300 });
      }, 50);
      return () => clearTimeout(timer);
    }
  }, [rfNodes.length, layoutReady, reactFlowInstance]);

  // Click node → select
  const onNodeClick = useCallback(
    (_event: React.MouseEvent, node: Node) => {
      selectNode(node.id);
    },
    [selectNode],
  );

  // Click blank → deselect
  const onPaneClick = useCallback(() => {
    selectNode(null);
    clearHighlight();
  }, [selectNode, clearHighlight]);

  const onConnect = useCallback(async (connection: Connection) => {
    if (!sessionId || !connection.source || !connection.target || connection.source === connection.target) return;
    try {
      await createDependency(sessionId, connection.source, connection.target);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : String(error));
    }
  }, [sessionId]);

  const onEdgeDoubleClick = useCallback(async (_event: React.MouseEvent, edge: Edge) => {
    // 事件边（eventEdge）仅用于展示，不支持双击删除；只有依赖边可删除。
    if (!sessionId || edge.type !== 'dependencyEdge') return;
    if (!window.confirm(t('canvas.removeDependencyConfirm'))) return;
    try {
      await deleteDependency(sessionId, edge.id);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : String(error));
    }
  }, [sessionId]);

  // Check for data
  const hasData = rfNodes.length > 0;

  if (!hasData) {
    return (
      <div
        ref={containerRef}
        style={{
          flex: 1,
          backgroundColor: '#F9FAFB',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: '#9CA3AF',
          height: '100%',
        }}
      >
        <div style={{ textAlign: 'center' }}>
          <p style={{ fontSize: 18, marginBottom: 8 }}>{t('canvas.graphCanvas')}</p>
          <p style={{ fontSize: 14 }}>{t('canvas.emptyGraph')}</p>
        </div>
      </div>
    );
  }

  return (
    <div ref={containerRef} style={{ flex: 1, height: '100%', width: '100%', position: 'relative' }}>
      <ReactFlow
        nodes={rfNodes}
        edges={allEdges}
        nodeTypes={virtualizedNodeTypes}
        edgeTypes={edgeTypes}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        onConnect={onConnect}
        onEdgeDoubleClick={onEdgeDoubleClick}
        nodesConnectable
        fitView
        fitViewOptions={{ padding: 0.2 }}
        selectionMode={SelectionMode.Partial}
        selectNodesOnDrag={false}
        panOnDrag={true}
        zoomOnScroll={true}
        zoomOnDoubleClick={false}
        deleteKeyCode={null}
        multiSelectionKeyCode={null}
        defaultEdgeOptions={{
          style: { stroke: '#9CA3AF', strokeWidth: 2 },
        }}
        minZoom={0.1}
        maxZoom={4}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#E5E7EB" />
        <MiniMap
          nodeStrokeColor="#6B7280"
          nodeColor={(node) => {
            const gn = node.data as GraphNode;
            if (!gn) return '#E5E7EB';
            const statusColors: Record<string, string> = {
              Waiting: '#9CA3AF',
              Ready: '#3B82F6',
              Running: '#F59E0B',
              Completed: '#10B981',
              Failed: '#EF4444',
              Stopped: '#F97316',
              Paused: '#8B5CF6',
            };
            return statusColors[gn.status] || '#E5E7EB';
          }}
          maskColor="rgba(0,0,0,0.08)"
          style={{ border: '1px solid #E5E7EB', borderRadius: 8 }}
        />

        {/* 画布控制按钮 */}
        <CanvasControls />

        {/* 状态图例 */}
        <Panel position="bottom-left">
          <div
            style={{
              background: 'rgba(255,255,255,0.95)',
              borderRadius: 8,
              padding: '6px 10px',
              fontSize: 10,
              boxShadow: '0 1px 4px rgba(0,0,0,0.1)',
              display: 'flex',
              gap: 8,
              flexWrap: 'wrap',
              maxWidth: 320,
            }}
          >
            {[
              { status: 'W', label: 'Waiting', color: '#9CA3AF' },
              { status: 'R', label: 'Ready', color: '#3B82F6' },
              { status: 'Rn', label: 'Running', color: '#F59E0B' },
              { status: 'C', label: 'Completed', color: '#10B981' },
              { status: 'F', label: 'Failed', color: '#EF4444' },
              { status: 'S', label: 'Stopped', color: '#F97316' },
              { status: 'P', label: 'Paused', color: '#8B5CF6' },
            ].map((item) => (
              <span key={item.status} style={{ display: 'flex', alignItems: 'center', gap: 3 }}>
                <span
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: '50%',
                    backgroundColor: item.color,
                    display: 'inline-block',
                  }}
                />
                <span style={{ fontWeight: 500, color: '#374151' }}>{item.status}</span>
              </span>
            ))}
          </div>
        </Panel>
      </ReactFlow>
    </div>
  );
};

// ============================================================================
// GraphCanvas — 外层组件包裹 ReactFlowProvider
// ============================================================================

const GraphCanvas: React.FC = () => {
  return (
    <div style={{ flex: 1, height: '100%', width: '100%' }}>
      <ReactFlowProvider>
        <GraphCanvasInner />
      </ReactFlowProvider>
    </div>
  );
};

export default GraphCanvas;
