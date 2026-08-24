// ============================================================================
// blog/stats/components/StatsDashboardPage.tsx — Reading statistics dashboard
// Composes: SummaryCards, ViewsTrendChart, TopArticlesTable, ArticleDetailStats
// Layout:
//   [Summary Cards Row]
//   [Views Trend Chart]
//   [Top Articles Table] | [Article Detail]
// ============================================================================

import React, { useState, useCallback } from 'react';
import { useStatsDashboard } from '../hooks/useStatsDashboard';
import { SummaryCards } from './SummaryCards';
import { ViewsTrendChart } from './ViewsTrendChart';
import { TopArticlesTable } from './TopArticlesTable';
import { ArticleDetailStats } from './ArticleDetailStats';
import { fetchArticleDetailStats } from '../api';
import type { ArticleDetailStats as ArticleDetailStatsType } from '../types';

interface StatsDashboardPageProps {
  onNavigate: (path: string) => void;
}

function DashboardSkeleton() {
  return (
    <div className="max-w-6xl mx-auto px-4 py-6" data-testid="stats-skeleton">
      <div className="h-8 bg-gray-200 rounded w-48 mb-6 animate-pulse" />
      {/* Summary cards skeleton */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        {Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="rounded-lg border border-gray-200 bg-white p-5 animate-pulse">
            <div className="h-4 bg-gray-200 rounded w-20 mb-3" />
            <div className="h-8 bg-gray-200 rounded w-24" />
          </div>
        ))}
      </div>
      {/* Chart skeleton */}
      <div className="rounded-lg border border-gray-200 bg-white p-5 mb-6 animate-pulse">
        <div className="h-5 bg-gray-200 rounded w-24 mb-4" />
        <div className="h-[220px] bg-gray-100 rounded" />
      </div>
      {/* Table skeleton */}
      <div className="rounded-lg border border-gray-200 bg-white animate-pulse">
        <div className="h-5 bg-gray-200 rounded w-40 m-5" />
        <div className="space-y-3 px-5 pb-5">
          {Array.from({ length: 5 }, (_, i) => (
            <div key={i} className="h-4 bg-gray-200 rounded w-full" />
          ))}
        </div>
      </div>
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

export function StatsDashboardPage({ onNavigate }: StatsDashboardPageProps) {
  const [state, actions] = useStatsDashboard();
  const [selectedArticle, setSelectedArticle] = useState<ArticleDetailStatsType | null>(null);
  const [articleLoading, setArticleLoading] = useState(false);

  const handleArticleClick = useCallback(
    async (id: number) => {
      setArticleLoading(true);
      try {
        const detail = await fetchArticleDetailStats(id);
        setSelectedArticle(detail);
      } catch {
        // If detail fetch fails, still try to navigate
        onNavigate(`/articles/${id}`);
      } finally {
        setArticleLoading(false);
      }
    },
    [onNavigate],
  );

  const handleBack = useCallback(() => {
    setSelectedArticle(null);
  }, []);

  // --- Loading state (initial load only — not on period change) ---
  if (state.pageState === 'loading' && !state.summary && state.topArticles.length === 0) {
    return <DashboardSkeleton />;
  }

  // --- Error state ---
  if (state.pageState === 'error') {
    return (
      <div className="max-w-6xl mx-auto px-4 py-6">
        <h1 className="text-2xl font-bold text-gray-900 mb-6">阅读统计</h1>
        <ErrorState onRetry={actions.retry} />
      </div>
    );
  }

  // --- Ready state ---
  return (
    <div className="max-w-6xl mx-auto px-4 py-6">
      {/* Title */}
      <h1 className="text-2xl font-bold text-gray-900 mb-6">阅读统计</h1>

      {/* Summary cards */}
      {state.summary && (
        <div className="mb-6">
          <SummaryCards summary={state.summary} />
        </div>
      )}

      {/* Views trend chart */}
      {state.viewsTrend && (
        <div className="mb-6">
          <ViewsTrendChart
            data={state.viewsTrend[state.selectedPeriod]}
            period={state.selectedPeriod}
            onPeriodChange={actions.setPeriod}
          />
        </div>
      )}

      {/* Top articles + Article detail (side-by-side on large screens) */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <TopArticlesTable
            articles={state.topArticles}
            onArticleClick={handleArticleClick}
          />
        </div>
        <div>
          {articleLoading ? (
            <div className="rounded-lg border border-gray-200 bg-white p-5 animate-pulse">
              <div className="h-5 bg-gray-200 rounded w-32 mb-4" />
              <div className="space-y-3">
                <div className="h-4 bg-gray-200 rounded w-full" />
                <div className="h-4 bg-gray-200 rounded w-3/4" />
                <div className="h-4 bg-gray-200 rounded w-1/2" />
              </div>
            </div>
          ) : selectedArticle ? (
            <ArticleDetailStats stats={selectedArticle} onBack={handleBack} />
          ) : (
            <div className="rounded-lg border border-gray-200 bg-white p-5 flex items-center justify-center h-48">
              <p className="text-sm text-gray-400">点击文章查看详情</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
