import React, { useCallback, useEffect, useRef, useState } from 'react';
import ItemHoverCard, { HoverCardContent, HoverCardDetail } from './ItemHoverCard';

export interface SearchCheckboxItem {
  id: string;
  label: string;
  /** 行内右侧的次要信息，会被省略号截断（角色类型、节点任务、skill id 等）。 */
  detail?: string;
  /**
   * 悬浮卡片标题旁的短标签，只放短词（角色类型这类）。
   * 长文本放 description 或 detailLoader —— 塞进标题行会被挤成跑出卡片边界的一条，什么都看不清。
   */
  badge?: string;
  /**
   * 悬浮卡片里的完整原文，保留原始换行、不截断。
   * 列表接口拿不到全部信息时（角色的 YAML 配置）用 detailLoader 懒加载，这里当兜底。
   */
  description?: string;
  /**
   * 悬停时才去取完整详情的可选钩子。
   *
   * 放在条目上而不是 picker 的 props 上：同一次渲染里可能混着不同类型的条目
   * （设置页的角色池就是 Manager 与 Worker 混排），类型由调用方在 .map() 里闭包捕获最省事。
   * 没有这个钩子时，卡片只展示已有的 description，不发任何请求。
   */
  detailLoader?: () => Promise<HoverCardDetail>;
}

interface SearchCheckboxPickerProps {
  items: SearchCheckboxItem[];
  selected: string[];
  onToggle: (id: string, checked: boolean) => void;
  query: string;
  onQueryChange: (query: string) => void;
  searchPlaceholder: string;
  emptyLabel: string;
  selectedLabel: string;
  disabled?: boolean;
  className?: string;
}

/** 指针在条目上停住多久才去取详情。鼠标扫过一整列时不该每一行都发一个请求。 */
const DETAIL_LOAD_DELAY = 160;
/** 行与卡片之间隔着 10px，指针横穿时会先离开行；留一段宽限让它能进到卡片里滚动正文。 */
const CARD_CLOSE_DELAY = 140;

/**
 * 带搜索的复选清单。纯展示组件，选中态由调用方持有。
 *
 * 消费方：节点依赖选择（WorkbenchDialogs）、会话候选角色池选择（CandidateRolesDialog）、
 * 设置页角色池编辑（WorkbenchSettings）。
 */
const SearchCheckboxPicker: React.FC<SearchCheckboxPickerProps> = ({
  items,
  selected,
  onToggle,
  query,
  onQueryChange,
  searchPlaceholder,
  emptyLabel,
  selectedLabel,
  disabled = false,
  className = '',
}) => {
  // 悬浮卡片的锚点。存 rect 而不是元素，卡片 portal 出去后不需要再回查 DOM。
  const [hovered, setHovered] = useState<{ rect: DOMRect; item: SearchCheckboxItem } | null>(null);
  // 懒加载到的详情。
  const [detail, setDetail] = useState<HoverCardDetail | null>(null);
  /**
   * 详情是否已经有结果（成功或失败）。为 false 时不挂载卡片。
   *
   * 卡片一旦挂载就会立刻显示，而详情是异步来的：先出一个只有标题的小卡片、再被详情撑大，
   * 用户看到的是"先弹一次小的、又弹一次大的"。宁可晚 ~200ms，只弹一次。
   * 请求失败也算有结果 —— 退回只有标题的卡片，总比悬停上去什么都没有强。
   */
  const [detailSettled, setDetailSettled] = useState(false);

  const closeTimer = useRef<number | null>(null);
  const loadTimer = useRef<number | null>(null);
  /**
   * 每次开合都自增，请求回来时对不上就丢弃。
   * 指针从 A 划到 B 时 A 的响应可能后到，没有这个就会把 A 的人格挂到 B 的卡片上。
   */
  const token = useRef(0);
  /**
   * 当前卡片挂在哪一行上，以及所在滚动容器。滚动时要重算坐标（见 onScroll）。
   */
  const anchorRow = useRef<HTMLElement | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const clearTimers = useCallback(() => {
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    if (loadTimer.current !== null) window.clearTimeout(loadTimer.current);
    closeTimer.current = null;
    loadTimer.current = null;
  }, []);

  const dismiss = useCallback(() => {
    clearTimers();
    token.current += 1;
    anchorRow.current = null;
    setHovered(null);
    setDetail(null);
  }, [clearTimers]);

  // 卸载时清计时器：否则组件没了，回调还会 setState。
  useEffect(() => clearTimers, [clearTimers]);

  // 过滤后原锚点已失效，直接收起，避免卡片停在错误的位置。
  // 只依赖 query：调用方都是内联 .map() 生成 items，数组每次渲染都是新身份，
  // 把它放进依赖会让卡片刚出现就被清掉。（dismiss 只碰 ref 和 setState，身份是稳定的。）
  useEffect(() => { dismiss(); }, [query, dismiss]);

  const openCard = (row: HTMLElement, item: SearchCheckboxItem) => {
    clearTimers();
    token.current += 1;
    const current = token.current;
    anchorRow.current = row;
    setHovered({ rect: row.getBoundingClientRect(), item });
    setDetail(null);
    // 没有懒加载钩子的条目（节点依赖、skill）手上就有全部信息，不必等。
    setDetailSettled(!item.detailLoader);
    if (!item.detailLoader) return;

    loadTimer.current = window.setTimeout(() => {
      item.detailLoader?.()
        .then((next) => { if (token.current === current) setDetail(next); })
        // 取不到就只显示已经拿到的部分，不为一个悬浮卡片弹错误提示。
        .catch(() => undefined)
        .finally(() => { if (token.current === current) setDetailSettled(true); });
    }, DETAIL_LOAD_DELAY);
  };

  /**
   * 滚动后行会移走，快照下来的 rect 就不对了。
   *
   * 这里**重算**而不是直接收起：scroll 事件是异步派发的，会晚于滚动到位后新触发的
   * pointerenter。直接收起就会把刚打开的新卡片一起清掉 —— 指针停在清单上用触控板滚动时，
   * 卡片会一闪就没。行滚出容器范围才收起。
   */
  const realign = () => {
    const row = anchorRow.current;
    const list = listRef.current;
    if (!row || !list) return;
    const rect = row.getBoundingClientRect();
    const bounds = list.getBoundingClientRect();
    if (rect.bottom < bounds.top || rect.top > bounds.bottom) { dismiss(); return; }
    setHovered((previous) => (previous ? { ...previous, rect } : previous));
  };

  /** 离开行 / 离开卡片后延迟收起，给指针留出横穿间隙进到卡片里的时间。 */
  const scheduleClose = () => {
    clearTimers();
    closeTimer.current = window.setTimeout(dismiss, CARD_CLOSE_DELAY);
  };

  /** 指针进到卡片里（要滚动正文）：取消关闭，但别动加载计时器。 */
  const keepOpen = () => {
    if (closeTimer.current === null) return;
    window.clearTimeout(closeTimer.current);
    closeTimer.current = null;
  };

  const normalizedQuery = query.trim().toLowerCase();
  const filtered = items.filter((item) =>
    !normalizedQuery
    || item.label.toLowerCase().includes(normalizedQuery)
    || item.id.toLowerCase().includes(normalizedQuery)
    || item.detail?.toLowerCase().includes(normalizedQuery)
    || item.description?.toLowerCase().includes(normalizedQuery),
  );

  // 详情有结果（拿到或失败）之前不挂载卡片，否则会先弹一个只有标题的小卡片再被撑大。
  // 这里可以直接退回列表里的 description 兜底：不再有"先拿摘要顶一下、详情到了整段换掉"的闪烁问题。
  const content: HoverCardContent | null = hovered && detailSettled ? {
    title: hovered.item.label,
    badge: hovered.item.badge,
    identifier: hovered.item.id,
    summary: detail?.summary,
    groups: detail?.groups,
    facts: detail?.facts,
    body: detail?.body ?? hovered.item.description,
    bodyLabel: detail?.body ? detail.bodyLabel : undefined,
  } : null;

  return (
    <div className={`search-checkbox-picker ${className}`}>
      <div className="picker-toolbar">
        <input
          type="search"
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder={searchPlaceholder}
          disabled={disabled}
          aria-label={searchPlaceholder}
        />
        <span className="picker-count">{selectedLabel.replace('{count}', String(selected.length))}</span>
      </div>
      <div className="dependency-picker" ref={listRef} onScroll={realign}>
        {filtered.length ? filtered.map((item) => (
          <label
            key={item.id}
            onPointerEnter={(event) => openCard(event.currentTarget, item)}
            onPointerLeave={scheduleClose}
          >
            <input
              type="checkbox"
              checked={selected.includes(item.id)}
              onChange={(event) => onToggle(item.id, event.target.checked)}
              disabled={disabled}
            />
            <span className="picker-item-label">{item.label}</span>
            {item.detail && <small>{item.detail}</small>}
          </label>
        )) : <span className="detail-empty">{emptyLabel}</span>}
      </div>
      <ItemHoverCard
        anchor={hovered?.rect ?? null}
        content={content}
        onPointerEnter={keepOpen}
        onPointerLeave={scheduleClose}
      />
    </div>
  );
};

export default SearchCheckboxPicker;
