// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react';
import React from 'react';
import { TagSidebar } from '../TagSidebar';
import * as api from '../../api';

afterEach(cleanup);

vi.mock('../../api', () => ({
  fetchTags: vi.fn(),
}));

const mockTags = [
  { id: 1, name: 'Go', slug: 'go', articleCount: 15 },
  { id: 2, name: 'React', slug: 'react', articleCount: 8 },
  { id: 3, name: 'TypeScript', slug: 'typescript', articleCount: 3 },
];

describe('TagSidebar', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should show loading state initially', () => {
    vi.mocked(api.fetchTags).mockReturnValue(new Promise(() => {}));

    render(<TagSidebar activeSlug={null} onChange={() => {}} />);

    expect(screen.getByText('标签筛选')).toBeDefined();
    expect(screen.getByText('全部文章')).toBeDefined();
  });

  it('should render tags when loaded', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<TagSidebar activeSlug={null} onChange={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    expect(screen.getByText('React')).toBeDefined();
    expect(screen.getByText('TypeScript')).toBeDefined();
    expect(screen.getByText('26')).toBeDefined(); // total: 15+8+3
  });

  it('should highlight active tag', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);

    render(<TagSidebar activeSlug="react" onChange={() => {}} />);

    await waitFor(() => {
      // The button containing "React" should have bg-blue-50 class
      const reactButton = screen.getByText('React').closest('button');
      expect(reactButton?.getAttribute('class')).toContain('bg-blue-50');
    });
  });

  it('should call onChange with slug when a tag is clicked', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);
    const handleChange = vi.fn();

    render(<TagSidebar activeSlug={null} onChange={handleChange} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    fireEvent.click(screen.getByText('Go'));
    expect(handleChange).toHaveBeenCalledWith('go');
  });

  it('should call onChange with null when "全部文章" is clicked', async () => {
    vi.mocked(api.fetchTags).mockResolvedValue(mockTags);
    const handleChange = vi.fn();

    render(<TagSidebar activeSlug="go" onChange={handleChange} />);

    await waitFor(() => {
      expect(screen.getByText('Go')).toBeDefined();
    });

    fireEvent.click(screen.getByText('全部文章'));
    expect(handleChange).toHaveBeenCalledWith(null);
  });
});
