// ============================================================================
// MockProvider — Mock 数据 Provider
// 在前端模拟后端数据推送，用于开发调试
// 仅在 VITE_MOCK_MODE === 'true' 时生效
// ============================================================================

import React, { useEffect, useRef } from 'react';
import { useGraphStore } from '../stores/GraphStore';
import { useUIStateStore } from '../stores/UIStateStore';
import { GraphNode, GraphEdge, NodeStatus, EdgeType } from '../types';

// ============================================================================
// Mock 数据
// ============================================================================

function createMockData(): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const manager1: GraphNode = {
    id: 'm1',
    label: 'Manager-1',
    status: NodeStatus.Running,
    exec_type: 'M',
    role: 'dev_lead',
    task: 'Implement login feature',
    config: { num_ccs: 3, model: 'gpt-4' },
    created_at: new Date(Date.now() - 60000).toISOString(),
    started_at: new Date(Date.now() - 30000).toISOString(),
    iteration: 2,
    max_iterations: 5,
  };

  const manager2: GraphNode = {
    id: 'm2',
    label: 'Manager-2',
    status: NodeStatus.Waiting,
    exec_type: 'M',
    role: 'architect',
    task: 'Design system architecture',
    config: { num_ccs: 2, model: 'gpt-4' },
    created_at: new Date(Date.now() - 45000).toISOString(),
    iteration: 0,
    max_iterations: 3,
  };

  const workers: GraphNode[] = [
    {
      id: 'w1',
      label: 'Worker-FE',
      status: NodeStatus.Running,
      exec_type: 'W',
      parent_id: 'm1',
      role: 'dev_member',
      task: 'Implement login form',
      created_at: new Date(Date.now() - 30000).toISOString(),
      started_at: new Date(Date.now() - 15000).toISOString(),
    },
    {
      id: 'w2',
      label: 'Worker-BE',
      status: NodeStatus.Completed,
      exec_type: 'W',
      parent_id: 'm1',
      role: 'dev_member',
      task: 'Implement auth API',
      created_at: new Date(Date.now() - 30000).toISOString(),
      started_at: new Date(Date.now() - 20000).toISOString(),
      finished_at: new Date(Date.now() - 5000).toISOString(),
    },
    {
      id: 'w3',
      label: 'Worker-DB',
      status: NodeStatus.Ready,
      exec_type: 'W',
      parent_id: 'm1',
      role: 'dev_member',
      task: 'Design database schema',
      created_at: new Date(Date.now() - 25000).toISOString(),
    },
    {
      id: 'w4',
      label: 'Worker-Test',
      status: NodeStatus.Waiting,
      exec_type: 'W',
      parent_id: 'm1',
      role: 'acceptance',
      task: 'Write integration tests',
      created_at: new Date(Date.now() - 20000).toISOString(),
    },
    {
      id: 'w5',
      label: 'Worker-FE2',
      status: NodeStatus.Paused,
      exec_type: 'W',
      parent_id: 'm2',
      role: 'dev_member',
      task: 'Implement dashboard UI',
      created_at: new Date(Date.now() - 15000).toISOString(),
    },
    {
      id: 'w6',
      label: 'Worker-Security',
      status: NodeStatus.Failed,
      exec_type: 'W',
      parent_id: 'm2',
      role: 'security_reviewer',
      task: 'Security audit',
      created_at: new Date(Date.now() - 10000).toISOString(),
      started_at: new Date(Date.now() - 8000).toISOString(),
      finished_at: new Date(Date.now() - 2000).toISOString(),
      error: 'Security vulnerabilities found',
    },
  ];

  const parentEdges: GraphEdge[] = [
    { id: 'e-m1-m2', source: 'm1', target: 'm2', type: EdgeType.Parent },
    { id: 'e-m1-w1', source: 'm1', target: 'w1', type: EdgeType.Parent },
    { id: 'e-m1-w2', source: 'm1', target: 'w2', type: EdgeType.Parent },
    { id: 'e-m1-w3', source: 'm1', target: 'w3', type: EdgeType.Parent },
    { id: 'e-m1-w4', source: 'm1', target: 'w4', type: EdgeType.Parent },
    { id: 'e-m2-w5', source: 'm2', target: 'w5', type: EdgeType.Parent },
    { id: 'e-m2-w6', source: 'm2', target: 'w6', type: EdgeType.Parent },
  ];

  const dependencyEdges: GraphEdge[] = [
    { id: 'e-dep-w2-w1', source: 'w2', target: 'w1', type: EdgeType.Dependency },
    { id: 'e-dep-w1-w4', source: 'w1', target: 'w4', type: EdgeType.Dependency },
    { id: 'e-dep-w3-w2', source: 'w3', target: 'w2', type: EdgeType.Dependency },
    { id: 'e-dep-w6-w5', source: 'w6', target: 'w5', type: EdgeType.Dependency },
  ];

  return {
    nodes: [manager1, manager2, ...workers],
    edges: [...parentEdges, ...dependencyEdges],
  };
}

// ============================================================================
// MockProvider 组件
// ============================================================================

interface MockProviderProps {
  children: React.ReactNode;
}

const MockProvider: React.FC<MockProviderProps> = ({ children }) => {
  const initializedRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (initializedRef.current) return;
    initializedRef.current = true;

    const { batchUpdate, updateNode, addNode } = useGraphStore.getState();
    const { addToast } = useUIStateStore.getState();

    // 加载 mock 数据
    const mockData = createMockData();
    batchUpdate(mockData.nodes, mockData.edges);

    addToast({ type: 'info', message: 'Mock mode: demo data loaded' });

    // 模拟状态更新（每 3 秒）
    timerRef.current = setInterval(() => {
      const { nodes } = useGraphStore.getState();
      if (nodes.size === 0) return;

      // 随机选取一个 waiting/ready/paused 节点推进状态
      const candidates = Array.from(nodes.values()).filter(
        (n) =>
          n.status === NodeStatus.Waiting ||
          n.status === NodeStatus.Ready ||
          n.status === NodeStatus.Paused ||
          n.status === NodeStatus.Running,
      );

      if (candidates.length > 0) {
        const node = candidates[Math.floor(Math.random() * candidates.length)];
        const statuses = [NodeStatus.Ready, NodeStatus.Running, NodeStatus.Completed];
        const nextStatus = statuses[Math.floor(Math.random() * statuses.length)];
        updateNode(node.id, { status: nextStatus });

        if (nextStatus === NodeStatus.Completed) {
          updateNode(node.id, {
            finished_at: new Date().toISOString(),
          });
        }
      }
    }, 3000);

    return () => {
      if (timerRef.current) {
        clearInterval(timerRef.current);
      }
    };
  }, []);

  return <>{children}</>;
};

export default MockProvider;
