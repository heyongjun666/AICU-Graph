// ============================================================================
// mapNodeFromBackend —— 后端节点 → 前端 GraphNode 的字段映射
//
// 这个函数是**手搭的对象字面量**（不是 spread），漏一个字段的后果不是「少个可选值」，
// 而是「界面上永远拿不到它」：NodeInspector 渲染的是本地 node state，而那份 state 是
// getSessionNode → mapNodeFromBackend 来的，后端快照里加了字段也到不了面板。
// 所以每加一个要显示的字段，这里就该有一条钉子。
// ============================================================================

import { describe, expect, it } from 'vitest';
import { mapNodeFromBackend } from '../api';

describe('mapNodeFromBackend', () => {
  it('maps the backend exec_type/role/task basics', () => {
    const node = mapNodeFromBackend({
      node_id: 'n-1', exec_type: 'manager', role: 'architect', task: 'Design it',
    });

    expect(node.id).toBe('n-1');
    expect(node.exec_type).toBe('M');
    expect(node.role).toBe('architect');
    expect(node.task).toBe('Design it');
  });

  it('carries task_history through the hand-built literal (V3-2.3 任务链)', () => {
    const history = [
      { task: 'original task', at: '2026-09-01T10:00:00', reason: 'revise' },
      { task: 'first increment', at: '2026-09-02T11:00:00', reason: 'repair' },
    ];

    const node = mapNodeFromBackend({ node_id: 'n-1', task: 'second increment', task_history: history });

    expect(node.task).toBe('second increment');
    expect(node.task_history).toEqual(history);
  });

  it('leaves task_history undefined for payloads that predate the field', () => {
    const node = mapNodeFromBackend({ node_id: 'n-1', task: 't' });

    expect(node.task_history).toBeUndefined();
  });
});
