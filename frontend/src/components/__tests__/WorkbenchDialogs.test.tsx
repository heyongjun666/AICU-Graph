// @vitest-environment jsdom

import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nProvider } from '../../i18n';
import { NewNodeDialog } from '../WorkbenchDialogs';
import * as api from '../../utils/api';
import { RoleInfo } from '../../types';

vi.mock('../../utils/api', () => ({
  getGlobalConfig: vi.fn(),
  getRole: vi.fn(),
  getRoles: vi.fn(),
  getSkills: vi.fn(),
}));

const roles: RoleInfo[] = [
  { id: 'worker-role', display_name: 'Worker role', type: 'worker' },
  { id: 'manager-role', display_name: 'Manager role', type: 'manager' },
];

const skill = { id: 'lint', name: 'Lint', description: 'Run lint' };

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
};

const deferred = <T,>(): Deferred<T> => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
};

const roleDetail = (skills?: string[]) => ({
  id: 'role',
  display_name: 'Role',
  type: 'worker' as const,
  effective_config: skills === undefined ? {} : { skills },
});

const renderDialog = (onSubmit = vi.fn(), onClose = vi.fn()) => render(
  <I18nProvider>
    <NewNodeDialog
      open
      nodes={[]}
      candidates={[]}
      busy={false}
      onClose={onClose}
      onSubmit={onSubmit}
    />
  </I18nProvider>,
);

const roleSelect = () => screen.getByText('角色').closest('label')?.querySelector('select') as HTMLSelectElement;
const typeSelect = () => screen.getByText('类型').closest('label')?.querySelector('select') as HTMLSelectElement;
const skillCheckbox = () => screen.getByText('Lint').closest('label')?.querySelector('input') as HTMLInputElement;
const submitButton = () => screen.getAllByText('添加节点').find((element) => element.closest('button'))?.closest('button') as HTMLButtonElement;

beforeEach(() => {
  vi.mocked(api.getGlobalConfig).mockResolvedValue({
    config: { workspace_root: null, cli_name: null, model: null, api_base_url: null, api_key_env: null },
    clis: [],
    default_cli: null,
  });
  vi.mocked(api.getRoles).mockResolvedValue(roles);
  vi.mocked(api.getSkills).mockResolvedValue([skill]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('NewNodeDialog role skills loading', () => {
  it('echoes effective_config.skills and submits an explicit empty skills list', async () => {
    const request = deferred<ReturnType<typeof roleDetail>>();
    vi.mocked(api.getRole).mockReturnValue(request.promise);
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    renderDialog(onSubmit);

    request.resolve(roleDetail([]));
    await waitFor(() => expect(skillCheckbox()).toBeDefined());
    expect(skillCheckbox().checked).toBe(false);

    fireEvent.change(screen.getByPlaceholderText('该节点应产出什么？'), { target: { value: 'Do work' } });
    fireEvent.click(submitButton());

    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(onSubmit.mock.calls[0][0].config.skills).toEqual([]);
  });

  it('shows selected skills from effective_config and clears them when the new role returns []', async () => {
    const first = deferred<ReturnType<typeof roleDetail>>();
    const second = deferred<ReturnType<typeof roleDetail>>();
    vi.mocked(api.getRole)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    renderDialog();

    first.resolve(roleDetail(['lint']));
    await waitFor(() => expect(skillCheckbox().checked).toBe(true));

    await waitFor(() => expect(roleSelect()).toBeDefined());
    fireEvent.change(roleSelect(), { target: { value: 'worker-role' } });
    second.resolve(roleDetail([]));
    await waitFor(() => expect(skillCheckbox().checked).toBe(false));
  });

  it('unlocks the form after a role config request fails', async () => {
    const pending = deferred<ReturnType<typeof roleDetail>>();
    vi.mocked(api.getRole).mockReturnValue(pending.promise);
    renderDialog();
    fireEvent.change(screen.getByPlaceholderText('该节点应产出什么？'), { target: { value: 'Do work' } });

    expect(submitButton().disabled).toBe(true);
    pending.reject(new Error('role unavailable'));

    await waitFor(() => expect(submitButton().disabled).toBe(false));
    expect(screen.getByText('role unavailable')).toBeDefined();
  });

  it('does not remain locked after closing while a request is pending and reopening', async () => {
    const first = deferred<ReturnType<typeof roleDetail>>();
    const second = deferred<ReturnType<typeof roleDetail>>();
    vi.mocked(api.getRole)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const onClose = vi.fn();
    const view = renderDialog(vi.fn(), onClose);
    fireEvent.change(screen.getByPlaceholderText('该节点应产出什么？'), { target: { value: 'Do work' } });
    fireEvent.click(screen.getByTitle('关闭'));
    expect(onClose).toHaveBeenCalled();

    view.rerender(
      <I18nProvider>
        <NewNodeDialog open={false} nodes={[]} candidates={[]} busy={false} onClose={onClose} onSubmit={vi.fn()} />
      </I18nProvider>,
    );
    view.rerender(
      <I18nProvider>
        <NewNodeDialog open nodes={[]} candidates={[]} busy={false} onClose={onClose} onSubmit={vi.fn()} />
      </I18nProvider>,
    );

    second.resolve(roleDetail([]));
    fireEvent.change(screen.getByPlaceholderText('该节点应产出什么？'), { target: { value: 'Do work again' } });
    await waitFor(() => expect(submitButton().disabled).toBe(false));
    first.resolve(roleDetail(['lint']));
    await waitFor(() => expect(skillCheckbox().checked).toBe(false));
  });

  it('does not let an old role response overwrite a newer role response', async () => {
    const oldRequest = deferred<ReturnType<typeof roleDetail>>();
    const newRequest = deferred<ReturnType<typeof roleDetail>>();
    vi.mocked(api.getRole)
      .mockReturnValueOnce(oldRequest.promise)
      .mockReturnValueOnce(newRequest.promise);
    renderDialog();

    await waitFor(() => expect(roleSelect()).toBeDefined());
    fireEvent.change(roleSelect(), { target: { value: 'worker-role' } });
    newRequest.resolve(roleDetail(['lint']));
    await waitFor(() => expect(skillCheckbox().checked).toBe(true));

    oldRequest.resolve(roleDetail([]));
    await waitFor(() => expect(skillCheckbox().checked).toBe(true));
  });

  it('resets loading when the role is temporarily empty and when the type changes', async () => {
    const initial = deferred<ReturnType<typeof roleDetail>>();
    const manager = deferred<ReturnType<typeof roleDetail>>();
    vi.mocked(api.getRole)
      .mockReturnValueOnce(initial.promise)
      .mockReturnValueOnce(manager.promise);
    renderDialog();
    await waitFor(() => expect(roleSelect()).toBeDefined());
    fireEvent.change(roleSelect(), {
      target: { value: '' },
    });
    await waitFor(() => expect(skillCheckbox().disabled).toBe(false));
    // An empty role still prevents submission even though loading has ended.
    expect(submitButton().disabled).toBe(true);

    fireEvent.change(typeSelect(), { target: { value: 'manager' } });
    fireEvent.change(screen.getByPlaceholderText('该节点应产出什么？'), { target: { value: 'Do manager work' } });
    expect(submitButton().disabled).toBe(true);
    manager.resolve(roleDetail([]));
    await waitFor(() => expect(submitButton().disabled).toBe(false));
    initial.resolve(roleDetail(['lint']));
  });
});
