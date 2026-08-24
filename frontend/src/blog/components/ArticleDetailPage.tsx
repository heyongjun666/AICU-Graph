// ============================================================================
// ArticleDetailPage — Full article detail page with rendered Markdown content
// Fetches article by slug, renders with BrowserMarkdownRenderer
// ============================================================================

import React, { useState, useEffect, useCallback } from 'react';
import { fetchArticle } from '../api';
import { BrowserMarkdownRenderer } from '../../shared/markdown/browser-renderer';
import type { ArticleDetail } from '../types';

interface ArticleDetailPageProps {
  slug: string;
  onNavigate: (path: string) => void;
}

const renderer = new BrowserMarkdownRenderer();

type DetailState = 'loading' | 'error' | 'ready';

function DetailSkeleton() {
  return (
    <div data-testid="article-detail-skeleton" className="max-w-3xl mx-auto px-4 py-6 animate-pulse">
      <div className="h-8 bg-gray-200 rounded w-3/4 mb-4" />
      <div className="flex gap-2 mb-6">
        <div className="h-5 bg-gray-200 rounded-full w-16" />
        <div className="h-5 bg-gray-200 rounded-full w-20" />
      </div>
      <div className="space-y-3">
        <div className="h-4 bg-gray-200 rounded w-full" />
        <div className="h-4 bg-gray-200 rounded w-5/6" />
        <div className="h-4 bg-gray-200 rounded w-4/6" />
        <div className="h-4 bg-gray-200 rounded w-full" />
        <div className="h-4 bg-gray-200 rounded w-3/4" />
      </div>
    </div>
  );
}

function ErrorState({ onRetry, onBack }: { onRetry: () => void; onBack: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <svg className="w-16 h-16 text-red-300 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4.5c-.77-.833-2.694-.833-3.464 0L3.34 16.5c-.77.833.192 2.5 1.732 2.5z" />
      </svg>
      <h3 className="text-lg font-medium text-gray-900 mb-1">加载失败</h3>
      <p className="text-sm text-gray-500 mb-4">请检查网络连接后重试</p>
      <div className="flex gap-3">
        <button type="button" onClick={onBack} className="px-4 py-2 bg-white text-gray-700 text-sm font-medium rounded-lg border border-gray-300 hover:bg-gray-50 transition-colors">
          返回列表
        </button>
        <button type="button" onClick={onRetry} className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors">
          重试
        </button>
      </div>
    </div>
  );
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  return d.toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function ArticleDetailPage({ slug, onNavigate }: ArticleDetailPageProps) {
  const [state, setState] = useState<DetailState>('loading');
  const [article, setArticle] = useState<ArticleDetail | null>(null);
  const [retryCounter, setRetryCounter] = useState(0);

  const loadArticle = useCallback(async () => {
    setState('loading');
    try {
      const data = await fetchArticle(slug);
      setArticle(data);
      setState('ready');
    } catch {
      setState('error');
    }
  }, [slug]);

  useEffect(() => {
    loadArticle();
  }, [loadArticle, retryCounter]);

  const handleRetry = useCallback(() => {
    setRetryCounter((c) => c + 1);
  }, []);

  const handleBack = useCallback(() => {
    onNavigate('/');
  }, [onNavigate]);

  // Loading
  if (state === 'loading') {
    return <DetailSkeleton />;
  }

  // Error
  if (state === 'error') {
    return (
      <div className="max-w-3xl mx-auto px-4 py-6">
        <ErrorState onRetry={handleRetry} onBack={handleBack} />
      </div>
    );
  }

  // Ready
  if (!article) return null;

  const renderedContent = renderer.render(article.content, {
    highlightCode: true,
    sanitizeHtml: true,
    headingAnchors: true,
  });

  return (
    <div className="max-w-3xl mx-auto px-4 py-6">
      {/* Back button */}
      <button
        type="button"
        onClick={handleBack}
        className="inline-flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700 mb-4 transition-colors"
        aria-label="返回"
      >
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
        </svg>
        返回列表
      </button>

      {/* Article header */}
      <header className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900 mb-3 leading-tight">
          {article.title}
        </h1>

        {/* Meta info */}
        <div className="flex flex-wrap items-center gap-4 text-sm text-gray-500 mb-4">
          <time>{formatDate(article.published_at ?? article.created_at)}</time>
          <span className="text-gray-300">·</span>
          <span>字数 {article.word_count}</span>
          {article.status === 'draft' && (
            <>
              <span className="text-gray-300">·</span>
              <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-yellow-100 text-yellow-800">
                草稿
              </span>
            </>
          )}
        </div>

        {/* Tags */}
        <div className="flex flex-wrap gap-2">
          {article.tags.map((tag) => (
            <span
              key={tag.id}
              className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-700"
            >
              {tag.name}
            </span>
          ))}
        </div>
      </header>

      {/* Article content (rendered markdown) */}
      <article
        className="prose prose-slate max-w-none prose-headings:font-semibold prose-a:text-blue-600 prose-pre:bg-gray-900 prose-code:text-blue-700"
        data-testid="article-content"
        dangerouslySetInnerHTML={{ __html: renderedContent.html }}
      />
    </div>
  );
}
