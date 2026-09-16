// @vitest-environment jsdom
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import WorkbenchSettings from '../WorkbenchSettings';
import * as api from '../../utils/api';
import { CliInfo, GlobalConfig, ProviderPreset, RoleField, RoleInfo } from '../../types';

vi.mock('../../utils/api', () => ({
  createRole: vi.fn(),
  createRolePool: vi.fn(),
  createSkill: vi.fn(),
  deleteRolePool: vi.fn(),
  getEffectiveBaseUrl: vi.fn(),
  getGlobalConfig: vi.fn(),
  getRole: vi.fn(),
  getRolePools: vi.fn(),
  getRoleSchema: vi.fn(),
  getRoles: vi.fn(),
  getSkill: vi.fn(),
  getSkills: vi.fn(),
  updateGlobalConfig: vi.fn(),
  updateRole: vi.fn(),
  updateRolePool: vi.fn(),
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
  vi.mocked(api.getRolePools).mockResolvedValue([]);
  // 生效地址由后端算（真实规则见 tests/test_cli_adapters.py::TestEffectiveBaseUrl）。
  // 这里只回显已配置的地址，判据不落在这份 stub 里 —— 前端本来就不该复刻那套规则。
  vi.mocked(api.getEffectiveBaseUrl).mockImplementation(async ({ apiBaseUrl }) => ({
    base_url: apiBaseUrl || null,
    source: apiBaseUrl ? 'configured' : 'cli_default',
  }));
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
    // 未选中任何角色时是空闲态，只给一句提示；表单要点了「新建角色」才出现。
    fireEvent.click(screen.getByText('新建角色'));
    expect(screen.getByText('main_doc')).toBeDefined();
    expect(screen.getByText('index_file')).toBeDefined();
    expect(screen.getAllByText('description')).toHaveLength(2);
    expect(container.querySelectorAll('.role-field')).toHaveLength(22);
  });

  it('unlocks the config fields on Edit but keeps the role identity locked', async () => {
    vi.mocked(api.getRoles).mockResolvedValue([
      { id: 'code', display_name: '代码编写', type: 'worker' },
    ]);
    vi.mocked(api.getRole).mockResolvedValue({
      id: 'code',
      display_name: '代码编写',
      type: 'worker',
      config: { summary: '写代码' },
      defaults: {},
      effective_config: { summary: '写代码' },
    });
    const { container } = render(
      <WorkbenchSettings open onClose={() => {}} onSaved={() => {}} />,
    );

    await waitFor(() => expect(api.getRoleSchema).toHaveBeenCalled());
    fireEvent.click(screen.getByText('角色'));
    fireEvent.click(await screen.findByText('代码编写'));

    const identityInputs = () =>
      Array.from(container.querySelectorAll('.form-grid input, .form-grid select')) as (
        | HTMLInputElement
        | HTMLSelectElement
      )[];

    // 查看态：整个表单只读，且没有保存/删除
    expect(identityInputs().every((input) => input.disabled)).toBe(true);
    expect(screen.queryByText('保存角色')).toBeNull();

    fireEvent.click(Array.from(document.querySelectorAll('.section-heading-actions button'))
      .find((button) => button.textContent?.includes('编辑')) as HTMLButtonElement);

    // 后端 PATCH 拒绝改 id/type，所以编辑态下这两个输入仍然锁着，并给出理由
    const [roleIdInput, roleTypeSelect] = identityInputs();
    expect(roleIdInput.disabled).toBe(true);
    expect(roleTypeSelect.disabled).toBe(true);
    expect(roleIdInput.title).toBe('角色 ID 与类型创建后不可修改，如需改名请删除后重建。');
    // 但 config 字段解锁了
    expect(container.querySelectorAll('.role-field input:not([disabled])').length).toBeGreaterThan(0);
    expect(screen.getByText('保存角色')).toBeDefined();
    expect(screen.getByText('删除角色')).toBeDefined();
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

describe('WorkbenchSettings role pools', () => {
  const poolRoles: RoleInfo[] = [
    { id: 'code', display_name: '代码编写', type: 'worker' },
    { id: 'test', display_name: '测试验证', type: 'worker' },
    { id: 'engineering_coord', display_name: '工程协调', type: 'manager' },
  ];
  const existingPool = { id: 'p1', name: '博客开发', roles: ['code'] };

  const renderSettings = () =>
    render(<WorkbenchSettings open onClose={() => {}} onSaved={() => {}} />);

  /** 顶部导航里的 tab 按钮（正文标题与 tab 同名，直接 getByText 会命中两个）。 */
  const tabButton = (label: string) =>
    Array.from(document.querySelectorAll('.settings-nav button'))
      .find((button) => button.textContent === label) as HTMLButtonElement;

  const openPoolsTab = async () => {
    fireEvent.click(tabButton('角色池'));
    await waitFor(() => expect(api.getRolePools).toHaveBeenCalled());
  };

  const nameInputOf = (container: HTMLElement) =>
    container.querySelector('.role-form input') as HTMLInputElement;
  /** picker 中的勾选框：label 内还有一个类型 small，getByLabelText 精确匹配会失败。 */
  const roleCheckbox = (label: string) =>
    (screen.getByText(label).closest('label') as HTMLLabelElement)
      .querySelector('input') as HTMLInputElement;

  const saveButtonOf = () =>
    screen.getByText('保存角色池').closest('button') as HTMLButtonElement;

  /** section-heading 右侧的「编辑」/「新建」按钮：只有非编辑态才会出现。 */
  const headingButton = (label: string) =>
    Array.from(document.querySelectorAll('.section-heading-actions button'))
      .find((button) => button.textContent?.includes(label)) as HTMLButtonElement;

  const editButtonOf = () => headingButton('编辑');
  const newPoolButtonOf = () => headingButton('新建角色池');

  /** 左侧资源列表里的行按钮（正文里的 input 不带文本，不会误命中）。 */
  const poolRow = (name: string) =>
    screen.getByText(name).closest('button') as HTMLButtonElement;

  const cancelButtonOf = () =>
    screen.getByText('取消').closest('button') as HTMLButtonElement;

  const deleteButtonOf = () =>
    screen.getByText('删除角色池').closest('button') as HTMLButtonElement;

  beforeEach(() => {
    vi.mocked(api.getRoles).mockResolvedValue(poolRoles);
  });

  it('lists existing pools and prefills the form with the first one', async () => {
    vi.mocked(api.getRolePools).mockResolvedValue([existingPool]);
    const { container } = renderSettings();
    await openPoolsTab();

    expect(await screen.findByText('博客开发')).toBeDefined();
    expect(nameInputOf(container).value).toBe('博客开发');
    expect((roleCheckbox('代码编写') as HTMLInputElement).checked).toBe(true);
    expect((roleCheckbox('测试验证') as HTMLInputElement).checked).toBe(false);
  });

  it('updates the selected pool instead of creating a new one', async () => {
    vi.mocked(api.getRolePools).mockResolvedValue([existingPool]);
    vi.mocked(api.updateRolePool).mockResolvedValue({
      ...existingPool,
      roles: ['code', 'test'],
    });
    renderSettings();
    await openPoolsTab();
    fireEvent.click(editButtonOf());

    fireEvent.click(roleCheckbox('测试验证'));
    fireEvent.click(saveButtonOf());

    await waitFor(() => expect(api.updateRolePool).toHaveBeenCalled());
    expect(vi.mocked(api.updateRolePool).mock.calls[0]).toEqual([
      'p1',
      { name: '博客开发', roles: ['code', 'test'] },
    ]);
    expect(api.createRolePool).not.toHaveBeenCalled();
  });

  it('creates a new pool when nothing is selected', async () => {
    vi.mocked(api.createRolePool).mockResolvedValue({ id: 'p2', name: '纯编码', roles: ['code'] });
    const { container } = renderSettings();
    await openPoolsTab();

    expect(await screen.findByText('暂无角色池，点击「新建角色池」创建。')).toBeDefined();
    // 未选中任何池时是只读态，保存/删除都不该出现
    expect(screen.queryByText('保存角色池')).toBeNull();
    expect(screen.queryByText('删除角色池')).toBeNull();

    fireEvent.click(newPoolButtonOf());
    expect(saveButtonOf().disabled).toBe(true);

    fireEvent.change(nameInputOf(container), { target: { value: '纯编码' } });
    fireEvent.click(roleCheckbox('代码编写'));
    fireEvent.click(saveButtonOf());

    await waitFor(() => expect(api.createRolePool).toHaveBeenCalledWith({ name: '纯编码', roles: ['code'] }));
    expect(api.updateRolePool).not.toHaveBeenCalled();
  });

  it('disables save when the name is blank or no role is picked', async () => {
    vi.mocked(api.getRolePools).mockResolvedValue([existingPool]);
    const { container } = renderSettings();
    await openPoolsTab();
    fireEvent.click(editButtonOf());

    expect(saveButtonOf().disabled).toBe(false);

    fireEvent.change(nameInputOf(container), { target: { value: '   ' } });
    expect(saveButtonOf().disabled).toBe(true);

    fireEvent.change(nameInputOf(container), { target: { value: '博客开发' } });
    fireEvent.click(roleCheckbox('代码编写'));
    expect(saveButtonOf().disabled).toBe(true);

    fireEvent.click(saveButtonOf());
    expect(api.updateRolePool).not.toHaveBeenCalled();
    expect(api.createRolePool).not.toHaveBeenCalled();
  });

  it('deletes the selected pool after confirmation', async () => {
    vi.stubGlobal('confirm', vi.fn(() => true));
    vi.mocked(api.getRolePools).mockResolvedValueOnce([existingPool]).mockResolvedValue([]);
    vi.mocked(api.deleteRolePool).mockResolvedValue(undefined);
    renderSettings();
    await openPoolsTab();
    fireEvent.click(editButtonOf());

    fireEvent.click(screen.getByText('删除角色池'));

    await waitFor(() => expect(api.deleteRolePool).toHaveBeenCalledWith('p1'));
    expect(await screen.findByText('暂无角色池，点击「新建角色池」创建。')).toBeDefined();
    vi.unstubAllGlobals();
  });

  it('keeps a selected pool read-only until Edit is clicked', async () => {
    vi.mocked(api.getRolePools).mockResolvedValue([existingPool]);
    const { container } = renderSettings();
    await openPoolsTab();

    // 点条目只是查看：没有保存/删除，表单是只读的
    expect(screen.queryByText('保存角色池')).toBeNull();
    expect(screen.queryByText('删除角色池')).toBeNull();
    expect(nameInputOf(container).disabled).toBe(true);
    expect(roleCheckbox('代码编写').disabled).toBe(true);

    // 「编辑」可用（有选中项），「新建」也在
    expect(editButtonOf().disabled).toBe(false);
    expect(newPoolButtonOf()).toBeDefined();
  });

  it('reveals save / delete / cancel and unlocks the form once Edit is clicked', async () => {
    vi.mocked(api.getRolePools).mockResolvedValue([existingPool]);
    const { container } = renderSettings();
    await openPoolsTab();
    fireEvent.click(editButtonOf());

    expect(saveButtonOf()).toBeDefined();
    expect(deleteButtonOf()).toBeDefined();
    expect(cancelButtonOf()).toBeDefined();
    expect(nameInputOf(container).disabled).toBe(false);
    expect(roleCheckbox('代码编写').disabled).toBe(false);
    // 编辑态下不该再挂一个「编辑」按钮
    expect(editButtonOf()).toBeUndefined();
  });

  it('discards the edits and restores the stored values on Cancel', async () => {
    vi.mocked(api.getRolePools).mockResolvedValue([existingPool]);
    const { container } = renderSettings();
    await openPoolsTab();

    fireEvent.click(editButtonOf());
    fireEvent.change(nameInputOf(container), { target: { value: '被改掉的名字' } });
    fireEvent.click(roleCheckbox('代码编写')); // 取消勾选
    fireEvent.click(cancelButtonOf());

    // 回到只读：保存/删除消失，取值回到已存的那份
    expect(screen.queryByText('保存角色池')).toBeNull();
    expect(nameInputOf(container).value).toBe('博客开发');
    expect(nameInputOf(container).disabled).toBe(true);
    expect(roleCheckbox('代码编写').checked).toBe(true);

    // 再进编辑态看到的仍是原值，说明改动确实被丢弃而不是被留在了草稿里
    fireEvent.click(editButtonOf());
    expect(nameInputOf(container).value).toBe('博客开发');
    expect(roleCheckbox('代码编写').checked).toBe(true);
  });

  it('asks before switching rows mid-edit and stays put when declined', async () => {
    const confirmMock = vi.fn(() => false);
    vi.stubGlobal('confirm', confirmMock);
    vi.mocked(api.getRolePools).mockResolvedValue([
      existingPool,
      { id: 'p2', name: '纯编码', roles: ['test'] },
    ]);
    const { container } = renderSettings();
    await openPoolsTab();

    fireEvent.click(editButtonOf());
    fireEvent.change(nameInputOf(container), { target: { value: '草稿' } });
    fireEvent.click(poolRow('纯编码'));

    expect(confirmMock).toHaveBeenCalled();
    // 拒绝 → 不切换，仍停在原条目的编辑态，草稿还在
    expect(nameInputOf(container).value).toBe('草稿');
    expect(saveButtonOf()).toBeDefined();
    expect(poolRow('博客开发').className).toContain('is-active');

    // 同意 → 切过去，并退出编辑态
    confirmMock.mockReturnValue(true);
    fireEvent.click(poolRow('纯编码'));
    await waitFor(() => expect(nameInputOf(container).value).toBe('纯编码'));
    expect(screen.queryByText('保存角色池')).toBeNull();
    expect(poolRow('纯编码').className).toContain('is-active');
    vi.unstubAllGlobals();
  });

  it('does not ask when switching while not editing', async () => {
    const confirmMock = vi.fn(() => true);
    vi.stubGlobal('confirm', confirmMock);
    vi.mocked(api.getRolePools).mockResolvedValue([
      existingPool,
      { id: 'p2', name: '纯编码', roles: ['test'] },
    ]);
    const { container } = renderSettings();
    await openPoolsTab();

    fireEvent.click(poolRow('纯编码'));

    await waitFor(() => expect(nameInputOf(container).value).toBe('纯编码'));
    expect(confirmMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('asks before switching to create mid-edit, and stays put when declined', async () => {
    const confirmMock = vi.fn(() => false);
    vi.stubGlobal('confirm', confirmMock);
    vi.mocked(api.getRolePools).mockResolvedValue([existingPool]);
    const { container } = renderSettings();
    await openPoolsTab();

    fireEvent.click(editButtonOf());
    fireEvent.change(nameInputOf(container), { target: { value: '草稿' } });
    // 「新建」在编辑态下仍然可见：它是除左侧列表之外第二条离开编辑态的路
    fireEvent.click(newPoolButtonOf());

    expect(confirmMock).toHaveBeenCalled();
    expect(confirmMock.mock.calls[0][0]).toContain('博客开发');
    expect(nameInputOf(container).value).toBe('草稿');

    confirmMock.mockReturnValue(true);
    fireEvent.click(newPoolButtonOf());
    await waitFor(() => expect(nameInputOf(container).value).toBe(''));
    // 新建态：可编辑、有保存、没有删除
    expect(nameInputOf(container).disabled).toBe(false);
    expect(saveButtonOf()).toBeDefined();
    expect(screen.queryByText('删除角色池')).toBeNull();
    vi.unstubAllGlobals();
  });

  it('asks before closing the panel mid-edit and names the entry being edited', async () => {
    const confirmMock = vi.fn(() => false);
    vi.stubGlobal('confirm', confirmMock);
    vi.mocked(api.getRolePools).mockResolvedValue([existingPool]);
    const onClose = vi.fn();
    render(<WorkbenchSettings open onClose={onClose} onSaved={() => {}} />);
    await openPoolsTab();

    const closeButton = () =>
      document.querySelector('.settings-dialog .dialog-header .icon-button') as HTMLButtonElement;

    // 不在编辑态：直接关，不该多问一句
    fireEvent.click(closeButton());
    expect(confirmMock).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledTimes(1);

    // 进入编辑态后关闭要问，且提示里指的是库里那条，不是表单里的草稿名
    onClose.mockClear();
    confirmMock.mockClear();
    fireEvent.click(editButtonOf());
    fireEvent.change(nameInputOf(document.body), { target: { value: '草稿' } });
    fireEvent.click(closeButton());

    expect(confirmMock.mock.calls[0][0]).toContain('博客开发');
    expect(confirmMock.mock.calls[0][0]).not.toContain('草稿');
    expect(onClose).not.toHaveBeenCalled();

    confirmMock.mockReturnValue(true);
    fireEvent.click(closeButton());
    expect(onClose).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it('starts a new pool directly editable and without a delete button', async () => {
    vi.mocked(api.getRolePools).mockResolvedValue([existingPool]);
    const { container } = renderSettings();
    await openPoolsTab();

    fireEvent.click(newPoolButtonOf());

    expect(nameInputOf(container).disabled).toBe(false);
    expect(nameInputOf(container).value).toBe('');
    expect(saveButtonOf()).toBeDefined();
    expect(cancelButtonOf()).toBeDefined();
    // 还没存下来的条目不该有删除按钮
    expect(screen.queryByText('删除角色池')).toBeNull();
  });
});

// 模型下拉只显示「当前 CLI 真能用」的模型。过滤的主体在后端（CliAdapter.known_models
// 按协议端点过滤），这里覆盖前端这一侧：候选是「当前 CLI 的 models」∪「厂商预置的
// models」，而后者与当前 CLI 无关，曾经会把别家 CLI 的模型混进来。
describe('WorkbenchSettings 模型候选按 CLI 过滤', () => {
  const CLIS: CliInfo[] = [
    { id: 'claude', display_name: 'Claude Code', installed: true, priority: 0,
      models: ['claude-opus-5', 'grok-4.6'] },
    { id: 'codex', display_name: 'OpenAI Codex CLI', installed: true, priority: 1,
      models: ['gpt-5.6', 'grok-4.6'] },
  ];

  // gpt-5.6-mini 刻意不在任何 CLI 的 models 里，只能从预置并进来 —— 它的出现与否
  // 就是「预置模型有没有被并入」的判据。
  const PRESETS: ProviderPreset[] = [
    { id: 'openai', display_name: 'OpenAI 官方 API', cli_name: 'codex', model: 'gpt-5.6',
      models: ['gpt-5.6', 'gpt-5.6-mini'], api_base_url: 'https://api.openai.com/v1',
      available: true },
  ];

  const renderSettings = async (config: Partial<GlobalConfig> = {}) => {
    vi.mocked(api.getGlobalConfig).mockResolvedValue({
      config: {
        workspace_root: null, cli_name: null, model: null,
        api_base_url: null, api_key_env: null, ...config,
      },
      clis: CLIS,
      provider_presets: PRESETS,
      default_cli: 'claude',
    });
    const view = render(<WorkbenchSettings open onClose={() => {}} onSaved={() => {}} />);
    await waitFor(() => expect(api.getGlobalConfig).toHaveBeenCalled());
    return view;
  };

  // 模型控件有两种形态：预置匹配上时是 <select id="model-select">，手动配置时是
  // <input list="model-options"> + <datalist id="model-options">。两者都读。
  const modelOptions = () =>
    Array.from(document.querySelectorAll<HTMLOptionElement>(
      '#model-options option, #model-select option'))
      .map((option) => option.value)
      .filter(Boolean);
  const providerSelect = () =>
    document.querySelector<HTMLSelectElement>('#provider-select') as HTMLSelectElement;
  const cliSelect = () =>
    document.querySelector<HTMLSelectElement>('#cli-select') as HTMLSelectElement;
  const modelSelect = () => document.querySelector<HTMLSelectElement>('#model-select');

  it('切换 CLI 后，别家预置的模型不再出现在候选里', async () => {
    await renderSettings();
    // 选 OpenAI 预置：CLI 自动变成 codex，预置的 gpt-5.6-mini 被并入候选
    fireEvent.change(providerSelect(), { target: { value: 'openai' } });
    await waitFor(() => expect(modelOptions()).toContain('gpt-5.6-mini'));

    // 再把 CLI 切成 claude：OpenAI 没有 /v1/messages 端点，gpt-5.6-mini 必须消失
    fireEvent.change(cliSelect(), { target: { value: 'claude' } });
    await waitFor(() => expect(modelOptions()).not.toContain('gpt-5.6-mini'));
    // 已存的 model 是 gpt-5.6，它此刻不在 claude 的候选里 —— 但不能被静默丢掉，
    // 要作为「当前值」仍在下拉里（见下面「目录外的 model」那条用例）。
    expect(modelOptions()).toEqual(['gpt-5.6', 'claude-opus-5', 'grok-4.6']);
  });

  it('预置的 CLI 与当前 CLI 一致时，预置模型仍然并入候选', async () => {
    // 初始化按 model 命中 openai 预置，而它的 cli_name 正是当前 CLI codex
    await renderSettings({ cli_name: 'codex', model: 'gpt-5.6' });
    await waitFor(() => expect(modelOptions()).toContain('gpt-5.6-mini'));
    expect(modelOptions()).toContain('grok-4.6');
  });

  it('配置里存着不存在的 CLI 时，候选退回默认 CLI 而不是全部 CLI 的并集', async () => {
    // 'gemini' 是后端隐藏的 CLI，不会出现在 clis 里；不退回的话候选会变成
    // claude ∪ codex 的并集，把 Claude 用不了的 gpt-5.6 带回来
    await renderSettings({ cli_name: 'gemini' });
    await waitFor(() => expect(modelOptions().length).toBeGreaterThan(0));
    expect(modelOptions()).toEqual(['claude-opus-5', 'grok-4.6']);
    expect(modelOptions()).not.toContain('gpt-5.6');
  });

  it('目录外的 model 会作为「当前值」保留在下拉里，不被静默清掉', async () => {
    // 老配置：CLI 与地址对得上 openai 预置，但 model 存着目录里没有的 gpt-5.6-turbo。
    // <select value> 匹配不到 option 会显示成空白 —— 用户看到的和实际保存的不是一回事，
    // 一动别的字段再保存这个值就被换掉了。
    await renderSettings({ cli_name: 'codex', model: 'gpt-5.6-turbo',
      api_base_url: 'https://api.openai.com/v1' });
    await waitFor(() => expect(modelSelect()).not.toBeNull());
    expect(modelOptions()).toEqual(['gpt-5.6-turbo', 'gpt-5.6', 'gpt-5.6-mini', 'grok-4.6']);
    expect(modelSelect()!.value).toBe('gpt-5.6-turbo');
  });
});

describe('WorkbenchSettings Provider 下拉与模型形态', () => {
  const CLIS: CliInfo[] = [
    { id: 'claude', display_name: 'Claude Code', installed: true, priority: 0,
      models: ['claude-opus-5'] },
    // deepseek-v4-pro 是「同协议可达但不属于 openai 预置」的模型：预置模式下切到它，
    // 生效地址应当跟着模型厂商走（真实规则在后端，见 TestEffectiveBaseUrl）。
    { id: 'codex', display_name: 'OpenAI Codex CLI', installed: true, priority: 1,
      models: ['gpt-5.6', 'deepseek-v4-pro'] },
  ];
  const PRESETS: ProviderPreset[] = [
    { id: 'openai', display_name: 'OpenAI 官方 API', cli_name: 'codex', model: 'gpt-5.6',
      models: ['gpt-5.6'], api_base_url: 'https://api.openai.com/v1', available: true },
    { id: 'xai', display_name: 'xAI 官方 API · Grok', cli_name: 'grok', model: 'grok-4.6',
      models: ['grok-4.6'], api_base_url: 'https://api.x.ai/v1', available: false },
  ];

  const renderSettings = async (config: Partial<GlobalConfig> = {}) => {
    vi.mocked(api.getGlobalConfig).mockResolvedValue({
      config: {
        workspace_root: null, cli_name: null, model: null,
        api_base_url: null, api_key_env: null, ...config,
      },
      clis: CLIS,
      provider_presets: PRESETS,
      default_cli: 'claude',
    });
    render(<WorkbenchSettings open onClose={() => {}} onSaved={() => {}} />);
    await waitFor(() => expect(api.getGlobalConfig).toHaveBeenCalled());
  };

  const providerSelect = () =>
    document.querySelector<HTMLSelectElement>('#provider-select') as HTMLSelectElement;
  const modelInput = () => document.querySelector<HTMLInputElement>('input[list="model-options"]');
  const modelSelect = () => document.querySelector<HTMLSelectElement>('#model-select');
  const addressInput = () =>
    document.querySelector<HTMLInputElement>('#api-base-url') as HTMLInputElement;

  it('Provider 只能下拉选择，没有自由输入框', async () => {
    await renderSettings();
    expect(document.querySelector('input[list="provider-options"]')).toBeNull();
    expect(providerSelect()).not.toBeNull();
    // 只列可用的预置；xai 是 available:false，不该出现
    const values = Array.from(providerSelect().options).map((option) => option.value);
    expect(values).toContain('openai');
    expect(values).not.toContain('xai');
    // 「手动配置」是下拉里的一项，不是靠手输触发
    expect(values).toContain('__manual__');
    // 没有「不指定」那一项：「什么都没选」和「我要自己填」在界面上长得一样，
    // 用户却以为是两回事 —— 空值一律由「手动配置」承担。
    expect(values).not.toContain('');
  });

  it('模型严格下拉里没有空值项（不再有「使用运行时默认值」）', async () => {
    await renderSettings({ cli_name: 'codex', model: 'gpt-5.6',
      api_base_url: 'https://api.openai.com/v1' });
    await waitFor(() => expect(modelSelect()).not.toBeNull());
    const values = Array.from(modelSelect()!.options).map((option) => option.value);
    expect(values).not.toContain('');
    expect(modelSelect()!.value).toBe('gpt-5.6');
  });

  it('选预置后 API 地址锁成只读；切「手动配置」会清空并解锁', async () => {
    // 匹配不到预置 → 默认就是「手动配置」：这一态不宣称任何事实
    await renderSettings();
    expect(providerSelect().value).toBe('__manual__');
    expect(addressInput().disabled).toBe(false);

    fireEvent.change(providerSelect(), { target: { value: 'openai' } });
    await waitFor(() => expect(addressInput().value).toBe('https://api.openai.com/v1'));
    expect(addressInput().disabled).toBe(true);

    fireEvent.change(providerSelect(), { target: { value: '__manual__' } });
    // 必须清空：残留的预置地址会被后端按模型厂商改写掉（cli_adapters.py:216-217），
    // 界面上写着「手动配置」而实际地址被换掉。
    await waitFor(() => expect(addressInput().value).toBe(''));
    expect(addressInput().disabled).toBe(false);
  });

  it('预置模式下换模型，地址框显示的生效地址跟着后端变', async () => {
    await renderSettings({ cli_name: 'codex', model: 'gpt-5.6',
      api_base_url: 'https://api.openai.com/v1' });
    await waitFor(() => expect(addressInput().value).toBe('https://api.openai.com/v1'));

    // 后端按模型厂商算出的端点（这里只 stub 出「换模型就换个地址」这个事实）
    vi.mocked(api.getEffectiveBaseUrl).mockImplementation(async ({ model }) =>
      model === 'deepseek-v4-pro'
        ? { base_url: 'https://api.deepseek.com/v1', source: 'model_vendor' }
        : { base_url: 'https://api.openai.com/v1', source: 'configured' });

    fireEvent.change(modelSelect(), { target: { value: 'deepseek-v4-pro' } });
    // 显示的必须是后端算出的生效地址，而不是 config 里存着的那个预置地址
    await waitFor(() => expect(addressInput().value).toBe('https://api.deepseek.com/v1'));
    expect(addressInput().disabled).toBe(true);
    expect(api.getEffectiveBaseUrl).toHaveBeenLastCalledWith(
      expect.objectContaining({ model: 'deepseek-v4-pro' }),
    );
    // 提示语也要跟着换，否则用户看不出这个地址是随模型变的
    expect(screen.getByText('生效地址：由当前模型的厂商决定，换模型会跟着变')).toBeDefined();
  });

  it('手动模式下不问后端要生效地址：地址由用户自己填', async () => {
    await renderSettings({ cli_name: 'claude', model: 'my-internal-model',
      api_base_url: 'https://gateway.corp.internal/v1' });
    await waitFor(() => expect(providerSelect().value).toBe('__manual__'));
    expect(api.getEffectiveBaseUrl).not.toHaveBeenCalled();
  });

  it('只有「手动配置」才把模型放开为自由输入', async () => {
    // 预置：名单确定 → 只能下拉
    await renderSettings({ cli_name: 'codex', model: 'gpt-5.6',
      api_base_url: 'https://api.openai.com/v1' });
    await waitFor(() => expect(modelSelect()).not.toBeNull());
    expect(modelInput()).toBeNull();

    // 手动配置：端点由用户决定，我们算不出名单 → 放开手输
    fireEvent.change(providerSelect(), { target: { value: '__manual__' } });
    await waitFor(() => expect(modelInput()).not.toBeNull());
    expect(modelSelect()).toBeNull();
  });

  it('配置里存着自定义端点时落回「手动配置」，模型可手输且值保留', async () => {
    // 私有网关的用户：model 是目录外的值。若不落回手动模式，模型被锁成下拉，
    // 他连自己正在用的模型都选不回来。
    await renderSettings({ cli_name: 'claude', model: 'my-internal-model',
      api_base_url: 'https://gateway.corp.internal/v1' });
    await waitFor(() => expect(providerSelect().value).toBe('__manual__'));
    expect(modelInput()).not.toBeNull();
    expect(modelInput()!.value).toBe('my-internal-model');
    // 自定义地址原样保留，不被当作预置值
    expect(addressInput().value).toBe('https://gateway.corp.internal/v1');
    expect(addressInput().disabled).toBe(false);
  });

  it('预置认的是「它自己声称的那个 CLI」，CLI 被改过就不算数', async () => {
    // 存着 openai 预置的地址，但 CLI 是 claude：这套配置已经不是该预置产生的了，
    // 此时宣称「Provider = OpenAI 官方 API」会把用户没选过的参数按在他头上。
    await renderSettings({ cli_name: 'claude', model: 'gpt-5.6',
      api_base_url: 'https://api.openai.com/v1' });
    await waitFor(() => expect(providerSelect().value).toBe('__manual__'));
    expect(addressInput().value).toBe('https://api.openai.com/v1');
  });
});
