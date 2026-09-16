// @vitest-environment jsdom
// ============================================================================
// SearchCheckboxPicker —— 带搜索的复选清单
//
// 这里钉的是三件曾经出过问题的事：
//   1. 勾选框与文字是 label 内的**同级**元素。CSS 上 label 一旦被外面那条
//      `.workbench-dialog label { flex-direction: column }` 带成竖排，勾选框就会跑到
//      文字上面——jsdom 不计算布局，所以只能断言 DOM 结构，视觉回归由 pickerStyles.test.ts
//      对着 CSS 源文本兜。
//   2. 悬浮要能看到完整信息（detail / description 在行内是放不下的）。
//   3. 卡片的开合与详情的懒加载都带延迟计时器，所以这些用例必须用假定时器推进时间，
//      不能靠 fireEvent 之后立刻断言。
// ============================================================================

import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import SearchCheckboxPicker, { SearchCheckboxItem } from '../SearchCheckboxPicker';
import type { HoverCardDetail } from '../ItemHoverCard';

const items: SearchCheckboxItem[] = [
  { id: 'code', label: '代码编写', detail: 'worker', badge: 'worker', description: '负责按方案落地实现' },
  { id: 'test', label: '测试验证', detail: 'worker', badge: 'worker', description: '负责回归与边界校验' },
];

const renderPicker = (overrides: Partial<React.ComponentProps<typeof SearchCheckboxPicker>> = {}) =>
  render(
    <SearchCheckboxPicker
      items={items}
      selected={['code']}
      onToggle={() => {}}
      query=""
      onQueryChange={() => {}}
      searchPlaceholder="搜索角色"
      emptyLabel="没有可选项"
      selectedLabel="已选 {count} 个"
      {...overrides}
    />,
  );

/**
 * 某一个候选项的 label 元素。
 * 必须限定在 .dependency-picker 里找：悬浮卡片 portal 到 body 之后也含同样的文案，
 * 用 screen.getByText 会一次命中两个。
 */
const rows = () => Array.from(document.querySelectorAll('.dependency-picker label'));

const rowOf = (label: string) =>
  rows().find((row) => row.querySelector('.picker-item-label')?.textContent === label) as HTMLLabelElement;

const hoverCard = () => screen.queryByTestId('item-hover-card');

/** 推进计时器并冲掉随之而来的 promise。 */
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

const CARD_TEXT = '.item-hover-card-body-text';

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); cleanup(); });

describe('SearchCheckboxPicker 结构', () => {
  it('keeps the checkbox and the label text as siblings inside one row', () => {
    renderPicker();
    const row = rowOf('代码编写');

    const checkbox = row.querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(checkbox).not.toBeNull();
    expect(checkbox.checked).toBe(true);

    // 同级：不要为了横排而把文字包进 checkbox 里面，也不要额外套一层
    expect(checkbox.parentElement).toBe(row);
    const text = row.querySelector('.picker-item-label') as HTMLElement;
    expect(text.parentElement).toBe(row);
    expect(text.textContent).toBe('代码编写');

    // 行内次要信息也在同一行
    expect((row.querySelector('small') as HTMLElement).textContent).toBe('worker');
    expect(rows()).toHaveLength(2);
  });

  it('renders every row with the same three-part shape', () => {
    renderPicker();

    expect(rows()).toHaveLength(2);
    rows().forEach((row) => {
      expect(row.children[0].tagName).toBe('INPUT');
      expect(row.children[1].className).toBe('picker-item-label');
      expect(row.children[2].tagName).toBe('SMALL');
    });
  });

  it('omits the inline detail when an item has none', () => {
    renderPicker({ items: [{ id: 'plain', label: '无详情' }] });
    expect(rowOf('无详情').querySelector('small')).toBeNull();
  });

  it('toggles through the callback rather than holding its own state', () => {
    const onToggle = vi.fn();
    renderPicker({ onToggle });

    fireEvent.click(rowOf('测试验证').querySelector('input') as HTMLInputElement);
    expect(onToggle).toHaveBeenCalledWith('test', true);
  });
});

describe('SearchCheckboxPicker 悬浮卡片', () => {
  it('shows the full information on hover, including the description', () => {
    renderPicker();
    expect(hoverCard()).toBeNull();

    fireEvent.pointerEnter(rowOf('代码编写'));

    const card = hoverCard() as HTMLElement;
    expect(card).not.toBeNull();
    expect(card.textContent).toContain('代码编写');
    expect(card.textContent).toContain('worker');
    expect(card.textContent).toContain('code');
    // 描述只在卡片里出现，行内不该有
    expect(card.textContent).toContain('负责按方案落地实现');
    expect(rowOf('代码编写').textContent).not.toContain('负责按方案落地实现');
  });

  it('puts a long description in its own block, never in the title row', () => {
    // 曾经的 bug：长文本被当成标题行的标签（flex-shrink: 0），不换行地冲出卡片被裁掉，
    // 用户看到的正是一个「什么都没说清的黑框」。长文本必须走正文块。
    const long = '第一行：做一个可运行的全栈博客系统。\n第二行：支持注册登录、发文章、打标签、评论、搜索。\n第三行：前后端分离，能跑起来。';
    renderPicker({
      items: [{ id: 'n1', label: 'default 61edb9b0', detail: long, description: long }],
    });

    fireEvent.pointerEnter(rowOf('default 61edb9b0'));

    const card = hoverCard() as HTMLElement;
    // 标题行只有标题：detail 是行内用的，不该被当成卡片标签
    const titleRow = card.querySelector('.item-hover-card-title') as HTMLElement;
    expect(titleRow.textContent).toBe('default 61edb9b0');
    expect(titleRow.querySelector('small')).toBeNull();

    // 原文一字不少，换行也照原样保留
    const desc = card.querySelector(CARD_TEXT) as HTMLElement;
    expect(desc.textContent).toBe(long);
    expect(desc.textContent?.split('\n')).toHaveLength(3);
  });

  it('renders a short badge as a pill beside the title', () => {
    renderPicker({ items: [{ id: 'dev', label: 'Dev Lead', badge: 'Manager' }] });
    fireEvent.pointerEnter(rowOf('Dev Lead'));

    const badge = (hoverCard() as HTMLElement).querySelector('.item-hover-card-title small');
    expect(badge?.textContent).toBe('Manager');
  });

  it('does not repeat the id when it is the same as the title', () => {
    renderPicker({ items: [{ id: 'canvas-design', label: 'canvas-design', description: '画图' }] });
    fireEvent.pointerEnter(rowOf('canvas-design'));

    expect((hoverCard() as HTMLElement).querySelector('.item-hover-card-id')).toBeNull();
  });

  it('shows the id on its own line when it differs from the title', () => {
    renderPicker({ items: [{ id: 'dev_lead', label: 'Dev Lead' }] });
    fireEvent.pointerEnter(rowOf('Dev Lead'));

    expect((hoverCard() as HTMLElement).querySelector('.item-hover-card-id')?.textContent)
      .toBe('dev_lead');
  });

  it('renders the card outside the scroll container so it cannot be clipped', () => {
    const { container } = renderPicker();
    fireEvent.pointerEnter(rowOf('代码编写'));

    // portal 到 body：留在 .dependency-picker 里会被它的 max-height + overflow 裁掉
    const card = hoverCard() as HTMLElement;
    expect(container.querySelector('[data-testid="item-hover-card"]')).toBeNull();
    expect(card.closest('.dependency-picker')).toBeNull();
    expect(card.parentElement).toBe(document.body);
    // position: fixed 由 .item-hover-card 规则给出（见 pickerStyles.test.ts），
    // 这里只确认坐标是直接写死的视口坐标，不参与滚动容器布局
    expect(card.style.left).not.toBe('');
    expect(card.style.top).not.toBe('');
  });

  it('hides the card on pointer leave', async () => {
    renderPicker();
    const row = rowOf('代码编写');

    fireEvent.pointerEnter(row);
    expect(hoverCard()).not.toBeNull();

    // 不是立刻收起：行与卡片之间隔着 10px，指针横穿时要留出进到卡片里的时间
    fireEvent.pointerLeave(row);
    expect(hoverCard()).not.toBeNull();

    await advance(300);
    expect(hoverCard()).toBeNull();
  });

  it('keeps the card while the pointer crosses the gap into it', async () => {
    renderPicker();
    const row = rowOf('代码编写');

    fireEvent.pointerEnter(row);
    fireEvent.pointerLeave(row);
    // 指针进到卡片里（用户想滚正文）
    fireEvent.pointerEnter(hoverCard() as HTMLElement);
    await advance(300);
    expect(hoverCard()).not.toBeNull();

    fireEvent.pointerLeave(hoverCard() as HTMLElement);
    await advance(300);
    expect(hoverCard()).toBeNull();
  });

  it('moves the card to whichever row is hovered', () => {
    renderPicker();

    fireEvent.pointerEnter(rowOf('代码编写'));
    expect((hoverCard() as HTMLElement).textContent).toContain('负责按方案落地实现');

    // 划到下一行时，上一行那个待关闭的计时器要被取消，否则卡片会在半路消失
    fireEvent.pointerLeave(rowOf('代码编写'));
    fireEvent.pointerEnter(rowOf('测试验证'));
    expect((hoverCard() as HTMLElement).textContent).toContain('负责回归与边界校验');
  });

  it('drops the card when the query changes, since the anchor is gone', () => {
    const { rerender } = renderPicker();
    fireEvent.pointerEnter(rowOf('代码编写'));
    expect(hoverCard()).not.toBeNull();

    rerender(
      <SearchCheckboxPicker
        items={[{ id: 'test', label: '测试验证', detail: 'worker', badge: 'worker', description: '负责回归与边界校验' }]}
        selected={[]}
        onToggle={() => {}}
        query="测试"
        onQueryChange={() => {}}
        searchPlaceholder="搜索角色"
        emptyLabel="没有可选项"
        selectedLabel="已选 {count} 个"
      />,
    );

    expect(hoverCard()).toBeNull();
  });

  it('re-anchors on scroll instead of dropping the card', () => {
    const { container } = renderPicker();
    fireEvent.pointerEnter(rowOf('代码编写'));

    // 曾经是 onScroll 直接收起。但 scroll 事件是异步派发的，晚于滚动到位后新触发的
    // pointerenter，于是会把刚打开的卡片一起清掉 —— 指针停在清单上用触控板滚动时卡片一闪就没
    // （在浏览器里实测复现过）。现在改成重算坐标，只有行滚出容器才收起。
    // jsdom 没有布局引擎，行与容器的 rect 都是 0，所以这里只能验证"卡片还在"，
    // 几何正确性由浏览器验证脚本兜。
    fireEvent.scroll(container.querySelector('.dependency-picker') as HTMLElement);
    expect(hoverCard()).not.toBeNull();
  });

  it('drops the card once its row scrolls out of the list', () => {
    const { container } = renderPicker();
    const row = rowOf('代码编写');
    const list = container.querySelector('.dependency-picker') as HTMLElement;
    // jsdom 不计算布局，手工造出"行被滚到容器上方"的几何
    const rectOf = (top: number, bottom: number): DOMRect =>
      ({ top, bottom, left: 0, right: 10, width: 10, height: bottom - top, x: 0, y: top, toJSON: () => ({}) }) as DOMRect;
    row.getBoundingClientRect = () => rectOf(-60, -40);
    list.getBoundingClientRect = () => rectOf(0, 150);

    fireEvent.pointerEnter(row);
    expect(hoverCard()).not.toBeNull();

    fireEvent.scroll(list);
    expect(hoverCard()).toBeNull();
  });
});

describe('SearchCheckboxPicker 详情懒加载', () => {
  const loaderReturning = (detail: HoverCardDetail) => vi.fn(() => Promise.resolve(detail));

  it('waits for the pointer to rest before fetching', async () => {
    const detailLoader = loaderReturning({ summary: '高级软件工程师' });
    renderPicker({ items: [{ id: 'code', label: '代码编写', detailLoader }] });

    fireEvent.pointerEnter(rowOf('代码编写'));
    // 卡片要等详情，所以这一刻还没有；详情请求也要等指针停住才发
    expect(hoverCard()).toBeNull();
    expect(detailLoader).not.toHaveBeenCalled();

    await advance(300);
    expect(detailLoader).toHaveBeenCalledTimes(1);
    expect((hoverCard() as HTMLElement).textContent).toContain('高级软件工程师');
  });

  it('does not fetch for rows the pointer only sweeps across', async () => {
    const first = loaderReturning({ summary: 'A 的详情' });
    const second = loaderReturning({ summary: 'B 的详情' });
    renderPicker({
      items: [
        { id: 'a', label: '甲', detailLoader: first },
        { id: 'b', label: '乙', detailLoader: second },
      ],
    });

    fireEvent.pointerEnter(rowOf('甲'));
    await advance(50);
    fireEvent.pointerLeave(rowOf('甲'));
    fireEvent.pointerEnter(rowOf('乙'));
    await advance(300);

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
    expect((hoverCard() as HTMLElement).textContent).toContain('B 的详情');
  });

  it('discards a response that lands after the pointer moved to another row', async () => {
    let resolveFirst: (detail: HoverCardDetail) => void = () => {};
    const first = vi.fn(() => new Promise<HoverCardDetail>((resolve) => { resolveFirst = resolve; }));
    renderPicker({
      items: [
        { id: 'a', label: '甲', detailLoader: first },
        { id: 'b', label: '乙' },
      ],
    });

    fireEvent.pointerEnter(rowOf('甲'));
    await advance(200);            // 请求已发出但还没回来
    fireEvent.pointerLeave(rowOf('甲'));
    fireEvent.pointerEnter(rowOf('乙'));
    await act(async () => { resolveFirst({ summary: '甲的人格' }); });

    // 甲的结果不能挂到乙的卡片上
    expect((hoverCard() as HTMLElement).textContent).toContain('乙');
    expect((hoverCard() as HTMLElement).textContent).not.toContain('甲的人格');
  });

  it('shows the card once and complete, not a small one that then grows', async () => {
    // 曾经的 bug：卡片挂载时详情还没到，于是先弹出一个只有标题的小卡片，
    // 200ms 后被详情撑成大卡片 —— 用户看到的是"先弹一次小的、又弹一次大的"。
    const detailLoader = loaderReturning({ body: '你是高级软件工程师', bodyLabel: '角色人格' });
    renderPicker({ items: [{ id: 'code', label: '代码编写', description: '首行摘要', detailLoader }] });

    fireEvent.pointerEnter(rowOf('代码编写'));
    expect(hoverCard()).toBeNull();

    await advance(300);
    const card = hoverCard() as HTMLElement;
    expect(card.querySelector(CARD_TEXT)?.textContent).toBe('你是高级软件工程师');
    expect(card.querySelector('.item-hover-card-body-label')?.textContent).toBe('角色人格');
  });

  it('falls back to the list description when the detail request fails', async () => {
    // 失败也要出卡片：退回列表里已有的描述，而不是让悬停毫无反应
    const detailLoader = vi.fn(() => Promise.reject(new Error('boom')));
    renderPicker({ items: [{ id: 'code', label: '代码编写', description: '首行摘要', detailLoader }] });

    fireEvent.pointerEnter(rowOf('代码编写'));
    await advance(300);

    const card = hoverCard() as HTMLElement;
    expect(card.querySelector(CARD_TEXT)?.textContent).toBe('首行摘要');
    // 详情没到手，就没有"角色人格"这种只有详情才有的小标题
    expect(card.querySelector('.item-hover-card-body-label')).toBeNull();
  });

  it('renders the loaded groups, facts and persona text', async () => {
    const detailLoader = loaderReturning({
      summary: '高级软件工程师',
      groups: [
        { label: '适合', items: ['代码实现', '性能优化'], tone: 'positive' },
        { label: '不适合', items: ['视觉设计'], tone: 'negative' },
      ],
      facts: [{ label: '并行 CC', value: '2' }],
      body: '你是高级软件工程师……',
      bodyLabel: '角色人格',
    });
    renderPicker({ items: [{ id: 'code', label: '代码编写', badge: 'Worker', detailLoader }] });

    fireEvent.pointerEnter(rowOf('代码编写'));
    await advance(300);

    const card = hoverCard() as HTMLElement;
    expect(card.querySelector('.item-hover-card-summary')?.textContent).toBe('高级软件工程师');
    expect(Array.from(card.querySelectorAll('.item-hover-card-group li')).map((li) => li.textContent))
      .toEqual(['代码实现', '性能优化', '视觉设计']);
    expect(card.querySelector('.is-positive .item-hover-card-group-label')?.textContent).toBe('适合');
    expect(card.querySelector('.is-negative .item-hover-card-group-label')?.textContent).toBe('不适合');
    expect(card.querySelector('.item-hover-card-facts')?.textContent).toBe('并行 CC2');
    expect(card.querySelector('.item-hover-card-body-label')?.textContent).toBe('角色人格');
    expect(card.querySelector(CARD_TEXT)?.textContent).toBe('你是高级软件工程师……');
  });

  it('falls back to the title alone when the detail request fails', async () => {
    // 接口失败不该冒泡成未处理的 rejection，也不该为一个悬浮卡片弹错误提示
    const detailLoader = vi.fn(() => Promise.reject(new Error('boom')));
    renderPicker({ items: [{ id: 'code', label: '代码编写', detailLoader }] });

    fireEvent.pointerEnter(rowOf('代码编写'));
    await advance(300);

    const card = hoverCard() as HTMLElement;
    expect(card).not.toBeNull();
    // 只剩标题和 id，没有半截详情
    expect(card.textContent).toBe('代码编写code');
    expect(card.querySelector(CARD_TEXT)).toBeNull();
  });

  it('does not fire timers after unmount', async () => {
    const detailLoader = loaderReturning({ summary: '不该出现' });
    const { unmount } = renderPicker({ items: [{ id: 'code', label: '代码编写', detailLoader }] });

    fireEvent.pointerEnter(rowOf('代码编写'));
    unmount();
    await advance(300);

    expect(detailLoader).not.toHaveBeenCalled();
  });
});

describe('SearchCheckboxPicker 搜索与禁用', () => {
  it('filters on the description too, so hover-only text stays reachable', () => {
    renderPicker({ query: '回归' });

    expect(rowOf('测试验证')).toBeDefined();
    expect(rowOf('代码编写')).toBeUndefined();
  });

  it('shows the caller-supplied empty label when nothing matches', () => {
    renderPicker({ query: '不存在的角色' });
    expect(screen.getByText('没有可选项')).toBeDefined();
  });

  it('locks the whole picker when disabled', () => {
    const { container } = renderPicker({ disabled: true });
    const controls = Array.from(
      container.querySelectorAll<HTMLInputElement>('input'),
    );
    expect(controls.length).toBeGreaterThan(0);
    expect(controls.every((input) => input.disabled)).toBe(true);
  });
});
