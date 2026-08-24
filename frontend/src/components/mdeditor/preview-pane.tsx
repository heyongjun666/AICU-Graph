// ===== src/components/mdeditor/preview-pane.tsx =====
// 实时预览面板 — 使用 BrowserMarkdownRenderer 渲染 Markdown

import React, { useMemo, useState } from 'react';
import { BrowserMarkdownRenderer } from '../../shared/markdown/browser-renderer';
import type { RenderResult } from '../../shared/markdown/renderer';

const renderer = new BrowserMarkdownRenderer();

export interface PreviewPaneProps {
  /** Markdown 源文本 */
  source: string;
  /** 编辑器主题（同步到预览区） */
  theme?: 'light' | 'dark';
  /** 是否显示 TOC */
  showToc?: boolean;
}

/**
 * PreviewPane — 实时 Markdown 预览面板
 *
 * 使用 BrowserMarkdownRenderer 对 source 进行渲染并显示结果。
 * 在编辑过程中实时更新预览内容。
 */
const PreviewPane: React.FC<PreviewPaneProps> = ({
  source,
  theme = 'light',
  showToc = false,
}) => {
  const [activeHeading, setActiveHeading] = useState<string>('');

  const result: RenderResult = useMemo(() => {
    try {
      return renderer.render(source, {
        highlightCode: true,
        sanitizeHtml: true,
        headingAnchors: true,
      });
    } catch {
      return {
        html: '<p style="color:red">渲染错误</p>',
        frontmatter: null,
        headingIds: [],
        wordCount: 0,
      };
    }
  }, [source]);

  const containerStyle: React.CSSProperties = {
    padding: '16px',
    overflow: 'auto',
    height: '100%',
    backgroundColor: theme === 'dark' ? '#1e1e1e' : '#ffffff',
    color: theme === 'dark' ? '#d4d4d4' : '#1a1a1a',
    fontFamily: "'Georgia', 'Noto Serif SC', 'Source Han Serif SC', serif",
    fontSize: '15px',
    lineHeight: '1.8',
  };

  const tocContainerStyle: React.CSSProperties = {
    padding: '12px',
    marginBottom: '16px',
    backgroundColor: theme === 'dark' ? '#2d2d2d' : '#f5f5f5',
    borderRadius: '6px',
    fontSize: '14px',
  };

  const wordCountStyle: React.CSSProperties = {
    fontSize: '12px',
    color: theme === 'dark' ? '#888' : '#999',
    textAlign: 'right',
    padding: '4px 0',
    borderTop: `1px solid ${theme === 'dark' ? '#333' : '#e0e0e0'}`,
    marginTop: '16px',
  };

  return (
    <div
      className="md-preview-pane"
      style={containerStyle}
      data-testid="preview-pane"
    >
      {showToc && result.headingIds.length > 0 && (
        <div style={tocContainerStyle}>
          <div style={{ fontWeight: 600, marginBottom: '8px', fontSize: '13px', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            目录
          </div>
          {result.headingIds.map((id) => (
            <div
              key={id}
              style={{
                padding: '2px 0',
                cursor: 'pointer',
                color: activeHeading === id ? '#0066cc' : theme === 'dark' ? '#9cdcfe' : '#333',
                fontWeight: activeHeading === id ? 600 : 400,
              }}
              onClick={() => setActiveHeading(id)}
            >
              {id.replace(/-/g, ' ')}
            </div>
          ))}
        </div>
      )}

      <div
        className="md-preview-content"
        dangerouslySetInnerHTML={{ __html: result.html }}
        style={{ minHeight: '200px' }}
      />

      <div style={wordCountStyle}>
        字数: ~{result.wordCount}
      </div>
    </div>
  );
};

export default PreviewPane;
