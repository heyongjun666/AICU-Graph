// ============================================================================
// CategoryTabs — Tag-based filter tabs / sidebar
// Shows "全部" tab and individual tag tabs with article counts
// Clicking a tag fires onChange(tagSlug), clicking "全部" fires onChange(null)
// ============================================================================

import React from 'react';
import type { Tag } from '../types';

interface CategoryTabsProps {
  tags: Tag[];
  activeSlug: string | null;
  onChange: (slug: string | null) => void;
}

function TabButton({
  label,
  count,
  isActive,
  onClick,
}: {
  label: string;
  count?: number;
  isActive: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      data-active={isActive ? 'true' : 'false'}
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-sm font-medium transition-colors ${
        isActive
          ? 'bg-blue-600 text-white'
          : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
      }`}
    >
      {label}
      {count !== undefined && (
        <span
          className={`text-xs ${
            isActive ? 'text-blue-200' : 'text-gray-400'
          }`}
        >
          {count}
        </span>
      )}
    </button>
  );
}

export function CategoryTabs({ tags, activeSlug, onChange }: CategoryTabsProps) {
  return (
    <div className="flex flex-wrap gap-2">
      <TabButton
        label="全部"
        isActive={activeSlug === null}
        onClick={() => onChange(null)}
      />
      {tags.map((tag) => (
        <TabButton
          key={tag.id}
          label={tag.name}
          count={tag.articleCount}
          isActive={activeSlug === tag.slug}
          onClick={() => onChange(tag.slug)}
        />
      ))}
    </div>
  );
}
