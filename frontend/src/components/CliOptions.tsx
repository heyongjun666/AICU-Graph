import React from 'react';
import { CliInfo } from '../types';
import { useI18n } from '../i18n';

/**
 * CLI 下拉的选项列表（设置页 / 添加节点 / 编辑节点三处共用）。
 *
 * 三处的 `<select>` 本身不同（绑定对象、onChange、id 都不一样），共用的是「列哪些
 * CLI、怎么标注」这一份事实：选项文案里的「（未安装）」「（MCP 未接入）」是给用户
 * 判断"这个 CLI 能不能用"的依据，各写一份的话，改标注规则会漏掉某一处，同一个 CLI
 * 在设置页和节点对话框里给出不同结论。
 *
 * 只渲染 `<option>`，不带 `<select>` —— 调用方各自决定选中态与回调。
 * 编辑节点对话框在列表之外还有一个「当前值不在列表中也保留」的兜底 option，
 * 由调用方追加在本组件之后（那是它独有的行为，不并入这里）。
 */
export const CliOptions: React.FC<{ clis: CliInfo[]; defaultCli: string | null }> = ({ clis, defaultCli }) => {
  const { t } = useI18n();
  return (
    <>
      <option value="">{t('settings.autoDetect')}{defaultCli ? ` (${defaultCli})` : ''}</option>
      {clis.map((cli) => <option key={cli.id} value={cli.id}>{cli.display_name}{cli.installed ? '' : t('settings.notInstalled')}{cli.supports_mcp ? '' : t('settings.mcpNotWired')}</option>)}
    </>
  );
};
