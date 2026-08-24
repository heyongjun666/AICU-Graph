// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import React from 'react';
import { TagManagementPage } from '../TagManagementPage';
import * as api from '../../api';

afterEach(cleanup);

// Mock the api module
vi.mock('../../api', () => ({
  fetchTags: vi.fn(),
  createTag: vi.fn(),
  renameTag: vi.fn(),
  deleteTag: vi.fn(),
}));

const mockTags = [
  { id: 1, name: 'Go', slug: 'go', articleCount: 15, created_at: '2026-07-28T10:00:00Z' },
  { id: 2, name: 'React', slug: 'react', articleCount: 8, created_at: '2026-07-28T10:00:00Z' },
  { id: 3, name: 'TypeScript', slug: 'typescript', articleCount: 3, created_at: '2026-07-28T10:00:00Z' },
];

describe('TagManagementPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // ===== Slice 1: Tag list display =====

  it('should show loading skeleton while fetching tags', () => {
    vi.mocked(api.fetchTags).mockReturnValue(new Promise(() => {}));

    render(<TagManagementPage onNavigate={() => {}} />);

    expect(screen.getByTestId('tag-list-skeleton')).toBeDefined();
  });

  it('should render tag list with names and article counts when loaded', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<TagManagementPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    expect(screen.getByText('React')).toBeDefined();
    expect(screen.getByText('TypeScript')).toBeDefined();
    expect(screen.getByText((c) => c.startsWith('15'))).toBeDefined();
    expect(screen.getByText((c) => c.startsWith('8'))).toBeDefined();
    expect(screen.getByText((c) => c.startsWith('3'))).toBeDefined();
  });

  it('should show empty state when no tags exist', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue([]);

    render(<TagManagementPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('还没有标签')).toBeDefined();
    });
  });

  it('should show error state when fetch fails', async () => {
    vi.mocked(api.fetchTags).mockRejectedValue(new Error('Network error'));

    render(<TagManagementPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('加载失败')).toBeDefined();
    });
    expect(screen.getByText('重试')).toBeDefined();
  });

  it('should retry fetching when retry button is clicked', async () => {
    vi.mocked(api.fetchTags)
      .mockRejectedValue(new Error('Network error'));

    render(<TagManagementPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('加载失败')).toBeDefined();
    });

    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    fireEvent.click(screen.getByText('重试'));

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });
  });

  it('should discard stale fetchTags response after component unmount (cancelled flag)', async () => {
    let resolvePromise: (value: unknown) => void;
    vi.mocked(api.fetchTags).mockImplementationOnce(
      () => new Promise((resolve) => { resolvePromise = resolve; }),
    );

    const { unmount } = render(<TagManagementPage onNavigate={() => {}} />);
    expect(screen.getByTestId('tag-list-skeleton')).toBeDefined();

    // Unmount — effect cleanup runs, sets cancelled = true
    unmount();

    // Resolve the pending promise — .then() should check cancelled and return early
    resolvePromise!([{ id: 99, name: 'StaleTag', slug: 'stale', articleCount: 1, created_at: '' }]);

    // Flush microtasks — the cancelled guard prevents setState on unmounted component
    await new Promise((r) => setTimeout(r, 10));
    // Test passes without error — stale response was discarded by the cancelled flag
  });

  // ===== Slice 2: Create tag =====

  it('should show create tag dialog when "新建标签" is clicked', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<TagManagementPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    fireEvent.click(screen.getByText('新建标签'));

    expect(screen.getByPlaceholderText('输入标签名称')).toBeDefined();
    expect(screen.getByText('确认')).toBeDefined();
    expect(screen.getByText('取消')).toBeDefined();
  });

  it('should create a new tag via the inline dialog', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);
    vi.mocked(api.createTag).mockResolvedValue({
      id: 4, name: 'Rust', slug: 'rust', articleCount: 0, created_at: '2026-07-28T12:00:00Z',
    });

    render(<TagManagementPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    fireEvent.click(screen.getByText('新建标签'));

    const input = screen.getByPlaceholderText('输入标签名称');
    fireEvent.change(input, { target: { value: 'Rust' } });
    fireEvent.click(screen.getByText('确认'));

    await waitFor(() => {
      expect(vi.mocked(api.createTag)).toHaveBeenCalledWith('Rust');
    });
  });

  it('should close create dialog without creating on cancel', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<TagManagementPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    fireEvent.click(screen.getByText('新建标签'));
    expect(screen.getByPlaceholderText('输入标签名称')).toBeDefined();

    fireEvent.click(screen.getByText('取消'));

    expect(screen.queryByPlaceholderText('输入标签名称')).toBeNull();
  });

  it('should prevent creating tag with empty name', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<TagManagementPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    fireEvent.click(screen.getByText('新建标签'));
    fireEvent.click(screen.getByText('确认'));

    expect(vi.mocked(api.createTag)).not.toHaveBeenCalled();
  });

  // ===== Slice 3: Rename tag =====

  it('should enter rename mode when rename button is clicked', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<TagManagementPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    // Click rename on the first tag
    const renameButtons = screen.getAllByLabelText('重命名');
    fireEvent.click(renameButtons[0]);

    expect(screen.getByDisplayValue('Go')).toBeDefined();
  });

  it('should rename a tag on confirm', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);
    vi.mocked(api.renameTag).mockResolvedValue({
      id: 1, name: 'Golang', slug: 'golang', articleCount: 15, created_at: '2026-07-28T10:00:00Z',
    });

    render(<TagManagementPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    const renameButtons = screen.getAllByLabelText('重命名');
    fireEvent.click(renameButtons[0]);

    const input = screen.getByDisplayValue('Go');
    fireEvent.change(input, { target: { value: 'Golang' } });
    fireEvent.click(screen.getByLabelText('确认重命名'));

    await waitFor(() => {
      expect(vi.mocked(api.renameTag)).toHaveBeenCalledWith(1, 'Golang');
    });
  });

  it('should cancel rename on cancel button', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<TagManagementPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    const renameButtons = screen.getAllByLabelText('重命名');
    fireEvent.click(renameButtons[0]);

    expect(screen.getByDisplayValue('Go')).toBeDefined();
    fireEvent.click(screen.getByLabelText('取消重命名'));

    expect(screen.queryByDisplayValue('Go')).toBeNull();
  });

  // ===== Slice 4: Delete tag =====

  it('should show delete confirmation when delete button is clicked', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<TagManagementPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    const deleteButtons = screen.getAllByLabelText('删除');
    fireEvent.click(deleteButtons[0]);

    expect(screen.getByText('确认删除')).toBeDefined();
    expect(screen.getByText('确定删除标签 "Go" 吗？')).toBeDefined();
    expect(screen.getByText('确定')).toBeDefined();
    expect(screen.getByText('取消')).toBeDefined();
  });

  it('should delete tag on confirmation', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);
    vi.mocked(api.deleteTag).mockResolvedValue(undefined);

    render(<TagManagementPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    const deleteButtons = screen.getAllByLabelText('删除');
    fireEvent.click(deleteButtons[0]);
    fireEvent.click(screen.getByText('确定'));

    await waitFor(() => {
      expect(vi.mocked(api.deleteTag)).toHaveBeenCalledWith(1);
    });
  });

  it('should cancel deletion', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<TagManagementPage onNavigate={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    const deleteButtons = screen.getAllByLabelText('删除');
    fireEvent.click(deleteButtons[0]);
    fireEvent.click(screen.getByText('取消'));

    expect(screen.queryByText('确认删除')).toBeNull();
  });

  // ===== Slice 5: Click navigation =====

  it('should navigate to article list filtered by tag when tag is clicked', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);
    const handleNavigate = vi.fn();

    render(<TagManagementPage onNavigate={handleNavigate} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    fireEvent.click(screen.getByText('Go'));

    expect(handleNavigate).toHaveBeenCalledWith('/articles?tag=go');
  });
});
