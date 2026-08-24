// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react';
import React from 'react';
import { BlogApp } from '../BlogApp';
import * as api from '../../api';

afterEach(cleanup);

// Mock all API modules used by blog pages
vi.mock('../../api', () => ({
  fetchArticleList: vi.fn(),
  fetchArticle: vi.fn(),
  fetchTags: vi.fn(),
  createArticle: vi.fn(),
  updateArticle: vi.fn(),
  deleteArticle: vi.fn(),
  setBaseUrl: vi.fn(),
}));

describe('BlogApp', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchTags).mockResolvedValue([]);
    vi.mocked(api.fetchArticleList).mockResolvedValue({ total: 0, items: [] });
  });

  it('should render navigation bar', () => {
    render(<BlogApp />);

    expect(screen.getByText('My Blog')).toBeDefined();
    expect(screen.getByText('文章')).toBeDefined();
    expect(screen.getByText('标签')).toBeDefined();
    expect(screen.getByText('统计')).toBeDefined();
    expect(screen.getByText('写文章')).toBeDefined();
  });

  it('should navigate to article list on "My Blog" click', async () => {
    render(<BlogApp />);

    fireEvent.click(screen.getByText('My Blog'));

    await waitFor(() => {
      expect(screen.getByText('文章列表')).toBeDefined();
    });
  });

  it('should navigate to tag management page', async () => {
    render(<BlogApp />);

    fireEvent.click(screen.getByText('标签'));

    await waitFor(() => {
      expect(screen.getByText('标签管理')).toBeDefined();
    });
  });

  it('should navigate to stats page', async () => {
    render(<BlogApp />);

    fireEvent.click(screen.getByText('统计'));

    await waitFor(() => {
      expect(screen.getByText('阅读统计')).toBeDefined();
    });
  });

  it('should navigate to article editor on "写文章" click', async () => {
    render(<BlogApp />);

    fireEvent.click(screen.getByText('写文章'));

    await waitFor(() => {
      expect(screen.getByText('新建文章')).toBeDefined();
    });
  });

  it('should render TagSidebar on the list page', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue([
      { id: 1, name: 'Go', slug: 'go', articleCount: 5 },
    ]);

    render(<BlogApp />);

    await waitFor(() => {
      expect(screen.getByText('标签筛选')).toBeDefined();
      expect(screen.getByText('全部文章')).toBeDefined();
      expect(screen.getByText('Go')).toBeDefined();
    });
  });

  it('should hide CategoryTabs when TagSidebar is present', async () => {
    render(<BlogApp />);

    await waitFor(() => {
      // TagSidebar is rendered (shows "全部文章")
      expect(screen.getByText('全部文章')).toBeDefined();
      // CategoryTabs is hidden — the "全部" tab from CategoryTabs should NOT be in DOM
      expect(screen.queryByText((content) => content === '全部')).toBeNull();
    });
  });

  it('should filter articles when TagSidebar tag is clicked', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue([
      { id: 1, name: 'Go', slug: 'go', articleCount: 5 },
    ]);

    render(<BlogApp />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    // Clicking "Go" tag in TagSidebar should trigger fetchArticleList with tag_id=1
    fireEvent.click(screen.getByText('Go'));

    await waitFor(() => {
      // fetchArticleList should be called with tagId=1
      const calls = vi.mocked(api.fetchArticleList).mock.calls;
      const tagCall = calls.find(([params]) => params.tagId === 1);
      expect(tagCall).toBeDefined();
    });
  });
});
