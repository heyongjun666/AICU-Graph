// ============================================================================
// Pagination — Page navigation control
// Shows page numbers with prev/next buttons, ellipsis for large page counts
// Hides when total <= 1
// ============================================================================

import React, { useCallback } from 'react';

interface PaginationProps {
  current: number;
  total: number;
  onChange: (page: number) => void;
}

function buildPageNumbers(current: number, total: number): (number | 'ellipsis')[] {
  if (total <= 7) {
    return Array.from({ length: total }, (_, i) => i + 1);
  }

  const pages: (number | 'ellipsis')[] = [];

  pages.push(1);

  if (current > 3) {
    pages.push('ellipsis');
  }

  const start = Math.max(2, current - 1);
  const end = Math.min(total - 1, current + 1);

  for (let i = start; i <= end; i++) {
    pages.push(i);
  }

  if (current < total - 2) {
    pages.push('ellipsis');
  }

  pages.push(total);

  return pages;
}

export function Pagination({ current, total, onChange }: PaginationProps) {
  const handlePrev = useCallback(() => {
    if (current > 1) onChange(current - 1);
  }, [current, onChange]);

  const handleNext = useCallback(() => {
    if (current < total) onChange(current + 1);
  }, [current, total, onChange]);

  if (total <= 1) return null;

  const pages = buildPageNumbers(current, total);

  return (
    <nav className="flex items-center justify-center gap-1" aria-label="分页导航">
      {/* Prev button */}
      <button
        type="button"
        aria-label="上一页"
        disabled={current <= 1}
        onClick={handlePrev}
        className="inline-flex items-center justify-center w-8 h-8 rounded text-sm text-gray-600 hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed"
      >
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
        </svg>
      </button>

      {/* Page numbers */}
      {pages.map((page, idx) =>
        page === 'ellipsis' ? (
          <span key={`e-${idx}`} className="w-8 h-8 flex items-center justify-center text-sm text-gray-400">
            ...
          </span>
        ) : (
          <button
            key={page}
            type="button"
            onClick={() => onChange(page)}
            className={`inline-flex items-center justify-center w-8 h-8 rounded text-sm font-medium ${
              page === current
                ? 'bg-blue-600 text-white'
                : 'text-gray-700 hover:bg-gray-100'
            }`}
          >
            {page}
          </button>
        ),
      )}

      {/* Next button */}
      <button
        type="button"
        aria-label="下一页"
        disabled={current >= total}
        onClick={handleNext}
        className="inline-flex items-center justify-center w-8 h-8 rounded text-sm text-gray-600 hover:bg-gray-100 disabled:opacity-30 disabled:cursor-not-allowed"
      >
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>
      </button>
    </nav>
  );
}
