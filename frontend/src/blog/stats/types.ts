// ============================================================================
// blog/stats/types.ts — Reading statistics domain types
// Consumed by all stats dashboard components
// ============================================================================

/** Summary statistics for the dashboard header cards */
export interface StatsSummary {
  total_articles: number;
  total_views: number;
  total_readers: number;
  avg_read_time_seconds: number;
}

/** A single data point in a views trend series */
export interface ViewsTrendPoint {
  label: string;
  views: number;
}

/** Views trend data, segmented by period */
export interface ViewsTrendData {
  daily: ViewsTrendPoint[];
  weekly: ViewsTrendPoint[];
  monthly: ViewsTrendPoint[];
}

/** Top article entry, ordered by views DESC */
export interface TopArticle {
  id: number;
  title: string;
  views: number;
  read_time_seconds: number;
  completion_rate: number;
}

/** Per-article detailed statistics */
export interface ArticleDetailStats {
  id: number;
  title: string;
  views: number;
  total_read_time_seconds: number;
  avg_read_time_seconds: number;
  completion_rate: number;
}

/** Views trend period selector */
export type TrendPeriod = 'daily' | 'weekly' | 'monthly';

/** Page-level fetch state */
export type StatsPageState = 'loading' | 'error' | 'ready';
