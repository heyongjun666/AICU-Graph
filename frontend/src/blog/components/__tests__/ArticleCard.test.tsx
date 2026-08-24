// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import React from 'react';
import { ArticleCard } from '../ArticleCard';
import type { Article } from '../types';

afterEach(cleanup);

const mockArticle: Article = {
  id: 1,
  title: '深入理解 Go 并发模型',
  slug: 'understanding-go-concurrency',
  excerpt: '深入理解 Go 的 goroutine 和 channel 机制，以及如何利用它们构建高效的并发程序。',
  status: 'published',
  tags: [
    { id: 1, name: 'Go', slug: 'go' },
    { id: 2, name: '并发编程', slug: 'concurrent-programming' },
  ],
  created_at: '2026-07-28T10:00:00Z',
  updated_at: '2026-07-28T10:00:00Z',
  published_at: '2026-07-28T10:00:00Z',
};

describe('ArticleCard', () => {
  it('should render article title', () => {
    render(<ArticleCard article={mockArticle} onClick={() => {}} />);
    expect(screen.getByText('深入理解 Go 并发模型')).toBeDefined();
  });

  it('should render article excerpt', () => {
    render(<ArticleCard article={mockArticle} onClick={() => {}} />);
    expect(
      screen.getByText(/深入理解 Go 的 goroutine 和 channel 机制/),
    ).toBeDefined();
  });

  it('should render published date formatted as YYYY/MM/DD', () => {
    render(<ArticleCard article={mockArticle} onClick={() => {}} />);
    expect(screen.getByText('2026/07/28')).toBeDefined();
  });

  it('should render tag chips', () => {
    render(<ArticleCard article={mockArticle} onClick={() => {}} />);
    expect(screen.getByText('Go')).toBeDefined();
    expect(screen.getByText('并发编程')).toBeDefined();
  });

  it('should call onClick with article id when clicked', () => {
    const handleClick = vi.fn();
    render(<ArticleCard article={mockArticle} onClick={handleClick} />);
    fireEvent.click(screen.getByRole('article'));
    expect(handleClick).toHaveBeenCalledWith(1);
  });

  it('should render status badge for draft articles', () => {
    const draftArticle: Article = { ...mockArticle, status: 'draft', published_at: null };
    render(<ArticleCard article={draftArticle} onClick={() => {}} />);
    expect(screen.getByText('草稿')).toBeDefined();
  });
});
