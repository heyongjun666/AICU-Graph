// ===== shared/markdown/renderer.ts =====
// MarkdownRenderer 接口定义 — 前后端渲染器的唯一契约

export interface RenderOptions {
  /** 代码语法高亮，默认 true */
  highlightCode?: boolean;
  /** 输出前过滤 XSS，默认 true */
  sanitizeHtml?: boolean;
  /** 是否允许图片标签，默认 true */
  allowImages?: boolean;
  /** 为标题生成锚点 ID，默认 true */
  headingAnchors?: boolean;
}

export interface RenderResult {
  /** 渲染后的 HTML */
  html: string;
  /** YAML frontmatter（如有） */
  frontmatter: Record<string, unknown> | null;
  /** 标题 ID 列表，用于 TOC 生成 */
  headingIds: string[];
  /** 中英文混排字数 */
  wordCount: number;
}

export interface MarkdownRenderer {
  /** 将 Markdown 渲染为 HTML */
  render(source: string, options?: RenderOptions): RenderResult;

  /** 将行内 Markdown 渲染为 HTML（用于评论内容、摘要等） */
  renderInline(source: string): string;

  /** 对 HTML 进行安全过滤（防止 XSS） */
  getSanitizedHtml(html: string): string;
}
