// ============================================================================
// ArticleCardGrid — Responsive grid of article cards
// Uses CSS Grid with responsive columns: 1 col mobile, 2 col tablet, 3 col desktop
// ============================================================================

import React from 'react';
import { ArticleCard } from './ArticleCard';
import type { Article } from '../types';

interface ArticleCardGridProps {
  articles: Article[];
  onArticleClick: (id: number) => void;
}

export function ArticleCardGrid({ articles, onArticleClick }: ArticleCardGridProps) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
      {articles.map((article) => (
        <ArticleCard
          key={article.id}
          article={article}
          onClick={onArticleClick}
        />
      ))}
    </div>
  );
}
