import React, { useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

/** 悬浮卡片里的一个分组清单（适合 / 不适合）。 */
export interface HoverCardGroup {
  label: string;
  items: string[];
  tone: 'positive' | 'negative';
}

/**
 * 悬浮卡片的内容。字段全部可选，有什么展示什么 —— 角色、skill、节点各自的
 * 可得信息差别很大，用一套可选字段比给每种实体各写一个卡片组件省事。
 */
export interface HoverCardContent {
  title: string;
  /** 标题右侧的短标签（角色类型）。只放短词。 */
  badge?: string;
  /** 独立成行的标识符，通常是 id；与 title 相同时不重复显示。 */
  identifier?: string;
  /** 一句话定位。 */
  summary?: string;
  groups?: HoverCardGroup[];
  /** 参数行，形如「Skills a, b」。 */
  facts?: { label: string; value: string }[];
  /** 完整原文（角色的人格配置），限高滚动展示。 */
  body?: string;
  /** 原文的小标题。 */
  bodyLabel?: string;
}

/**
 * 卡片里由「详情」推导的那部分，不含标题行。
 * 懒加载器（如 roleHoverDetail）只负责产出这几项，标题与标签由列表数据填。
 */
export type HoverCardDetail = Omit<HoverCardContent, 'title' | 'badge' | 'identifier'>;

interface ItemHoverCardProps {
  /** 触发元素的视口坐标；null 表示不展示。 */
  anchor: DOMRect | null;
  content: HoverCardContent | null;
  /** 指针移进卡片。卡片正文可滚动，所以在指针进入时要让调用方取消关闭计时。 */
  onPointerEnter?: () => void;
  onPointerLeave?: () => void;
}

const CARD_WIDTH = 320;
const VIEWPORT_MARGIN = 10;

/**
 * 清单条目的悬浮完整信息卡片。
 *
 * 走 portal 挂到 document.body 并用 position: fixed —— 这是必需的：消费方
 * （.dependency-picker 与 .workbench-dialog）都有 overflow，卡片留在原地会被裁掉。
 * 观感沿用面板本身的白底 + 浅描边，而不是 .help-tip-pop 那套深色气泡：
 * 那是个带小箭头的短提示，配色只借过来会变成一个浮在空中的黑块。
 */
const ItemHoverCard: React.FC<ItemHoverCardProps> = ({
  anchor,
  content,
  onPointerEnter,
  onPointerLeave,
}) => {
  const cardRef = useRef<HTMLDivElement>(null);
  const [measuredHeight, setMeasuredHeight] = useState(0);

  // 量真实高度而不是靠字数估算：人格原文动辄几十行，估低了卡片就会从视口底部探出去。
  // 首帧量不到（还是 0）时才退回一个够用的估值。无依赖数组 —— 内容变了要重新量，
  // 值没变就 return 同一个数，不会自激。
  useLayoutEffect(() => {
    const height = cardRef.current?.offsetHeight ?? 0;
    setMeasuredHeight((previous) => (previous === height ? previous : height));
  });

  if (!anchor || !content || typeof document === 'undefined') return null;

  const flipX = anchor.left + anchor.width + VIEWPORT_MARGIN + CARD_WIDTH > window.innerWidth;
  const left = flipX
    ? Math.max(VIEWPORT_MARGIN, anchor.left - VIEWPORT_MARGIN - CARD_WIDTH)
    : anchor.left + anchor.width + VIEWPORT_MARGIN;

  // 贴顶显示，除非下方装不下 —— 这时整体上移，而不是从底部翻到上面去。
  const estimatedHeight = 54 + (content.identifier ? 16 : 0) + (content.body ? 200 : 0);
  const height = measuredHeight || estimatedHeight;
  const top = Math.max(
    VIEWPORT_MARGIN,
    Math.min(anchor.top, window.innerHeight - height - VIEWPORT_MARGIN),
  );

  // 名称就是 id 时不重复占一行（很多 skill 没有单独的 name）。
  const showIdentifier = Boolean(content.identifier) && content.identifier !== content.title;

  return createPortal(
    <div
      ref={cardRef}
      className="item-hover-card"
      data-testid="item-hover-card"
      role="tooltip"
      style={{ left, top }}
      onPointerEnter={onPointerEnter}
      onPointerLeave={onPointerLeave}
    >
      <div className="item-hover-card-title">
        <span>{content.title}</span>
        {content.badge && <small>{content.badge}</small>}
      </div>
      {showIdentifier && <div className="item-hover-card-id">{content.identifier}</div>}
      {content.summary && <div className="item-hover-card-summary">{content.summary}</div>}
      {content.groups?.filter((group) => group.items.length > 0).map((group) => (
        <div key={group.label} className={`item-hover-card-group is-${group.tone}`}>
          <div className="item-hover-card-group-label">{group.label}</div>
          <ul>
            {group.items.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </div>
      ))}
      {content.facts && content.facts.length > 0 && (
        <div className="item-hover-card-facts">
          {content.facts.map((fact) => (
            <span key={fact.label}><b>{fact.label}</b>{fact.value}</span>
          ))}
        </div>
      )}
      {content.body && (
        <div className="item-hover-card-body">
          {content.bodyLabel && <div className="item-hover-card-body-label">{content.bodyLabel}</div>}
          <div className="item-hover-card-body-text">{content.body}</div>
        </div>
      )}
    </div>,
    document.body,
  );
};

export default ItemHoverCard;
