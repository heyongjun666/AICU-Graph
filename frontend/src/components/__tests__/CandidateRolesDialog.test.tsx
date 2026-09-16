// @vitest-environment jsdom
// ============================================================================
// CandidateRolesDialog —— 会话候选角色池选择面板
//
// 面板是「三选一」的：选池 / 手动选 / 智能推荐（未开放，占位）。任一时刻只展开
// 选中那一种的内容 —— 这几条断言就是钉住这个互斥性。
//
// 关键不变量：写入会话配置的是**角色 id 快照**，不是池 id。选池保存必须把池的 roles
// 展开写进 candidate_roles，否则之后改池会影响已绑定会话。
// ============================================================================

import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import CandidateRolesDialog from '../CandidateRolesDialog';
import { I18nProvider } from '../../i18n';
import { useUIStateStore } from '../../stores/UIStateStore';
import { RoleInfo, RolePool } from '../../types';
import * as api from '../../utils/api';

vi.mock('../../utils/api', () => ({
  getRolePools: vi.fn(),
  getRoles: vi.fn(),
  getSessionSettings: vi.fn(),
  updateSessionSettings: vi.fn(),
}));

const roles: RoleInfo[] = [
  { id: 'code', display_name: '代码编写', type: 'worker' },
  { id: 'test', display_name: '测试验证', type: 'worker' },
  { id: 'engineering_coord', display_name: '工程协调', type: 'manager' },
];

const pools: RolePool[] = [
  { id: 'p1', name: '博客开发', roles: ['code', 'test'] },
  { id: 'p2', name: '纯编码', roles: ['engineering_coord'] },
];

const renderDialog = (onClose = () => {}, onSaved = () => {}) =>
  render(
    <I18nProvider>
      <CandidateRolesDialog open sessionId="s1" onClose={onClose} onSaved={onSaved} />
    </I18nProvider>,
  );

/** picker 的 label 里还带一个类型 small，getByLabelText 精确匹配会失败。 */
const roleCheckbox = (label: string) =>
  (screen.getByText(label).closest('label') as HTMLLabelElement)
    .querySelector('input') as HTMLInputElement;

/** 模式单选行的 input（label 内除标题外还有说明 small）。 */
const modeRadio = (label: string) =>
  (screen.getByText(label).closest('.candidate-pool-mode') as HTMLElement)
    .querySelector('input') as HTMLInputElement;

const poolSelect = () => document.querySelector('.workbench-dialog select');

const lastConfig = () => vi.mocked(api.updateSessionSettings).mock.calls[0][1];

const bind = (candidate_roles: string[]) =>
  vi.mocked(api.getSessionSettings).mockResolvedValue({
    config: { candidate_roles },
    workspace_root: '/tmp/w',
    credential_configured: false,
  });

beforeEach(() => {
  useUIStateStore.getState().reset();
  useUIStateStore.getState().setLanguage('zh');
  vi.mocked(api.getRolePools).mockResolvedValue(pools);
  vi.mocked(api.getRoles).mockResolvedValue(roles);
  vi.mocked(api.getSessionSettings).mockResolvedValue({
    config: {},
    workspace_root: '/tmp/w',
    credential_configured: false,
  });
  vi.mocked(api.updateSessionSettings).mockResolvedValue({} as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useUIStateStore.getState().reset();
});

describe('CandidateRolesDialog 模式互斥', () => {
  it('默认落在「选池」模式，只展示池下拉，不展示角色勾选', async () => {
    renderDialog();

    await waitFor(() => expect(api.getRolePools).toHaveBeenCalled());
    expect(modeRadio('选择角色池').checked).toBe(true);
    expect(modeRadio('手动选择角色').checked).toBe(false);
    expect(poolSelect()).not.toBeNull();
    expect(screen.queryByText('代码编写')).toBeNull();
  });

  it('切到「手动选」后只展示角色勾选，池下拉消失', async () => {
    renderDialog();

    await waitFor(() => expect(api.getRolePools).toHaveBeenCalled());
    fireEvent.click(modeRadio('手动选择角色'));

    expect(modeRadio('选择角色池').checked).toBe(false);
    expect(poolSelect()).toBeNull();
    expect(screen.getByText('代码编写')).toBeDefined();
    expect(screen.getByText('工程协调')).toBeDefined();
  });

  it('两种模式可来回切换，且各自的选择各自保留', async () => {
    renderDialog();

    await waitFor(() => expect(api.getRolePools).toHaveBeenCalled());
    fireEvent.change(poolSelect() as HTMLSelectElement, { target: { value: 'p1' } });

    fireEvent.click(modeRadio('手动选择角色'));
    fireEvent.click(roleCheckbox('测试验证'));
    fireEvent.click(modeRadio('选择角色池'));
    expect((poolSelect() as HTMLSelectElement).value).toBe('p1');

    fireEvent.click(modeRadio('手动选择角色'));
    expect(roleCheckbox('测试验证').checked).toBe(true);
  });

  it('「智能推荐」是不占位展开的禁用项，文案注明预计未来开放', async () => {
    const { container } = renderDialog();

    await waitFor(() => expect(api.getRolePools).toHaveBeenCalled());
    const radio = modeRadio('智能推荐');
    expect(radio.disabled).toBe(true);
    expect(screen.getByText('根据任务自动推荐候选角色（预计未来开放）')).toBeDefined();
    // 未开放 → 永远不该成为展开的那一项
    expect(container.querySelectorAll('.candidate-pool-mode.is-disabled')).toHaveLength(1);
    expect(modeRadio('选择角色池').checked).toBe(true);
  });
});

describe('CandidateRolesDialog 绑定与保存', () => {
  it('saves the selected pool as a role snapshot, not the pool id', async () => {
    const onSaved = vi.fn();
    const onClose = vi.fn();
    renderDialog(onClose, onSaved);

    await waitFor(() => expect(api.getRolePools).toHaveBeenCalled());
    fireEvent.change(poolSelect() as HTMLSelectElement, { target: { value: 'p1' } });
    fireEvent.click(screen.getByText('应用'));

    await waitFor(() => expect(api.updateSessionSettings).toHaveBeenCalled());
    expect(lastConfig()).toEqual({ candidate_roles: ['code', 'test'] });
    expect(onSaved).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it('saves manually picked roles and ignores the pool selection', async () => {
    renderDialog();

    await waitFor(() => expect(api.getRoles).toHaveBeenCalled());
    fireEvent.click(modeRadio('手动选择角色'));
    fireEvent.click(roleCheckbox('测试验证'));
    fireEvent.click(screen.getByText('应用'));

    await waitFor(() => expect(api.updateSessionSettings).toHaveBeenCalled());
    expect(lastConfig()).toEqual({ candidate_roles: ['test'] });
  });

  it('prefills an existing binding that matches no pool and unions further picks', async () => {
    bind(['code', 'engineering_coord']);
    renderDialog();

    // 现有绑定不等于任何池 → 落到手选态，不静默改绑
    await waitFor(() => expect(modeRadio('手动选择角色').checked).toBe(true));
    expect(poolSelect()).toBeNull();
    expect(roleCheckbox('代码编写').checked).toBe(true);
    expect(roleCheckbox('工程协调').checked).toBe(true);

    fireEvent.click(roleCheckbox('测试验证'));
    fireEvent.click(screen.getByText('应用'));

    await waitFor(() => expect(api.updateSessionSettings).toHaveBeenCalled());
    expect(lastConfig()).toEqual({ candidate_roles: ['code', 'engineering_coord', 'test'] });
  });

  it('prefills the form from the matching pool when the binding equals it', async () => {
    bind(['code', 'test']);
    renderDialog();

    await waitFor(() => expect((poolSelect() as HTMLSelectElement).value).toBe('p1'));
    expect(modeRadio('选择角色池').checked).toBe(true);
    expect(screen.getByText('当前绑定 2 个角色')).toBeDefined();
  });

  it('unbinds with an empty candidate_roles list', async () => {
    bind(['code', 'test']);
    renderDialog();

    await waitFor(() => expect(screen.getByText('解除绑定')).toBeDefined());
    fireEvent.click(screen.getByText('解除绑定'));

    await waitFor(() => expect(api.updateSessionSettings).toHaveBeenCalled());
    expect(lastConfig()).toEqual({ candidate_roles: [] });
  });

  it('defaults the dropdown to the first pool so Apply is ready without an extra pick', async () => {
    renderDialog();

    await waitFor(() => expect((poolSelect() as HTMLSelectElement).value).toBe('p1'));
    const apply = screen.getByText('应用').closest('button') as HTMLButtonElement;
    expect(apply.disabled).toBe(false);

    fireEvent.click(apply);
    await waitFor(() => expect(api.updateSessionSettings).toHaveBeenCalled());
    // 仍然写角色 id 快照，而不是池 id
    expect(lastConfig()).toEqual({ candidate_roles: ['code', 'test'] });
  });

  it('offers no empty option in the pool dropdown', async () => {
    renderDialog();

    await waitFor(() => expect(api.getRolePools).toHaveBeenCalled());
    const options = Array.from((poolSelect() as HTMLSelectElement).options);
    expect(options.map((option) => option.value)).toEqual(['p1', 'p2']);
    expect(options.some((option) => option.value === '')).toBe(false);
  });

  it('never submits an empty selection when there are no pools at all', async () => {
    vi.mocked(api.getRolePools).mockResolvedValue([]);
    renderDialog();

    // 没有池 → 落到手选态，且没有可勾选项 → 应用保持禁用
    await waitFor(() => expect(modeRadio('手动选择角色').checked).toBe(true));
    expect(poolSelect()).toBeNull();

    const apply = screen.getByText('应用').closest('button') as HTMLButtonElement;
    expect(apply.disabled).toBe(true);

    fireEvent.click(apply);
    expect(api.updateSessionSettings).not.toHaveBeenCalled();
  });

  it('shows a hint instead of a dropdown when pool mode is picked with no pools', async () => {
    vi.mocked(api.getRolePools).mockResolvedValue([]);
    renderDialog();

    await waitFor(() => expect(modeRadio('手动选择角色').checked).toBe(true));
    fireEvent.click(modeRadio('选择角色池'));

    expect(poolSelect()).toBeNull();
    expect(screen.getByText('暂无角色池。角色池的新增与编辑在「设置 → 角色池」中完成。')).toBeDefined();
  });

  it('renders nothing without an active session', () => {
    const { container } = render(
      <I18nProvider>
        <CandidateRolesDialog open sessionId={null} onClose={() => {}} onSaved={() => {}} />
      </I18nProvider>,
    );

    expect(container.querySelector('.workbench-dialog')).toBeNull();
    expect(api.getRolePools).not.toHaveBeenCalled();
  });
});
