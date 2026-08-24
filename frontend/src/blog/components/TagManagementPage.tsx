// ============================================================================
// TagManagementPage — Tag management page
// Features:
//   (1) Tag list with name + article count
//   (2) Create new tag inline dialog
//   (3) Rename tag inline edit
//   (4) Delete tag with confirmation
//   (5) Click tag navigates to filtered article list
// ============================================================================

import React, { useState, useCallback, useEffect, useRef } from 'react';
import { fetchTags, createTag, renameTag, deleteTag } from '../api';
import type { Tag } from '../types';

interface TagManagementPageProps {
  onNavigate: (path: string) => void;
}

type PageState = 'loading' | 'error' | 'empty' | 'ready';

// ============================================================================
// Skeleton component for loading state
// ============================================================================

function TagListSkeleton() {
  return (
    <div data-testid="tag-list-skeleton" className="space-y-3">
      {Array.from({ length: 5 }, (_, i) => (
        <div
          key={i}
          className="flex items-center justify-between rounded-lg border border-gray-200 bg-white p-4 animate-pulse"
        >
          <div className="h-5 bg-gray-200 rounded w-32" />
          <div className="h-5 bg-gray-200 rounded w-16" />
        </div>
      ))}
    </div>
  );
}

// ============================================================================
// Empty state
// ============================================================================

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <svg
        className="w-16 h-16 text-gray-300 mb-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={1}
          d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z"
        />
      </svg>
      <h3 className="text-lg font-medium text-gray-900 mb-1">还没有标签</h3>
      <p className="text-sm text-gray-500">点击上方"新建标签"按钮创建第一个标签</p>
    </div>
  );
}

// ============================================================================
// Error state
// ============================================================================

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <svg
        className="w-16 h-16 text-red-300 mb-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={1}
          d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4.5c-.77-.833-2.694-.833-3.464 0L3.34 16.5c-.77.833.192 2.5 1.732 2.5z"
        />
      </svg>
      <h3 className="text-lg font-medium text-gray-900 mb-1">加载失败</h3>
      <p className="text-sm text-gray-500 mb-4">请检查网络连接后重试</p>
      <button
        type="button"
        onClick={onRetry}
        className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
      >
        重试
      </button>
    </div>
  );
}

// ============================================================================
// Create tag inline dialog
// ============================================================================

interface CreateTagDialogProps {
  onSubmit: (name: string) => void;
  onCancel: () => void;
}

function CreateTagDialog({ onSubmit, onCancel }: CreateTagDialogProps) {
  const [name, setName] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const handleSubmit = useCallback(() => {
    const trimmed = name.trim();
    if (trimmed) {
      onSubmit(trimmed);
    }
  }, [name, onSubmit]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter') {
        handleSubmit();
      } else if (e.key === 'Escape') {
        onCancel();
      }
    },
    [handleSubmit, onCancel],
  );

  return (
    <div className="flex items-center gap-2 p-3 bg-blue-50 rounded-lg border border-blue-200">
      <input
        ref={inputRef}
        type="text"
        value={name}
        placeholder="输入标签名称"
        onChange={(e) => setName(e.target.value)}
        onKeyDown={handleKeyDown}
        className="flex-1 px-3 py-1.5 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
      />
      <button
        type="button"
        onClick={handleSubmit}
        className="px-3 py-1.5 bg-blue-600 text-white text-sm font-medium rounded-md hover:bg-blue-700 transition-colors"
      >
        确认
      </button>
      <button
        type="button"
        onClick={onCancel}
        className="px-3 py-1.5 bg-white text-gray-700 text-sm font-medium rounded-md border border-gray-300 hover:bg-gray-50 transition-colors"
      >
        取消
      </button>
    </div>
  );
}

// ============================================================================
// Confirm delete dialog
// ============================================================================

interface ConfirmDeleteDialogProps {
  tagName: string;
  onConfirm: () => void;
  onCancel: () => void;
}

function ConfirmDeleteDialog({ tagName, onConfirm, onCancel }: ConfirmDeleteDialogProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="bg-white rounded-lg shadow-xl p-6 max-w-sm w-full mx-4">
        <h3 className="text-lg font-semibold text-gray-900 mb-2">确认删除</h3>
        <p className="text-sm text-gray-600 mb-6">
          确定删除标签 "{tagName}" 吗？
        </p>
        <div className="flex justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            className="px-4 py-2 bg-white text-gray-700 text-sm font-medium rounded-md border border-gray-300 hover:bg-gray-50 transition-colors"
          >
            取消
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="px-4 py-2 bg-red-600 text-white text-sm font-medium rounded-md hover:bg-red-700 transition-colors"
          >
            确定
          </button>
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// Tag row component
// ============================================================================

interface TagRowProps {
  tag: Tag;
  onRename: (id: number, name: string) => Promise<void>;
  onDelete: (id: number) => void;
  onClick: (slug: string) => void;
}

function TagRow({ tag, onRename, onDelete, onClick }: TagRowProps) {
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(tag.name);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);

  const handleStartEdit = useCallback((e: React.MouseEvent) => {
    e.stopPropagation();
    setEditName(tag.name);
    setEditing(true);
  }, [tag.name]);

  const handleConfirmRename = useCallback(async () => {
    const trimmed = editName.trim();
    if (trimmed && trimmed !== tag.name) {
      try {
        await onRename(tag.id, trimmed);
      } catch {
        // API call failed — keep editing open so user sees failure and can retry
        inputRef.current?.focus();
        return;
      }
    }
    // Only close editing on success or no-op (name unchanged)
    setEditing(false);
  }, [editName, tag.id, tag.name, onRename]);

  const handleCancelRename = useCallback(() => {
    setEditing(false);
  }, []);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      e.stopPropagation();
      if (e.key === 'Enter') {
        handleConfirmRename();
      } else if (e.key === 'Escape') {
        handleCancelRename();
      }
    },
    [handleConfirmRename, handleCancelRename],
  );

  const handleDelete = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      onDelete(tag.id);
    },
    [tag.id, onDelete],
  );

  const handleClick = useCallback(() => {
    onClick(tag.slug);
  }, [tag.slug, onClick]);

  if (editing) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-blue-300 bg-blue-50 p-3">
        <input
          ref={inputRef}
          type="text"
          value={editName}
          onChange={(e) => setEditName(e.target.value)}
          onKeyDown={handleKeyDown}
          className="flex-1 px-3 py-1.5 text-sm border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
        />
        <button
          type="button"
          aria-label="确认重命名"
          onClick={handleConfirmRename}
          className="px-3 py-1.5 bg-blue-600 text-white text-xs font-medium rounded-md hover:bg-blue-700 transition-colors"
        >
          确认
        </button>
        <button
          type="button"
          aria-label="取消重命名"
          onClick={handleCancelRename}
          className="px-3 py-1.5 bg-white text-gray-700 text-xs font-medium rounded-md border border-gray-300 hover:bg-gray-50 transition-colors"
        >
          取消
        </button>
      </div>
    );
  }

  return (
    <div
      className="flex items-center justify-between rounded-lg border border-gray-200 bg-white p-3 hover:shadow-sm transition-shadow cursor-pointer"
      onClick={handleClick}
    >
      <div className="flex items-center gap-3">
        <span className="text-sm font-medium text-gray-900">{tag.name}</span>
        <span className="text-xs text-gray-400 bg-gray-100 px-2 py-0.5 rounded-full">
          {tag.articleCount} 篇文章
        </span>
      </div>
      <div className="flex items-center gap-1">
        <button
          type="button"
          aria-label="重命名"
          onClick={handleStartEdit}
          className="p-1.5 text-gray-400 hover:text-blue-600 hover:bg-blue-50 rounded transition-colors"
          title="重命名"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"
            />
          </svg>
        </button>
        <button
          type="button"
          aria-label="删除"
          onClick={handleDelete}
          className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded transition-colors"
          title="删除"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
            />
          </svg>
        </button>
      </div>
    </div>
  );
}

// ============================================================================
// Main page component
// ============================================================================

export function TagManagementPage({ onNavigate }: TagManagementPageProps) {
  const [pageState, setPageState] = useState<PageState>('loading');
  const [tags, setTags] = useState<Tag[]>([]);
  const [retryCounter, setRetryCounter] = useState(0);
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [deleteTargetId, setDeleteTargetId] = useState<number | null>(null);

  // Fetch tags with cancellation to avoid stale-response races on fast retry
  useEffect(() => {
    let cancelled = false;
    setPageState('loading');

    fetchTags()
      .then((data) => {
        if (cancelled) return;
        setTags(data);
        setPageState(data.length === 0 ? 'empty' : 'ready');
      })
      .catch(() => {
        if (cancelled) return;
        setPageState('error');
      });

    return () => {
      cancelled = true;
    };
  }, [retryCounter]);

  const handleRetry = useCallback(() => {
    setRetryCounter((c) => c + 1);
  }, []);

  // Create tag
  const handleCreateTag = useCallback(
    async (name: string) => {
      try {
        const newTag = await createTag(name);
        setTags((prev) => [...prev, newTag]);
        setShowCreateDialog(false);
      } catch {
        // silently fail — could surface via toast
      }
    },
    [],
  );

  // Rename tag
  const handleRenameTag = useCallback(
    async (id: number, name: string) => {
      const updated = await renameTag(id, name);
      setTags((prev) => prev.map((t) => (t.id === id ? updated : t)));
    },
    [],
  );

  // Delete tag
  const handleDeleteRequest = useCallback((id: number) => {
    setDeleteTargetId(id);
  }, []);

  const handleDeleteConfirm = useCallback(async () => {
    if (deleteTargetId === null) return;
    try {
      await deleteTag(deleteTargetId);
      setTags((prev) => prev.filter((t) => t.id !== deleteTargetId));
      setDeleteTargetId(null);
    } catch {
      // silently fail
    }
  }, [deleteTargetId]);

  const handleDeleteCancel = useCallback(() => {
    setDeleteTargetId(null);
  }, []);

  // Navigation
  const handleTagClick = useCallback(
    (slug: string) => {
      onNavigate(`/articles?tag=${slug}`);
    },
    [onNavigate],
  );

  const deleteTargetTag = deleteTargetId !== null
    ? tags.find((t) => t.id === deleteTargetId) ?? null
    : null;

  return (
    <div className="max-w-3xl mx-auto px-4 py-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">标签管理</h1>
        <button
          type="button"
          onClick={() => setShowCreateDialog(true)}
          className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
        >
          新建标签
        </button>
      </div>

      {/* Create tag dialog */}
      {showCreateDialog && (
        <div className="mb-4">
          <CreateTagDialog
            onSubmit={handleCreateTag}
            onCancel={() => setShowCreateDialog(false)}
          />
        </div>
      )}

      {/* Loading */}
      {pageState === 'loading' && <TagListSkeleton />}

      {/* Error */}
      {pageState === 'error' && <ErrorState onRetry={handleRetry} />}

      {/* Empty */}
      {pageState === 'empty' && <EmptyState />}

      {/* Tag list */}
      {pageState === 'ready' && (
        <div className="space-y-2" data-testid="tag-list">
          {tags.map((tag) => (
            <TagRow
              key={tag.id}
              tag={tag}
              onRename={handleRenameTag}
              onDelete={handleDeleteRequest}
              onClick={handleTagClick}
            />
          ))}
        </div>
      )}

      {/* Delete confirmation modal */}
      {deleteTargetTag && (
        <ConfirmDeleteDialog
          tagName={deleteTargetTag.name}
          onConfirm={handleDeleteConfirm}
          onCancel={handleDeleteCancel}
        />
      )}
    </div>
  );
}
