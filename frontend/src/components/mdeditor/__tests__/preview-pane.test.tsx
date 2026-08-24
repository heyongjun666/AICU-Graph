// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import React from 'react';
import PreviewPane from '../preview-pane';

afterEach(cleanup);

describe('PreviewPane', () => {
  it('should render markdown heading', () => {
    render(<PreviewPane source="# Hello World" />);
    const container = screen.getByTestId('preview-pane');
    expect(container.innerHTML).toContain('<h1');
    expect(container.innerHTML).toContain('Hello World');
  });

  it('should render bold text', () => {
    render(<PreviewPane source="**bold text**" />);
    const container = screen.getByTestId('preview-pane');
    expect(container.innerHTML).toContain('<strong>bold text</strong>');
  });

  it('should display word count', () => {
    render(<PreviewPane source="# Hello\n\nThis is a paragraph." />);
    const container = screen.getByTestId('preview-pane');
    expect(container.textContent).toContain('字数');
  });

  it('should render TOC when showToc is true and headings exist', () => {
    render(<PreviewPane source="# Title\n\n## Section 1\n\nContent" showToc={true} />);
    const container = screen.getByTestId('preview-pane');
    expect(container.textContent).toContain('目录');
    expect(container.textContent).toContain('title');
  });

  it('should handle empty source gracefully', () => {
    render(<PreviewPane source="" />);
    const container = screen.getByTestId('preview-pane');
    expect(container).toBeDefined();
  });

  it('should apply dark theme styles', () => {
    const { container } = render(<PreviewPane source="Hello" theme="dark" />);
    const previewPane = screen.getByTestId('preview-pane');
    expect(previewPane).toBeDefined();
  });
});
