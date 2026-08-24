// ============================================================================
// dagreLayout — 基于 dagre 的树形图自动布局
// ============================================================================

import dagre from 'dagre/dist/dagre.min.js';
import { GraphNode, GraphEdge, EdgeType, RFNode, RFEdge } from '../types';
import { MarkerType } from 'reactflow';

const NODE_WIDTH = 210;
const NODE_HEIGHT = 84;

/**
 * 使用 dagre 计算布局
 * @param nodes 业务节点列表
 * @param edges 业务边列表
 * @param showDependencyEdges 是否显示依赖边
 * @returns [RFNode[], RFEdge[]] ReactFlow 格式的节点和边
 */
export function computeLayout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  showDependencyEdges: boolean = true,
): [RFNode[], RFEdge[]] {
  const dagreGraph = new dagre.graphlib.Graph();
  dagreGraph.setDefaultEdgeLabel(() => ({}));
  dagreGraph.setGraph({
    rankdir: 'TB',
    nodesep: 60,
    ranksep: 100,
    edgesep: 20,
    marginx: 40,
    marginy: 40,
  });

  // 添加 dagre 节点
  for (const node of nodes) {
    dagreGraph.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }

  // 添加 dagre 边（仅 parent 边参与布局）
  for (const edge of edges) {
    if (edge.type === EdgeType.Parent) {
      dagreGraph.setEdge(edge.source, edge.target);
    }
  }

  // 执行布局
  dagre.layout(dagreGraph);

  // 生成 ReactFlow 节点
  const rfNodes: RFNode[] = nodes.map((node) => {
    const dagreNode = dagreGraph.node(node.id);
    return {
      id: node.id,
      type: 'treeNode',
      position: {
        x: dagreNode ? dagreNode.x - NODE_WIDTH / 2 : 0,
        y: dagreNode ? dagreNode.y - NODE_HEIGHT / 2 : 0,
      },
      data: { ...node },
      draggable: true,
    };
  });

  // 生成 ReactFlow 边
  const rfEdges: RFEdge[] = [];

  for (const edge of edges) {
    if (edge.type === EdgeType.Parent) {
      rfEdges.push({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: 'default',
        style: { stroke: '#D1D5DB', strokeWidth: 2 },
        animated: false,
      });
    } else if (edge.type === EdgeType.Dependency && showDependencyEdges) {
      rfEdges.push({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: 'dependencyEdge',
        style: { stroke: '#9CA3AF', strokeWidth: 1.5, strokeDasharray: '5,5' },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: '#9CA3AF',
          width: 15,
          height: 15,
        },
      });
    }
  }

  return [rfNodes, rfEdges];
}
