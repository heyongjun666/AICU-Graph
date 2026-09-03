import { useEffect, useState } from 'react';
import { CliInfo } from '../types';
import { getGlobalConfig } from '../utils/api';

/**
 * 加载系统可用 CLI 列表（与设置页同源：/v1/config 的 clis + default_cli）。
 * 用于「添加节点 / 编辑节点」对话框的 CLI 下拉，保证与设置页可选数量一致。
 * 加载失败时静默返回空列表，由调用方回退处理。
 */
export function useAvailableClis(): { clis: CliInfo[]; defaultCli: string | null } {
  const [clis, setClis] = useState<CliInfo[]>([]);
  const [defaultCli, setDefaultCli] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getGlobalConfig()
      .then((resp) => {
        if (!alive) return;
        setClis(resp.clis || []);
        setDefaultCli(resp.default_cli ?? null);
      })
      .catch(() => {
        // 保持空列表，下拉仅显示"自动（会话默认）"。
      });
    return () => { alive = false; };
  }, []);

  return { clis, defaultCli };
}
