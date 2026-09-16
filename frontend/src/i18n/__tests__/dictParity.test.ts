// ============================================================================
// dictParity.test.ts — zh / en 两份字典的 key 必须完全对齐
// ============================================================================
//
// 为什么需要这条：`t(key)` 的签名是 `t(key: string)`（见 i18n/index.tsx:22），
// 查不到就用另一语言兜底、再查不到就把 key 原文吐出来。也就是说**漏写一个 key
// 既不会编译报错、也不会抛异常**，只会在界面上静静显示成 `settings.foo` 这种
// 占位串 —— 而 `npm run build` 的 `tsc` 关抓不到它（tsconfig.json 还把
// src/**/__tests__ 排除在类型检查外）。
//
// 人眼维护 550+ 条、两边成对增删，迟早会漂。这条用例把「漂了」变成红灯。

import { describe, expect, it } from 'vitest';
import { en } from '../en';
import { zh } from '../zh';

const keysOf = (dict: Record<string, string>) => Object.keys(dict).sort();

describe('i18n 字典对齐', () => {
  it('zh 与 en 的 key 集合完全一致', () => {
    const zhKeys = keysOf(zh);
    const enKeys = keysOf(en);

    const onlyZh = zhKeys.filter((key) => !(key in en));
    const onlyEn = enKeys.filter((key) => !(key in zh));

    expect({ onlyZh, onlyEn }).toEqual({ onlyZh: [], onlyEn: [] });
    expect(zhKeys).toHaveLength(enKeys.length);
  });

  it('没有空文案', () => {
    const blank = [...Object.entries(zh), ...Object.entries(en)]
      .filter(([, value]) => !String(value).trim())
      .map(([key]) => key);

    expect(blank).toEqual([]);
  });

  it('占位符两边一致（少一个 {n} 会让插值静默失效）', () => {
    // 只比占位符集合，不比文案本身 —— 中英文句式不同是正常的，
    // 但 `{model}` 这种插值槽两边必须都能填进去。
    const holesOf = (value: string) => (value.match(/\{(\w+)\}/g) || []).sort();
    const mismatched = Object.keys(zh)
      .filter((key) => key in en)
      .filter((key) => holesOf(zh[key]).join() !== holesOf(en[key]).join());

    expect(mismatched).toEqual([]);
  });
});
