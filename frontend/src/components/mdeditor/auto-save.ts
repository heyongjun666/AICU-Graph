// ===== src/components/mdeditor/auto-save.ts =====
// 文章草稿本地自动保存 Hook
// 在 localStorage 中保存草稿，支持恢复、清理、过期检测

import { useState, useEffect, useCallback, useRef } from 'react';

export interface AutoSaveOptions {
  /** 文章 ID（用于区分不同文章的草稿） */
  postId?: string;
  /** 自动保存间隔（毫秒），默认 30000（30 秒） */
  interval?: number;
  /** 最大保存字数，默认 100000 */
  maxLength?: number;
  /** 草稿过期时间（毫秒），默认 7 天 */
  expiryMs?: number;
  /** 每篇文章最大草稿数，默认 5 */
  maxDrafts?: number;
}

export interface AutoSaveState {
  /** 最后保存的内容 */
  content: string;
  /** 最后保存时间 */
  savedAt: string;
  /** 是否有未保存的草稿 */
  hasDraft: boolean;
  /** 是否有未保存的更改 */
  isDirty: boolean;
  /** 保存状态 */
  status: 'idle' | 'saving' | 'saved' | 'error';
  /** 错误消息 */
  error?: string;
}

const STORAGE_PREFIX = 'md_autosave_';

/**
 * 生成存储 key
 */
function getStorageKey(postId?: string): string {
  return postId ? `${STORAGE_PREFIX}${postId}` : `${STORAGE_PREFIX}draft`;
}

/**
 * 读取 localStorage 中的草稿
 */
function loadDraft(postId?: string): string | null {
  try {
    const key = getStorageKey(postId);
    const stored = localStorage.getItem(key);
    if (!stored) return null;

    const { content, savedAt } = JSON.parse(stored) as { content: string; savedAt: string };
    const age = Date.now() - new Date(savedAt).getTime();
    const maxAge = 7 * 24 * 60 * 60 * 1000; // 7 天过期

    if (age > maxAge) {
      localStorage.removeItem(key);
      return null;
    }

    return content;
  } catch {
    return null;
  }
}

/**
 * 保存草稿到 localStorage
 */
function saveDraft(content: string, postId?: string): void {
  try {
    const key = getStorageKey(postId);
    const draft = JSON.stringify({
      content,
      savedAt: new Date().toISOString(),
    });
    localStorage.setItem(key, draft);

    // 清理旧草稿（每用户最多 5 份）
    cleanupOldDrafts(postId);
  } catch {
    // localStorage 满时静默失败
    console.warn('[AutoSave] Failed to save draft: localStorage may be full');
  }
}

/**
 * 清理过期草稿
 */
function cleanupOldDrafts(currentPostId?: string): void {
  try {
    const keys: { key: string; time: number }[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key?.startsWith(STORAGE_PREFIX) && key !== getStorageKey(currentPostId)) {
        try {
          const data = JSON.parse(localStorage.getItem(key) || '{}');
          keys.push({ key, time: new Date(data.savedAt || 0).getTime() });
        } catch {
          localStorage.removeItem(key!);
        }
      }
    }

    // 只保留最新的 5 份草稿
    if (keys.length > 5) {
      keys.sort((a, b) => b.time - a.time);
      for (let i = 5; i < keys.length; i++) {
        localStorage.removeItem(keys[i].key);
      }
    }
  } catch {
    // 静默失败
  }
}

/**
 * 删除草稿
 */
function removeDraft(postId?: string): void {
  try {
    const key = getStorageKey(postId);
    localStorage.removeItem(key);
  } catch {
    // 静默失败
  }
}

/**
 * useAutoSave Hook
 *
 * 自动保存文章草稿到 localStorage。提供保存状态指示、草稿恢复、草稿清理功能。
 *
 * @param content - 当前编辑器内容
 * @param options - 配置选项
 * @returns AutoSaveState
 */
export function useAutoSave(
  content: string,
  options?: AutoSaveOptions,
): AutoSaveState & { removeDraft: () => void; forceSave: () => void } {
  const { postId, interval = 30000 } = options || {};

  const [state, setState] = useState<AutoSaveState>(() => ({
    content: '',
    savedAt: '',
    hasDraft: loadDraft(postId) !== null,
    isDirty: false,
    status: 'idle',
  }));

  const prevContentRef = useRef(content);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 比较内容变化，标记 dirty
  useEffect(() => {
    if (prevContentRef.current !== content) {
      prevContentRef.current = content;
      setState((prev) => ({ ...prev, isDirty: true, status: 'idle' }));
    }
  }, [content]);

  // 自动保存定时器
  useEffect(() => {
    if (!content || content.length === 0) return;

    // 只有内容有变化时才启动自动保存
    timerRef.current = setInterval(() => {
      setState((prev) => {
        if (!prev.isDirty) return { ...prev, status: 'idle' as const };

        try {
          saveDraft(content, postId);
          return {
            ...prev,
            content,
            savedAt: new Date().toISOString(),
            isDirty: false,
            hasDraft: true,
            status: 'saved' as const,
            error: undefined,
          };
        } catch (err) {
          return {
            ...prev,
            status: 'error' as const,
            error: err instanceof Error ? err.message : 'Auto-save failed',
          };
        }
      });
    }, interval);

    return () => {
      if (timerRef.current) {
        clearInterval(timerRef.current);
      }
    };
  }, [content, interval, postId]);

  // 手动删除草稿
  const handleRemoveDraft = useCallback(() => {
    removeDraft(postId);
    setState((prev) => ({
      ...prev,
      hasDraft: false,
      content: '',
      savedAt: '',
      isDirty: false,
      status: 'idle',
    }));
  }, [postId]);

  // 强制保存
  const handleForceSave = useCallback(() => {
    if (!content) return;
    try {
      saveDraft(content, postId);
      setState((prev) => ({
        ...prev,
        content,
        savedAt: new Date().toISOString(),
        isDirty: false,
        hasDraft: true,
        status: 'saved',
        error: undefined,
      }));
    } catch (err) {
      setState((prev) => ({
        ...prev,
        status: 'error',
        error: err instanceof Error ? err.message : 'Auto-save failed',
      }));
    }
  }, [content, postId]);

  return {
    ...state,
    removeDraft: handleRemoveDraft,
    forceSave: handleForceSave,
  };
}

/**
 * 恢复草稿
 */
export function restoreDraft(postId?: string): string | null {
  return loadDraft(postId);
}

/**
 * 清除所有自动保存的草稿
 */
export function clearAllDrafts(): void {
  try {
    const keysToRemove: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key?.startsWith(STORAGE_PREFIX)) {
        keysToRemove.push(key);
      }
    }
    keysToRemove.forEach((key) => localStorage.removeItem(key));
  } catch {
    // 静默失败
  }
}
