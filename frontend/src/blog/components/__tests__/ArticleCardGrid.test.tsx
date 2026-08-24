// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import React from 'react';
import { ArticleCardGrid } from '../ArticleCardGrid';
import type { Article } from '../types';

afterEach(cleanup);

const mockArticles: Article[] = [
  {
    id: 1,
    title: 'Article 1',
    slug: 'article-1',
    excerpt: 'Excerpt 1',
    status: 'published',
    tags: [{ id: 1, name: 'Go', slug: 'go' }],
    created_at: '2026-07-28T10:00:00Z',
    updated_at: '2026-07-28T10:00:00Z',
    published_at: '2026-07-28T10:00:00Z',
  },
  {
    id: 2,
    title: 'Article 2',
    slug: 'article-2',
    excerpt: 'Excerpt 2',
    status: 'published',
    tags: [],
    created_at: '2026-07-27T10:00:00Z',
    updated_at: '2026-07-27T10:00:00Z',
    published_at: '2026-07-27T10:00:00Z',
  },
  {
    id: 3,
    title: 'Article 3',
    slug: 'article-3',
    excerpt: 'Excerpt 3',
    status: 'draft',
    tags: [],
    created_at: '2026-07-26T10:00:00Z',
    updated_at: '2026-07-26T10:00:00Z',
    published_at: null,
  },
];

describe('ArticleCardGrid', () => {
  it('should render the correct number of article cards', () => {
    render(<ArticleCardGrid articles={mockArticles} onArticleClick={() => {}} />);
    const cards = screen.getAllByRole('article');
    expect(cards).toHaveLength(3);
  });

  it('should render article titles', () => {
    render(<ArticleCardGrid articles={mockArticles} onArticleClick={() => {}} />);
    expect(screen.getByText('Article 1')).toBeDefined();
    expect(screen.getByText('Article 2')).toBeDefined();
    expect(screen.getByText('Article 3')).toBeDefined();
  });

  it('should render empty grid when articles is empty', () => {
    const { container } = render(<ArticleCardGrid articles={[]} onArticleClick={() => {}} />);
    // Should render the grid container but no cards
    const cards = screen.queryAllByRole('article');
    expect(cards).toHaveLength(0);
    const gridEl = container.firstChild as HTMLElement;
    expect(gridEl.className).toContain('grid');
  });
});
