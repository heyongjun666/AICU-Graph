// ============================================================================
// 「该角色声明了 MCP，但这个 CLI 接不了」这条提示
//
// 触发条件是**两个事实同时成立**：角色声明了 mcp，且选中的 CLI 接不了。只挂在「选的是
// pi」上会变成对所有用户的唠叨 —— 绝大多数节点压根没声明 MCP，这些用户没有理由被打扰；
// 反过来只看角色也不够，没声明 MCP 的角色换到哪个 CLI 都无所谓。
//
// 提示里的**原因短语来自后端**（``list_clis`` 下发的 ``mcp_unwired_reason``），前端一个字
// 的成因都不写。成因会变：pi 今天是「装个扩展就行」，grok / agy 是「AICU 还没接」。前端
// 各拼一份的话，后端改了口径前端不会跟着变，用户读到的就是一句与实际不符的话。代价是
// 英文界面里那句原因是中文 —— 后端文案本来就是中文、也都是原样显示（见各处 error 提示），
// 宁可中英混排，也不把成因维护成两份。
// ============================================================================

import { useEffect, useState } from 'react';
import { getRole } from '../utils/api';
import { useI18n } from '../i18n';
import type { CliInfo, RoleInfo } from '../types';

type RoleType = NonNullable<RoleInfo['type']>;

/**
 * 这份配置有没有声明 mcp。
 *
 * 读的是 ``effective_config``（后端给的 ``{**default.yaml, **角色.yaml}``，与运行时
 * ``role_config`` 同一份合并结果），所以 default.yaml 里声明的 mcp 也算数 —— 那正是最
 * 常见的全局声明方式。空数组 / 空串都算「没声明」。
 */
export function declaresMcp(config: Record<string, unknown> | undefined): boolean {
  const mcp = config?.mcp;
  if (Array.isArray(mcp)) return mcp.length > 0;
  return Boolean(mcp);
}

export function useMcpCompatWarning({
  enabled,
  roleType,
  roleId,
  cliId,
  clis,
}: {
  /** 面板关着时不请求，也不提示。 */
  enabled: boolean;
  roleType: RoleType;
  roleId: string;
  cliId: string;
  clis: CliInfo[];
}): string | null {
  const { t } = useI18n();
  const cli = clis.find((item) => item.id === cliId);
  const reason = cli?.mcp_unwired_reason ?? null;
  const [declared, setDeclared] = useState(false);
  const wanted = enabled ? roleId.trim() : '';

  useEffect(() => {
    // 先看 CLI 那一边：它接得了的话，这个角色声明没声明 mcp 都不影响运行，不必去问。
    // 换句话说这一步请求只在「CLI 确实接不了」时才会发 —— 那本来就是少数。
    if (!enabled || !reason || !roleType || !wanted) {
      setDeclared(false);
      return;
    }
    let alive = true;
    getRole(roleType, wanted)
      .then((role) => {
        if (alive) setDeclared(declaresMcp(role.effective_config ?? role.config));
      })
      .catch(() => {
        // 取不到就**不提示**。这只是一句提醒，不是闸门；把一次网络失败说成一条关于
        // 角色的结论（「它没声明 mcp」）同样是假话。
        if (alive) setDeclared(false);
      });
    return () => { alive = false; };
  }, [enabled, reason, roleType, wanted]);

  if (!enabled || !cli || !reason || !declared) return null;
  return t('dialog.mcpNotWiredHint', { cli: cli.display_name, reason });
}
