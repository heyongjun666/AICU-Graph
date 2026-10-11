// @vitest-environment jsdom
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import WorkbenchSettings from '../WorkbenchSettings';
import * as api from '../../utils/api';
import { useSessionStore } from '../../stores/SessionStore';
import { CliInfo, GlobalConfig, McpServerInfo, ProviderPreset, RoleField, RoleInfo, RolePoolExportPreview, RolePoolImportResult } from '../../types';
import { zh } from '../../i18n/zh';
import { en } from '../../i18n/en';

vi.mock('../../utils/api', () => ({
  createMcpServer: vi.fn(),
  createRole: vi.fn(),
  createRolePool: vi.fn(),
  createSkill: vi.fn(),
  deleteMcpServer: vi.fn(),
  deleteRolePool: vi.fn(),
  exportRolePool: vi.fn(),
  getEffectiveBaseUrl: vi.fn(),
  getGlobalConfig: vi.fn(),
  getMcpServers: vi.fn(),
  getRole: vi.fn(),
  getRolePools: vi.fn(),
  getRoleSchema: vi.fn(),
  getRoles: vi.fn(),
  getSkill: vi.fn(),
  getSkills: vi.fn(),
  importRolePool: vi.fn(),
  previewRolePoolExport: vi.fn(),
  probeMcpServer: vi.fn(),
  updateGlobalConfig: vi.fn(),
  updateMcpServer: vi.fn(),
  updateRole: vi.fn(),
  updateRolePool: vi.fn(),
  updateSkill: vi.fn(),
}));

const fieldNames = [
  'summary', 'description', 'suitable_for', 'not_for',
  'model', 'cli_name',
  'skills_dir', 'skills', 'output_file', 'final_file',
  'modular_output', 'main_doc', 'index_file', 'generate_instruction',
  'role_md',
];

const fields: RoleField[] = fieldNames.map((name) => ({
  name,
  label: name.replace(/_/g, ' '),
  type: ['suitable_for', 'not_for', 'skills'].includes(name)
    ? 'list'
    : name === 'modular_output'
      ? 'boolean'
      : name.includes('instruction') || name === 'role_md'
        ? 'multiline'
        : 'text',
  default: null,
  has_default: !['description', 'main_doc', 'index_file'].includes(name),
}));

beforeEach(() => {
  useSessionStore.setState({ sessions: [], activeSessionId: null });
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
  vi.mocked(api.getMcpServers).mockResolvedValue([]);
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

    expect(await screen.findByText('该角色类型支持 15 个字段。仅覆盖该角色所需的值。')).toBeDefined();
    // 未选中任何角色时是空闲态，只给一句提示；表单要点了「新建角色」才出现。
    fireEvent.click(screen.getByText('新建角色'));
    expect(screen.getByText('main_doc')).toBeDefined();
    expect(screen.getByText('index_file')).toBeDefined();
    expect(screen.getAllByText('description')).toHaveLength(2);
    expect(container.querySelectorAll('.role-field')).toHaveLength(15);
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


describe('WorkbenchSettings workspace save', () => {
  it('does not touch execution settings when saving the workspace only', async () => {
    vi.mocked(api.updateGlobalConfig).mockResolvedValue({ config: {} as never });
    render(<WorkbenchSettings open onClose={() => {}} onSaved={() => {}} />);
    fireEvent.click(screen.getByText('工作区与执行'));
    await waitFor(() => expect(screen.getByLabelText('最大同时运行节点数')).toBeDefined());

    fireEvent.click(screen.getByText('保存工作区'));

    await waitFor(() => expect(api.updateGlobalConfig).toHaveBeenCalled());
    const payload = vi.mocked(api.updateGlobalConfig).mock.calls[0][0];
    expect(payload).not.toHaveProperty('max_concurrency');
  });
});

describe('WorkbenchSettings 全局最大并发', () => {
  const renderSettings = () =>
    render(<WorkbenchSettings open onClose={() => {}} onSaved={() => {}} />);

  const openWorkspaceTab = async () => {
    fireEvent.click(screen.getByText('工作区与执行'));
    await waitFor(() => expect(screen.getByLabelText('最大同时运行节点数')).toBeDefined());
  };

  const concurrencyInput = () =>
    screen.getByLabelText('最大同时运行节点数') as HTMLInputElement;

  it('renders a global 1–16 input defaulting to 1 when the backend omits the field', async () => {
    renderSettings();
    await openWorkspaceTab();

    const input = concurrencyInput();
    expect(input.min).toBe('1');
    expect(input.max).toBe('16');
    expect(input.value).toBe('1');
  });

  it('displays the persisted global value', async () => {
    vi.mocked(api.getGlobalConfig).mockResolvedValue({
      config: {
        workspace_root: null, cli_name: null, model: null,
        api_base_url: null, api_key_env: null, max_concurrency: 8,
      },
      clis: [],
      default_cli: 'claude',
    });
    renderSettings();
    await openWorkspaceTab();

    expect(concurrencyInput().value).toBe('8');
  });

  it('saves max_concurrency together with the execution settings', async () => {
    vi.mocked(api.updateGlobalConfig).mockResolvedValue({ config: {} as never });
    renderSettings();
    await openWorkspaceTab();

    fireEvent.change(concurrencyInput(), { target: { value: '6' } });
    fireEvent.click(screen.getByText('保存执行配置'));

    await waitFor(() => expect(api.updateGlobalConfig).toHaveBeenCalled());
    expect(vi.mocked(api.updateGlobalConfig).mock.calls[0][0]).toMatchObject({
      max_concurrency: 6,
    });
  });

  it('clamps out-of-range max_concurrency before saving', async () => {
    vi.mocked(api.updateGlobalConfig).mockResolvedValue({ config: {} as never });
    renderSettings();
    await openWorkspaceTab();

    fireEvent.change(concurrencyInput(), { target: { value: '99' } });
    fireEvent.click(screen.getByText('保存执行配置'));

    await waitFor(() => expect(api.updateGlobalConfig).toHaveBeenCalled());
    expect(vi.mocked(api.updateGlobalConfig).mock.calls[0][0]).toMatchObject({
      max_concurrency: 16,
    });
  });

  it('has matching zh/en dictionary keys and concurrency placeholders', () => {
    expect(Object.keys(zh).sort()).toEqual(Object.keys(en).sort());
    for (const key of Object.keys(zh).filter((key) => /Concurrency/.test(key))) {
      expect(zh[key].match(/\{\w+\}/g) || []).toEqual(en[key].match(/\{\w+\}/g) || []);
    }
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

  describe('export / import', () => {
    const preview = (overrides: Partial<RolePoolExportPreview> = {}): RolePoolExportPreview => ({
      pool: { id: 'p1', name: '博客开发', roles: ['code'] },
      roles: [], skills: [], personal_skills: [], mcp: [], sensitive_mcp: [], warnings: [],
      ...overrides,
    } as RolePoolExportPreview);

    const importPlan = (overrides: Partial<RolePoolImportResult> = {}): RolePoolImportResult => ({
      dry_run: true,
      on_conflict: 'skip',
      has_conflict: false,
      pool: { name: '博客开发', roles: ['code'], status: 'new', local_id: null, key: 'pool' },
      roles: [{ id: 'code', type: 'worker', status: 'new', key: 'role:worker/code' }],
      skills: [],
      mcp: [],
      readiness: [],
      warnings: [],
      ...overrides,
    });

    const exportButton = () => headingButton('导出');
    const importButton = () => headingButton('导入');
    const pickFile = () => {
      const input = screen.getByTestId('pool-import-input') as HTMLInputElement;
      fireEvent.change(input, { target: { files: [new File(['zip'], '博客开发.aicu-pool.zip')] } });
    };

    beforeEach(() => {
      vi.mocked(api.getRolePools).mockResolvedValue([existingPool]);
    });

    it('warns about credential keys (never values) before exporting, then downloads', async () => {
      const confirm = vi.fn(() => true);
      vi.stubGlobal('confirm', confirm);
      vi.mocked(api.previewRolePoolExport).mockResolvedValue(
        preview({ sensitive_mcp: [{ name: 'github', keys: ['GITHUB_TOKEN'] }] }),
      );
      vi.mocked(api.exportRolePool).mockResolvedValue(undefined);
      renderSettings();
      await openPoolsTab();
      await screen.findByText('博客开发');

      fireEvent.click(exportButton());

      await waitFor(() => expect(api.exportRolePool).toHaveBeenCalledWith('p1', '博客开发'));
      const prompt = String((confirm.mock.calls[0] as unknown[])[0]);
      expect(prompt).toContain('github');
      expect(prompt).toContain('GITHUB_TOKEN');
      vi.unstubAllGlobals();
    });

    it('also tells the user which skills come from their personal directory', async () => {
      const confirm = vi.fn(() => false);
      vi.stubGlobal('confirm', confirm);
      vi.mocked(api.previewRolePoolExport).mockResolvedValue(preview({ personal_skills: ['mine'] }));
      renderSettings();
      await openPoolsTab();
      await screen.findByText('博客开发');

      fireEvent.click(exportButton());

      await waitFor(() => expect(confirm).toHaveBeenCalled());
      const prompt = String((confirm.mock.calls[0] as unknown[])[0]);
      expect(prompt).toContain('~/.claude/skills');
      expect(prompt).toContain('mine');
      expect(api.exportRolePool).not.toHaveBeenCalled();
      vi.unstubAllGlobals();
    });

    it('does not download when the credential warning is declined', async () => {
      vi.stubGlobal('confirm', vi.fn(() => false));
      vi.mocked(api.previewRolePoolExport).mockResolvedValue(
        preview({ sensitive_mcp: [{ name: 'github', keys: ['GITHUB_TOKEN'] }] }),
      );
      renderSettings();
      await openPoolsTab();
      await screen.findByText('博客开发');

      fireEvent.click(exportButton());

      await waitFor(() => expect(api.previewRolePoolExport).toHaveBeenCalledWith('p1'));
      expect(api.exportRolePool).not.toHaveBeenCalled();
      vi.unstubAllGlobals();
    });

    it('exports straight away when there is nothing to warn about', async () => {
      const confirm = vi.fn(() => true);
      vi.stubGlobal('confirm', confirm);
      vi.mocked(api.previewRolePoolExport).mockResolvedValue(preview());
      vi.mocked(api.exportRolePool).mockResolvedValue(undefined);
      renderSettings();
      await openPoolsTab();
      await screen.findByText('博客开发');

      fireEvent.click(exportButton());

      await waitFor(() => expect(api.exportRolePool).toHaveBeenCalledWith('p1', '博客开发'));
      expect(confirm).not.toHaveBeenCalled();
      vi.unstubAllGlobals();
    });

    it('keeps export disabled until a pool is selected, but import is always available', async () => {
      vi.mocked(api.getRolePools).mockResolvedValue([]);
      renderSettings();
      await openPoolsTab();

      expect(exportButton().disabled).toBe(true);
      expect(importButton().disabled).toBe(false);
    });

    it('previews the picked file with a dry run and only imports after confirming', async () => {
      vi.mocked(api.importRolePool)
        .mockResolvedValueOnce(importPlan())
        .mockResolvedValueOnce(importPlan({
          dry_run: false,
          counts: { created: 2, overwritten: 0, skipped: 0, unchanged: 0 },
        }));
      renderSettings();
      await openPoolsTab();
      await screen.findByText('博客开发');
      const poolFetchesBefore = vi.mocked(api.getRolePools).mock.calls.length;

      pickFile();

      await waitFor(() => expect(api.importRolePool).toHaveBeenCalledTimes(1));
      expect(vi.mocked(api.importRolePool).mock.calls[0][1]).toEqual({ dryRun: true, onConflict: 'skip', decisions: {} });

      fireEvent.click(await screen.findByText('确认导入'));

      await waitFor(() => expect(api.importRolePool).toHaveBeenCalledTimes(2));
      expect(vi.mocked(api.importRolePool).mock.calls[1][1]).toEqual({ dryRun: false, onConflict: 'skip', decisions: {} });
      await waitFor(() =>
        expect(vi.mocked(api.getRolePools).mock.calls.length).toBeGreaterThan(poolFetchesBefore));
      expect(api.getRoles).toHaveBeenCalled();
    });
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
      models: ['claude-opus-5', 'grok-4.6'], supports_mcp: true },
    { id: 'codex', display_name: 'OpenAI Codex CLI', installed: true, priority: 1,
      models: ['gpt-5.6', 'grok-4.6'], supports_mcp: true },
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

describe('WorkbenchSettings CLI 下拉的 MCP 标注', () => {
  // 标注必须从后端下发的 supports_mcp 派生。若改回在前端硬编码名单，将来后端接一个
  // 新 CLI 就会出现「实际已接入、界面却说未接入」的假象 —— 这条测试就是拦这个的。
  const CLIS: CliInfo[] = [
    { id: 'claude', display_name: 'Claude Code', installed: true, priority: 0,
      models: ['claude-sonnet-5'], supports_mcp: true },
    { id: 'codex', display_name: 'OpenAI Codex CLI', installed: true, priority: 1,
      models: ['gpt-5.6'], supports_mcp: true },
    // 未接线 + 未安装：两个标注都要出，且互不覆盖
    { id: 'kimi', display_name: 'Kimi CLI', installed: false, priority: 2,
      models: [], supports_mcp: false },
    // 未接线但已装：只该出 MCP 这一条
    { id: 'pi', display_name: 'Pi CLI', installed: true, priority: 3,
      models: [], supports_mcp: false },
  ];

  const renderSettings = async () => {
    vi.mocked(api.getGlobalConfig).mockResolvedValue({
      config: {
        workspace_root: null, cli_name: null, model: null,
        api_base_url: null, api_key_env: null,
      },
      clis: CLIS,
      provider_presets: [],
      default_cli: 'claude',
    });
    render(<WorkbenchSettings open onClose={() => {}} onSaved={() => {}} />);
    await waitFor(() => expect(api.getGlobalConfig).toHaveBeenCalled());
  };

  const optionLabels = () =>
    Object.fromEntries(
      Array.from(document.querySelectorAll<HTMLOptionElement>('#cli-select option'))
        .filter((option) => option.value)
        .map((option) => [option.value, option.textContent ?? '']),
    );

  it('已接入 MCP 的 CLI 不带任何标注', async () => {
    await renderSettings();
    const labels = optionLabels();
    expect(labels.claude).toBe('Claude Code');
    expect(labels.codex).toBe('OpenAI Codex CLI');
  });

  it('未接入 MCP 的 CLI 标注「MCP 未接入」', async () => {
    await renderSettings();
    const labels = optionLabels();
    expect(labels.pi).toBe('Pi CLI（MCP 未接入）');
  });

  it('未安装与未接入两个标注同时成立且互不覆盖', async () => {
    await renderSettings();
    expect(optionLabels().kimi).toBe('Kimi CLI（未安装）（MCP 未接入）');
  });

  it('标注措辞是「未接入」而不是「不支持」', async () => {
    // 这个标注说的是 AICU 这边没接线，不是在给 CLI 下结论（kimi / pi 的入口取决于
    // 本机装的版本，agy 自己是带 MCP 的）。写「不支持」是关于 CLI 的假陈述，
    // 用户会跑去换 CLI 而不是来问我们能不能接。
    await renderSettings();
    const joined = Object.values(optionLabels()).join('');
    expect(joined).toContain('未接入');
    expect(joined).not.toContain('不支持');
  });
});

describe('WorkbenchSettings Provider 下拉与模型形态', () => {
  const CLIS: CliInfo[] = [
    { id: 'claude', display_name: 'Claude Code', installed: true, priority: 0,
      models: ['claude-opus-5'], supports_mcp: true },
    // deepseek-v4-pro 是「同协议可达但不属于 openai 预置」的模型：预置模式下切到它，
    // 生效地址应当跟着模型厂商走（真实规则在后端，见 TestEffectiveBaseUrl）。
    { id: 'codex', display_name: 'OpenAI Codex CLI', installed: true, priority: 1,
      models: ['gpt-5.6', 'deepseek-v4-pro'], supports_mcp: true },
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

describe('WorkbenchSettings MCP registry', () => {
  const echoServer: McpServerInfo = {
    name: 'echo-probe',
    command: 'python3',
    args: ['tests/mcp_echo_server.py'],
    env: { MCP_TOKEN: 'x' },
    description: '探针',
    required: true,
  };

  const renderSettings = () =>
    render(<WorkbenchSettings open onClose={() => {}} onSaved={() => {}} />);

  /** 顶部导航里的 tab 按钮（正文标题与 tab 同名，直接 getByText 会命中两个）。 */
  const tabButton = (label: string) =>
    Array.from(document.querySelectorAll('.settings-nav button'))
      .find((button) => button.textContent === label) as HTMLButtonElement;

  const openMcpTab = async () => {
    fireEvent.click(tabButton('MCP'));
    await waitFor(() => expect(api.getMcpServers).toHaveBeenCalled());
  };

  /**
   * 某个字段的控件。
   *
   * 限定在 ``.mcp-form label`` 里找，不能按文字全屏找：示例块的行标题与字段同名
   * （「启动程序」「参数」…）—— 那个同名是**故意**的，用户要的就是字段与例子的对应关系，
   * 所以该改的是查法，不是界面。
   */
  const fieldOf = (label: string) =>
    (Array.from(document.querySelectorAll('.mcp-form label')) as HTMLLabelElement[])
      .find((element) => element.textContent?.startsWith(label))
      ?.querySelector('input, textarea') as HTMLInputElement;

  const buttonOf = (label: string) =>
    screen.getByText(label).closest('button') as HTMLButtonElement;

  /** section-heading 右侧的「编辑」/「新建」按钮。 */
  const headingButton = (label: string) =>
    Array.from(document.querySelectorAll('.section-heading-actions button'))
      .find((button) => button.textContent?.includes(label)) as HTMLButtonElement;

  const cliOf = (overrides: Partial<CliInfo>): CliInfo => ({
    id: 'pi', display_name: 'Pi', installed: true, priority: 1, models: [],
    supports_mcp: false, mcp_unwired_reason: null, ...overrides,
  });

  it('lists the registry and prefills the form with what the user actually wrote', async () => {
    vi.mocked(api.getMcpServers).mockResolvedValue([echoServer]);
    renderSettings();
    await openMcpTab();

    expect(await screen.findByText('echo-probe')).toBeDefined();
    expect(fieldOf('名称').value).toBe('echo-probe');
    // 名字是注册表的键、也是角色 yaml 里的引用，改它要同步重写所有引用方 → 不可改
    expect(fieldOf('名称').disabled).toBe(true);
    expect(fieldOf('启动程序').value).toBe('python3');
    // args 存的是**原始写法**（相对路径照原样显示），解析成绝对路径是运行时的事
    expect(fieldOf('参数').value).toBe('tests/mcp_echo_server.py');
    expect(fieldOf('环境变量').value).toBe('MCP_TOKEN=x');
    expect((fieldOf('连不上时让节点启动失败') as unknown as HTMLInputElement).checked).toBe(true);
  });

  it('updates the selected server instead of creating a new one', async () => {
    vi.mocked(api.getMcpServers).mockResolvedValue([echoServer]);
    vi.mocked(api.updateMcpServer).mockResolvedValue(echoServer);
    renderSettings();
    await openMcpTab();
    fireEvent.click(headingButton('编辑'));

    fireEvent.change(fieldOf('说明'), { target: { value: '改过的说明' } });
    fireEvent.click(buttonOf('保存 MCP server'));

    await waitFor(() => expect(api.updateMcpServer).toHaveBeenCalled());
    expect(vi.mocked(api.updateMcpServer).mock.calls[0]).toEqual([
      'echo-probe',
      {
        name: 'echo-probe', command: 'python3', args: ['tests/mcp_echo_server.py'],
        env: { MCP_TOKEN: 'x' }, description: '改过的说明', required: true,
      },
    ]);
    expect(api.createMcpServer).not.toHaveBeenCalled();
  });

  it('creates a new server, splitting args one per line so an arg may contain spaces', async () => {
    vi.mocked(api.createMcpServer).mockResolvedValue({ ...echoServer, name: 'my-probe' });
    renderSettings();
    await openMcpTab();

    expect(await screen.findByText('暂无 MCP server，点击「新建 MCP server」创建。')).toBeDefined();
    // 未选中任何条目时是只读态，保存/删除都不该出现
    expect(screen.queryByText('保存 MCP server')).toBeNull();

    fireEvent.click(headingButton('新建 MCP server'));
    fireEvent.change(fieldOf('名称'), { target: { value: 'my-probe' } });
    fireEvent.change(fieldOf('启动程序'), { target: { value: 'npx' } });
    // 第二行含空格。按空格分会把它劈成两个参数**且不报错**，用户只能从 server 那边
    // 一句莫名其妙的报错反推回来 —— 所以必须一行一个。
    fireEvent.change(fieldOf('参数'), { target: { value: '-y\n--config a b.json\n' } });
    fireEvent.click(buttonOf('保存 MCP server'));

    await waitFor(() => expect(api.createMcpServer).toHaveBeenCalledWith({
      name: 'my-probe', command: 'npx', args: ['-y', '--config a b.json'],
      env: {}, description: '', required: true,
    }));
    expect(api.updateMcpServer).not.toHaveBeenCalled();
  });

  it('refuses an env line without "=" instead of silently dropping it', async () => {
    vi.mocked(api.getMcpServers).mockResolvedValue([echoServer]);
    renderSettings();
    await openMcpTab();
    fireEvent.click(headingButton('编辑'));

    // 后端只会收到解析结果，这一行会被静默丢掉 —— 用户以为存进去了。所以必须在这里拦。
    fireEvent.change(fieldOf('环境变量'), { target: { value: 'MCP_TOKEN=x\nBROKEN' } });
    fireEvent.click(buttonOf('保存 MCP server'));

    expect((await screen.findAllByText('环境变量需写成 KEY=VALUE，变量名以字母或下划线开头')).length)
      .toBeGreaterThan(0);
    expect(api.updateMcpServer).not.toHaveBeenCalled();
  });

  it('names the roles still referencing the server in the delete confirmation', async () => {
    const confirmMock = vi.fn(() => true);
    vi.stubGlobal('confirm', confirmMock);
    vi.mocked(api.getMcpServers)
      .mockResolvedValueOnce([
        { ...echoServer, referencing_roles: [{ id: 'mcp_probe', type: 'worker' }] },
      ])
      .mockResolvedValue([]);
    vi.mocked(api.deleteMcpServer).mockResolvedValue(undefined);
    renderSettings();
    await openMcpTab();
    fireEvent.click(headingButton('编辑'));
    fireEvent.click(buttonOf('删除 MCP server'));

    await waitFor(() => expect(api.deleteMcpServer).toHaveBeenCalledWith('echo-probe'));
    // 删掉之后那些角色下次启动会当场报错 —— 这一行字是用户唯一的判断依据
    expect(String(confirmMock.mock.calls[0][0])).toContain('mcp_probe');
    vi.unstubAllGlobals();
  });

  it('probes the values on screen, not the saved ones, and shows the exposed tools', async () => {
    vi.mocked(api.getMcpServers).mockResolvedValue([echoServer]);
    vi.mocked(api.probeMcpServer).mockResolvedValue(['probe_echo']);
    renderSettings();
    await openMcpTab();
    fireEvent.click(headingButton('编辑'));

    // 改成一个还没保存的值：否则用户每次都得先存下一份自己都不确定对不对的定义，才轮到测
    fireEvent.change(fieldOf('参数'), { target: { value: 'tests/mcp_echo_server.py\n--verbose' } });
    fireEvent.click(buttonOf('测试连接'));

    await waitFor(() => expect(api.probeMcpServer).toHaveBeenCalledWith(
      expect.objectContaining({ args: ['tests/mcp_echo_server.py', '--verbose'] }),
    ));
    expect(await screen.findByText('probe_echo')).toBeDefined();
    expect(api.updateMcpServer).not.toHaveBeenCalled();
  });

  it('shows the backend sentence verbatim when the probe fails', async () => {
    // 这句由后端写好（含命令名、超时、stderr 尾部）。前端重写一份的话，两边就会各自漂移。
    const reason = "MCP server 'echo-probe' 启动失败：找不到命令 'definitely-not-a-command'";
    vi.mocked(api.getMcpServers).mockResolvedValue([echoServer]);
    vi.mocked(api.probeMcpServer).mockRejectedValue(new Error(reason));
    renderSettings();
    await openMcpTab();
    fireEvent.click(headingButton('编辑'));
    fireEvent.click(buttonOf('测试连接'));

    expect((await screen.findAllByText(reason)).length).toBeGreaterThan(0);
  });

  it('lists the installed CLIs whose MCP is not wired, using the backend-provided reason', async () => {
    vi.mocked(api.getGlobalConfig).mockResolvedValue({
      config: { workspace_root: null, cli_name: null, model: null, api_base_url: null, api_key_env: null },
      clis: [
        cliOf({ id: 'pi', display_name: 'Pi', mcp_unwired_reason: '需要先安装 pi-mcp 扩展' }),
        cliOf({ id: 'agy', display_name: 'Agy', installed: false, mcp_unwired_reason: 'AICU 还没接' }),
        cliOf({ id: 'kimi', display_name: 'Kimi', supports_mcp: true, mcp_unwired_reason: null }),
      ],
      default_cli: 'pi',
    });
    renderSettings();
    await openMcpTab();

    // 未安装的 Agy 不列（下拉里根本选不到）；已接入的 Kimi 不列；成因逐字来自后端。
    expect(await screen.findByText(
      '这些 CLI 还没接入 MCP：Pi — 需要先安装 pi-mcp 扩展。用它们启动的节点即使角色声明了 MCP 也用不上。',
    )).toBeDefined();
  });
});

describe('WorkbenchSettings MCP 远程条目', () => {
  const remoteServer: McpServerInfo = {
    name: 'notion',
    // 后端对远程条目也回空值，形状与本地保持一致 —— 前端不该靠「有没有这些键」判传输。
    command: '',
    args: [],
    env: {},
    url: 'https://mcp.example.com/mcp',
    headers: { Authorization: 'Bearer t' },
    description: '远程服务',
    required: true,
  };

  const renderSettings = () =>
    render(<WorkbenchSettings open onClose={() => {}} onSaved={() => {}} />);

  const tabButton = (label: string) =>
    Array.from(document.querySelectorAll('.settings-nav button'))
      .find((button) => button.textContent === label) as HTMLButtonElement;

  const openMcpTab = async () => {
    fireEvent.click(tabButton('MCP'));
    await waitFor(() => expect(api.getMcpServers).toHaveBeenCalled());
  };

  /**
   * 表单里某个字段的 <label>。
   *
   * 必须限定在 ``.mcp-form label`` 上找，不能直接按文字全屏找：示例块的行标题与字段同名
   * （「启动程序」「参数」「地址」「请求头」）—— 那个同名是**故意**的，用户要的就是这个对应
   * 关系，所以撞车的是查法不是界面。同理也不能拿来判断字段在不在。
   */
  const formLabel = (label: string) =>
    (Array.from(document.querySelectorAll('.mcp-form label')) as HTMLLabelElement[])
      .find((element) => element.textContent?.startsWith(label)) as HTMLLabelElement;

  const fieldOf = (label: string) =>
    formLabel(label).querySelector('input, textarea') as HTMLInputElement;

  /** 传输下拉：它是这一组字段里唯一的 select，所以按 label 找得到。 */
  const transportOf = () => formLabel('传输方式').querySelector('select') as HTMLSelectElement;

  const buttonOf = (label: string) =>
    screen.getByText(label).closest('button') as HTMLButtonElement;

  const headingButton = (label: string) =>
    Array.from(document.querySelectorAll('.section-heading-actions button'))
      .find((button) => button.textContent?.includes(label)) as HTMLButtonElement;

  /** 某个字段在不在屏幕上（不在 = 这一组字段被藏起来了）。同样只看 label，不看示例。 */
  const hasField = (label: string) =>
    Array.from(document.querySelectorAll('.mcp-form label'))
      .some((element) => element.textContent?.startsWith(label));

  it('prefills the transport from what was saved rather than from a stored key', async () => {
    vi.mocked(api.getMcpServers).mockResolvedValue([remoteServer]);
    renderSettings();
    await openMcpTab();

    // 传输不另存字段：有没有 url 就是传输本身，与后端 transport_of 同一条判据。
    expect(transportOf().value).toBe('http');
    expect(fieldOf('地址').value).toBe('https://mcp.example.com/mcp');
    expect(fieldOf('请求头').value).toBe('Authorization=Bearer t');
    // 本地那三个框一个都不在 —— 留着旧 command 挨着 URL，用户就会同时提交两样。
    expect(hasField('启动程序')).toBe(false);
    expect(hasField('参数')).toBe(false);
    expect(hasField('环境变量')).toBe(false);
  });

  it('sends only the remote fields once the transport is switched', async () => {
    vi.mocked(api.createMcpServer).mockResolvedValue({ ...remoteServer, name: 'my-remote' });
    renderSettings();
    await openMcpTab();
    fireEvent.click(headingButton('新建 MCP server'));

    fireEvent.change(fieldOf('名称'), { target: { value: 'my-remote' } });
    fireEvent.change(fieldOf('启动程序'), { target: { value: 'python3' } });
    fireEvent.change(transportOf(), { target: { value: 'http' } });

    // 切传输要清掉另一组：不清的话那个 python3 会跟着 URL 一起提交，而后端对
    // 「url 与 command 同时给」是**报错**不是忽略 —— 用户看到的将是一句无从解释的话。
    expect(hasField('启动程序')).toBe(false);
    fireEvent.change(fieldOf('地址'), { target: { value: 'https://example.com/mcp' } });
    fireEvent.change(fieldOf('请求头'), { target: { value: 'X-Api-Key=k\n' } });
    fireEvent.click(buttonOf('保存 MCP server'));

    await waitFor(() => expect(api.createMcpServer).toHaveBeenCalledWith({
      name: 'my-remote',
      url: 'https://example.com/mcp',
      headers: { 'X-Api-Key': 'k' },
      description: '',
      required: true,
    }));
  });

  it('keeps the stdio payload byte-for-byte what it was before', async () => {
    vi.mocked(api.getMcpServers).mockResolvedValue([
      { name: 'echo-probe', command: 'npx', args: ['-y', 'pkg'], env: { A: '1' }, description: 'x', required: false },
    ]);
    vi.mocked(api.updateMcpServer).mockResolvedValue({
      name: 'echo-probe', command: 'npx', args: ['-y', 'pkg'], env: { A: '1' }, description: 'x', required: false,
    });
    renderSettings();
    await openMcpTab();
    fireEvent.click(headingButton('编辑'));
    fireEvent.click(buttonOf('保存 MCP server'));

    // 一次都没碰过传输下拉的条目，提交的仍该是原来那五个键 —— 不许凭空多出 url/headers。
    await waitFor(() => expect(api.updateMcpServer).toHaveBeenCalledWith('echo-probe', {
      name: 'echo-probe', command: 'npx', args: ['-y', 'pkg'], env: { A: '1' },
      description: 'x', required: false,
    }));
  });

  it('refuses a URL that is empty or not http(s)', async () => {
    renderSettings();
    await openMcpTab();
    fireEvent.click(headingButton('新建 MCP server'));
    fireEvent.change(fieldOf('名称'), { target: { value: 'my-remote' } });
    fireEvent.change(transportOf(), { target: { value: 'http' } });

    // 地址空着时按钮就是灰的：这里只判「空没空」，格式对不对留到点下去时说。
    expect(buttonOf('保存 MCP server').disabled).toBe(true);

    fireEvent.change(fieldOf('地址'), { target: { value: 'ftp://example.com/mcp' } });
    fireEvent.click(buttonOf('保存 MCP server'));
    expect((await screen.findAllByText('地址需以 http:// 或 https:// 开头')).length).toBeGreaterThan(0);
    expect(api.createMcpServer).not.toHaveBeenCalled();
  });

  it('refuses a header line without "=" instead of silently dropping it', async () => {
    renderSettings();
    await openMcpTab();
    fireEvent.click(headingButton('新建 MCP server'));
    fireEvent.change(fieldOf('名称'), { target: { value: 'my-remote' } });
    fireEvent.change(transportOf(), { target: { value: 'http' } });
    fireEvent.change(fieldOf('地址'), { target: { value: 'https://example.com/mcp' } });
    fireEvent.change(fieldOf('请求头'), { target: { value: 'Authorization=Bearer t\nBROKEN' } });
    fireEvent.click(buttonOf('保存 MCP server'));

    // 同样的理由：后端只收到解析结果，这一行会被静默丢掉，用户以为凭据带上了。
    expect((await screen.findAllByText('请求头需写成 NAME=VALUE，一行一个')).length).toBeGreaterThan(0);
    expect(api.createMcpServer).not.toHaveBeenCalled();
  });

  it('probes the remote entry through the same button', async () => {
    vi.mocked(api.getMcpServers).mockResolvedValue([remoteServer]);
    vi.mocked(api.probeMcpServer).mockResolvedValue(['search']);
    renderSettings();
    await openMcpTab();
    fireEvent.click(headingButton('编辑'));
    fireEvent.change(fieldOf('地址'), { target: { value: 'https://other.example.com/mcp' } });
    fireEvent.click(buttonOf('测试连接'));

    // 测的是屏幕上这份：改了地址还没保存就该能测，且请求体里只有远程那组字段。
    await waitFor(() => expect(api.probeMcpServer).toHaveBeenCalledWith(
      expect.objectContaining({
        url: 'https://other.example.com/mcp',
        headers: { Authorization: 'Bearer t' },
      }),
    ));
    const payload = vi.mocked(api.probeMcpServer).mock.calls[0][0];
    expect(payload.command).toBeUndefined();
    expect(await screen.findByText('search')).toBeDefined();
  });

  it('spells out that only Streamable HTTP works, so an SSE server is not a mystery', async () => {
    renderSettings();
    await openMcpTab();
    fireEvent.click(headingButton('新建 MCP server'));
    fireEvent.change(transportOf(), { target: { value: 'http' } });

    // 文档里写着 SSE 的远程 server 会硬失败。不说清楚的话，第一个 bug 报告就是
    // 「我的远程 server 不能用」，而答案在别处。
    expect(screen.getByText(/只支持 Streamable HTTP/)).toBeDefined();
    expect(screen.getByText(/SSE/)).toBeDefined();
  });

  it('shows a worked example of both transports, and swaps it with the transport', async () => {
    renderSettings();
    await openMcpTab();
    fireEvent.click(headingButton('新建 MCP server'));

    // 逐字段的占位符只教语法（「启动程序」框里填程序），而用户卡住的是**一整条定义长什么样**。
    // 所以这里守的是：例子给出的是可以直接照抄的一整组值，不是一个字段一个提示。
    expect(screen.getByText('照这样填')).toBeDefined();
    expect(screen.getByText('npx')).toBeDefined();

    // 按 getAllByText 的规矩，查出的是**空白已归一**的元素，所以这里用空格连写查。
    // 换行本身另测（见下面那两行）：能不能看见「一行一个」靠的是值里的换行加上
    // CSS 的 white-space: pre-line，归一之后查得到只说明三个词按序都在。
    const args = screen.getByText('-y @modelcontextprotocol/server-filesystem /要开放的目录');
    expect(args.tagName).toBe('CODE');
    expect(args.textContent?.split('\n')).toEqual([
      '-y', '@modelcontextprotocol/server-filesystem', '/要开放的目录',
    ]);

    fireEvent.change(transportOf(), { target: { value: 'http' } });

    expect(screen.getByText('https://mcp.example.com/mcp')).toBeDefined();
    expect(screen.getByText('Authorization=Bearer 你的令牌')).toBeDefined();
    // 上一种传输的例子必须跟着消失，否则用户会照着 npx 那组值去填远程表单。
    expect(screen.queryByText('npx')).toBeNull();
  });

  it('keeps the example out of the read-only view, where the real values are already shown', async () => {
    vi.mocked(api.getMcpServers).mockResolvedValue([remoteServer]);
    renderSettings();
    await openMcpTab();

    expect(screen.queryByText('照这样填')).toBeNull();
  });
});

describe('WorkbenchSettings MCP loading isolation', () => {
  it('keeps the other settings usable when the MCP registry is unavailable', async () => {
    vi.mocked(api.getMcpServers).mockRejectedValue(new Error('registry is invalid'));
    render(<WorkbenchSettings open onClose={() => {}} onSaved={() => {}} />);

    await waitFor(() => expect(within(document.querySelector('.settings-nav') as HTMLElement).getByText('连接')).toBeDefined());
    fireEvent.click(within(document.querySelector('.settings-nav') as HTMLElement).getByText('MCP'));
    expect(await screen.findByText('registry is invalid')).toBeDefined();
    expect(screen.getByText('新建 MCP server')).toBeDefined();

    fireEvent.click(within(document.querySelector('.settings-nav') as HTMLElement).getByText('连接'));
    expect(document.querySelector('.settings-section h3')?.textContent).toBe('连接');
  });
});

describe('WorkbenchSettings 角色侧的 MCP 选择器', () => {
  /**
   * 角色字段的可见范围由「default.yaml 的键 ∪ 同类角色用过的键」决定，所以这份 schema
   * 就代表「mcp 已经进了 default.yaml」之后的样子。
   */
  const mcpFields: RoleField[] = [
    { name: 'mcp', label: 'mcp', type: 'list', default: [], has_default: true },
  ];

  const mcpServers: McpServerInfo[] = [
    { name: 'notion', command: '', args: [], env: {}, url: 'https://mcp.example.com/mcp', description: '知识库检索', required: true },
    { name: 'github', command: 'npx', args: ['-y', 'pkg'], env: {}, description: '仓库操作', required: true },
  ];

  const renderRoles = async (servers: McpServerInfo[] = mcpServers) => {
    vi.mocked(api.getRoleSchema).mockResolvedValue({ role_type: 'manager', defaults: {}, fields: mcpFields });
    vi.mocked(api.getMcpServers).mockResolvedValue(servers);
    render(<WorkbenchSettings open onClose={() => {}} onSaved={() => {}} />);
    await waitFor(() => expect(api.getMcpServers).toHaveBeenCalled());
    // 角色 tab 默认加载的是 worker 的 schema，manager 那份要切过去才会用到。
    fireEvent.click(screen.getByText('角色'));
    fireEvent.click(await screen.findByText('新建角色'));
  };

  it('renders a picker instead of a textarea once mcp is a known field', async () => {
    await renderRoles();

    // 从前这里是个三行文本框，手打名字、打错不拦。有下拉才谈得上「能挂上」。
    expect(document.querySelector('.role-field .skill-select')).not.toBeNull();
    expect(document.querySelector('.role-field textarea')).toBeNull();
    expect(screen.getByText('未挂载任何 MCP server，可直接勾选')).toBeDefined();
  });

  it('lists servers, filters by description, and shows the picked one as a chip', async () => {
    await renderRoles();
    fireEvent.click(document.querySelector('.skill-select-trigger') as HTMLButtonElement);

    expect(screen.getByText('notion')).toBeDefined();
    expect(screen.getByText('github')).toBeDefined();

    // 描述不上屏（摆进行里会把菜单撑变形），但要能当搜索关键字 —— 用户记得的是「能干嘛」。
    fireEvent.change(document.querySelector('.skill-select-search input') as HTMLInputElement, {
      target: { value: '知识库' },
    });
    expect(screen.queryByText('github')).toBeNull();

    fireEvent.click(document.querySelector('.skill-select-item input') as HTMLInputElement);
    expect(document.querySelector('.skill-select-chip')?.textContent).toBe('notion');
  });

  it('says so when the registry is empty rather than showing a blank menu', async () => {
    await renderRoles([]);
    fireEvent.click(document.querySelector('.skill-select-trigger') as HTMLButtonElement);

    expect(screen.getByText('无匹配 MCP server')).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// 设置 → 工作区：把「当前实际用的目录」显出来（只读），但绝不填进输入框
//
// 背景：输入框绑的是 `config.workspace_root`，即「新建会话的全局根」，留空 = 不覆盖。
// 用户希望在设置里看到当前目录，但把当前会话的路径**填进那个框**是危险的：
// 顺手一保存，null 就变成 runtime_sessions/<某个会话id>/workspace，此后所有新会话
// 共用一个目录（共享区互相覆盖）。而且未设置时每个会话各用各的，也没有「一个」
// 当前目录可填。所以做成只读的一行。
// ---------------------------------------------------------------------------
describe('WorkbenchSettings 工作区：当前生效目录', () => {
  const renderSettings = () =>
    render(<WorkbenchSettings open onClose={() => {}} onSaved={() => {}} />);

  const openWorkspaceTab = async () => {
    fireEvent.click(screen.getByText('工作区与执行'));
    await waitFor(() => expect(screen.getByLabelText('最大同时运行节点数')).toBeDefined());
  };

  const effectiveHint = () =>
    Array.from(document.querySelectorAll('.settings-section small'))
      .map((el) => el.textContent || '')
      .find((text) => text.includes('当前会话实际使用')) || null;

  /** 工作区根输入框。按 placeholder 找 —— 它没写 type 属性，`input[type=text]` 匹配不到。 */
  const workspaceInput = () =>
    document.querySelector(
      '.settings-section input[placeholder="运行时管理工作区（默认）"]',
    ) as HTMLInputElement;

  function setActiveSession(workspaceRoot: string | undefined) {
    useSessionStore.setState({
      sessions: workspaceRoot
        ? ([{ id: 's1', title: '会话一', workspace_root: workspaceRoot }] as never)
        : ([] as never),
      activeSessionId: workspaceRoot ? 's1' : null,
    });
  }

  afterEach(() => {
    useSessionStore.setState({ sessions: [], activeSessionId: null });
  });

  it('未设置全局根时，显示当前会话实际使用的目录', async () => {
    setActiveSession('/repo/runtime_sessions/s1/workspace');
    renderSettings();
    await openWorkspaceTab();

    expect(effectiveHint()).toContain('/repo/runtime_sessions/s1/workspace');
    // 输入框仍然留空（留空 = 不覆盖），两者是分开的两件事
    expect(workspaceInput().value).toBe('');
  });

  it('有选中会话时，输入框仍是空的 —— 生效值只读展示，不冒充配置值', async () => {
    setActiveSession('/repo/runtime_sessions/s1/workspace');
    renderSettings();
    await openWorkspaceTab();

    expect(workspaceInput().value).toBe('');
    expect(workspaceInput().placeholder).toBe('运行时管理工作区（默认）');
  });

  it('保存时不会把只读的生效路径写成全局根', async () => {
    // 这条是整件事的关键：看一眼当前目录，不能顺手改掉全局配置。
    vi.mocked(api.updateGlobalConfig).mockResolvedValue({ config: {} as never });
    setActiveSession('/repo/runtime_sessions/s1/workspace');
    renderSettings();
    await openWorkspaceTab();

    fireEvent.click(screen.getByText('保存工作区'));

    await waitFor(() => expect(api.updateGlobalConfig).toHaveBeenCalled());
    expect(vi.mocked(api.updateGlobalConfig).mock.calls[0][0]).toMatchObject({
      workspace_root: null,
    });
  });

  it('会话实际目录与全局根不同（全局根改过之后的老会话）时，显示的是会话那个', async () => {
    vi.mocked(api.getGlobalConfig).mockResolvedValue({
      config: {
        workspace_root: '/new/global/root',
        cli_name: null, model: null, api_base_url: null, api_key_env: null,
      },
      clis: [],
      default_cli: 'claude',
    });
    setActiveSession('/old/session/workspace');
    renderSettings();
    await openWorkspaceTab();

    expect(workspaceInput().value).toBe('/new/global/root');
    expect(effectiveHint()).toContain('/old/session/workspace');
  });

  it('没有选中会话时不显示这一行', async () => {
    setActiveSession(undefined);
    renderSettings();
    await openWorkspaceTab();

    expect(effectiveHint()).toBeNull();
  });
});
