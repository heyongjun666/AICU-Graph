// ============================================================================
// ArticleListPage — Main article list page container
// Composes: SearchBar, CategoryTabs, ArticleCardGrid, Pagination, Skeleton, EmptyState, ErrorState
// Implements the prototype layout:
//   [SearchBar]
//   [CategoryTabs]
//   [ArticleCardGrid]
//   [Pagination]
// ============================================================================

import React, { useCallback } from 'react';
import { useArticleList } from './useArticleList';
import type { ArticleListState, ArticleListActions } from './useArticleList';
import { ArticleCardGrid } from './ArticleCardGrid';
import { CategoryTabs } from './CategoryTabs';
import { SearchBar } from './SearchBar';
import { Pagination } from './Pagination';
import { ArticleListSkeleton } from '../../infra/components/Skeleton';

interface ArticleListPageProps {
  onNavigate: (path: string) => void;
  /** Optional external state/actions — when provided, bypasses internal useArticleList hook */
  externalState?: ArticleListState;
  externalActions?: ArticleListActions;
  /** When true, hides CategoryTabs (e.g. when a sidebar provides equivalent filtering) */
  hideTagTabs?: boolean;
}

function TagLoadWarning() {
  return (
    <div className="mb-4 px-4 py-2 bg-yellow-50 border border-yellow-200 rounded-lg text-sm text-yellow-800">
      标签加载失败，标签筛选功能暂不可用。
      <span className="ml-2 text-yellow-600 underline cursor-pointer">重试</span>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <svg
        className="w-16 h-16 text-gray-300 mb-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={1}
          d="M19 20H5a2 2 0 01-2-2V6a2 2 0 012-2h10a2 2 0 012 2v1m2 13a2 2 0 01-2-2V7m2 13a2 2 0 002-2V9a2 2 0 00-2-2h-2m-4-3H9M7 16h6M7 8h6v4H7V8z"
        />
      </svg>
      <h3 className="text-lg font-medium text-gray-900 mb-1">还没有文章</h3>
      <p className="text-sm text-gray-500">快去写第一篇吧</p>
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <svg
        className="w-16 h-16 text-red-300 mb-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={1}
          d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4.5c-.77-.833-2.694-.833-3.464 0L3.34 16.5c-.77.833.192 2.5 1.732 2.5z"
        />
      </svg>
      <h3 className="text-lg font-medium text-gray-900 mb-1">加载失败</h3>
      <p className="text-sm text-gray-500 mb-4">请检查网络连接后重试</p>
      <button
        type="button"
        onClick={onRetry}
        className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
      >
        重试
      </button>
    </div>
  );
}

function getPageCount(total: number, pageSize: number): number {
  return Math.ceil(total / pageSize);
}

const PAGE_SIZE = 20;

export function ArticleListPage({ onNavigate, externalState, externalActions, hideTagTabs }: ArticleListPageProps) {
  const internalState = useArticleList();
  const state = externalState ?? internalState[0];
  const actions = externalActions ?? internalState[1];

  const handleArticleClick = useCallback(
    (id: number) => {
      const article = state.articles.find((a) => a.id === id);
      if (article) {
        onNavigate(`/articles/${article.slug}`);
      }
    },
    [state.articles, onNavigate],
  );

  const pageCount = getPageCount(state.total, PAGE_SIZE);

  return (
    <div className="max-w-6xl mx-auto px-4 py-6">
      {/* Title */}
      <h1 className="text-2xl font-bold text-gray-900 mb-6">文章列表</h1>

      {/* Search bar */}
      <div className="mb-4">
        <SearchBar
          onSearch={actions.setKeyword}
          initialValue={state.keyword}
        />
      </div>

      {/* Tag load warning */}
      {state.tagsError && (
        <div className="mb-4">
          <TagLoadWarning />
        </div>
      )}

      {/* Category tabs — hidden when TagSidebar is used */}
      {!hideTagTabs && (
        <div className="mb-6">
          <CategoryTabs
            tags={state.tags}
            activeSlug={state.activeTagSlug}
            onChange={actions.setTagFilter}
          />
        </div>
      )}

      {/* Content area: show skeleton only on initial load (no articles at all) */}
      {state.pageState === 'loading' && state.articles.length === 0 && (
        <ArticleListSkeleton count={6} />
      )}

      {state.pageState === 'error' && state.articles.length === 0 && (
        <ErrorState onRetry={actions.retry} />
      )}

      {state.pageState === 'empty' && <EmptyState />}

      {(state.pageState === 'ready' || state.articles.length > 0) && (
        <>
          <ArticleCardGrid
            articles={state.articles}
            onArticleClick={handleArticleClick}
          />
          {pageCount > 1 && (
            <div className="mt-8">
              <Pagination
                current={state.currentPage}
                total={pageCount}
                onChange={actions.setPage}
              />
            </div>
          )}
        </>
      )}
    </div>
  );
}
