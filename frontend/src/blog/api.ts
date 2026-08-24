// ============================================================================
// blog/api.ts — Blog API client
// Consumes shared HTTP infrastructure from frontend_infra (../infra)
// ============================================================================

import type { ApiResponse, PaginatedData } from '../infra/types';
import { setBaseUrl, buildQuery, requestRaw, requestJSON } from '../infra/api';
import type {
  Article,
  ArticleDetail,
  ArticleCreateInput,
  ArticleUpdateInput,
  ArticleListParams,
  Tag,
} from './types';

export { setBaseUrl };

// ---- Field mappers (API snake_case → frontend camelCase) ----

function mapTag(raw: any): Tag {
  return {
    id: raw.id,
    name: raw.name,
    slug: raw.slug,
    description: raw.description,
    articleCount: raw.article_count ?? 0,
    created_at: raw.created_at,
  };
}

function mapArticle(raw: any): Article {
  return {
    id: raw.id,
    title: raw.title,
    slug: raw.slug,
    excerpt: raw.excerpt ?? '',
    tags: raw.tags ?? [],
    status: raw.status,
    created_at: raw.created_at,
    updated_at: raw.updated_at,
    published_at: raw.published_at ?? null,
  };
}

// ---- Public API ----

/**
 * Fetch paginated article list.
 * GET /api/v1/articles?page=&page_size=&status=&tag_id=&keyword=
 */
export async function fetchArticleList(
  params: ArticleListParams,
): Promise<PaginatedData<Article>> {
  const query = buildQuery({
    page: params.page,
    page_size: params.pageSize ?? 20,
    status: params.status,
    tag_id: params.tagId,
    keyword: params.keyword,
  });
  const body = await requestRaw<PaginatedData<any>>('/articles', query);

  if (body.code !== 0) {
    throw new Error(body.message || 'unknown error');
  }

  return {
    total: body.data?.total ?? 0,
    items: (body.data?.items ?? []).map(mapArticle),
  };
}

function mapArticleDetail(raw: any): ArticleDetail {
  return {
    id: raw.id,
    title: raw.title,
    slug: raw.slug,
    excerpt: raw.excerpt ?? '',
    content: raw.content ?? '',
    content_html: raw.content_html ?? '',
    word_count: raw.word_count ?? 0,
    tags: (raw.tags ?? []).map(mapTag),
    status: raw.status,
    created_at: raw.created_at,
    updated_at: raw.updated_at,
    published_at: raw.published_at ?? null,
  };
}

/**
 * Fetch a single article by slug (with full content).
 * GET /api/v1/articles/:slug
 */
export async function fetchArticle(slug: string): Promise<ArticleDetail> {
  const body = await requestRaw<{ article: any }>(`/articles/${encodeURIComponent(slug)}`);

  if (body.code !== 0) {
    throw new Error(body.message || 'unknown error');
  }

  return mapArticleDetail(body.data?.article ?? body.data);
}

/**
 * Fetch a single article by numeric ID (with full content).
 * GET /api/v1/articles/detail/:id
 */
export async function fetchArticleById(id: number): Promise<ArticleDetail> {
  const body = await requestRaw<{ article: any }>(`/articles/detail/${id}`);

  if (body.code !== 0) {
    throw new Error(body.message || 'unknown error');
  }

  return mapArticleDetail(body.data?.article ?? body.data);
}

/**
 * Create a new article.
 * POST /api/v1/articles
 */
export async function createArticle(input: ArticleCreateInput): Promise<Article> {
  const body = await requestJSON<{ article: any }>('/articles', 'POST', {
    title: input.title,
    content: input.content,
    excerpt: input.excerpt,
    tags: input.tags,
    status: input.status,
  });

  if (body.code !== 0) {
    throw new Error(body.message || 'unknown error');
  }

  return mapArticle(body.data?.article ?? body.data);
}

/**
 * Update an existing article.
 * PUT /api/v1/articles/:id
 */
export async function updateArticle(id: number, input: ArticleUpdateInput): Promise<Article> {
  const body = await requestJSON<{ article: any }>(`/articles/${id}`, 'PUT', {
    title: input.title,
    content: input.content,
    excerpt: input.excerpt,
    tags: input.tags,
    status: input.status,
  });

  if (body.code !== 0) {
    throw new Error(body.message || 'unknown error');
  }

  return mapArticle(body.data?.article ?? body.data);
}

/**
 * Delete an article.
 * DELETE /api/v1/articles/:id
 */
export async function deleteArticle(id: number): Promise<void> {
  const body = await requestJSON<null>(`/articles/${id}`, 'DELETE');

  if (body.code !== 0) {
    throw new Error(body.message || 'unknown error');
  }
}

/**
 * Fetch all tags (sorted by article_count DESC).
 * GET /api/v1/tags
 */
export async function fetchTags(): Promise<Tag[]> {
  const body = await requestRaw<{ tags: any[] }>('/tags');

  if (body.code !== 0) {
    throw new Error(body.message || 'unknown error');
  }

  return (body.data?.tags ?? []).map(mapTag);
}

/**
 * Create a new tag.
 * POST /api/v1/tags
 */
export async function createTag(name: string): Promise<Tag> {
  const body = await requestJSON<{ tag: any }>('/tags', 'POST', { name });

  if (body.code !== 0) {
    throw new Error(body.message || 'unknown error');
  }

  return mapTag(body.data?.tag ?? body.data);
}

/**
 * Rename an existing tag.
 * PUT /api/v1/tags/:id
 */
export async function renameTag(id: number, name: string): Promise<Tag> {
  const body = await requestJSON<{ tag: any }>(`/tags/${id}`, 'PUT', { name });

  if (body.code !== 0) {
    throw new Error(body.message || 'unknown error');
  }

  return mapTag(body.data?.tag ?? body.data);
}

/**
 * Delete a tag.
 * DELETE /api/v1/tags/:id
 */
export async function deleteTag(id: number): Promise<void> {
  const body = await requestJSON<null>(`/tags/${id}`, 'DELETE');

  if (body.code !== 0) {
    throw new Error(body.message || 'unknown error');
  }
}
