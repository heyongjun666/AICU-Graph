// @vitest-environment jsdom
// ============================================================================
// UIStateStore.test.ts — 界面语言的同步缓存（消除刷新时「中文一闪而过」）
// ============================================================================

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const LANGUAGE_KEY = 'graph-workbench.language';

/**
 * 重新加载 store 模块，模拟「刷新页面」——模块级 initialState 会重新求值，
 * 正如浏览器重新执行整个 bundle。
 */
async function freshStore() {
  vi.resetModules();
  const mod = await import('../UIStateStore');
  return mod.useUIStateStore;
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('UIStateStore 界面语言', () => {
  it('无缓存时默认中文', async () => {
    const store = await freshStore();
    expect(store.getState().language).toBe('zh');
  });

  it('首帧即命中缓存，英文用户刷新不会先闪中文', async () => {
    localStorage.setItem(LANGUAGE_KEY, 'en');
    const store = await freshStore();
    // 关键：这是初始状态本身，而不是等异步 /v1/config 回来后才变成 en
    expect(store.getState().language).toBe('en');
  });

  it('setLanguage 同步写入缓存（下次刷新才有的读）', async () => {
    const store = await freshStore();

    store.getState().setLanguage('en');
    expect(store.getState().language).toBe('en');
    expect(localStorage.getItem(LANGUAGE_KEY)).toBe('en');

    // 切回中文同样要落盘，否则下次刷新会误判成英文
    store.getState().setLanguage('zh');
    expect(localStorage.getItem(LANGUAGE_KEY)).toBe('zh');
  });

  it('缓存值非法时回退中文', async () => {
    localStorage.setItem(LANGUAGE_KEY, 'fr');
    const store = await freshStore();
    expect(store.getState().language).toBe('zh');
  });

  it('localStorage 不可用时回退中文，且切换本身不抛错', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage disabled');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage disabled');
    });

    const store = await freshStore();
    expect(store.getState().language).toBe('zh');
    expect(() => store.getState().setLanguage('en')).not.toThrow();
    expect(store.getState().language).toBe('en');
  });
});
