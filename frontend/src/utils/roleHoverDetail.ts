// ============================================================================
// 角色悬浮卡片的详情加载
//
// 列表接口（getRoles）只返回 {id, display_name, description, type}，**不带 config**，
// 所以要展示 YAML 里的完整人格配置就得按 id 再取一次（getRole 会带 content /
// effective_config）。勾选清单里鼠标扫过十几行是很常见的动作，所以这里做了两件事：
//   · 缓存：同一条角色只请求一次，来回悬停不再打接口。
//   · 加载节流交给调用方（SearchCheckboxPicker 的 detailLoader 会等指针停留一下才调用），
//     这里不做防抖，免得"延迟"这件事散在两个地方各自为政。
//
// 缓存是模块级的，编辑角色后必须由调用方 clearRoleHoverDetailCache()，否则卡片会
// 一直显示改之前的旧人格。
// ============================================================================

import { getRole } from './api';
import { translate } from '../i18n';
import type { HoverCardDetail } from '../components/ItemHoverCard';
import type { RoleInfo } from '../types';

const cache = new Map<string, HoverCardDetail>();
const inflight = new Map<string, Promise<HoverCardDetail>>();

/**
 * YAML 字段不保证是数组（写成单个字符串的也有），统一成字符串数组。
 * 顺带丢掉空字符串 —— 列表里出现一个空圆点是纯噪声。
 */
function toStringList(value: unknown): string[] {
  const raw = Array.isArray(value) ? value : [value];
  return raw
    .filter((item) => item !== null && item !== undefined)
    .map((item) => String(item).trim())
    .filter(Boolean);
}

/** 标量转成可显示的文本；数字要留（num_ccs: 2），null / undefined / 空串要丢掉。 */
function toText(value: unknown): string | undefined {
  if (value === null || value === undefined) return undefined;
  const text = String(value).trim();
  return text || undefined;
}

/** 把 effective_config 映射成卡片内容。导出是为了能脱离网络单测这段映射。 */
export function toRoleHoverDetail(role: RoleInfo): HoverCardDetail {
  const config = (role.effective_config ?? role.config ?? {}) as Record<string, unknown>;

  const groups = [
    { label: translate('hover.suitableFor'), items: toStringList(config.suitable_for), tone: 'positive' as const },
    { label: translate('hover.notFor'), items: toStringList(config.not_for), tone: 'negative' as const },
  ];

  // 事实行：值取不到就整条不显示，而不是留一个「num_ccs: 」的空壳。
  const facts = [
    { label: translate('hover.numCcs'), value: toText(config.num_ccs) },
    { label: translate('hover.auditRounds'), value: toText(config.audit_rounds) },
    { label: translate('hover.skills'), value: toStringList(config.skills).join(', ') || undefined },
  ].filter((fact): fact is { label: string; value: string } => Boolean(fact.value));

  // role_md 是写进每个 CC 的 CLAUDE.md 的人格正文。Manager 角色常常是空串
  // （通用组织者人格由运行时的骨架拼在前面），这时不留空块。
  const body = toText(config.role_md);

  return {
    summary: toText(config.summary) ?? toText(role.description),
    groups,
    facts,
    body,
    bodyLabel: body ? translate('hover.persona') : undefined,
  };
}

/**
 * 取一条角色的悬浮详情。
 * 同 id 的并发调用共用同一个请求（指针快速划过两行时会出现这种情况）。
 */
export function loadRoleHoverDetail(
  roleType: RoleInfo['type'],
  roleId: string,
): Promise<HoverCardDetail> {
  // 没有类型就拼不出 URL。宁可不请求让它只显示标题，也好过猜一个类型去 404。
  if (!roleType) return Promise.resolve({ groups: [], facts: [] });

  const key = `${roleType}:${roleId}`;
  const cached = cache.get(key);
  if (cached) return Promise.resolve(cached);

  const pending = inflight.get(key);
  if (pending) return pending;

  const request = getRole(roleType, roleId)
    .then((role) => {
      const detail = toRoleHoverDetail(role);
      cache.set(key, detail);
      return detail;
    })
    .finally(() => {
      inflight.delete(key);
    });

  inflight.set(key, request);
  return request;
}

/** 角色内容被改动后调用，丢弃缓存，让下次悬停拿到新的 YAML。 */
export function clearRoleHoverDetailCache(): void {
  cache.clear();
}
