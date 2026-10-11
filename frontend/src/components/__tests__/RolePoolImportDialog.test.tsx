// @vitest-environment jsdom
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { RolePoolImportDialog, describeBundleWarning } from '../RolePoolImportDialog';
import * as api from '../../utils/api';
import { RolePoolImportResult } from '../../types';
import { zh } from '../../i18n/zh';

vi.mock('../../utils/api', () => ({
  importRolePool: vi.fn(),
}));

const file = new File(['zip'], '博客开发.aicu-pool.zip');

const plan = (overrides: Partial<RolePoolImportResult> = {}): RolePoolImportResult => ({
  dry_run: true,
  on_conflict: 'skip',
  has_conflict: false,
  pool: { name: '博客开发', roles: ['code', 'test'], status: 'new', local_id: null, key: 'pool' },
  roles: [{ id: 'code', type: 'worker', status: 'new', key: 'role:worker/code' }],
  skills: [{ id: 'pdf', status: 'new', key: 'skill:pdf', file_count: 3, script_count: 0 }],
  mcp: [],
  readiness: [],
  warnings: [],
  ...overrides,
});

const mcpItem = {
  name: 'github',
  status: 'new' as const,
  key: 'mcp:github',
  transport: 'stdio',
  command: 'npx',
  args: ['-y', '@modelcontextprotocol/server-github'],
  url: '',
  sensitive_keys: ['GITHUB_TOKEN'],
};

beforeEach(() => {
  vi.mocked(api.importRolePool).mockReset();
});
afterEach(cleanup);

const renderDialog = (props: Partial<React.ComponentProps<typeof RolePoolImportDialog>> = {}) => {
  const onClose = vi.fn();
  const onImported = vi.fn();
  render(<RolePoolImportDialog file={file} onClose={onClose} onImported={onImported} {...props} />);
  return { onClose, onImported };
};

describe('RolePoolImportDialog', () => {
  it('renders nothing and does not call the API without a file', () => {
    renderDialog({ file: null });

    expect(document.querySelector('.pool-bundle-dialog')).toBeNull();
    expect(api.importRolePool).not.toHaveBeenCalled();
  });

  it('dry-runs on open and lists the pool, roles and skills without writing anything', async () => {
    vi.mocked(api.importRolePool).mockResolvedValue(plan());
    renderDialog();

    expect(await screen.findByText('pdf')).toBeDefined();
    expect(screen.getByText('code')).toBeDefined();
    expect(vi.mocked(api.importRolePool)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.importRolePool).mock.calls[0]).toEqual([file, { dryRun: true, onConflict: 'skip', decisions: {} }]);
  });

  it('shows the MCP command that will run and the credential key names', async () => {
    vi.mocked(api.importRolePool).mockResolvedValue(plan({ mcp: [mcpItem] }));
    renderDialog();

    const command = await screen.findByText('npx -y @modelcontextprotocol/server-github');
    expect(command.tagName).toBe('CODE');
    expect(screen.getByText(/GITHUB_TOKEN/)).toBeDefined();
  });

  it('shows a remote MCP url instead of a command', async () => {
    vi.mocked(api.importRolePool).mockResolvedValue(plan({
      mcp: [{ ...mcpItem, command: '', args: [], url: 'https://mcp.example.com', sensitive_keys: [] }],
    }));
    renderDialog();

    expect(await screen.findByText(/https:\/\/mcp\.example\.com/)).toBeDefined();
  });

  it('only offers skip / overwrite when something conflicts, and re-previews on change', async () => {
    vi.mocked(api.importRolePool).mockResolvedValue(plan({
      has_conflict: true,
      roles: [{ id: 'code', type: 'worker', status: 'conflict', key: 'role:worker/code' }],
    }));
    renderDialog();

    const overwrite = await screen.findByLabelText(zh['poolBundle.policy.overwrite']);
    expect((screen.getByLabelText(zh['poolBundle.policy.skip']) as HTMLInputElement).checked).toBe(true);

    fireEvent.click(overwrite);

    await waitFor(() => expect(api.importRolePool).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.importRolePool).mock.calls[1][1]).toEqual({ dryRun: true, onConflict: 'overwrite', decisions: {} });
  });

  describe('per-item decisions and context', () => {
    const conflictPlan = (policy: 'skip' | 'overwrite' = 'skip') => plan({
      has_conflict: true,
      roles: [{ id: 'code', type: 'worker', status: 'conflict', key: 'role:worker/code', used_by: ['池A', '池B'], policy }],
      skills: [{
        id: 'pdf', status: 'conflict', key: 'skill:pdf', file_count: 3, script_count: 2, used_by: ['code'], policy: 'skip',
      }],
      pool: {
        name: '博客开发', roles: ['code'], status: 'conflict', local_id: 'p1', key: 'pool', local_roles: ['old'], policy: 'skip',
      },
    });

    it('lets one conflicting item be overwritten while the rest stay skipped', async () => {
      vi.mocked(api.importRolePool).mockResolvedValue(conflictPlan());
      renderDialog();

      const select = await screen.findByLabelText(zh['poolBundle.rowPolicy'].replace('{name}', 'code'));
      fireEvent.change(select, { target: { value: 'overwrite' } });

      await waitFor(() => expect(api.importRolePool).toHaveBeenCalledTimes(2));
      expect(vi.mocked(api.importRolePool).mock.calls[1][1]).toEqual({
        dryRun: true, onConflict: 'skip', decisions: { 'role:worker/code': 'overwrite' },
      });
    });

    it('sends the decisions with the real import and puts the overwrite count on the button', async () => {
      vi.mocked(api.importRolePool)
        .mockResolvedValueOnce(conflictPlan())
        .mockResolvedValueOnce(conflictPlan('overwrite'))
        .mockResolvedValueOnce(plan({ dry_run: false, counts: { created: 0, overwritten: 1, skipped: 2, unchanged: 0 } }));
      renderDialog();

      fireEvent.change(await screen.findByLabelText(zh['poolBundle.rowPolicy'].replace('{name}', 'code')), {
        target: { value: 'overwrite' },
      });
      fireEvent.click(await screen.findByText(zh['poolBundle.confirmOverwrite'].replace('{count}', '1')));

      await waitFor(() => expect(api.importRolePool).toHaveBeenCalledTimes(3));
      expect(vi.mocked(api.importRolePool).mock.calls[2][1]).toEqual({
        dryRun: false, onConflict: 'skip', decisions: { 'role:worker/code': 'overwrite' },
      });
    });

    it('drops the per-item overrides when the global default changes', async () => {
      vi.mocked(api.importRolePool).mockResolvedValue(conflictPlan());
      renderDialog();
      fireEvent.change(await screen.findByLabelText(zh['poolBundle.rowPolicy'].replace('{name}', 'code')), {
        target: { value: 'overwrite' },
      });
      await waitFor(() => expect(api.importRolePool).toHaveBeenCalledTimes(2));

      fireEvent.click(screen.getByLabelText(zh['poolBundle.policy.overwrite']));

      await waitFor(() => expect(api.importRolePool).toHaveBeenCalledTimes(3));
      expect(vi.mocked(api.importRolePool).mock.calls[2][1]).toEqual({
        dryRun: true, onConflict: 'overwrite', decisions: {},
      });
    });

    it('shows what else on this machine uses a conflicting item, and script counts', async () => {
      vi.mocked(api.importRolePool).mockResolvedValue(conflictPlan());
      renderDialog();

      expect(await screen.findByText(zh['poolBundle.usedByPools'].replace('{names}', '池A, 池B'))).toBeDefined();
      expect(screen.getByText(zh['poolBundle.usedByRoles'].replace('{names}', 'code'))).toBeDefined();
      expect(screen.getByText(zh['poolBundle.localPoolRoles'].replace('{names}', 'old'))).toBeDefined();
      expect(screen.getByText(/含 2 个脚本/)).toBeDefined();
    });

    it('always shows the trust notice before importing', async () => {
      vi.mocked(api.importRolePool).mockResolvedValue(plan());
      renderDialog();
      expect(await screen.findByText(zh['poolBundle.trustNotice'])).toBeDefined();
    });
  });

  it('hides the policy choice when nothing conflicts', async () => {
    vi.mocked(api.importRolePool).mockResolvedValue(plan());
    renderDialog();
    await screen.findByText('pdf');

    expect(screen.queryByText(zh['poolBundle.policy.skip'])).toBeNull();
  });

  it('lists the roles that still need attention with the reason', async () => {
    vi.mocked(api.importRolePool).mockResolvedValue(plan({
      readiness: [
        { id: 'code', type: 'worker', ready: true, issues: [] },
        { id: 'test', type: 'worker', ready: false, issues: [{ kind: 'skill_missing', ref: 'pdf', detail: '' }] },
      ],
    }));
    renderDialog();

    expect(await screen.findByText(zh['poolBundle.needsAttention'])).toBeDefined();
    expect(screen.getByText(zh['poolBundle.readySummary'].replace('{ready}', '1').replace('{attention}', '1'))).toBeDefined();
  });

  it('imports for real on confirm, then shows the result and reports it', async () => {
    const done = plan({
      dry_run: false,
      counts: { created: 3, overwritten: 0, skipped: 0, unchanged: 0 },
      mcp: [mcpItem],
    });
    vi.mocked(api.importRolePool).mockResolvedValueOnce(plan()).mockResolvedValueOnce(done);
    const { onImported } = renderDialog();

    fireEvent.click(await screen.findByText(zh['poolBundle.confirm']));

    await waitFor(() => expect(onImported).toHaveBeenCalledWith(done));
    expect(vi.mocked(api.importRolePool).mock.calls[1]).toEqual([file, { dryRun: false, onConflict: 'skip', decisions: {} }]);
    expect(screen.getByText(zh['poolBundle.resultTitle'])).toBeDefined();
    expect(screen.queryByText(/确认前不会写入/)).toBeNull();
    expect(screen.queryByText(zh['poolBundle.confirm'])).toBeNull();
  });

  it('disables confirm until the preview has loaded', async () => {
    let resolve: (value: RolePoolImportResult) => void = () => {};
    vi.mocked(api.importRolePool).mockReturnValue(new Promise((r) => { resolve = r; }));
    renderDialog();

    const button = screen.getByText(zh['poolBundle.confirm']).closest('button') as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    resolve(plan());
    await waitFor(() => expect(button.disabled).toBe(false));
  });

  it('shows the backend error and keeps confirm disabled when the preview fails', async () => {
    vi.mocked(api.importRolePool).mockRejectedValue(new Error('manifest 缺失'));
    renderDialog();

    expect(await screen.findByText('manifest 缺失')).toBeDefined();
    expect((screen.getByText(zh['poolBundle.confirm']).closest('button') as HTMLButtonElement).disabled).toBe(true);
  });

  it('keeps the dialog open and surfaces the error when the real import fails', async () => {
    vi.mocked(api.importRolePool).mockResolvedValueOnce(plan()).mockRejectedValueOnce(new Error('磁盘已满'));
    const { onImported } = renderDialog();

    fireEvent.click(await screen.findByText(zh['poolBundle.confirm']));

    expect(await screen.findByText('磁盘已满')).toBeDefined();
    expect(onImported).not.toHaveBeenCalled();
    expect(screen.getByText(zh['poolBundle.confirm'])).toBeDefined();
  });

  it('closes via the backdrop and the cancel button', async () => {
    vi.mocked(api.importRolePool).mockResolvedValue(plan());
    const { onClose } = renderDialog();
    await screen.findByText('pdf');

    fireEvent.click(screen.getByText(zh['common.cancel']));
    fireEvent.mouseDown(document.querySelector('.dialog-backdrop') as HTMLElement);

    expect(onClose).toHaveBeenCalledTimes(2);
  });
});

describe('describeBundleWarning', () => {
  const t = (key: string, params?: Record<string, string | number>) => {
    const template = (zh as Record<string, string>)[key];
    if (!template) return key;
    return template.replace(/\{(\w+)\}/g, (_, name) => String(params?.[name] ?? ''));
  };

  it('uses the localized text for a known kind', () => {
    expect(describeBundleWarning(t, { kind: 'missing_skill', ref: 'pdf', role: 'code' }))
      .toBe(t('poolBundle.warn.missing_skill', { ref: 'pdf', role: 'code' }));
  });

  it('falls back to kind and ref for a kind from a newer export', () => {
    const text = describeBundleWarning(t, { kind: 'brand_new_kind', ref: 'x', role: '' });

    expect(text).toContain('brand_new_kind');
    expect(text).toContain('x');
  });
});
