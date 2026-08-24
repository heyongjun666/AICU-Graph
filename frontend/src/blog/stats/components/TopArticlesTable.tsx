// ============================================================================
// blog/stats/components/TopArticlesTable.tsx — Top 10 most-read articles table
// Columns: rank, title, views, read time, completion rate
// ============================================================================

import React from 'react';
import type { TopArticle } from '../types';
import { formatReadTime, formatRate, formatViews } from '../utils';

interface TopArticlesTableProps {
  articles: TopArticle[];
  onArticleClick: (id: number) => void;
}

export function TopArticlesTable({ articles, onArticleClick }: TopArticlesTableProps) {
  return (
    <div className="rounded-lg border border-gray-200 bg-white shadow-sm">
      <div className="px-5 py-4 border-b border-gray-100">
        <h2 className="text-lg font-semibold text-gray-900">热门文章 TOP 10</h2>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm" data-testid="top-articles-table">
          <thead>
            <tr className="border-b border-gray-100 bg-gray-50">
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-12">
                排名
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                标题
              </th>
              <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider w-20">
                阅读量
              </th>
              <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider w-24">
                阅读时长
              </th>
              <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider w-24">
                完成率
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50">
            {articles.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-4 py-12 text-center text-sm text-gray-400">
                  暂无数据
                </td>
              </tr>
            ) : (
              articles.map((article, idx) => (
                <tr
                  key={article.id}
                  className="hover:bg-gray-50 cursor-pointer transition-colors"
                  onClick={() => onArticleClick(article.id)}
                >
                  <td className="px-4 py-3 whitespace-nowrap">
                    <span
                      className={`inline-flex items-center justify-center w-6 h-6 rounded-full text-xs font-bold ${
                        idx === 0
                          ? 'bg-yellow-100 text-yellow-800'
                          : idx === 1
                            ? 'bg-gray-100 text-gray-600'
                            : idx === 2
                              ? 'bg-orange-100 text-orange-800'
                              : 'bg-gray-50 text-gray-400'
                      }`}
                    >
                      {idx + 1}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-gray-900 font-medium max-w-xs truncate">
                    {article.title}
                  </td>
                  <td className="px-4 py-3 text-right text-gray-700 whitespace-nowrap tabular-nums">
                    {formatViews(article.views)}
                  </td>
                  <td className="px-4 py-3 text-right text-gray-700 whitespace-nowrap tabular-nums">
                    {formatReadTime(article.read_time_seconds)}
                  </td>
                  <td className="px-4 py-3 text-right whitespace-nowrap tabular-nums">
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                        article.completion_rate >= 0.7
                          ? 'bg-green-100 text-green-800'
                          : article.completion_rate >= 0.4
                            ? 'bg-yellow-100 text-yellow-800'
                            : 'bg-red-100 text-red-800'
                      }`}
                    >
                      {formatRate(article.completion_rate)}
                    </span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
