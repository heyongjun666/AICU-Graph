// @vitest-environment jsdom
// ============================================================================
// languageFlash.test.tsx — 刷新页面时首帧语言必须直接命中，不能先渲染中文再翻
// ============================================================================

import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';

const LANGUAGE_KEY = 'graph-workbench.language';

/** 与 zh/en 字典里取值不同的键，用于区分当前实际渲染语言。 */
const PROBE_KEY = 'settings.tab.workspace';
const ZH_TEXT = '工作区与执行';
const EN_TEXT = 'Workspace & Execution';

/**
 * 模拟浏览器重新执行整个 bundle：重置模块注册表后重新导入 i18n 与 store，
 * 使模块级 initialState 重新求值。
 */
async function bootApp() {
  vi.resetModules();
  const { I18nProvider, useI18n } = await import('..');

  const Probe = () => {
    const { t } = useI18n();
    return <span>{t(PROBE_KEY)}</span>;
  };

  return render(
    <I18nProvider>
      <Probe />
    </I18nProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('刷新页面时的首帧语言', () => {
  it('缓存为英文时，首帧即英文（不再先闪一下中文）', async () => {
    localStorage.setItem(LANGUAGE_KEY, 'en');

    const { container } = await bootApp();

    // 注意：这里没有任何 await / waitFor —— 断言的就是「第一次渲染」
    expect(container.textContent).toBe(EN_TEXT);
  });

  it('缓存为中文时，首帧即中文', async () => {
    localStorage.setItem(LANGUAGE_KEY, 'zh');

    const { container } = await bootApp();

    expect(container.textContent).toBe(ZH_TEXT);
  });

  it('无缓存时回退中文（首次访问不受影响）', async () => {
    const { container } = await bootApp();

    expect(container.textContent).toBe(ZH_TEXT);
  });
});
