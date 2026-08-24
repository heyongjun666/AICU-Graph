// ============================================================================
// blog/stats/utils.ts — Shared formatting utilities for stats dashboard
// Single source of truth for number/time/rate formatting
// ============================================================================

/**
 * Format a read time in seconds to a human-readable string.
 * e.g. 45 → "45s", 185 → "3m 5s", 3600 → "60m 0s"
 */
export function formatReadTime(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}m ${secs}s`;
}

/**
 * Format a view/reader count with K/M suffix.
 * e.g. 42 → "42", 3200 → "3.2K", 15800 → "15.8K", 1500000 → "1.5M"
 */
export function formatViews(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

/**
 * Format a completion rate (0..1) as a percentage string.
 * e.g. 0.85 → "85.0%", 0.42 → "42.0%"
 */
export function formatRate(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

/**
 * Format a count for chart axis labels (no M suffix, chart uses compact labels).
 * e.g. 42 → "42", 3200 → "3.2K", 1500000 → "1500.0K"
 */
export function formatCount(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
}
