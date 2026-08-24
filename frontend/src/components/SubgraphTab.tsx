// ============================================================================
// SubgraphTab — 子图/迭代切换 Tab
// 显示迭代切换器和子节点的 Mini ReactFlow 视图
// ============================================================================

import React, { useState, useMemo } from 'react';
import ReactFlow, { Background, BackgroundVariant, MiniMap } from 'reactflow';
import 'reactflow/dist/style.css';
import { useGraphStore } from '../stores/GraphStore';
import { GraphNode, EdgeType, RFNode, RFEdge } from '../types';
import { computeLayout } from '../utils/dagreLayout';
import { useI18n } from '../i18n';

interface SubgraphTabProps {
  nodeId: string;
}

const SubgraphTab: React.FC<SubgraphTabProps> = ({ nodeId }) => {
  const { t } = useI18n();
  const getNode = useGraphStore((state) => state.getNode);
  const nodes = useGraphStore((state) => state.nodes);
  const edges = useGraphStore((state) => state.edges);

  const [currentIteration, setCurrentIteration] = useState(0);

  const node = getNode(nodeId);

  // Get child nodes (direct parent edges from this node)
  const childNodes = useMemo(() => {
    const children: GraphNode[] = [];
    for (const edge of edges.values()) {
      if (edge.type === EdgeType.Parent && edge.source === nodeId) {
        const childNode = nodes.get(edge.target);
        if (childNode) children.push(childNode);
      }
    }
    return children;
  }, [nodeId, nodes, edges]);

  // Get edges between child nodes
  const childEdges = useMemo(() => {
    const childIds = new Set(childNodes.map((n) => n.id));
    const result: typeof edges extends Map<string, infer V> ? V[] : any[] = [];
    for (const edge of edges.values()) {
      if (childIds.has(edge.source) && childIds.has(edge.target)) {
        result.push(edge);
      }
    }
    return result;
  }, [childNodes, edges]);

  // Compute layout for subgraph
  const [rfNodes, rfEdges] = useMemo(() => {
    if (childNodes.length === 0) return [[], []] as [RFNode[], RFEdge[]];
    return computeLayout(childNodes, childEdges, true);
  }, [childNodes, childEdges]);

  // Mock iterations
  const maxIteration = node?.max_iterations || 1;
  const iterations = Array.from({ length: maxIteration }, (_, i) => i);

  if (!node) return null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Iteration switcher */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 8,
          padding: '8px 12px',
          borderBottom: '1px solid #F3F4F6',
        }}
      >
        <button
          onClick={() => setCurrentIteration(Math.max(0, currentIteration - 1))}
          disabled={currentIteration === 0}
          style={{
            border: 'none',
            background: 'none',
            cursor: currentIteration === 0 ? 'not-allowed' : 'pointer',
            fontSize: 16,
            color: currentIteration === 0 ? '#E5E7EB' : '#374151',
            padding: '0 4px',
          }}
        >
          ◀
        </button>

        {iterations.map((i) => (
          <div
            key={i}
            onClick={() => setCurrentIteration(i)}
            style={{
              width: 10,
              height: 10,
              borderRadius: '50%',
              backgroundColor: i === currentIteration ? '#6366F1' : '#D1D5DB',
              cursor: 'pointer',
              transition: 'background-color 0.15s ease',
            }}
            title={t('subgraph.iteration', { n: i + 1 })}
          />
        ))}

        <button
          onClick={() => setCurrentIteration(Math.min(maxIteration - 1, currentIteration + 1))}
          disabled={currentIteration >= maxIteration - 1}
          style={{
            border: 'none',
            background: 'none',
            cursor: currentIteration >= maxIteration - 1 ? 'not-allowed' : 'pointer',
            fontSize: 16,
            color: currentIteration >= maxIteration - 1 ? '#E5E7EB' : '#374151',
            padding: '0 4px',
          }}
        >
          ▶
        </button>

        <span style={{ fontSize: 11, color: '#6B7280', marginLeft: 8 }}>
          {t('subgraph.iteration', { n: currentIteration + 1 })} / {maxIteration}
        </span>
      </div>

      {/* Child count */}
      <div
        style={{
          padding: '6px 12px',
          fontSize: 11,
          color: '#9CA3AF',
          borderBottom: '1px solid #F3F4F6',
        }}
      >
        {t('subgraph.childNodes', { count: childNodes.length, s: childNodes.length !== 1 ? 's' : '' })}
      </div>

      {/* Mini ReactFlow */}
      <div style={{ flex: 1, minHeight: 200 }}>
        {childNodes.length > 0 ? (
          <ReactFlow
            nodes={rfNodes}
            edges={rfEdges}
            fitView
            fitViewOptions={{ padding: 0.3 }}
            panOnDrag={true}
            zoomOnScroll={true}
            zoomOnDoubleClick={false}
            nodesDraggable={false}
            nodesConnectable={false}
            elementsSelectable={false}
            minZoom={0.1}
            maxZoom={2}
          >
            <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#E5E7EB" />
          </ReactFlow>
        ) : (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              height: '100%',
              color: '#9CA3AF',
              fontSize: 12,
            }}
          >
            {t('subgraph.noChildNodes')}
          </div>
        )}
      </div>
    </div>
  );
};

export default SubgraphTab;
