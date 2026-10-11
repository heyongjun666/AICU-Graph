/** catch 到的 unknown 转成可展示的文案：Error 取 message，其余 String()。 */
export function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}
