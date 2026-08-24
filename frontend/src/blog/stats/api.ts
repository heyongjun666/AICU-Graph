// ============================================================================
// blog/stats/api.ts — Reading statistics API client
// Consumes shared HTTP infrastructure from frontend_infra (../../infra)
// ============================================================================

import type { ApiResponse } from '../../infra/types';
import { setBaseUrl, buildQuery, requestRaw } from '../../infra/api';
import type {
  StatsSummary,
  ViewsTrendData,
  TopArticle,
  ArticleDetailStats,
  TrendPeriod,
} from './types';

export { setBaseUrl };

// ---- Field mappers (API snake_case → frontend camelCase) ----

function mapStatsSummary(raw: any): StatsSummary {
  return {
    total_articles: raw.total_articles ?? 0,
    total_views: raw.total_views ?? 0,
    total_readers: raw.total_readers ?? 0,
    avg_read_time_seconds: raw.avg_read_time_seconds ?? 0,
  };
}

function mapViewsTrendData(raw: any): ViewsTrendData {
  return {
    daily: (raw.daily ?? []).map((p: any) => ({ label: p.label, views: p.views ?? 0 })),
    weekly: (raw.weekly ?? []).map((p: any) => ({ label: p.label, views: p.views ?? 0 })),
    monthly: (raw.monthly ?? []).map((p: any) => ({ label: p.label, views: p.views ?? 0 })),
  };
}

function mapTopArticle(raw: any): TopArticle {
  return {
    id: raw.id,
    title: raw.title ?? '',
    views: raw.views ?? 0,
    read_time_seconds: raw.read_time_seconds ?? 0,
    completion_rate: raw.completion_rate ?? 0,
  };
}

function mapArticleDetailStats(raw: any): ArticleDetailStats {
  return {
    id: raw.id,
    title: raw.title ?? '',
    views: raw.views ?? 0,
    total_read_time_seconds: raw.total_read_time_seconds ?? 0,
    avg_read_time_seconds: raw.avg_read_time_seconds ?? 0,
    completion_rate: raw.completion_rate ?? 0,
  };
}

// ---- Public API ----

/**
 * Fetch summary statistics for the dashboard header cards.
 * GET /api/v1/stats/summary
 */
export async function fetchStatsSummary(): Promise<StatsSummary> {
  const body = await requestRaw<StatsSummary>('/stats/summary');

  if (body.code !== 0) {
    throw new Error(body.message || 'unknown error');
  }

  return mapStatsSummary(body.data);
}

/**
 * Fetch views trend data.
 * GET /api/v1/stats/views-trend?period=daily|weekly|monthly
 */
export async function fetchViewsTrend(params?: {
  period?: TrendPeriod;
}): Promise<ViewsTrendData> {
  const query = buildQuery({
    period: params?.period,
  });
  const body = await requestRaw<ViewsTrendData>('/stats/views-trend', query);

  if (body.code !== 0) {
    throw new Error(body.message || 'unknown error');
  }

  return mapViewsTrendData(body.data);
}

/**
 * Fetch top N most-read articles.
 * GET /api/v1/stats/top-articles?limit=10
 */
export async function fetchTopArticles(limit = 10): Promise<TopArticle[]> {
  const query = buildQuery({ limit });
  const body = await requestRaw<{ articles: any[] }>('/stats/top-articles', query);

  if (body.code !== 0) {
    throw new Error(body.message || 'unknown error');
  }

  return (body.data?.articles ?? []).map(mapTopArticle);
}

/**
 * Fetch per-article detailed statistics.
 * GET /api/v1/stats/articles/:id
 */
export async function fetchArticleDetailStats(id: number): Promise<ArticleDetailStats> {
  const body = await requestRaw<ArticleDetailStats>(`/stats/articles/${id}`);

  if (body.code !== 0) {
    throw new Error(body.message || 'unknown error');
  }

  return mapArticleDetailStats(body.data);
}
