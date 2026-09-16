// ============================================================================
// roleHoverDetail —— 角色 YAML → 悬浮卡片的映射
//
// 用户要的是「悬浮能看到 yaml 里跟角色人格相关的配置」，所以这里的契约是
// **哪个字段进卡片的哪一块**。文案本身归 i18n 管，这里只断言分组语义与取值，
// 免得把译文抄进测试、改一次文案要改两处。
// ============================================================================

import { describe, expect, it } from 'vitest';
import { toRoleHoverDetail } from '../roleHoverDetail';
import type { RoleInfo } from '../../types';

const roleOf = (effective_config: Record<string, unknown>): RoleInfo => ({
  id: 'code',
  display_name: 'Code',
  type: 'worker',
  effective_config,
});

describe('toRoleHoverDetail', () => {
  it('maps the decision fields to summary, two groups and the fact row', () => {
    const detail = toRoleHoverDetail(roleOf({
      summary: '高级软件工程师，擅长生产级代码编写',
      suitable_for: ['代码实现与算法开发', 'Bug修复与功能开发'],
      not_for: ['需求分析与需求文档编写'],
      num_ccs: 2,
      audit_rounds: 2,
      skills: ['diagnosing-bugs', 'codegraph'],
      role_md: '你是高级软件工程师……',
    }));

    expect(detail.summary).toBe('高级软件工程师，擅长生产级代码编写');

    // 适合 / 不适合各成一组，正负分明 —— 顺序即展示顺序
    expect(detail.groups?.map((group) => group.tone)).toEqual(['positive', 'negative']);
    expect(detail.groups?.[0].items).toEqual(['代码实现与算法开发', 'Bug修复与功能开发']);
    expect(detail.groups?.[1].items).toEqual(['需求分析与需求文档编写']);
    expect(detail.groups?.every((group) => group.label.length > 0)).toBe(true);

    // skills 是数组，展示时要拼成一行而不是原样甩个数组进 React
    expect(detail.facts?.map((fact) => fact.value)).toEqual(['2', '2', 'diagnosing-bugs, codegraph']);

    expect(detail.body).toBe('你是高级软件工程师……');
    expect(detail.bodyLabel).toBeTruthy();
  });

  it('keeps multi-line persona text verbatim', () => {
    const persona = '你是高级软件工程师。\n\n## 职责\n- 写代码\n- 修 bug';
    const detail = toRoleHoverDetail(roleOf({ role_md: persona }));

    // 换行必须原样保留：卡片是按原文呈现，不是摘要
    expect(detail.body).toBe(persona);
    expect(detail.body?.split('\n')).toHaveLength(5);
  });

  it('drops the persona block entirely for roles with an empty role_md', () => {
    // Manager 的 default 角色就是这样：通用组织者人格由运行时骨架拼在前面，
    // 这里的 role_md 是空串，卡片不该留一个空白块。
    const detail = toRoleHoverDetail(roleOf({ summary: '通用组织构建者', role_md: '' }));

    expect(detail.body).toBeUndefined();
    expect(detail.bodyLabel).toBeUndefined();
  });

  it('omits a fact row rather than showing an empty value', () => {
    const detail = toRoleHoverDetail(roleOf({ summary: 'x', skills: [] }));

    // num_ccs / audit_rounds / skills 都没有，事实行整块不出现
    expect(detail.facts).toEqual([]);
  });

  it('treats a scalar where a list is expected as a one-item list', () => {
    // 只覆盖 default.yaml 的角色可能把这类字段写成单个字符串，不该在卡片上炸掉
    const detail = toRoleHoverDetail(roleOf({ suitable_for: '代码实现' }));

    expect(detail.groups?.[0].items).toEqual(['代码实现']);
  });

  it('falls back to the list description when the yaml has no summary', () => {
    const role: RoleInfo = { ...roleOf({ role_md: '人格' }), description: '首行摘要' };
    expect(toRoleHoverDetail(role).summary).toBe('首行摘要');
  });

  it('prefers effective_config over the raw config', () => {
    // effective_config 是 defaults 叠上 config 的结果，卡片要用那份带继承的，
    // 否则只覆盖了 role_md 的角色会把 num_ccs 这类继承来的值显示成空。
    const role: RoleInfo = {
      ...roleOf({ num_ccs: 2 }),
      config: { role_md: '人格' },
    };

    expect(toRoleHoverDetail(role).facts?.map((fact) => fact.value)).toContain('2');
  });
});
