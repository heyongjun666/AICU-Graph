// ============================================================================
// workbench.css 里 picker 区块的契约断言
//
// 为什么要有这么一个"读源文件"的测试：条目的勾选框曾经被排到文字上面（用户看到的就是
// "勾选框在文本上面、文本信息在下面"），根因是 CSS 的**属性级联**——
// `.inspector-editor label, .workbench-dialog label` 声明了 `flex-direction: column`，
// 而 `.dependency-picker label` 只声明了 `display: flex`，没写 `flex-direction`，
// 于是后者盖不掉前者的 column。同一条规则带进来的 `font-weight: 650` 也一样漏了重置。
//
// jsdom 不计算布局，常规组件测试永远抓不到这种回归，只能对着 CSS 源文本钉住这几条声明。
// 代价是这个测试认字符串而不是认渲染结果，但它测的正是曾经真实出问题的那一行。
// ============================================================================

import { readFileSync } from 'fs';
import { resolve } from 'path';
import { describe, expect, it } from 'vitest';

const css = readFileSync(resolve(__dirname, '../../workbench.css'), 'utf8');

/** 去掉注释后再匹配，避免注释正文里的花括号干扰规则体切分。 */
const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, '');

const escapeSelector = (selector: string) => selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** 取出某条规则（按选择器精确匹配）的声明体；找不到返回 null。 */
const ruleBodyOf = (selector: string): string | null => {
  const pattern = new RegExp(`(?:^|[},])\\s*${escapeSelector(selector)}\\s*\\{([^}]*)\\}`, 'm');
  const match = withoutComments.match(pattern);
  return match ? match[1] : null;
};

/** 把声明体拆成 "属性: 值" 数组，便于按属性名断言。 */
const declarationsOf = (selector: string): Record<string, string> => {
  const body = ruleBodyOf(selector);
  if (!body) return {};
  return Object.fromEntries(
    body
      .split(';')
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const index = line.indexOf(':');
        return [line.slice(0, index).trim(), line.slice(index + 1).trim()];
      }),
  );
};

describe('workbench.css 选择器契约', () => {
  it('still has the column declaration that caused the stacking bug', () => {
    // 前提校验：本文件的其余断言是在"有这么一条 column 规则"的前提下才有意义的。
    // 哪天它被删了，这条会先失败，提示后来人重新审视下面的显式声明是否还需要。
    const body = ruleBodyOf('.inspector-editor label, .workbench-dialog label');
    expect(body).not.toBeNull();
    expect(body).toContain('flex-direction: column');
  });

  it('pins the picker row to a horizontal layout with normal font weight', () => {
    const declarations = declarationsOf('.dependency-picker label');

    // 这两条都是拿来对抗上面那条 column / 650 的，缺一条就会退回竖排或粗体
    expect(declarations['flex-direction']).toBe('row');
    expect(declarations['font-weight']).toBe('400');
    expect(declarations['display']).toBe('flex');
    expect(declarations['align-items']).toBe('center');
  });

  it('resets what the generic dialog input rule does to the mode radios', () => {
    // 同一个根因的另一处：`.workbench-dialog label` 的 column 会把单选框推到文字上面，
    // `.workbench-dialog input` 的 width:100% + 内边距 + 描边又把它撑成横贯一行的大白框。
    expect(ruleBodyOf('.inspector-editor label, .workbench-dialog label'))
      .toContain('flex-direction: column');
    expect(ruleBodyOf('.workbench-dialog input, .workbench-dialog textarea, .workbench-dialog select, .inspector-editor input, .inspector-editor textarea, .inspector-editor select'))
      .toContain('width: 100%');

    // 横排必须写在带 .workbench-dialog 的那条规则里：单个类权重不够，写着也没用。
    expect(declarationsOf('.workbench-dialog .candidate-pool-mode')['flex-direction']).toBe('row');

    const declarations = declarationsOf('.candidate-pool-mode input');
    expect(declarations['width']).toBe('auto');
    expect(declarations['flex']).toBe('0 0 auto');
    expect(declarations['padding']).toBe('0');
    expect(declarations['border']).toBe('0');
  });

  it('separates adjacent candidates with a rule instead of spacing them apart', () => {
    const declarations = declarationsOf('.dependency-picker label + label');

    // 相邻兄弟选择器：不用给每项加 border-bottom，也就没有最后一项多一条线的问题
    expect(declarations['border-top']).toContain('1px solid');
    expect(declarations['padding-top']).toBeTruthy();
  });

  it('keeps the inline label truncatable and the detail pinned right', () => {
    expect(declarationsOf('.dependency-picker .picker-item-label')['text-overflow']).toBe('ellipsis');
    expect(declarationsOf('.dependency-picker small')['margin-left']).toBe('auto');
  });

  it('floats the hover card above two layers of scroll containers', () => {
    const declarations = declarationsOf('.item-hover-card');

    // portal 到 body 之后再靠 fixed 定位；用 absolute 会被 .workbench-dialog 的
    // overflow: auto 按滚动容器而不是视口定位。
    expect(declarations['position']).toBe('fixed');
    // 必须接收指针事件：卡片正文要能用滚轮滚。防抖动不靠这里（那会让正文滚不动），
    // 而是靠 picker 里的关闭延迟 —— 见 SearchCheckboxPicker.test.tsx。
    expect(declarations['pointer-events']).toBe('auto');
    expect(Number(declarations['z-index'])).toBeGreaterThan(100);
  });

  it('caps the card height so a long persona cannot run off the viewport', () => {
    const declarations = declarationsOf('.item-hover-card');

    // 角色的 role_md 有三四十行，不封顶卡片会从视口上下一路探出去。
    expect(declarations['max-height']).toBeTruthy();
    // 纵向 flex 才能让正文那层拿到剩余高度并自己滚
    expect(declarations['display']).toBe('flex');
    expect(declarations['flex-direction']).toBe('column');
  });

  it('scrolls the persona text instead of overflowing the card', () => {
    const body = declarationsOf('.item-hover-card-body-text');

    expect(body['overflow']).toBe('auto');
    expect(body['max-height']).toBeTruthy();
    // 滚到底之后不要把背后的对话框一起滚走
    expect(body['overscroll-behavior']).toBe('contain');

    // 卡片自己被视口卡住时，正文要能跟着缩，而不是顶着 max-height 从底部溢出
    expect(declarationsOf('.item-hover-card-body')['min-height']).toBe('0');
    expect(body['min-height']).toBe('0');
    expect(body['flex']).toContain('1');
  });

  it('styles the hover card as a light panel like the rest of the app', () => {
    const declarations = declarationsOf('.item-hover-card');

    // 曾经照搬 .help-tip-pop 的深色气泡（#2b3338），结果是一块和全站都不搭的黑框。
    // 面板类元素在本文件里一律是白底 + #e4e9eb 描边，卡片必须跟这套走。
    expect(declarations['background']).toBe('#fff');
    expect(declarations['border']).toContain('#e4e9eb');
    expect(declarations['color']).toBe('#505b62');
    expect(declarations.background).not.toContain('#2b3338');
  });

  it('wraps and preserves the original text instead of clipping it', () => {
    const body = declarationsOf('.item-hover-card-body-text');

    // 长文本曾经作为标题行里 flex-shrink: 0 的标签，不换行地冲出卡片被裁掉。
    // pre-wrap 同时保证：会换行 + 任务原文里的换行照原样呈现。
    expect(body['white-space']).toBe('pre-wrap');
    expect(body['overflow-wrap']).toBe('anywhere');

    // 标题行的标签必须是可收缩的短标签，不能反过来把卡片撑破
    const badge = declarationsOf('.item-hover-card-title small');
    expect(badge['flex']).toBe('0 0 auto');
    expect(declarationsOf('.item-hover-card-title')['overflow-wrap']).toBe('anywhere');
  });

  it('lays the section heading buttons out in a row', () => {
    const declarations = declarationsOf('.section-heading-actions');
    expect(declarations['display']).toBe('flex');
    expect(declarations['flex-shrink']).toBe('0');
  });
});
