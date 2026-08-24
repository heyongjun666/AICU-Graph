// ============================================================================
// infra/components/Skeleton.tsx — Shared loading skeleton component
// ============================================================================

import React from 'react';

interface SkeletonProps {
  count?: number;
}

/**
 * Generic card skeleton grid for loading states.
 * Renders `count` animated pulse placeholder cards in a 1/2/3-column responsive grid.
 */
export function ArticleListSkeleton({ count = 6 }: SkeletonProps) {
  return (
    <div
      data-testid="article-list-skeleton"
      className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4"
    >
      {Array.from({ length: count }, (_, i) => (
        <div
          key={i}
          className="rounded-lg border border-gray-200 bg-white p-4 animate-pulse"
        >
          <div className="h-5 bg-gray-200 rounded w-3/4 mb-3" />
          <div className="h-4 bg-gray-200 rounded w-full mb-2" />
          <div className="h-4 bg-gray-200 rounded w-2/3 mb-4" />
          <div className="h-3 bg-gray-200 rounded w-1/4 mb-3" />
          <div className="flex gap-1.5">
            <div className="h-5 bg-gray-200 rounded-full w-12" />
            <div className="h-5 bg-gray-200 rounded-full w-16" />
          </div>
        </div>
      ))}
    </div>
  );
}
