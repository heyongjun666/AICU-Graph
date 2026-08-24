// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import React from 'react';
import { CategoryTabs } from '../CategoryTabs';
import type { Tag } from '../../types';

afterEach(cleanup);

const mockTags: Tag[] = [
  { id: 1, name: '技术', slug: 'tech', articleCount: 15 },
  { id: 2, name: '生活', slug: 'life', articleCount: 8 },
  { id: 3, name: '开源', slug: 'open-source', articleCount: 5 },
];

describe('CategoryTabs', () => {
  it('should render "全部" tab and all tag tabs', () => {
    render(<CategoryTabs tags={mockTags} activeSlug={null} onChange={() => {}} />);
    expect(screen.getByText('全部')).toBeDefined();
    expect(screen.getByText('技术')).toBeDefined();
    expect(screen.getByText('生活')).toBeDefined();
    expect(screen.getByText('开源')).toBeDefined();
  });

  it('should highlight the active tag', () => {
    const { container } = render(
      <CategoryTabs tags={mockTags} activeSlug="life" onChange={() => {}} />,
    );
    const activeTab = container.querySelector('[data-active="true"]');
    expect(activeTab).not.toBeNull();
    expect(activeTab?.textContent).toContain('生活');
  });

  it('should call onChange with tag slug when a tag tab is clicked', () => {
    const handleChange = vi.fn();
    render(<CategoryTabs tags={mockTags} activeSlug={null} onChange={handleChange} />);
    fireEvent.click(screen.getByText('开源'));
    expect(handleChange).toHaveBeenCalledWith('open-source');
  });

  it('should call onChange with null when "全部" tab is clicked', () => {
    const handleChange = vi.fn();
    render(<CategoryTabs tags={mockTags} activeSlug="tech" onChange={handleChange} />);
    fireEvent.click(screen.getByText('全部'));
    expect(handleChange).toHaveBeenCalledWith(null);
  });

  it('should render article count for each tag', () => {
    render(<CategoryTabs tags={mockTags} activeSlug={null} onChange={() => {}} />);
    expect(screen.getByText('15')).toBeDefined();
    expect(screen.getByText('8')).toBeDefined();
    expect(screen.getByText('5')).toBeDefined();
  });
});
