// @vitest-environment jsdom
// ============================================================================
// useMcpCompatWarning：那条「角色声明了 mcp，但这个 CLI 接不了」的行内提示
//
// 触发条件是**两个事实同时成立**，缺一不可 —— 只挂在 CLI 上会变成对所有 pi 用户的唠叨
// （绝大多数节点压根没声明 MCP，他们没有理由被打扰）；只看角色又会在接得了的 CLI 上白提示。
//
// 提示里的成因**必须逐字来自后端**（list_clis 的 mcp_unwired_reason）。前端重写一份的话，
// 后端改了口径前端不跟着变，用户读到的就是一句与实际不符的话 —— 正是 supports_mcp 当初
// 下发到前端要避免的坑。所以下面的断言一律落在「后端那句原样出现了」，而不是「文案长这样」。
// ============================================================================

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { declaresMcp, useMcpCompatWarning } from '../useMcpCompatWarning';
import { getRole } from '../../utils/api';
import type { CliInfo } from '../../types';

vi.mock('../../utils/api', () => ({ getRole: vi.fn() }));

/** 后端 pi 那句的原文（含安装命令）。断言里逐字用它，前端改写了就会红。 */
const PI_REASON = '需要先安装 pi-mcp-adapter 扩展（pi install npm:pi-mcp-adapter）';

const PI: CliInfo = {
  id: 'pi', display_name: 'Pi', installed: true, priority: 0, models: [],
  supports_mcp: false, mcp_unwired_reason: PI_REASON,
};
const CLAUDE: CliInfo = {
  id: 'claude', display_name: 'Claude Code', installed: true, priority: 1, models: [],
  supports_mcp: true, mcp_unwired_reason: null,
};

function mockRole(config: Record<string, unknown> | undefined, effective?: Record<string, unknown>) {
  vi.mocked(getRole).mockResolvedValue({
    id: 'dev', type: 'worker', config, effective_config: effective,
  } as never);
}

function render(cliId: string, enabled = true) {
  return renderHook(() =>
    useMcpCompatWarning({ enabled, roleType: 'worker', roleId: 'dev', cliId, clis: [PI, CLAUDE] }),
  );
}

beforeEach(() => {
  vi.mocked(getRole).mockReset();
});

describe('declaresMcp', () => {
  it('空数组、缺字段、null 都算没声明', () => {
    expect(declaresMcp({ mcp: [] })).toBe(false);
    expect(declaresMcp({})).toBe(false);
    expect(declaresMcp({ mcp: null })).toBe(false);
    expect(declaresMcp(undefined)).toBe(false);
  });

  it('有名字就算声明了', () => {
    expect(declaresMcp({ mcp: ['echo-probe'] })).toBe(true);
    // YAML 里写成单数标量的也认，别因为形状不对就漏掉一句该给的提示
    expect(declaresMcp({ mcp: 'echo-probe' })).toBe(true);
  });
});

describe('useMcpCompatWarning', () => {
  it('角色声明了 mcp 而 CLI 接不了 → 用后端那句原因提示', async () => {
    mockRole({ mcp: ['echo-probe'] });
    const { result } = render('pi');

    await waitFor(() => expect(result.current).not.toBeNull());
    // 判据两半：后端原句在里面（成因没被前端重写），且说清了是哪个 CLI。
    expect(result.current).toContain(PI_REASON);
    expect(result.current).toContain('Pi');
  });

  it('CLI 接得了 → 不提示，并且**根本不问角色**', async () => {
    mockRole({ mcp: ['echo-probe'] });
    const { result } = render('claude');

    expect(result.current).toBeNull();
    // 这一条是省下来的那次请求：接得了的 CLI 上，角色声明没声明 mcp 都不影响运行。
    expect(getRole).not.toHaveBeenCalled();
  });

  it('角色没声明 mcp → 不提示', async () => {
    mockRole({ mcp: [] });
    const { result } = render('pi');

    await waitFor(() => expect(getRole).toHaveBeenCalled());
    expect(result.current).toBeNull();
  });

  it('面板没打开 → 不提示，也不请求', () => {
    mockRole({ mcp: ['echo-probe'] });
    const { result } = render('pi', false);

    expect(result.current).toBeNull();
    expect(getRole).not.toHaveBeenCalled();
  });

  it('角色取不到 → 不提示（不把一次网络失败说成「它没声明 mcp」）', async () => {
    vi.mocked(getRole).mockRejectedValue(new Error('boom'));
    const { result } = render('pi');

    await waitFor(() => expect(getRole).toHaveBeenCalled());
    expect(result.current).toBeNull();
  });

  it('读的是 effective_config —— default.yaml 里声明的 mcp 同样算数', async () => {
    // 后端给的 effective_config = {**default.yaml, **角色.yaml}，与运行时 role_config 同源。
    // 全局声明 mcp 恰恰是最常见的用法，漏掉这一层就会在最该提示的时候闭嘴。
    mockRole({}, { mcp: ['echo-probe'] });
    const { result } = render('pi');

    await waitFor(() => expect(result.current).not.toBeNull());
    expect(result.current).toContain(PI_REASON);
  });

  it('effective_config 缺失时回落到 config', async () => {
    mockRole({ mcp: ['echo-probe'] }, undefined);
    const { result } = render('pi');

    await waitFor(() => expect(result.current).not.toBeNull());
  });
});
