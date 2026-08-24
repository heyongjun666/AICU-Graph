// ============================================================================
// ArticleCard — Article card for grid display
// Shows: title, excerpt, published date, tag chips, status badge
// ============================================================================

import React from 'react';
import type { Article } from '../types';

interface ArticleCardProps {
  article: Article;
  onClick: (id: number) => void;
}

const DATE_OPTIONS: Intl.DateTimeFormatOptions = {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
};

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  return d.toLocaleDateString('zh-CN', DATE_OPTIONS);
}

export function ArticleCard({ article, onClick }: ArticleCardProps) {
  return (
    <article
      role="article"
      className="rounded-lg border border-gray-200 bg-white shadow-sm transition-shadow hover:shadow-md cursor-pointer flex flex-col"
      onClick={() => onClick(article.id)}
    >
      {/* Top section */}
      <div className="p-4 flex-1 flex flex-col">
        {/* Header: title + status badge */}
        <div className="flex items-start justify-between gap-2 mb-2">
          <h3 className="text-lg font-semibold text-gray-900 line-clamp-2 leading-snug">
            {article.title}
          </h3>
          {article.status === 'draft' && (
            <span className="shrink-0 inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-yellow-100 text-yellow-800">
              草稿
            </span>
          )}
        </div>

        {/* Excerpt */}
        <p className="text-sm text-gray-600 line-clamp-2 mb-3 leading-relaxed">
          {article.excerpt}
        </p>

        {/* Spacer */}
        <div className="flex-1" />

        {/* Date */}
        <time className="text-xs text-gray-400 mb-2 block">
          {formatDate(article.published_at ?? article.created_at)}
        </time>

        {/* Tag chips */}
        <div className="flex flex-wrap gap-1.5">
          {article.tags.map((tag) => (
            <span
              key={tag.id}
              className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-50 text-blue-700"
            >
              {tag.name}
            </span>
          ))}
        </div>
      </div>
    </article>
  );
}
