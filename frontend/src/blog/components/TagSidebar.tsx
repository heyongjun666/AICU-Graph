// ============================================================================
// TagSidebar — Tag filter navigation sidebar
// Vertical sidebar showing tags with article counts.
// Clicking a tag navigates to filtered article list.
// ============================================================================

import React, { useState, useEffect, useCallback } from 'react';
import { fetchTags } from '../api';
import type { Tag } from '../types';

interface TagSidebarProps {
  activeSlug: string | null;
  onChange: (slug: string | null) => void;
}

export function TagSidebar({ activeSlug, onChange }: TagSidebarProps) {
  const [tags, setTags] = useState<Tag[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetchTags()
      .then((data) => {
        if (cancelled) return;
        setTags(data);
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const totalArticles = tags.reduce((sum, t) => sum + t.articleCount, 0);

  return (
    <aside className="w-48 shrink-0">
      <h2 className="text-sm font-semibold text-gray-500 uppercase tracking-wider mb-3 px-3">
        标签筛选
      </h2>
      <nav className="space-y-0.5">
        {/* All articles */}
        <button
          type="button"
          onClick={() => onChange(null)}
          className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
            activeSlug === null
              ? 'bg-blue-50 text-blue-700'
              : 'text-gray-700 hover:bg-gray-100'
          }`}
        >
          <span>全部文章</span>
          {!loading && (
            <span className={`text-xs ${
              activeSlug === null ? 'text-blue-400' : 'text-gray-400'
            }`}>
              {totalArticles}
            </span>
          )}
        </button>

        {/* Loading skeleton */}
        {loading && (
          <div className="space-y-1 px-3 py-2">
            {Array.from({ length: 4 }, (_, i) => (
              <div key={i} className="h-5 bg-gray-200 rounded animate-pulse" />
            ))}
          </div>
        )}

        {/* Tags */}
        {!loading && tags.map((tag) => (
          <button
            key={tag.id}
            type="button"
            onClick={() => onChange(tag.slug)}
            className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
              activeSlug === tag.slug
                ? 'bg-blue-50 text-blue-700'
                : 'text-gray-700 hover:bg-gray-100'
            }`}
          >
            <span>{tag.name}</span>
            <span className={`text-xs ${
              activeSlug === tag.slug ? 'text-blue-400' : 'text-gray-400'
            }`}>
              {tag.articleCount}
            </span>
          </button>
        ))}

        {!loading && tags.length === 0 && (
          <p className="px-3 py-2 text-sm text-gray-400">暂无标签</p>
        )}
      </nav>
    </aside>
  );
}
