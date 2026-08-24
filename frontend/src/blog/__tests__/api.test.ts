// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fetchArticleList, fetchTags, fetchArticle, fetchArticleById, createArticle, updateArticle, deleteArticle, setBaseUrl } from '../api';

// Regression: verify blog/api correctly re-exports runtime infra functions
import * as blogApi from '../api';
// Regression: verify infra/api exports runtime functions for blog to consume
import { setBaseUrl as infraSetBaseUrl, buildQuery, requestRaw } from '../../infra/api';

describe('module boundary — domain consumes infrastructure', () => {
  it('blog/api should re-export setBaseUrl from infra/api', () => {
    // setBaseUrl is a runtime function imported from infra/api and re-exported
    expect(typeof blogApi.setBaseUrl).toBe('function');
    // Re-export is the same function reference as the infra original
    expect(blogApi.setBaseUrl).toBe(infraSetBaseUrl);
  });

  it('infra/api should provide all HTTP utilities consumed by blog', () => {
    expect(typeof infraSetBaseUrl).toBe('function');
    expect(typeof buildQuery).toBe('function');
    expect(typeof requestRaw).toBe('function');
  });
});

describe('blog API client', () => {
  beforeEach(() => {
    setBaseUrl('http://test.local/api/v1');
    vi.restoreAllMocks();
  });

  describe('fetchArticleList', () => {
    it('should fetch articles with default params', async () => {
      const mockResponse = {
        code: 0,
        message: 'success',
        data: {
          total: 1,
          items: [
            {
              id: 1,
              title: 'Test Article',
              slug: 'test-article',
              excerpt: 'An excerpt',
              status: 'published',
              tags: [{ id: 1, name: 'Go', slug: 'go' }],
              created_at: '2026-07-28T10:00:00Z',
              updated_at: '2026-07-28T10:00:00Z',
              published_at: '2026-07-28T10:00:00Z',
            },
          ],
        },
      };
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await fetchArticleList({ page: 1, pageSize: 20 });

      expect(globalThis.fetch).toHaveBeenCalledWith(
        'http://test.local/api/v1/articles?page=1&page_size=20',
        expect.any(Object),
      );
      expect(result.total).toBe(1);
      expect(result.items).toHaveLength(1);
      expect(result.items[0].title).toBe('Test Article');
      expect(result.items[0].tags).toHaveLength(1);
    });

    it('should include tag_id and keyword when provided', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ code: 0, message: 'success', data: { total: 0, items: [] } }),
      });

      await fetchArticleList({ page: 1, pageSize: 20, tagId: 5, keyword: 'golang' });

      const calledUrl = globalThis.fetch.mock.calls[0][0];
      expect(calledUrl).toContain('tag_id=5');
      expect(calledUrl).toContain('keyword=golang');
    });

    it('should throw on API error code', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ code: 50000, message: 'server error', data: null }),
      });

      await expect(fetchArticleList({ page: 1 })).rejects.toThrow('server error');
    });

    it('should throw on HTTP error', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
      });

      await expect(fetchArticleList({ page: 1 })).rejects.toThrow();
    });
  });

  describe('fetchTags', () => {
    it('should fetch tag list', async () => {
      const mockResponse = {
        code: 0,
        message: 'success',
        data: {
          tags: [
            { id: 1, name: 'Go', slug: 'go', article_count: 15 },
            { id: 2, name: 'React', slug: 'react', article_count: 8 },
          ],
        },
      };
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await fetchTags();

      expect(globalThis.fetch).toHaveBeenCalledWith(
        'http://test.local/api/v1/tags',
        expect.any(Object),
      );
      expect(result).toHaveLength(2);
      expect(result[0].name).toBe('Go');
      expect(result[0].articleCount).toBe(15);
    });

    it('should throw on API error', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ code: 50000, message: 'tags error', data: null }),
      });

      await expect(fetchTags()).rejects.toThrow('tags error');
    });
  });

  describe('fetchArticle', () => {
    it('should fetch article detail by slug', async () => {
      const mockResponse = {
        code: 0,
        message: 'success',
        data: {
          article: {
            id: 1,
            title: 'Full Article',
            slug: 'full-article',
            excerpt: 'Excerpt',
            content: '# Hello\n\nThis is **markdown** content.',
            content_html: '<h1>Hello</h1><p>This is <strong>markdown</strong> content.</p>',
            word_count: 42,
            status: 'published',
            tags: [{ id: 1, name: 'Go', slug: 'go' }],
            created_at: '2026-07-28T10:00:00Z',
            updated_at: '2026-07-28T10:00:00Z',
            published_at: '2026-07-28T10:00:00Z',
          },
        },
      };
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await fetchArticle('full-article');

      expect(globalThis.fetch).toHaveBeenCalledWith(
        'http://test.local/api/v1/articles/full-article',
        expect.any(Object),
      );
      expect(result.title).toBe('Full Article');
      expect(result.content).toContain('**markdown**');
      expect(result.content_html).toContain('<strong>markdown</strong>');
      expect(result.word_count).toBe(42);
    });

    it('should throw on API error', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ code: 404, message: 'not found', data: null }),
      });

      await expect(fetchArticle('nonexistent')).rejects.toThrow('not found');
    });
  });

  describe('fetchArticleById', () => {
    it('should fetch article detail by numeric ID', async () => {
      const mockResponse = {
        code: 0,
        message: 'success',
        data: {
          article: {
            id: 1,
            title: 'Article By ID',
            slug: 'article-by-id',
            excerpt: 'ID fetch',
            content: '# Content by ID',
            content_html: '<h1>Content by ID</h1>',
            word_count: 10,
            status: 'published',
            tags: [],
            created_at: '2026-07-28T10:00:00Z',
            updated_at: '2026-07-28T10:00:00Z',
            published_at: '2026-07-28T10:00:00Z',
          },
        },
      };
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await fetchArticleById(42);

      expect(globalThis.fetch).toHaveBeenCalledWith(
        'http://test.local/api/v1/articles/detail/42',
        expect.any(Object),
      );
      expect(result.title).toBe('Article By ID');
      expect(result.content).toContain('# Content by ID');
    });

    it('should throw on API error for fetchArticleById', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ code: 404, message: 'article not found', data: null }),
      });

      await expect(fetchArticleById(999)).rejects.toThrow('article not found');
    });
  });

  describe('createArticle', () => {
    it('should create a new article', async () => {
      const mockResponse = {
        code: 0,
        message: 'success',
        data: {
          article: {
            id: 1,
            title: 'New Article',
            slug: 'new-article',
            excerpt: 'A new article',
            status: 'draft',
            tags: [],
            created_at: '2026-07-28T10:00:00Z',
            updated_at: '2026-07-28T10:00:00Z',
            published_at: null,
          },
        },
      };
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await createArticle({
        title: 'New Article',
        content: '# Content',
        status: 'draft',
      });

      expect(globalThis.fetch).toHaveBeenCalledWith(
        'http://test.local/api/v1/articles',
        expect.objectContaining({
          method: 'POST',
          body: expect.stringContaining('"title":"New Article"'),
        }),
      );
      expect(result.title).toBe('New Article');
      expect(result.status).toBe('draft');
    });

    it('should throw on API error', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ code: 400, message: 'bad request', data: null }),
      });

      await expect(createArticle({ title: '', content: '' })).rejects.toThrow('bad request');
    });
  });

  describe('updateArticle', () => {
    it('should update an existing article', async () => {
      const mockResponse = {
        code: 0,
        message: 'success',
        data: {
          article: {
            id: 1,
            title: 'Updated Title',
            slug: 'updated-title',
            excerpt: 'Updated',
            status: 'published',
            tags: [],
            created_at: '2026-07-28T10:00:00Z',
            updated_at: '2026-07-28T11:00:00Z',
            published_at: '2026-07-28T11:00:00Z',
          },
        },
      };
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await updateArticle(1, { title: 'Updated Title', status: 'published' });

      expect(globalThis.fetch).toHaveBeenCalledWith(
        'http://test.local/api/v1/articles/1',
        expect.objectContaining({ method: 'PUT' }),
      );
      expect(result.title).toBe('Updated Title');
      expect(result.status).toBe('published');
    });
  });

  describe('deleteArticle', () => {
    it('should delete an article', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ code: 0, message: 'success', data: null }),
      });

      await expect(deleteArticle(1)).resolves.toBeUndefined();

      expect(globalThis.fetch).toHaveBeenCalledWith(
        'http://test.local/api/v1/articles/1',
        expect.objectContaining({ method: 'DELETE' }),
      );
    });

    it('should throw on API error', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ code: 500, message: 'server error', data: null }),
      });

      await expect(deleteArticle(999)).rejects.toThrow('server error');
    });
  });
});
