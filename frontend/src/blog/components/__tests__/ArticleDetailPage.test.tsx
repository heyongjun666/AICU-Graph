// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup, waitFor } from '@testing-library/react';
import React from 'react';
import { ArticleDetailPage } from '../ArticleDetailPage';
import * as api from '../../api';

afterEach(cleanup);

vi.mock('../../api', () => ({
  fetchArticle: vi.fn(),
  fetchArticleList: vi.fn(),
  fetchTags: vi.fn(),
}));

const mockArticleDetail = {
  id: 1,
  title: 'Test Article',
  slug: 'test-article',
  excerpt: 'An excerpt',
  content: '# Hello\n\nThis is **markdown** content.\n\n```js\nconst x = 1;\n```',
  content_html: '<h1>Hello</h1><p>This is <strong>markdown</strong> content.</p><pre><code class="hljs language-js">const x = 1;</code></pre>',
  word_count: 42,
  status: 'published',
  tags: [
    { id: 1, name: 'Go', slug: 'go' },
    { id: 2, name: 'React', slug: 'react' },
  ],
  created_at: '2026-07-28T10:00:00Z',
  updated_at: '2026-07-28T10:00:00Z',
  published_at: '2026-07-28T10:00:00Z',
};

describe('ArticleDetailPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should show loading state initially', () => {
    vi.mocked(api.fetchArticle).mockReturnValue(new Promise(() => {}));

    render(<ArticleDetailPage slug="test-article" onNavigate={() => {}} />);

    expect(screen.getByTestId('article-detail-skeleton')).toBeDefined();
  });

  it('should render article content when loaded', async () => {
    vi.mocked(api.fetchArticle).mockResolvedValue(mockArticleDetail);

    render(<ArticleDetailPage slug="test-article" onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Test Article')).toBeDefined();
    });

    expect(screen.getByText('Go')).toBeDefined();
    expect(screen.getByText('React')).toBeDefined();
    expect(screen.getByText((content) => content.includes('42'))).toBeDefined();
  });

  it('should render markdown content as HTML', async () => {
    vi.mocked(api.fetchArticle).mockResolvedValue(mockArticleDetail);

    render(<ArticleDetailPage slug="test-article" onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByTestId('article-content')).toBeDefined();
    });

    const content = screen.getByTestId('article-content');
    expect(content.innerHTML).toContain('<h1 id="hello">Hello</h1>');
    expect(content.innerHTML).toContain('<strong>markdown</strong>');
  });

  it('should show error state when fetch fails', async () => {
    vi.mocked(api.fetchArticle).mockRejectedValue(new Error('Not found'));

    render(<ArticleDetailPage slug="nonexistent" onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('加载失败')).toBeDefined();
    });
  });

  it('should call onNavigate back when back button is clicked', async () => {
    vi.mocked(api.fetchArticle).mockResolvedValue(mockArticleDetail);
    const handleNavigate = vi.fn();

    render(<ArticleDetailPage slug="test-article" onNavigate={handleNavigate} />);

    await waitFor(() => {
      expect(screen.getByText('Test Article')).toBeDefined();
    });

    const backButton = screen.getByLabelText('返回');
    backButton.click();
    expect(handleNavigate).toHaveBeenCalledWith('/');
  });
});
