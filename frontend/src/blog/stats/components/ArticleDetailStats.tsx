// ============================================================================
// blog/stats/components/ArticleDetailStats.tsx — Per-article detailed stats
// Shows: views, total read time, average read time, completion rate
// ============================================================================

import React from 'react';
import type { ArticleDetailStats as ArticleDetailStatsType } from '../types';
import { formatReadTime, formatRate, formatViews } from '../utils';

interface ArticleDetailStatsProps {
  stats: ArticleDetailStatsType;
  onBack: () => void;
}

export function ArticleDetailStats({ stats, onBack }: ArticleDetailStatsProps) {
  const completionPercent = Math.round(stats.completion_rate * 100);
  const barColor = completionPercent >= 70
    ? 'bg-green-500'
    : completionPercent >= 40
      ? 'bg-yellow-500'
      : 'bg-red-500';

  return (
    <div className="rounded-lg border border-gray-200 bg-white shadow-sm">
      {/* Header */}
      <div className="flex items-center gap-3 px-5 py-4 border-b border-gray-100">
        <button
          type="button"
          onClick={onBack}
          className="inline-flex items-center justify-center w-8 h-8 rounded hover:bg-gray-100 transition-colors"
          aria-label="返回"
        >
          <svg className="w-5 h-5 text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
        </button>
        <div>
          <h2 className="text-lg font-semibold text-gray-900">文章统计详情</h2>
          <p className="text-sm text-gray-500 truncate max-w-md">{stats.title}</p>
        </div>
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-px bg-gray-100">
        {/* Views */}
        <div className="bg-white px-5 py-6">
          <div className="text-xs font-medium text-gray-500 mb-1">总阅读量</div>
          <div className="text-2xl font-bold text-gray-900">{formatViews(stats.views)}</div>
        </div>

        {/* Total read time */}
        <div className="bg-white px-5 py-6">
          <div className="text-xs font-medium text-gray-500 mb-1">总阅读时长</div>
          <div className="text-2xl font-bold text-gray-900">{formatReadTime(stats.total_read_time_seconds)}</div>
        </div>

        {/* Average read time */}
        <div className="bg-white px-5 py-6">
          <div className="text-xs font-medium text-gray-500 mb-1">平均阅读时长</div>
          <div className="text-2xl font-bold text-gray-900">{formatReadTime(stats.avg_read_time_seconds)}</div>
        </div>

        {/* Completion rate */}
        <div className="bg-white px-5 py-6">
          <div className="text-xs font-medium text-gray-500 mb-1">完成率</div>
          <div className="text-2xl font-bold text-gray-900">{formatRate(stats.completion_rate)}</div>
        </div>
      </div>

      {/* Progress bar for completion rate */}
      <div className="px-5 py-4 border-t border-gray-100">
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm font-medium text-gray-700">阅读完成率</span>
          <span className="text-sm font-medium text-gray-900">{completionPercent}%</span>
        </div>
        <div className="w-full h-2.5 bg-gray-200 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-500 ${barColor}`}
            style={{ width: `${Math.min(completionPercent, 100)}%` }}
            data-testid="completion-bar"
          />
        </div>
      </div>
    </div>
  );
}
