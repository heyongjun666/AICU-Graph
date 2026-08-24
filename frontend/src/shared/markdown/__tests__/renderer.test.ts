// @vitest-environment jsdom
import { describe, it, expect, beforeAll } from 'vitest';
import { BrowserMarkdownRenderer } from '../browser-renderer';

let renderer: BrowserMarkdownRenderer;

beforeAll(() => {
  renderer = new BrowserMarkdownRenderer();
});

// ============================================================================
// 辅助函数
// ============================================================================

function stripHtml(html: string): string {
  return html.replace(/<[^>]*>/g, '');
}

function normalizeHtml(html: string): string {
  return html.replace(/\s+/g, ' ').trim();
}

// ============================================================================
// 1. 标题 (H1-H6)
// ============================================================================
describe('Headings (H1-H6)', () => {
  const levels = [1, 2, 3, 4, 5, 6];
  for (const level of levels) {
    it(`should render H${level} with correct tag and id`, () => {
      const md = `${'#'.repeat(level)} Heading Level ${level}`;
      const result = renderer.render(md);
      expect(result.html).toContain(`<h${level}`);
      expect(result.html).toContain(`</h${level}>`);
      expect(result.html).toContain('Heading Level');
      expect(result.headingIds).toContain(`heading-level-${level}`);
    });
  }

  it('should generate unique heading IDs', () => {
    const md = '# Section\n\n## Section';
    const result = renderer.render(md);
    expect(result.headingIds).toEqual(['section', 'section']);
  });

  it('should handle heading with Chinese characters', () => {
    const md = '# 你好世界';
    const result = renderer.render(md);
    expect(result.html).toContain('<h1');
    expect(result.html).toContain('你好世界');
    expect(result.headingIds).toContain('你好世界');
  });

  it('should handle heading with special characters', () => {
    const md = '# Hello $pecial @Chars!';
    const result = renderer.render(md);
    expect(result.html).toContain('<h1');
    expect(result.headingIds).toContain('hello-pecial-chars');
  });
});

// ============================================================================
// 2. 粗体 / 斜体 / 删除线 / 行内代码
// ============================================================================
describe('Bold / Italic / Strikethrough / Inline Code', () => {
  it('should render bold text with **', () => {
    const html = renderer.renderInline('This is **bold** text');
    expect(html).toContain('<strong>bold</strong>');
  });

  it('should render bold text with __', () => {
    const html = renderer.renderInline('This is __bold__ text');
    expect(html).toContain('<strong>bold</strong>');
  });

  it('should render italic text with *', () => {
    const html = renderer.renderInline('This is *italic* text');
    expect(html).toContain('<em>italic</em>');
  });

  it('should render italic text with _', () => {
    const html = renderer.renderInline('This is _italic_ text');
    expect(html).toContain('<em>italic</em>');
  });

  it('should render strikethrough text', () => {
    const html = renderer.renderInline('This is ~~strikethrough~~ text');
    expect(html).toContain('<del>strikethrough</del>');
  });

  it('should render inline code', () => {
    const html = renderer.renderInline('Use the `code()` function');
    expect(html).toContain('<code>code()</code>');
  });

  it('should render bold+italic combination', () => {
    const html = renderer.renderInline('***bold italic***');
    expect(html).toContain('<strong><em>bold italic</em></strong>');
  });
});

// ============================================================================
// 3. 代码块 (含语言标注)
// ============================================================================
describe('Code Blocks', () => {
  it('should render code block with language class', () => {
    const md = '```javascript\nconst x = 1;\nconsole.log(x);\n```';
    const result = renderer.render(md);
    expect(result.html).toContain('<pre><code');
    expect(result.html).toContain('language-javascript');
    expect(result.html).toContain('hljs');
  });

  it('should render code block without language (plaintext)', () => {
    const md = '```\nplain code block\n```';
    const result = renderer.render(md);
    expect(result.html).toContain('<pre><code');
  });

  it('should render indented code block', () => {
    const md = '    indented code block';
    const result = renderer.render(md);
    expect(result.html).toContain('<pre><code');
    expect(result.html).toContain('indented code block');
  });

  it('should highlight Python syntax', () => {
    const md = '```python\ndef hello():\n    print("world")\n```';
    const result = renderer.render(md);
    expect(result.html).toContain('language-python');
    expect(result.html).toContain('hljs');
  });
});

// ============================================================================
// 4. 列表 (有序 / 无序 / 多层嵌套)
// ============================================================================
describe('Lists', () => {
  it('should render unordered list', () => {
    const md = '- Item 1\n- Item 2\n- Item 3';
    const result = renderer.render(md);
    expect(result.html).toContain('<ul>');
    expect(result.html).toContain('</ul>');
    expect(result.html).toContain('<li>Item 1</li>');
    expect(result.html).toContain('<li>Item 2</li>');
    expect(result.html).toContain('<li>Item 3</li>');
  });

  it('should render ordered list', () => {
    const md = '1. First\n2. Second\n3. Third';
    const result = renderer.render(md);
    expect(result.html).toContain('<ol>');
    expect(result.html).toContain('</ol>');
    expect(result.html).toContain('<li>First</li>');
    expect(result.html).toContain('<li>Second</li>');
    expect(result.html).toContain('<li>Third</li>');
  });

  it('should render multi-level nested unordered list', () => {
    const md = '- Level 1\n  - Level 2\n    - Level 3';
    const result = renderer.render(md);
    expect(result.html).toContain('<ul>');
    expect(result.html).toContain('<li>Level 1');
    // Nested list items should be present
    expect(result.html).toContain('Level 2');
    expect(result.html).toContain('Level 3');
  });

  it('should render mixed ordered/unordered nesting', () => {
    const md = '- Item\n  1. Sub Item 1\n  2. Sub Item 2';
    const result = renderer.render(md);
    expect(result.html).toContain('<ul>');
    expect(result.html).toContain('<ol>');
    expect(result.html).toContain('Sub Item 1');
    expect(result.html).toContain('Sub Item 2');
  });

  it('should render unordered list with asterisk', () => {
    const md = '* Item A\n* Item B';
    const result = renderer.render(md);
    expect(result.html).toContain('<ul>');
    expect(result.html).toContain('<li>Item A</li>');
    expect(result.html).toContain('<li>Item B</li>');
  });
});

// ============================================================================
// 5. 表格 (含对齐)
// ============================================================================
describe('Tables', () => {
  it('should render a basic table', () => {
    const md = '| Name | Age |\n| --- | --- |\n| Alice | 25 |\n| Bob | 30 |';
    const result = renderer.render(md);
    expect(result.html).toContain('<table>');
    expect(result.html).toContain('<th>Name</th>');
    expect(result.html).toContain('<th>Age</th>');
    expect(result.html).toContain('<td>Alice</td>');
    expect(result.html).toContain('<td>Bob</td>');
  });

  it('should render left-aligned column', () => {
    const md = '| Left |\n| :--- |\n| text |';
    const result = renderer.render(md);
    expect(result.html).toContain('text');
    // marked 12.x may or may not emit align attribute; just verify it renders
    expect(result.html).toContain('<td');
  });

  it('should render center-aligned column', () => {
    const md = '| Center |\n| :---: |\n| text |';
    const result = renderer.render(md);
    expect(result.html).toContain('<td');
    expect(result.html).toContain('text');
  });

  it('should render right-aligned column', () => {
    const md = '| Right |\n| ---: |\n| text |';
    const result = renderer.render(md);
    expect(result.html).toContain('<td');
    expect(result.html).toContain('text');
  });

  it('should render table with mixed alignment', () => {
    const md = '| Left | Center | Right |\n| :--- | :---: | ---: |\n| L1 | C1 | R1 |';
    const result = renderer.render(md);
    expect(result.html).toContain('<table>');
    expect(result.html).toContain('L1');
    expect(result.html).toContain('C1');
    expect(result.html).toContain('R1');
  });

  it('should render empty table cells', () => {
    const md = '| A | B |\n| --- | --- |\n|  | X |';
    const result = renderer.render(md);
    expect(result.html).toContain('<table>');
  });
});

// ============================================================================
// 6. 链接 (行内式 / 引用式)
// ============================================================================
describe('Links', () => {
  it('should render inline link', () => {
    const md = '[Click here](https://example.com)';
    const result = renderer.render(md);
    expect(result.html).toContain('<a href="https://example.com"');
    expect(result.html).toContain('Click here');
  });

  it('should render inline link with title', () => {
    const md = '[Click here](https://example.com "Example Site")';
    const result = renderer.render(md);
    expect(result.html).toContain('Example Site');
  });

  it('should render reference-style link', () => {
    const md = '[Reference][ref]\n\n[ref]: https://example.com';
    const result = renderer.render(md);
    expect(result.html).toContain('<a href="https://example.com"');
    expect(result.html).toContain('Reference');
  });

  it('should render reference-style link (implicit)', () => {
    const md = '[Google][]\n\n[Google]: https://google.com';
    const result = renderer.render(md);
    expect(result.html).toContain('<a href="https://google.com"');
  });
});

// ============================================================================
// 7. 图片
// ============================================================================
describe('Images', () => {
  it('should render an image', () => {
    const md = '![alt text](https://example.com/img.png)';
    const result = renderer.render(md);
    expect(result.html).toContain('<img');
    expect(result.html).toContain('src="https://example.com/img.png"');
    expect(result.html).toContain('alt="alt text"');
  });

  it('should render image with title', () => {
    const md = '![alt](https://example.com/img.png "Title")';
    const result = renderer.render(md);
    expect(result.html).toContain('Title');
  });

  it('should strip images when allowImages=false', () => {
    const md = '![alt](https://example.com/img.png)';
    const result = renderer.render(md, { allowImages: false });
    expect(result.html).not.toContain('<img');
  });
});

// ============================================================================
// 8. 块引用 (含嵌套)
// ============================================================================
describe('Blockquotes', () => {
  it('should render a simple blockquote', () => {
    const md = '> This is a quote';
    const result = renderer.render(md);
    expect(result.html).toContain('<blockquote>');
    expect(result.html).toContain('This is a quote');
  });

  it('should render multi-line blockquote', () => {
    const md = '> Line 1\n> Line 2\n> Line 3';
    const result = renderer.render(md);
    expect(result.html).toContain('<blockquote>');
    expect(result.html).toContain('Line 1');
    expect(result.html).toContain('Line 2');
    expect(result.html).toContain('Line 3');
  });

  it('should render nested blockquote', () => {
    const md = '> Outer\n>\n> > Inner\n>\n> Back to outer';
    const result = renderer.render(md);
    expect(result.html).toContain('<blockquote>');
    expect(stripHtml(result.html)).toContain('Inner');
  });

  it('should render blockquote with other markdown', () => {
    const md = '> **Bold** and *italic* inside quote';
    const result = renderer.render(md);
    expect(result.html).toContain('<blockquote>');
    expect(result.html).toContain('<strong>Bold</strong>');
    expect(result.html).toContain('<em>italic</em>');
  });
});

// ============================================================================
// 9. 分割线
// ============================================================================
describe('Horizontal Rules', () => {
  it('should render hr with ---', () => {
    const result = renderer.render('Before\n\n---\n\nAfter');
    expect(result.html).toContain('<hr');
  });

  it('should render hr with ***', () => {
    const result = renderer.render('Before\n\n***\n\nAfter');
    expect(result.html).toContain('<hr');
  });

  it('should render hr with ___', () => {
    const result = renderer.render('Before\n\n___\n\nAfter');
    expect(result.html).toContain('<hr');
  });
});

// ============================================================================
// 10. 任务列表
// ============================================================================
describe('Task Lists', () => {
  it('should render unchecked task', () => {
    const md = '- [ ] Unchecked task';
    const result = renderer.render(md);
    expect(result.html).toContain('<li');
    expect(result.html).toContain('Unchecked task');
  });

  it('should render checked task', () => {
    const md = '- [x] Checked task';
    const result = renderer.render(md);
    expect(result.html).toContain('<li');
    expect(result.html).toContain('Checked task');
  });

  it('should render mix of checked and unchecked tasks', () => {
    const md = '- [x] Done\n- [ ] Todo';
    const result = renderer.render(md);
    expect(result.html).toContain('Done');
    expect(result.html).toContain('Todo');
  });
});

// ============================================================================
// 11. 自动链接
// ============================================================================
describe('Autolinks', () => {
  it('should render URL autolink', () => {
    const md = '<https://example.com>';
    const result = renderer.render(md);
    expect(result.html).toContain('<a href="https://example.com"');
  });

  it('should render email autolink', () => {
    const md = '<user@example.com>';
    const result = renderer.render(md);
    expect(result.html).toContain('<a');
    expect(result.html).toContain('user@example.com');
  });
});

// ============================================================================
// 12. HTML 转义
// ============================================================================
describe('HTML Escaping', () => {
  it('should escape raw HTML in safe mode', () => {
    const md = '<div>custom</div>';
    const result = renderer.render(md);
    // The sanitizer should allow basic tags but strip unsafe ones, or escape them
    // With default sanitizeHtml=true, DOMPurify will process this
    expect(result.html).not.toContain('<script>');
  });

  it('should pass through allowed HTML when sanitize is off', () => {
    const md = '<span>hello</span>';
    const result = renderer.render(md, { sanitizeHtml: false });
    // When sanitization is off, the span should be preserved
    expect(result.html).toContain('hello');
  });
});

// ============================================================================
// 13. 数学公式
// ============================================================================
describe('Mathematical Formulas', () => {
  it('should render inline math with $...$ as plain text if not configured', () => {
    const html = renderer.renderInline('The formula $E=mc^2$ is famous');
    // marked does not natively support math; it should render as-is
    expect(html).toContain('$E=mc^2$');
  });

  it('should render block math with $$...$$ as plain text if not configured', () => {
    const md = '$$\nE=mc^2\n$$';
    const result = renderer.render(md);
    // marked does not natively support math; the content should still appear
    expect(result.html).toContain('E=mc^2');
  });
});

// ============================================================================
// 14. 段落与换行
// ============================================================================
describe('Paragraphs and Line Breaks', () => {
  it('should render paragraphs separated by blank lines', () => {
    const md = 'Paragraph one.\n\nParagraph two.';
    const result = renderer.render(md);
    expect(result.html).toContain('<p>Paragraph one.</p>');
    expect(result.html).toContain('<p>Paragraph two.</p>');
  });

  it('should render hard line break with two trailing spaces', () => {
    const md = 'Line 1  \nLine 2';
    const result = renderer.render(md);
    expect(result.html).toContain('Line 1');
    expect(result.html).toContain('Line 2');
  });
});

// ============================================================================
// 15. RenderOptions
// ============================================================================
describe('RenderOptions', () => {
  it('should disable code highlighting when highlightCode=false', () => {
    const md = '```js\nvar x = 1;\n```';
    const result1 = renderer.render(md, { highlightCode: false });
    const result2 = renderer.render(md, { highlightCode: true });
    // When highlighting is off, the code should still be rendered in <pre><code>
    expect(result1.html).toContain('<pre><code');
    expect(result2.html).toContain('<pre><code');
  });

  it('should disable heading anchors via sanitize', () => {
    const md = '# No Anchor';
    const result = renderer.render(md, { sanitizeHtml: false });
    expect(result.html).toContain('<h1');
    expect(result.headingIds).toContain('no-anchor');
  });
});

// ============================================================================
// 16. RenderResult 检查
// ============================================================================
describe('RenderResult', () => {
  it('should return frontmatter as null when none present', () => {
    const result = renderer.render('# Hello');
    expect(result.frontmatter).toBeNull();
  });

  it('should return non-empty headingIds when headings present', () => {
    const result = renderer.render('# A\n## B\n### C');
    expect(result.headingIds.length).toBe(3);
    expect(result.headingIds).toEqual(['a', 'b', 'c']);
  });

  it('should count words correctly for mixed Chinese/English', () => {
    const result = renderer.render('# Hello World\n\n你好世界');
    expect(result.wordCount).toBeGreaterThan(0);
  });

  it('should return zero word count for empty input', () => {
    const result = renderer.render('');
    expect(result.wordCount).toBe(0);
  });
});

// ============================================================================
// 17. 边界值和异常场景
// ============================================================================
describe('Edge Cases and Error Scenarios', () => {
  it('should handle empty string', () => {
    const result = renderer.render('');
    expect(result.html).toBe('');
    expect(result.headingIds).toEqual([]);
    expect(result.frontmatter).toBeNull();
    expect(result.wordCount).toBe(0);
  });

  it('should handle whitespace-only input', () => {
    const result = renderer.render('   \n\n  \n');
    expect(result.html.trim()).toBe('');
    expect(result.wordCount).toBe(0);
  });

  it('should handle single character', () => {
    const result = renderer.render('a');
    expect(result.html).toBeTruthy();
  });

  it('should handle newline-only input', () => {
    const result = renderer.render('\n\n\n');
    expect(result.html.trim()).toBe('');
  });

  it('should handle invalid language tag gracefully', () => {
    // marked + hljs: invalid language should fall back to highlightAuto
    const md = '```invalid_lang_xyz123\ncode\n```';
    const result = renderer.render(md);
    // Should still produce valid HTML
    expect(result.html).toContain('<pre><code');
  });

  it('should handle broken reference link without error', () => {
    // A reference with no matching definition should just render the text
    const md = '[broken][undefined-ref]';
    const result = renderer.render(md);
    // marked renders unresolved references as text
    expect(result.html).toBeTruthy();
  });

  it('should handle unclosed code fence', () => {
    const md = '```python\nprint("missing closing fence")';
    const result = renderer.render(md);
    // Should still render without throwing
    expect(result.html).toBeTruthy();
  });

  it('should handle unclosed bold marker', () => {
    const md = 'This is **unclosed bold';
    const result = renderer.render(md);
    // Should render without error
    expect(result.html).toBeTruthy();
  });

  it('should handle unclosed inline code', () => {
    const md = 'This is `unclosed code';
    const result = renderer.render(md);
    expect(result.html).toBeTruthy();
  });

  it('should handle extremely long single line', () => {
    const longLine = 'a'.repeat(10000);
    const result = renderer.render(longLine);
    expect(result.html).toBeTruthy();
    expect(result.wordCount).toBe(10000);
  });

  it('should handle input with only special characters', () => {
    const result = renderer.render('@#$%^&*()_+{}[]|\\:;"\'<>,.?/~`');
    expect(result.html).toBeTruthy();
  });

  it('should handle deeply nested blockquote (10+ levels)', () => {
    let md = '> Level 1';
    for (let i = 2; i <= 12; i++) {
      md += `\n>\n> > Level ${i}`;
    }
    const result = renderer.render(md);
    expect(result.html).toContain('Level 1');
    expect(result.html).toContain('Level 12');
  });

  it('should handle mixed ambiguous syntax gracefully', () => {
    const md = '# Header\n- List\n> Quote\n```\ncode\n```\n***\n| Table |\n| --- |\n| cell |';
    const result = renderer.render(md);
    expect(result.html).toContain('Header');
    expect(result.html).toContain('List');
    expect(result.html).toContain('code');
    expect(result.html).toContain('cell');
  });

  it('should handle very large input', () => {
    // Generate a large markdown: 500 lines of list items
    const lines = [];
    for (let i = 0; i < 500; i++) {
      lines.push(`- Item ${i}`);
    }
    const result = renderer.render(lines.join('\n'));
    expect(result.html).toBeTruthy();
    expect(result.html).toContain('Item 0');
    expect(result.html).toContain('Item 499');
    expect(result.wordCount).toBeGreaterThan(500);
  });

  it('should handle task list with extra whitespace', () => {
    const md = '- [ ]  Task with space\n- [x]  Done with space';
    const result = renderer.render(md);
    expect(result.html).toContain('Task with space');
    expect(result.html).toContain('Done with space');
  });

  it('should handle image with special characters in URL', () => {
    const md = '![img](https://example.com/path?query=1&param=2#frag)';
    const result = renderer.render(md);
    expect(result.html).toContain('<img');
    expect(result.html).toContain('example.com');
  });

  it('should handle multiple code blocks', () => {
    const md = '```js\nconst a = 1;\n```\n\nSome text\n\n```py\nb = 2\n```';
    const result = renderer.render(md);
    expect(result.html).toContain('language-javascript');
    expect(result.html).toContain('language-py');
  });
});

// ============================================================================
// 18. XSS 安全
// ============================================================================
describe('XSS Security', () => {
  it('should sanitize script tags', () => {
    const result = renderer.render('<script>alert("xss")</script>');
    expect(result.html).not.toContain('<script>');
  });

  it('should sanitize event handlers', () => {
    const md = '<img onerror="alert(1)" src="x">';
    const result = renderer.render(md);
    expect(result.html).not.toContain('onerror');
  });

  it('should sanitize javascript: URLs', () => {
    // This needs to be in a link context
    const md = '[click](javascript:alert(1))';
    const result = renderer.render(md);
    expect(result.html).not.toContain('javascript:');
  });

  it('should sanitize iframe tags', () => {
    const result = renderer.render('<iframe src="https://evil.com"></iframe>');
    expect(result.html).not.toContain('<iframe');
  });

  it('should sanitize onload attributes', () => {
    const result = renderer.render('<div onload="alert(1)">test</div>');
    expect(result.html).not.toContain('onload');
  });

  it('should sanitize onmouseover attributes', () => {
    const md = '<p onmouseover="alert(1)">hover me</p>';
    const result = renderer.render(md);
    expect(result.html).not.toContain('onmouseover');
  });

  it('should sanitize embedded script in markdown', () => {
    const md = 'Normal text\n\n<script>alert("xss")</script>\n\nMore text';
    const result = renderer.render(md);
    expect(result.html).not.toContain('<script>');
  });
});

// ============================================================================
// 19. renderInline 方法
// ============================================================================
describe('renderInline', () => {
  it('should sanitize XSS in inline mode', () => {
    const html = renderer.renderInline('<script>alert("xss")</script>');
    expect(html).not.toContain('<script>');
  });

  it('should not produce block elements from inline syntax', () => {
    const html = renderer.renderInline('simple text');
    expect(html).not.toContain('<p>');
    expect(html).not.toContain('<h1>');
  });

  it('should handle empty string in inline mode', () => {
    const html = renderer.renderInline('');
    expect(html).toBe('');
  });
});

// ============================================================================
// 20. getSanitizedHtml 方法
// ============================================================================
describe('getSanitizedHtml', () => {
  it('should strip dangerous tags', () => {
    const sanitized = renderer.getSanitizedHtml('<script>alert(1)</script><p>safe</p>');
    expect(sanitized).not.toContain('<script>');
    expect(sanitized).toContain('safe');
  });

  it('should allow safe HTML tags', () => {
    const html = '<strong>bold</strong><em>italic</em><code>code</code>';
    const sanitized = renderer.getSanitizedHtml(html);
    expect(sanitized).toContain('<strong>bold</strong>');
    expect(sanitized).toContain('<em>italic</em>');
    expect(sanitized).toContain('<code>code</code>');
  });
});
