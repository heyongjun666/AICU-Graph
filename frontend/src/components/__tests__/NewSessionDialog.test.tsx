// @vitest-environment jsdom

import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nProvider } from '../../i18n';
import { NewSessionDialog } from '../WorkbenchDialogs';
import * as api from '../../utils/api';

vi.mock('../../utils/api', () => ({
  getRolePools: vi.fn(),
  getRole: vi.fn(),
  getRoles: vi.fn(),
  getSkills: vi.fn(),
}));

const pool = { id: 'p1', name: '全栈开发角色池', roles: ['code', 'test'] };

const renderDialog = (onSubmit = vi.fn().mockResolvedValue(undefined)) => {
  render(
    <I18nProvider>
      <NewSessionDialog open busy={false} onClose={vi.fn()} onSubmit={onSubmit} />
    </I18nProvider>,
  );
  return onSubmit;
};

const poolSelect = () => screen.getByText('候选角色池').closest('label')?.querySelector('select') as HTMLSelectElement;
const nameInput = () => screen.getByPlaceholderText(/./) as HTMLInputElement;
const submit = () => fireEvent.click(screen.getByText('创建会话').closest('button') as HTMLButtonElement);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('NewSessionDialog 角色池选择', () => {
  it('不选池 → 不带候选角色提交（Manager 看到全部角色）', async () => {
    vi.mocked(api.getRolePools).mockResolvedValue([pool]);
    const onSubmit = renderDialog();
    await waitFor(() => expect(poolSelect()).toBeDefined());

    fireEvent.change(nameInput(), { target: { value: '博客' } });
    submit();

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('博客', undefined));
  });

  it('选中池 → 提交该池的角色 id 快照', async () => {
    vi.mocked(api.getRolePools).mockResolvedValue([pool]);
    const onSubmit = renderDialog();
    await waitFor(() => expect(poolSelect()).toBeDefined());

    fireEvent.change(poolSelect(), { target: { value: 'p1' } });
    fireEvent.change(nameInput(), { target: { value: '博客' } });
    submit();

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('博客', ['code', 'test']));
  });

  it('池列表拉取失败 → 不显示选择器，但仍能建会话', async () => {
    vi.mocked(api.getRolePools).mockRejectedValue(new Error('offline'));
    const onSubmit = renderDialog();
    await waitFor(() => expect(api.getRolePools).toHaveBeenCalled());

    expect(screen.queryByText('候选角色池')).toBeNull();
    fireEvent.change(nameInput(), { target: { value: '博客' } });
    submit();

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('博客', undefined));
  });
});
