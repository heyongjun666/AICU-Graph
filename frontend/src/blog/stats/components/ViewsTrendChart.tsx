// ============================================================================
// blog/stats/components/ViewsTrendChart.tsx — Views trend bar chart
// Renders a simple SVG bar chart for daily/weekly/monthly views data
// ============================================================================

import React from 'react';
import type { ViewsTrendPoint, TrendPeriod } from '../types';
import { formatCount } from '../utils';

interface ViewsTrendChartProps {
  data: ViewsTrendPoint[];
  period: TrendPeriod;
  onPeriodChange: (period: TrendPeriod) => void;
}

const PERIOD_OPTIONS: { value: TrendPeriod; label: string }[] = [
  { value: 'daily', label: '每日' },
  { value: 'weekly', label: '每周' },
  { value: 'monthly', label: '每月' },
];

const CHART_HEIGHT = 220;
const BAR_GAP = 4;
const CHART_PADDING = { top: 20, right: 16, bottom: 36, left: 16 };
const LABEL_COUNT = 5; // Number of Y-axis labels

function calculateYAxisTicks(maxValue: number): number[] {
  if (maxValue === 0) return [0, 1, 2, 3, 4];
  const roughStep = maxValue / (LABEL_COUNT - 1);
  const magnitude = Math.pow(10, Math.floor(Math.log10(roughStep)));
  const niceStep = Math.ceil(roughStep / magnitude) * magnitude;
  const ticks: number[] = [];
  for (let i = 0; i < LABEL_COUNT; i++) {
    ticks.push(i * niceStep);
  }
  return ticks;
}

export function ViewsTrendChart({ data, period, onPeriodChange }: ViewsTrendChartProps) {
  const maxViews = Math.max(...data.map((d) => d.views), 1);
  const chartWidth = Math.max(data.length * (32 + BAR_GAP) + CHART_PADDING.left + CHART_PADDING.right, 400);
  const plotWidth = chartWidth - CHART_PADDING.left - CHART_PADDING.right;
  const barWidth = Math.min(28, (plotWidth / data.length) - BAR_GAP);
  const yTicks = calculateYAxisTicks(maxViews);

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold text-gray-900">阅读趋势</h2>
        <div className="flex gap-1">
          {PERIOD_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              onClick={() => onPeriodChange(opt.value)}
              className={`px-3 py-1 text-sm font-medium rounded-md transition-colors ${
                period === opt.value
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {data.length === 0 ? (
        <div className="flex items-center justify-center py-16 text-sm text-gray-400">
          暂无数据
        </div>
      ) : (
        <svg
          width="100%"
          height={CHART_HEIGHT}
          viewBox={`0 0 ${chartWidth} ${CHART_HEIGHT}`}
          preserveAspectRatio="xMidYMid meet"
          role="img"
          aria-label="阅读趋势图表"
          data-testid="views-trend-chart"
        >
          {/* Y-axis grid lines */}
          {yTicks.map((tick) => {
            const y = CHART_PADDING.top + (1 - tick / yTicks[yTicks.length - 1]) * (CHART_HEIGHT - CHART_PADDING.top - CHART_PADDING.bottom);
            return (
              <g key={`y-${tick}`}>
                <line
                  x1={CHART_PADDING.left}
                  y1={y}
                  x2={chartWidth - CHART_PADDING.right}
                  y2={y}
                  stroke="#E5E7EB"
                  strokeWidth={1}
                />
                <text
                  x={CHART_PADDING.left - 8}
                  y={y + 4}
                  textAnchor="end"
                  fill="#9CA3AF"
                  fontSize={11}
                >
                  {formatCount(tick)}
                </text>
              </g>
            );
          })}

          {/* Bars */}
          {data.map((point, idx) => {
            const barHeight = maxViews > 0
              ? (point.views / yTicks[yTicks.length - 1]) * (CHART_HEIGHT - CHART_PADDING.top - CHART_PADDING.bottom)
              : 0;
            const x = CHART_PADDING.left + idx * (barWidth + BAR_GAP) + (plotWidth - data.length * (barWidth + BAR_GAP)) / 2;
            const y = CHART_HEIGHT - CHART_PADDING.bottom - barHeight;

            // Truncate label
            const label = point.label.length > 5 ? point.label.slice(0, 5) + '…' : point.label;

            return (
              <g key={idx}>
                <rect
                  x={x}
                  y={y}
                  width={barWidth}
                  height={Math.max(barHeight, 1)}
                  rx={3}
                  ry={3}
                  fill="#3B82F6"
                  opacity={0.85}
                />
                <text
                  x={x + barWidth / 2}
                  y={CHART_HEIGHT - 6}
                  textAnchor="middle"
                  fill="#6B7280"
                  fontSize={10}
                >
                  {label}
                </text>
              </g>
            );
          })}
        </svg>
      )}
    </div>
  );
}
