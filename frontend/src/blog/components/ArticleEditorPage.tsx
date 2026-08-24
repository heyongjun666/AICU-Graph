// ============================================================================
// ArticleEditorPage — Create/Edit article page with MDEditor
// Create mode: no articleId provided
// Edit mode: articleId provided, loads existing article data
// ============================================================================

import React, { useState, useEffect, useCallback, useRef } from 'react';
import { fetchArticleById, createArticle, updateArticle, fetchTags } from '../api';
import { MDEditor, PreviewPane } from '../../components/mdeditor';
import type { MDEditorRef } from '../../components/mdeditor';
import type { ArticleDetail, Tag } from '../types';

interface ArticleEditorPageProps {
  articleId?: number;
  onNavigate: (path: string) => void;
}

type EditorState = 'loading' | 'error' | 'ready' | 'saving';

export function ArticleEditorPage({ articleId, onNavigate }: ArticleEditorPageProps) {
  const isEditMode = articleId !== undefined;
  const editorRef = useRef<MDEditorRef>(null);

  const [pageState, setPageState] = useState<EditorState>(isEditMode ? 'loading' : 'ready');
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [selectedTags, setSelectedTags] = useState<Tag[]>([]);
  const [availableTags, setAvailableTags] = useState<Tag[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Load tags
  useEffect(() => {
    fetchTags()
      .then((tags) => setAvailableTags(tags))
      .catch(() => { /* tags are optional */ });
  }, []);

  // Load existing article in edit mode
  useEffect(() => {
    if (!isEditMode) return;
    let cancelled = false;

    fetchArticleById(articleId)
      .then((data: ArticleDetail) => {
        if (cancelled) return;
        setTitle(data.title);
        setContent(data.content);
        setSelectedTags(data.tags);
        setPageState('ready');
      })
      .catch(() => {
        if (cancelled) return;
        setPageState('error');
      });

    return () => { cancelled = true; };
  }, [articleId, isEditMode]);

  const handleSave = useCallback(async (status: 'draft' | 'published') => {
    if (!title.trim()) {
      setError('请输入文章标题');
      return;
    }
    setError(null);
    setPageState('saving');

    try {
      const tagNames = selectedTags.map((t) => t.name);
      if (isEditMode && articleId !== undefined) {
        await updateArticle(articleId, {
          title: title.trim(),
          content,
          tags: tagNames,
          status,
        });
      } else {
        await createArticle({
          title: title.trim(),
          content,
          tags: tagNames,
          status,
        });
      }
      onNavigate('/');
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败');
      setPageState('ready');
    }
  }, [title, content, selectedTags, isEditMode, articleId, onNavigate]);

  const handleTagToggle = useCallback((tag: Tag) => {
    setSelectedTags((prev) =>
      prev.some((t) => t.id === tag.id)
        ? prev.filter((t) => t.id !== tag.id)
        : [...prev, tag],
    );
  }, []);

  // Loading state
  if (pageState === 'loading') {
    return (
      <div data-testid="editor-skeleton" className="max-w-4xl mx-auto px-4 py-6 animate-pulse">
        <div className="h-8 bg-gray-200 rounded w-48 mb-6" />
        <div className="h-10 bg-gray-200 rounded w-full mb-4" />
        <div className="h-64 bg-gray-200 rounded w-full mb-4" />
      </div>
    );
  }

  // Error state
  if (pageState === 'error' && isEditMode) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <svg className="w-16 h-16 text-red-300 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4.5c-.77-.833-2.694-.833-3.464 0L3.34 16.5c-.77.833.192 2.5 1.732 2.5z" />
        </svg>
        <h3 className="text-lg font-medium text-gray-900 mb-1">加载失败</h3>
        <p className="text-sm text-gray-500 mb-4">无法加载文章数据</p>
        <button type="button" onClick={() => onNavigate('/')} className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors">
          返回列表
        </button>
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto px-4 py-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">
          {isEditMode ? '编辑文章' : '新建文章'}
        </h1>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => onNavigate('/')}
            className="px-4 py-2 bg-white text-gray-700 text-sm font-medium rounded-lg border border-gray-300 hover:bg-gray-50 transition-colors"
          >
            取消
          </button>
          <button
            type="button"
            onClick={() => handleSave('draft')}
            disabled={pageState === 'saving'}
            className="px-4 py-2 bg-white text-gray-700 text-sm font-medium rounded-lg border border-gray-300 hover:bg-gray-50 transition-colors disabled:opacity-50"
          >
            存为草稿
          </button>
          <button
            type="button"
            onClick={() => handleSave('published')}
            disabled={pageState === 'saving'}
            className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50"
          >
            发布
          </button>
        </div>
      </div>

      {/* Error message */}
      {error && (
        <div className="mb-4 px-4 py-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
          {error}
        </div>
      )}

      {/* Title input */}
      <div className="mb-4">
        <input
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="输入文章标题"
          className="w-full px-4 py-3 text-lg font-semibold border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
        />
      </div>

      {/* Tags */}
      <div className="mb-4">
        <label className="block text-sm font-medium text-gray-700 mb-2">标签</label>
        <div className="flex flex-wrap gap-2">
          {availableTags.map((tag) => {
            const isSelected = selectedTags.some((t) => t.id === tag.id);
            return (
              <button
                key={tag.id}
                type="button"
                onClick={() => handleTagToggle(tag)}
                className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-sm font-medium transition-colors ${
                  isSelected
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
                }`}
              >
                {tag.name}
              </button>
            );
          })}
          {availableTags.length === 0 && (
            <span className="text-sm text-gray-400">暂无可用标签</span>
          )}
        </div>
      </div>

      {/* Editor + Preview split */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Editor */}
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">内容 (Markdown)</label>
          <MDEditor
            ref={editorRef}
            value={content}
            onChange={setContent}
            height="500px"
            placeholder="开始写作..."
          />
        </div>

        {/* Live preview */}
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">预览</label>
          <div className="border border-gray-200 rounded-lg overflow-hidden" style={{ height: '558px' }}>
            <PreviewPane source={content} showToc={false} />
          </div>
        </div>
      </div>
    </div>
  );
}
