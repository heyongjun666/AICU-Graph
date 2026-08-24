// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import React from 'react';
import { SummaryCards } from '../SummaryCards';
import { ViewsTrendChart } from '../ViewsTrendChart';
import { TopArticlesTable } from '../TopArticlesTable';
import { ArticleDetailStats } from '../ArticleDetailStats';
import { StatsDashboardPage } from '../StatsDashboardPage';
import * as api from '../../api';
import type {
  StatsSummary,
  ViewsTrendData,
  TopArticle,
  ArticleDetailStats as ArticleDetailStatsType,
} from '../../types';

afterEach(cleanup);

// ---- Mock Data ----

const mockSummary: StatsSummary = {
  total_articles: 42,
  total_views: 15800,
  total_readers: 3200,
  avg_read_time_seconds: 185,
};

const mockViewsTrend: ViewsTrendData = {
  daily: [
    { label: '07-21', views: 420 },
    { label: '07-22', views: 580 },
    { label: '07-23', views: 350 },
    { label: '07-24', views: 720 },
    { label: '07-25', views: 610 },
    { label: '07-26', views: 890 },
    { label: '07-27', views: 450 },
  ],
  weekly: [
    { label: 'W29', views: 3200 },
    { label: 'W30', views: 4100 },
    { label: 'W31', views: 3800 },
  ],
  monthly: [
    { label: '05月', views: 12000 },
    { label: '06月', views: 15800 },
    { label: '07月', views: 14200 },
  ],
};

const mockTopArticles: TopArticle[] = [
  { id: 1, title: '如何学习 TypeScript', views: 3200, read_time_seconds: 420, completion_rate: 0.85 },
  { id: 2, title: 'React 性能优化指南', views: 2800, read_time_seconds: 360, completion_rate: 0.72 },
  { id: 3, title: 'Go 语言并发编程', views: 2100, read_time_seconds: 540, completion_rate: 0.63 },
  { id: 4, title: '深入理解 CSS Grid', views: 1800, read_time_seconds: 300, completion_rate: 0.91 },
  { id: 5, title: 'Node.js 最佳实践', views: 1500, read_time_seconds: 480, completion_rate: 0.55 },
  { id: 6, title: 'Python 数据科学入门', views: 1200, read_time_seconds: 600, completion_rate: 0.48 },
  { id: 7, title: 'Docker 容器化部署', views: 980, read_time_seconds: 390, completion_rate: 0.78 },
  { id: 8, title: 'Git 高级技巧', views: 850, read_time_seconds: 240, completion_rate: 0.88 },
  { id: 9, title: 'Rust 所有权系统', views: 720, read_time_seconds: 660, completion_rate: 0.35 },
  { id: 10, title: '设计模式在 JavaScript 中的应用', views: 610, read_time_seconds: 510, completion_rate: 0.42 },
];

const mockArticleDetail: ArticleDetailStatsType = {
  id: 1,
  title: '如何学习 TypeScript',
  views: 3200,
  total_read_time_seconds: 134400,
  avg_read_time_seconds: 420,
  completion_rate: 0.85,
};

// Mock the api module
vi.mock('../../api', () => ({
  fetchStatsSummary: vi.fn(),
  fetchViewsTrend: vi.fn(),
  fetchTopArticles: vi.fn(),
  fetchArticleDetailStats: vi.fn(),
}));

// ============================================================================
// SummaryCards
// ============================================================================

describe('SummaryCards', () => {
  it('renders all four summary cards with correct values', () => {
    render(<SummaryCards summary={mockSummary} />);

    expect(screen.getByText('总文章数')).toBeDefined();
    expect(screen.getByText('42')).toBeDefined();

    expect(screen.getByText('总阅读量')).toBeDefined();
    expect(screen.getByText('15.8K')).toBeDefined();

    expect(screen.getByText('总读者数')).toBeDefined();
    expect(screen.getByText('3.2K')).toBeDefined();

    expect(screen.getByText('平均阅读时长')).toBeDefined();
    expect(screen.getByText('3m 5s')).toBeDefined();
  });

  it('formats zero values correctly', () => {
    render(<SummaryCards summary={{
      total_articles: 0,
      total_views: 0,
      total_readers: 0,
      avg_read_time_seconds: 0,
    }} />);

    const zeros = screen.getAllByText('0');
    expect(zeros.length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText('0s')).toBeDefined();
  });

  it('formats large numbers with K/M suffix', () => {
    render(<SummaryCards summary={{
      total_articles: 1500000,
      total_views: 2500000,
      total_readers: 999999,
      avg_read_time_seconds: 0,
    }} />);

    expect(screen.getByText('1.5M')).toBeDefined();
    expect(screen.getByText('2.5M')).toBeDefined();
    expect(screen.getByText('1000.0K')).toBeDefined(); // 999999 / 1000 rounds to 1000.0
  });
});

// ============================================================================
// ViewsTrendChart
// ============================================================================

describe('ViewsTrendChart', () => {
  it('renders the chart with daily data', () => {
    render(
      <ViewsTrendChart
        data={mockViewsTrend.daily}
        period="daily"
        onPeriodChange={() => {}}
      />,
    );

    expect(screen.getByText('阅读趋势')).toBeDefined();
    expect(screen.getByTestId('views-trend-chart')).toBeDefined();
  });

  it('shows period toggle buttons', () => {
    render(
      <ViewsTrendChart
        data={mockViewsTrend.daily}
        period="daily"
        onPeriodChange={() => {}}
      />,
    );

    expect(screen.getByText('每日')).toBeDefined();
    expect(screen.getByText('每周')).toBeDefined();
    expect(screen.getByText('每月')).toBeDefined();
  });

  it('highlights the active period button', () => {
    render(
      <ViewsTrendChart
        data={mockViewsTrend.weekly}
        period="weekly"
        onPeriodChange={() => {}}
      />,
    );

    const weeklyBtn = screen.getByText('每周');
    expect(weeklyBtn.className).toContain('bg-blue-600');
  });

  it('calls onPeriodChange when period button is clicked', () => {
    const handleChange = vi.fn();
    render(
      <ViewsTrendChart
        data={mockViewsTrend.daily}
        period="daily"
        onPeriodChange={handleChange}
      />,
    );

    fireEvent.click(screen.getByText('每月'));
    expect(handleChange).toHaveBeenCalledWith('monthly');
  });

  it('shows empty state when no data', () => {
    render(
      <ViewsTrendChart
        data={[]}
        period="daily"
        onPeriodChange={() => {}}
      />,
    );

    expect(screen.getByText('暂无数据')).toBeDefined();
  });
});

// ============================================================================
// TopArticlesTable
// ============================================================================

describe('TopArticlesTable', () => {
  it('renders the table header', () => {
    render(<TopArticlesTable articles={mockTopArticles} onArticleClick={() => {}} />);

    expect(screen.getByText('热门文章 TOP 10')).toBeDefined();
    expect(screen.getByText('排名')).toBeDefined();
    expect(screen.getByText('标题')).toBeDefined();
    expect(screen.getByText('阅读量')).toBeDefined();
    expect(screen.getByText('阅读时长')).toBeDefined();
    expect(screen.getByText('完成率')).toBeDefined();
  });

  it('renders all top articles with correct ranking', () => {
    render(<TopArticlesTable articles={mockTopArticles} onArticleClick={() => {}} />);

    expect(screen.getByText('如何学习 TypeScript')).toBeDefined();
    expect(screen.getByText('React 性能优化指南')).toBeDefined();
    expect(screen.getByText('Go 语言并发编程')).toBeDefined();
  });

  it('formats completion rate with color badges', () => {
    render(<TopArticlesTable articles={mockTopArticles} onArticleClick={() => {}} />);

    // High completion rate (>= 70%)
    expect(screen.getByText('85.0%')).toBeDefined();
    // Medium completion rate (>= 40%)
    expect(screen.getByText('42.0%')).toBeDefined();
  });

  it('calls onArticleClick when a row is clicked', () => {
    const handleClick = vi.fn();
    render(<TopArticlesTable articles={mockTopArticles} onArticleClick={handleClick} />);

    fireEvent.click(screen.getByText('如何学习 TypeScript'));
    expect(handleClick).toHaveBeenCalledWith(1);
  });

  it('shows empty state when no articles', () => {
    render(<TopArticlesTable articles={[]} onArticleClick={() => {}} />);

    expect(screen.getByText('暂无数据')).toBeDefined();
  });
});

// ============================================================================
// ArticleDetailStats
// ============================================================================

describe('ArticleDetailStats', () => {
  it('renders article title and stats', () => {
    render(<ArticleDetailStats stats={mockArticleDetail} onBack={() => {}} />);

    expect(screen.getByText('文章统计详情')).toBeDefined();
    expect(screen.getByText('如何学习 TypeScript')).toBeDefined();
    expect(screen.getByText('总阅读量')).toBeDefined();
    expect(screen.getByText('3.2K')).toBeDefined();
    expect(screen.getByText('总阅读时长')).toBeDefined();
    expect(screen.getByText('平均阅读时长')).toBeDefined();
    expect(screen.getByText('完成率')).toBeDefined();
  });

  it('calls onBack when back button is clicked', () => {
    const handleBack = vi.fn();
    render(<ArticleDetailStats stats={mockArticleDetail} onBack={handleBack} />);

    fireEvent.click(screen.getByLabelText('返回'));
    expect(handleBack).toHaveBeenCalledTimes(1);
  });

  it('renders completion progress bar', () => {
    render(<ArticleDetailStats stats={mockArticleDetail} onBack={() => {}} />);

    expect(screen.getByTestId('completion-bar')).toBeDefined();
    expect(screen.getByText('85%')).toBeDefined();
  });
});

// ============================================================================
// StatsDashboardPage
// ============================================================================

describe('StatsDashboardPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // --- Loading state ---
  it('should show skeleton loading state while fetching', () => {
    vi.mocked(api.fetchStatsSummary).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.fetchViewsTrend).mockReturnValue(new Promise(() => {}));
    vi.mocked(api.fetchTopArticles).mockReturnValue(new Promise(() => {}));

    render(<StatsDashboardPage onNavigate={() => {}} />);

    expect(screen.getByTestId('stats-skeleton')).toBeDefined();
  });

  // --- Ready state ---
  it('should render dashboard when data is loaded', async () => {
    vi.mocked(api.fetchStatsSummary).mockResolvedValue(mockSummary);
    vi.mocked(api.fetchViewsTrend).mockResolvedValue(mockViewsTrend);
    vi.mocked(api.fetchTopArticles).mockResolvedValue(mockTopArticles);

    render(<StatsDashboardPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('阅读统计')).toBeDefined();
    });

    // Summary cards
    expect(screen.getByText('42')).toBeDefined();
    expect(screen.getByText('15.8K')).toBeDefined();

    // Chart
    expect(screen.getByText('阅读趋势')).toBeDefined();

    // Top articles table
    expect(screen.getByText('热门文章 TOP 10')).toBeDefined();
    expect(screen.getByText('如何学习 TypeScript')).toBeDefined();

    // Article detail placeholder
    expect(screen.getByText('点击文章查看详情')).toBeDefined();
  });

  // --- Error state ---
  it('should show error state when fetch fails', async () => {
    vi.mocked(api.fetchStatsSummary).mockRejectedValue(new Error('Network error'));
    vi.mocked(api.fetchViewsTrend).mockRejectedValue(new Error('Network error'));
    vi.mocked(api.fetchTopArticles).mockRejectedValue(new Error('Network error'));

    render(<StatsDashboardPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('加载失败')).toBeDefined();
    });
    expect(screen.getByText('重试')).toBeDefined();
  });

  // --- Retry ---
  it('should retry fetching when retry button is clicked', async () => {
    vi.mocked(api.fetchStatsSummary)
      .mockRejectedValueOnce(new Error('Network error'))
      .mockResolvedValueOnce(mockSummary);
    vi.mocked(api.fetchViewsTrend)
      .mockRejectedValueOnce(new Error('Network error'))
      .mockResolvedValueOnce(mockViewsTrend);
    vi.mocked(api.fetchTopArticles)
      .mockRejectedValueOnce(new Error('Network error'))
      .mockResolvedValueOnce(mockTopArticles);

    render(<StatsDashboardPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('加载失败')).toBeDefined();
    });

    fireEvent.click(screen.getByText('重试'));

    await waitFor(() => {
      expect(screen.getByText('阅读统计')).toBeDefined();
    });
    expect(screen.getByText('42')).toBeDefined();
  });

  // --- Article detail click ---
  it('should load and display article detail when a top article is clicked', async () => {
    vi.mocked(api.fetchStatsSummary).mockResolvedValue(mockSummary);
    vi.mocked(api.fetchViewsTrend).mockResolvedValue(mockViewsTrend);
    vi.mocked(api.fetchTopArticles).mockResolvedValue(mockTopArticles);
    vi.mocked(api.fetchArticleDetailStats).mockResolvedValue(mockArticleDetail);

    render(<StatsDashboardPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('如何学习 TypeScript')).toBeDefined();
    });

    // Click on the top article
    fireEvent.click(screen.getByText('如何学习 TypeScript'));

    await waitFor(() => {
      expect(screen.getByText('文章统计详情')).toBeDefined();
    });
    // "3.2K" appears both in summary cards and the article detail — use getAllByText
    const matches = screen.getAllByText('3.2K');
    expect(matches.length).toBeGreaterThanOrEqual(1);
  });

  // --- Period change ---
  it('should re-fetch data when period changes', async () => {
    vi.mocked(api.fetchStatsSummary).mockResolvedValue(mockSummary);
    vi.mocked(api.fetchViewsTrend).mockResolvedValue(mockViewsTrend);
    vi.mocked(api.fetchTopArticles).mockResolvedValue(mockTopArticles);

    render(<StatsDashboardPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('阅读趋势')).toBeDefined();
    });

    // Switch to weekly
    fireEvent.click(screen.getByText('每周'));

    await waitFor(() => {
      expect(vi.mocked(api.fetchViewsTrend)).toHaveBeenCalledWith(
        expect.objectContaining({ period: 'weekly' }),
      );
    });
  });

  // --- Article detail back button ---
  it('should go back to placeholder when back is clicked on article detail', async () => {
    vi.mocked(api.fetchStatsSummary).mockResolvedValue(mockSummary);
    vi.mocked(api.fetchViewsTrend).mockResolvedValue(mockViewsTrend);
    vi.mocked(api.fetchTopArticles).mockResolvedValue(mockTopArticles);
    vi.mocked(api.fetchArticleDetailStats).mockResolvedValue(mockArticleDetail);

    render(<StatsDashboardPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('如何学习 TypeScript')).toBeDefined();
    });

    fireEvent.click(screen.getByText('如何学习 TypeScript'));

    await waitFor(() => {
      expect(screen.getByText('文章统计详情')).toBeDefined();
    });

    // Click back
    fireEvent.click(screen.getByLabelText('返回'));

    await waitFor(() => {
      expect(screen.getByText('点击文章查看详情')).toBeDefined();
    });
  });

  // --- Article click fallback navigation ---
  it('should navigate when article detail fetch fails', async () => {
    vi.mocked(api.fetchStatsSummary).mockResolvedValue(mockSummary);
    vi.mocked(api.fetchViewsTrend).mockResolvedValue(mockViewsTrend);
    vi.mocked(api.fetchTopArticles).mockResolvedValue(mockTopArticles);
    vi.mocked(api.fetchArticleDetailStats).mockRejectedValue(new Error('Not found'));

    const handleNavigate = vi.fn();
    render(<StatsDashboardPage onNavigate={handleNavigate} />);

    await waitFor(() => {
      expect(screen.getByText('如何学习 TypeScript')).toBeDefined();
    });

    fireEvent.click(screen.getByText('如何学习 TypeScript'));

    await waitFor(() => {
      expect(handleNavigate).toHaveBeenCalledWith('/articles/1');
    });
  });

  // --- Race condition: rapid period changes ---
  it('should not show stale data when period changes rapidly', async () => {
    // Mock fetchViewsTrend to return different data depending on period
    // AND delay the first call so it resolves AFTER the second.
    vi.mocked(api.fetchStatsSummary).mockResolvedValue(mockSummary);
    vi.mocked(api.fetchTopArticles).mockResolvedValue(mockTopArticles);

    const dailyData: ViewsTrendData = {
      daily: [{ label: 'D1', views: 100 }],
      weekly: [],
      monthly: [],
    };
    const weeklyData: ViewsTrendData = {
      daily: [],
      weekly: [{ label: 'W1', views: 500 }],
      monthly: [],
    };

    // First call (daily) is slow; second call (weekly) resolves fast
    vi.mocked(api.fetchViewsTrend)
      .mockResolvedValueOnce(new Promise((resolve) =>
        setTimeout(() => resolve(dailyData), 200),
      ))
      .mockResolvedValueOnce(weeklyData);

    render(<StatsDashboardPage onNavigate={() => {}} />);

    // Wait for initial load to complete
    await waitFor(() => {
      expect(screen.getByText('阅读趋势')).toBeDefined();
    });

    // Switch to weekly — the daily request may still be in-flight
    fireEvent.click(screen.getByText('每周'));

    // Wait for the weekly chart to render
    // With the generation counter, even if the daily response arrives
    // after the weekly, the chart must show weekly data.
    await waitFor(() => {
      expect(screen.getByText('W1')).toBeDefined();
    });

    // Wait long enough for the slow daily response to have arrived too
    await new Promise((r) => setTimeout(r, 250));

    // The chart must still show weekly label (not reverted to daily)
    expect(screen.getByText('W1')).toBeDefined();
    expect(screen.queryByText('D1')).toBeNull();
  });
});
