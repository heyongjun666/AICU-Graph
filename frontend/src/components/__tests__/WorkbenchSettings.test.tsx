// @vitest-environment jsdom
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import WorkbenchSettings from '../WorkbenchSettings';
import * as api from '../../utils/api';
import { RoleField } from '../../types';

vi.mock('../../utils/api', () => ({
  createRole: vi.fn(),
  createSkill: vi.fn(),
  getGlobalConfig: vi.fn(),
  getRole: vi.fn(),
  getRoleSchema: vi.fn(),
  getRoles: vi.fn(),
  getSkill: vi.fn(),
  getSkills: vi.fn(),
  updateGlobalConfig: vi.fn(),
  updateRole: vi.fn(),
  updateSkill: vi.fn(),
}));

const fieldNames = [
  'summary', 'description', 'suitable_for', 'not_for',
  'num_ccs', 'model', 'cli_name', 'audit_rounds',
  'skills_dir', 'skills', 'output_file', 'final_file',
  'modular_output', 'main_doc', 'index_file', 'generate_instruction',
  'revise_instruction', 'review_checklist', 'adversarial_role',
  'audit_role', 'audit_angles', 'role_md',
];

const fields: RoleField[] = fieldNames.map((name) => ({
  name,
  label: name.replace(/_/g, ' '),
  type: ['suitable_for', 'not_for', 'skills', 'review_checklist', 'audit_angles'].includes(name)
    ? 'list'
    : name === 'num_ccs' || name === 'audit_rounds'
      ? 'number'
      : name === 'modular_output'
        ? 'boolean'
        : name.includes('instruction') || ['adversarial_role', 'audit_role', 'role_md'].includes(name)
          ? 'multiline'
          : 'text',
  default: null,
  has_default: !['description', 'main_doc', 'index_file'].includes(name),
}));

beforeEach(() => {
  vi.mocked(api.getGlobalConfig).mockResolvedValue({
    config: { workspace_root: null, cli_name: null, model: null, api_base_url: null, api_key_env: null },
    clis: [],
    default_cli: 'claude',
  });
  vi.mocked(api.getRoles).mockResolvedValue([]);
  vi.mocked(api.getSkills).mockResolvedValue([]);
  vi.mocked(api.getRoleSchema).mockResolvedValue({
    role_type: 'worker',
    defaults: {},
    fields,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('WorkbenchSettings roles form', () => {
  it('renders every schema field, including fields absent from default.yaml', async () => {
    const { container } = render(
      <WorkbenchSettings
        open
        onClose={() => {}}
        onSaved={() => {}}
      />,
    );

    await waitFor(() => expect(api.getRoleSchema).toHaveBeenCalledWith('worker'));
    fireEvent.click(screen.getByText('角色'));

    expect(await screen.findByText('该角色类型支持 22 个字段。仅覆盖该角色所需的值。')).toBeDefined();
    expect(screen.getByText('main_doc')).toBeDefined();
    expect(screen.getByText('index_file')).toBeDefined();
    expect(screen.getAllByText('description')).toHaveLength(2);
    expect(container.querySelectorAll('.role-field')).toHaveLength(22);
  });
});


describe('WorkbenchSettings execution section', () => {
  const renderSettings = () => {
    const view = render(
      <WorkbenchSettings open onClose={() => {}} onSaved={() => {}} />,
    );
    return view;
  };

  const openWorkspaceTab = async (container: HTMLElement) => {
    fireEvent.click(screen.getByText('工作区与执行'));
    // 等待异步 getGlobalConfig 落地（右侧面板渲染出执行区块）
    await waitFor(() =>
      expect(screen.getByText('单节点 agent 数量动态配置')).toBeDefined(),
    );
    return container;
  };

  const toggleOf = (container: HTMLElement) =>
    container.querySelector('.settings-toggle input[type="checkbox"]') as HTMLInputElement;

  const numInput = (container: HTMLElement) =>
    container.querySelector('.settings-section input[type="number"]') as HTMLInputElement | null;

  it('defaults to dynamic with no number input', async () => {
    // getGlobalConfig mock（beforeEach）未提供新字段，模拟旧后端 / 老 config.json
    const { container } = renderSettings();
    await openWorkspaceTab(container);

    expect(toggleOf(container).checked).toBe(true);
    expect(numInput(container)).toBeNull();
  });

  it('defaults to dynamic when the backend returns the fields explicitly', async () => {
    vi.mocked(api.getGlobalConfig).mockResolvedValue({
      config: {
        workspace_root: null, cli_name: null, model: null,
        api_base_url: null, api_key_env: null,
        dynamic_num_ccs: true, default_num_ccs: 2,
      },
      clis: [],
      default_cli: 'claude',
    });
    const { container } = renderSettings();
    await openWorkspaceTab(container);

    expect(toggleOf(container).checked).toBe(true);
    expect(numInput(container)).toBeNull();
  });

  it('reveals a 1-4 number input when the toggle is turned off', async () => {
    const { container } = renderSettings();
    await openWorkspaceTab(container);

    fireEvent.click(toggleOf(container));

    const input = await waitFor(() => {
      const el = numInput(container);
      expect(el).not.toBeNull();
      return el as HTMLInputElement;
    });
    expect(input.min).toBe('1');
    expect(input.max).toBe('4');
    expect(input.value).toBe('1');
  });

  it('shows the persisted default count when reopened with the toggle off', async () => {
    vi.mocked(api.getGlobalConfig).mockResolvedValue({
      config: {
        workspace_root: null, cli_name: null, model: null,
        api_base_url: null, api_key_env: null,
        dynamic_num_ccs: false, default_num_ccs: 3,
      },
      clis: [],
      default_cli: 'claude',
    });
    const { container } = renderSettings();
    await openWorkspaceTab(container);

    expect(toggleOf(container).checked).toBe(false);
    expect((numInput(container) as HTMLInputElement).value).toBe('3');
  });

  it('saves the toggle state and default count', async () => {
    vi.mocked(api.updateGlobalConfig).mockResolvedValue({ config: {} as never });
    const { container } = renderSettings();
    await openWorkspaceTab(container);

    fireEvent.click(toggleOf(container));
    fireEvent.change(numInput(container) as HTMLInputElement, { target: { value: '3' } });
    fireEvent.click(screen.getByText('保存执行配置'));

    await waitFor(() => expect(api.updateGlobalConfig).toHaveBeenCalled());
    expect(vi.mocked(api.updateGlobalConfig).mock.calls[0][0]).toMatchObject({
      dynamic_num_ccs: false,
      default_num_ccs: 3,
    });
  });

  it('clamps out-of-range values before saving', async () => {
    vi.mocked(api.updateGlobalConfig).mockResolvedValue({ config: {} as never });
    const { container } = renderSettings();
    await openWorkspaceTab(container);

    fireEvent.click(toggleOf(container));
    fireEvent.change(numInput(container) as HTMLInputElement, { target: { value: '9' } });
    fireEvent.click(screen.getByText('保存执行配置'));

    await waitFor(() => expect(api.updateGlobalConfig).toHaveBeenCalled());
    expect(vi.mocked(api.updateGlobalConfig).mock.calls[0][0]).toMatchObject({
      default_num_ccs: 4,
    });
  });

  it('does not touch execution settings when saving the workspace only', async () => {
    vi.mocked(api.updateGlobalConfig).mockResolvedValue({ config: {} as never });
    const { container } = renderSettings();
    await openWorkspaceTab(container);

    fireEvent.click(screen.getByText('保存工作区'));

    await waitFor(() => expect(api.updateGlobalConfig).toHaveBeenCalled());
    const payload = vi.mocked(api.updateGlobalConfig).mock.calls[0][0];
    expect(payload).not.toHaveProperty('dynamic_num_ccs');
    expect(payload).not.toHaveProperty('default_num_ccs');
  });
});
