// ============================================================================
// useArticleList — Hook for fetching article list data with filtering/pagination
// ============================================================================

import { useState, useEffect, useCallback, useRef } from 'react';
import { fetchArticleList, fetchTags } from '../api';
import type { Article, Tag } from '../types';

export interface ArticleListState {
  pageState: 'loading' | 'error' | 'empty' | 'ready';
  articles: Article[];
  tags: Tag[];
  total: number;
  currentPage: number;
  activeTagSlug: string | null;
  keyword: string;
  /** Whether tag fetching failed. UI can show a warning but need not block */
  tagsError: boolean;
}

export interface ArticleListActions {
  setPage: (page: number) => void;
  setTagFilter: (slug: string | null) => void;
  setKeyword: (keyword: string) => void;
  retry: () => void;
}

export function useArticleList(): [ArticleListState, ArticleListActions] {
  const [pageState, setPageState] = useState<ArticleListState['pageState']>('loading');
  const [articles, setArticles] = useState<Article[]>([]);
  const [tags, setTags] = useState<Tag[]>([]);
  const [tagsError, setTagsError] = useState(false);
  const [total, setTotal] = useState(0);
  const [currentPage, setCurrentPage] = useState(1);
  const [activeTagSlug, setActiveTagSlug] = useState<string | null>(null);
  const [keyword, setKeyword] = useState('');
  const [retryCounter, setRetryCounter] = useState(0);
  const hasData = useRef(false);

  // Load tags — re-fetches on explicit retry
  useEffect(() => {
    let cancelled = false;
    setTagsError(false);

    fetchTags()
      .then((data) => {
        if (cancelled) return;
        setTags(data);
      })
      .catch(() => {
        if (cancelled) return;
        setTagsError(true);
        console.warn('[useArticleList] Failed to load tags — tag filtering unavailable');
      });

    return () => {
      cancelled = true;
    };
  }, [retryCounter]);

  // Load articles
  useEffect(() => {
    let cancelled = false;

    // Only show full skeleton on initial load (no data yet)
    // On re-fetches (pagination, filter, search), keep existing content visible
    if (!hasData.current) {
      setPageState('loading');
    }

    const activeTagId = activeTagSlug
      ? tags.find((t) => t.slug === activeTagSlug)?.id
      : undefined;

    fetchArticleList({
      page: currentPage,
      pageSize: 20,
      status: 'published',
      tagId: activeTagId,
      keyword: keyword || undefined,
    })
      .then((data) => {
        if (cancelled) return;
        hasData.current = data.items.length > 0;
        setArticles(data.items);
        setTotal(data.total);
        setPageState(
          data.items.length === 0 && !hasData.current ? 'empty' : 'ready',
        );
      })
      .catch(() => {
        if (cancelled) return;
        // On re-fetch failure: if we already have data, keep showing it
        // Only show error state if we have nothing to show
        if (!hasData.current) {
          setPageState('error');
        }
      });

    return () => {
      cancelled = true;
    };
  }, [currentPage, activeTagSlug, keyword, retryCounter, tags]);

  const handleSetPage = useCallback((page: number) => {
    setCurrentPage(page);
  }, []);

  const handleSetTagFilter = useCallback((slug: string | null) => {
    setActiveTagSlug(slug);
    setCurrentPage(1);
  }, []);

  const handleSetKeyword = useCallback((kw: string) => {
    setKeyword(kw);
    setCurrentPage(1);
  }, []);

  const handleRetry = useCallback(() => {
    setRetryCounter((c) => c + 1);
  }, []);

  return [
    { pageState, articles, tags, total, currentPage, activeTagSlug, keyword, tagsError },
    {
      setPage: handleSetPage,
      setTagFilter: handleSetTagFilter,
      setKeyword: handleSetKeyword,
      retry: handleRetry,
    },
  ];
}
