/**
 * 展示层格式化助手。
 *
 * 放这里而不是各组件内各自实现，是因为「多少字节怎么显示」是全站一致的事实：
 * 原来 NodeInspector / SharedWorkspacePanel 等各抄了一份逐字相同的实现，
 * 改一处（比如加 GB 档、改小数位）会漏掉其它几处，同一个文件在不同面板里
 * 显示成不同大小。
 */

/** 字节数转人类可读（B / KB / MB，保留一位小数）。 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
