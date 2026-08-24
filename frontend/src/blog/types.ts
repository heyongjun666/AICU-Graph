// ============================================================================
// blog/types.ts — Blog platform type definitions
// Domain types consume shared infrastructure types from ../infra
// ============================================================================

/** Article tag reference */
export interface TagRef {
  id: number;
  name: string;
  slug: string;
}

/** Article model (list view — no content/content_html) */
export interface Article {
  id: number;
  title: string;
  slug: string;
  excerpt: string;
  tags: TagRef[];
  status: 'draft' | 'published' | 'deleted';
  created_at: string;
  updated_at: string;
  published_at: string | null;
}

/** Tag model with article count */
export interface Tag {
  id: number;
  name: string;
  slug: string;
  description?: string;
  articleCount: number;
  created_at?: string;
}

/** Article list query parameters */
export interface ArticleListParams {
  page: number;
  pageSize?: number;
  status?: string;
  tagId?: number;
  keyword?: string;
}

/** Article detail (list view fields + full content) */
export interface ArticleDetail extends Article {
  content: string;
  content_html: string;
  word_count: number;
}

/** Input for creating a new article */
export interface ArticleCreateInput {
  title: string;
  content: string;
  excerpt?: string;
  tags?: string[];
  status?: 'draft' | 'published';
}

/** Input for updating an existing article */
export interface ArticleUpdateInput {
  title?: string;
  content?: string;
  excerpt?: string;
  tags?: string[];
  status?: 'draft' | 'published' | 'deleted';
}

/** Article list page state */
export type PageState = 'loading' | 'error' | 'empty' | 'ready';
