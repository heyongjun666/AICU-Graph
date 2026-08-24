// ===== shared/markdown/browser-renderer.ts =====
// 客户端 Markdown 渲染器（Browser 绑定）
// 使用 marked + highlight.js + DOMPurify

import { Marked, Renderer as MarkedRenderer } from 'marked';
import hljs from 'highlight.js';
import DOMPurify from 'dompurify';
import type { MarkdownRenderer, RenderOptions, RenderResult } from './renderer';

let purifyInstance: DOMPurify.DOMPurifyI | null = null;

function getPurify(): DOMPurify.DOMPurifyI {
  if (!purifyInstance) {
    // dompurify in jsdom/Node is a factory function that needs a window
    if ((DOMPurify as unknown as { sanitize?: unknown }).sanitize) {
      // Already a DOMPurifyI instance (browser ESM)
      purifyInstance = DOMPurify as unknown as DOMPurify.DOMPurifyI;
    } else if (typeof window !== 'undefined') {
      // jsdom or browser with factory function
      purifyInstance = (DOMPurify as unknown as (w: Window) => DOMPurify.DOMPurifyI)(window);
    } else {
      // No DOM available - create a pass-through sanitizer
      purifyInstance = { sanitize: (html: string) => html, isValid: false, version: '0.0.0', removed: [], isSupported: false } as unknown as DOMPurify.DOMPurifyI;
    }
  }
  return purifyInstance;
}

export class BrowserMarkdownRenderer implements MarkdownRenderer {
  private marked: Marked;

  constructor() {
    this.marked = new Marked();

    const renderer = new MarkedRenderer();

    renderer.code = (code: string, infostring: string) => {
      const language = (infostring || '').match(/^\S*/)?.[0] || 'plaintext';
      let highlighted: string;
      try {
        highlighted = hljs.highlight(code, { language }).value;
      } catch {
        highlighted = hljs.highlightAuto(code).value;
      }
      return `<pre><code class="hljs language-${language}">${highlighted}</code></pre>`;
    };

    renderer.heading = (text: string, level: number) => {
      const id = text
        .toLowerCase()
        .replace(/<[^>]*>/g, '')
        .replace(/[^\w\u4e00-\u9fff]+/g, '-')
        .replace(/^-+|-+$/g, '');
      return `<h${level} id="${id}">${text}</h${level}>`;
    };

    this.marked.use({ renderer });
  }

  render(source: string, options?: RenderOptions): RenderResult {
    const opts: Required<RenderOptions> = {
      highlightCode: options?.highlightCode ?? true,
      sanitizeHtml: options?.sanitizeHtml ?? true,
      allowImages: options?.allowImages ?? true,
      headingAnchors: options?.headingAnchors ?? true,
    };

    const tokens = this.marked.lexer(source);
    const headingIds: string[] = [];

    let frontmatter: Record<string, unknown> | null = null;

    for (const token of tokens) {
      if (token.type === 'heading') {
        const id = token.text
          .toLowerCase()
          .replace(/<[^>]*>/g, '')
          .replace(/[^\w\u4e00-\u9fff]+/g, '-')
          .replace(/^-+|-+$/g, '');
        headingIds.push(id);
      }
    }

    let html = this.marked.parse(source) as string;

    if (!opts.allowImages) {
      html = html.replace(/<img[^>]*>/g, '');
    }

    if (opts.sanitizeHtml) {
      html = this.getSanitizedHtml(html);
    }

    const wordCount = this.countWords(source);

    return {
      html,
      frontmatter,
      headingIds,
      wordCount,
    };
  }

  renderInline(source: string): string {
    const html = this.marked.parseInline(source) as string;
    return this.getSanitizedHtml(html);
  }

  getSanitizedHtml(html: string): string {
    return getPurify().sanitize(html, {
      ALLOWED_TAGS: [
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'p', 'br', 'hr',
        'ul', 'ol', 'li',
        'blockquote', 'pre', 'code',
        'strong', 'em', 'del', 'ins', 'sub', 'sup',
        'a', 'img',
        'table', 'thead', 'tbody', 'tr', 'th', 'td',
        'dl', 'dt', 'dd',
        'span', 'div',
      ],
      ALLOWED_ATTR: ['id', 'class', 'href', 'src', 'alt', 'title', 'target', 'rel'],
    });
  }

  private countWords(source: string): number {
    const text = source
      .replace(/^---[\s\S]*?---\n?/m, '')
      .replace(/<[^>]*>/g, '')
      .trim();

    if (!text) return 0;

    const chineseChars = (text.match(/[\u4e00-\u9fff\u3400-\u4dbf]/g) || []).length;
    const englishText = text.replace(/[\u4e00-\u9fff\u3400-\u4dbf]/g, ' ');
    const englishWords = englishText
      .split(/\s+/)
      .filter((w) => w.length > 0).length;

    return chineseChars + englishWords;
  }
}
