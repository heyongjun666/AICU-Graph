// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import React from 'react';
import { ArticleListPage } from '../ArticleListPage';
import * as api from '../../api';

afterEach(cleanup);

// Mock the api module
vi.mock('../../api', () => ({
  fetchArticleList: vi.fn(),
  fetchTags: vi.fn(),
}));

const mockArticles = {
  total: 2,
  items: [
    {
      id: 1,
      title: 'Article One',
      slug: 'article-one',
      excerpt: 'First article excerpt',
      status: 'published' as const,
      tags: [{ id: 1, name: 'Go', slug: 'go' }],
      created_at: '2026-07-28T10:00:00Z',
      updated_at: '2026-07-28T10:00:00Z',
      published_at: '2026-07-28T10:00:00Z',
    },
    {
      id: 2,
      title: 'Article Two',
      slug: 'article-two',
      excerpt: 'Second article excerpt',
      status: 'published' as const,
      tags: [{ id: 2, name: 'React', slug: 'react' }],
      created_at: '2026-07-27T10:00:00Z',
      updated_at: '2026-07-27T10:00:00Z',
      published_at: '2026-07-27T10:00:00Z',
    },
  ],
};

const mockTags = [
  { id: 1, name: 'Go', slug: 'go', articleCount: 15 },
  { id: 2, name: 'React', slug: 'react', articleCount: 8 },
];

describe('ArticleListPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // --- Loading state (Slice 7) ---
  it('should show skeleton loading state while fetching', () => {
    vi.mocked(api.fetchArticleList).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<ArticleListPage onNavigate={() => {}} />);

    expect(screen.getByTestId('article-list-skeleton')).toBeDefined();
    expect(screen.queryByText('还没有文章')).toBeNull();
  });

  // --- Data state (Slice 8) ---
  it('should render article list when data is loaded', async () => {
    vi.mocked(api.fetchArticleList).mockResolvedValue(mockArticles);
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<ArticleListPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Article One')).toBeDefined();
    });

    expect(screen.getByText('Article Two')).toBeDefined();
    expect(screen.getByText('全部')).toBeDefined();
    expect(screen.getByPlaceholderText('搜索文章标题...')).toBeDefined();
  });

  // --- Tag filtering ---
  it('should re-fetch articles when a tag is selected', async () => {
    vi.mocked(api.fetchArticleList).mockResolvedValue(mockArticles);
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<ArticleListPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Article One')).toBeDefined();
    });

    // Click on "Go" category tab — use getAllByText since it appears in both tabs and cards
    fireEvent.click(screen.getAllByText('Go')[0]);

    expect(vi.mocked(api.fetchArticleList)).toHaveBeenCalledWith(
      expect.objectContaining({ tagId: 1, page: 1 }),
    );
  });

  // --- Empty state (Slice 9) ---
  it('should show empty state when no articles', async () => {
    vi.mocked(api.fetchArticleList).mockResolvedValue({ total: 0, items: [] });
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<ArticleListPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('还没有文章')).toBeDefined();
    });
    expect(screen.getByText('快去写第一篇吧')).toBeDefined();
  });

  // --- Error state (Slice 10) ---
  it('should show error state when fetch fails', async () => {
    vi.mocked(api.fetchArticleList).mockRejectedValue(new Error('Network error'));
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<ArticleListPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('加载失败')).toBeDefined();
    });
    expect(screen.getByText('重试')).toBeDefined();
  });

  it('should retry fetching when retry button is clicked', async () => {
    vi.mocked(api.fetchArticleList)
      .mockRejectedValueOnce(new Error('Network error'))
      .mockRejectedValueOnce(new Error('Network error'))
      .mockResolvedValueOnce(mockArticles);
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<ArticleListPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('加载失败')).toBeDefined();
    });

    fireEvent.click(screen.getByText('重试'));

    await waitFor(() => {
      expect(screen.getByText('Article One')).toBeDefined();
    });

    expect(vi.mocked(api.fetchArticleList)).toHaveBeenCalledTimes(3);
  });

  // --- Navigation ---
  it('should call onNavigate when an article card is clicked', async () => {
    vi.mocked(api.fetchArticleList).mockResolvedValue(mockArticles);
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);
    const handleNavigate = vi.fn();

    render(<ArticleListPage onNavigate={handleNavigate} />);

    await waitFor(() => {
      expect(screen.getByText('Article One')).toBeDefined();
    });

    fireEvent.click(screen.getAllByRole('article')[0]);
    expect(handleNavigate).toHaveBeenCalledWith('/articles/article-one');
  });

  // --- Pagination ---
  it('should show pagination when total > page size', async () => {
    const manyArticles = {
      total: 25,
      items: Array.from({ length: 20 }, (_, i) => ({
        id: i + 1,
        title: `Article ${i + 1}`,
        slug: `article-${i + 1}`,
        excerpt: `Excerpt ${i + 1}`,
        status: 'published' as const,
        tags: [] as { id: number; name: string; slug: string }[],
        created_at: '2026-07-28T10:00:00Z',
        updated_at: '2026-07-28T10:00:00Z',
        published_at: '2026-07-28T10:00:00Z',
      })),
    };

    vi.mocked(api.fetchArticleList).mockResolvedValue(manyArticles);
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<ArticleListPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Article 20')).toBeDefined();
    });

    expect(screen.getByText('2')).toBeDefined();
  });
});
