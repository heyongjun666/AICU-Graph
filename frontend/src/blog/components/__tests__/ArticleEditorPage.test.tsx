// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react';
import React from 'react';
import { ArticleEditorPage } from '../ArticleEditorPage';
import * as api from '../../api';

afterEach(cleanup);

vi.mock('../../api', () => ({
  fetchArticle: vi.fn(),
  fetchArticleById: vi.fn(),
  createArticle: vi.fn(),
  updateArticle: vi.fn(),
  fetchTags: vi.fn(),
}));

const mockTags = [
  { id: 1, name: 'Go', slug: 'go', articleCount: 15 },
  { id: 2, name: 'React', slug: 'react', articleCount: 8 },
];

describe('ArticleEditorPage — create mode', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);
  });

  it('should render empty form in create mode', async () => {
    render(<ArticleEditorPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByPlaceholderText('输入文章标题')).toBeDefined();
    });

    expect(screen.getByText('新建文章')).toBeDefined();
    expect(screen.getByText('发布')).toBeDefined();
    expect(screen.getByText('存为草稿')).toBeDefined();
  });

  it('should call createArticle with draft status on save draft', async () => {
    vi.mocked(api.createArticle).mockResolvedValue({
      id: 1, title: 'Test', slug: 'test', excerpt: '',
      tags: [], status: 'draft',
      created_at: '', updated_at: '', published_at: null,
    });
    const handleNavigate = vi.fn();

    render(<ArticleEditorPage onNavigate={handleNavigate} />);

    await waitFor(() => {
      expect(screen.getByPlaceholderText('输入文章标题')).toBeDefined();
    });

    const titleInput = screen.getByPlaceholderText('输入文章标题');
    fireEvent.change(titleInput, { target: { value: 'Test Article' } });

    fireEvent.click(screen.getByText('存为草稿'));

    await waitFor(() => {
      expect(vi.mocked(api.createArticle)).toHaveBeenCalledWith(
        expect.objectContaining({
          title: 'Test Article',
          status: 'draft',
        }),
      );
    });

    expect(handleNavigate).toHaveBeenCalled();
  });

  it('should call createArticle with published status on publish', async () => {
    vi.mocked(api.createArticle).mockResolvedValue({
      id: 1, title: 'Test', slug: 'test', excerpt: '',
      tags: [], status: 'published',
      created_at: '', updated_at: '', published_at: new Date().toISOString(),
    });
    const handleNavigate = vi.fn();

    render(<ArticleEditorPage onNavigate={handleNavigate} />);

    await waitFor(() => {
      expect(screen.getByPlaceholderText('输入文章标题')).toBeDefined();
    });

    const titleInput = screen.getByPlaceholderText('输入文章标题');
    fireEvent.change(titleInput, { target: { value: 'Test Article' } });

    fireEvent.click(screen.getByText('发布'));

    await waitFor(() => {
      expect(vi.mocked(api.createArticle)).toHaveBeenCalledWith(
        expect.objectContaining({
          title: 'Test Article',
          status: 'published',
        }),
      );
    });
  });
});

describe('ArticleEditorPage — edit mode', () => {
  const mockExistingArticle = {
    id: 1,
    title: 'Existing Article',
    slug: 'existing-article',
    excerpt: 'Existing',
    content: '# Existing Content',
    content_html: '<h1>Existing Content</h1>',
    word_count: 10,
    status: 'draft',
    tags: [mockTags[0]],
    created_at: '2026-07-28T10:00:00Z',
    updated_at: '2026-07-28T10:00:00Z',
    published_at: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);
    vi.mocked(api.fetchArticleById).mockResolvedValue(mockExistingArticle);
  });

  it('should load existing article in edit mode', async () => {
    render(<ArticleEditorPage articleId={1} onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByDisplayValue('Existing Article')).toBeDefined();
    });
  });

  it('should call updateArticle on save', async () => {
    vi.mocked(api.updateArticle).mockResolvedValue({
      id: 1,
      title: 'Updated',
      slug: 'existing-article',
      excerpt: '',
      tags: [],
      status: 'published',
      created_at: '2026-07-28T10:00:00Z',
      updated_at: '2026-07-28T11:00:00Z',
      published_at: '2026-07-28T11:00:00Z',
    });
    const handleNavigate = vi.fn();

    render(<ArticleEditorPage articleId={1} onNavigate={handleNavigate} />);

    await waitFor(() => {
      expect(screen.getByDisplayValue('Existing Article')).toBeDefined();
    });

    const titleInput = screen.getByDisplayValue('Existing Article');
    fireEvent.change(titleInput, { target: { value: 'Updated Article' } });

    fireEvent.click(screen.getByText('发布'));

    await waitFor(() => {
      expect(vi.mocked(api.updateArticle)).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ title: 'Updated Article', status: 'published' }),
      );
    });
  });

  it('should show error state when article fetch fails', async () => {
    vi.mocked(api.fetchArticleById).mockRejectedValue(new Error('Not found'));

    render(<ArticleEditorPage articleId={999} onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('加载失败')).toBeDefined();
    });
  });
});
