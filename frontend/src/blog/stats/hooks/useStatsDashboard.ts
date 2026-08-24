// ============================================================================
// blog/stats/hooks/useStatsDashboard.ts — Stats dashboard data fetching hook
// Manages loading/error/ready state transitions and data fetching
// Uses a generation counter to prevent stale responses from overwriting
// newer data when period changes rapidly (race condition prevention).
// ============================================================================

import { useState, useEffect, useCallback, useRef } from 'react';
import {
  fetchStatsSummary,
  fetchViewsTrend,
  fetchTopArticles,
} from '../api';
import type {
  StatsSummary,
  ViewsTrendData,
  TopArticle,
  StatsPageState,
  TrendPeriod,
} from '../types';

export interface StatsDashboardState {
  pageState: StatsPageState;
  summary: StatsSummary | null;
  viewsTrend: ViewsTrendData | null;
  topArticles: TopArticle[];
  selectedPeriod: TrendPeriod;
}

export interface StatsDashboardActions {
  setPeriod: (period: TrendPeriod) => void;
  retry: () => void;
}

export function useStatsDashboard(): [StatsDashboardState, StatsDashboardActions] {
  const [pageState, setPageState] = useState<StatsPageState>('loading');
  const [summary, setSummary] = useState<StatsSummary | null>(null);
  const [viewsTrend, setViewsTrend] = useState<ViewsTrendData | null>(null);
  const [topArticles, setTopArticles] = useState<TopArticle[]>([]);
  const [selectedPeriod, setSelectedPeriod] = useState<TrendPeriod>('daily');
  const [retryCounter, setRetryCounter] = useState(0);
  const mountedRef = useRef(true);
  const generationRef = useRef(0);
  const hasData = useRef(false);

  const load = useCallback(async () => {
    const gen = ++generationRef.current;
    // Capture the period at call time so a mid-flight period change
    // won't affect which data this request thinks it fetched.
    const period = selectedPeriod;

    // Only show full skeleton on initial load (no data yet).
    // On re-fetches (period change), keep existing dashboard visible.
    if (!hasData.current) {
      setPageState('loading');
    }

    try {
      const [summaryData, trendData, articlesData] = await Promise.all([
        fetchStatsSummary(),
        fetchViewsTrend({ period }),
        fetchTopArticles(10),
      ]);
      // Stale response: a newer request has already started.
      if (gen !== generationRef.current) return;
      if (!mountedRef.current) return;
      hasData.current = true;
      setSummary(summaryData);
      setViewsTrend(trendData);
      setTopArticles(articlesData);
      setPageState('ready');
    } catch {
      // Stale error: discard to avoid showing error for an abandoned period.
      if (gen !== generationRef.current) return;
      if (!mountedRef.current) return;
      // On re-fetch failure, keep existing data visible
      if (!hasData.current) {
        setPageState('error');
      }
    }
  }, [selectedPeriod]);

  useEffect(() => {
    mountedRef.current = true;
    load();
    return () => {
      mountedRef.current = false;
    };
  }, [load, retryCounter]);

  const handleSetPeriod = useCallback((period: TrendPeriod) => {
    setSelectedPeriod(period);
  }, []);

  const handleRetry = useCallback(() => {
    setRetryCounter((c) => c + 1);
  }, []);

  return [
    { pageState, summary, viewsTrend, topArticles, selectedPeriod },
    { setPeriod: handleSetPeriod, retry: handleRetry },
  ];
}
