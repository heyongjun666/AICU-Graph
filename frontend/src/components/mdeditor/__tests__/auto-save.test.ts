// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useAutoSave, restoreDraft, clearAllDrafts } from '../auto-save';

describe('useAutoSave', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.useFakeTimers();
  });

  it('should start with idle status and no draft', () => {
    const { result } = renderHook(() => useAutoSave(''));
    expect(result.current.status).toBe('idle');
    expect(result.current.hasDraft).toBe(false);
    expect(result.current.isDirty).toBe(false);
  });

  it('should mark as dirty when content changes', () => {
    const { result, rerender } = renderHook(
      ({ content }) => useAutoSave(content, { postId: 'test-1' }),
      { initialProps: { content: '' } },
    );

    rerender({ content: 'new content' });
    expect(result.current.isDirty).toBe(true);
  });

  it('should auto-save after interval and update status to saved', () => {
    const { result, rerender } = renderHook(
      ({ content }) => useAutoSave(content, { postId: 'test-2', interval: 30000 }),
      { initialProps: { content: '' } },
    );

    rerender({ content: 'draft content' });
    expect(result.current.isDirty).toBe(true);

    act(() => {
      vi.advanceTimersByTime(30000);
    });

    expect(result.current.status).toBe('saved');
    expect(result.current.hasDraft).toBe(true);
    expect(result.current.isDirty).toBe(false);
  });

  it('should save and restore draft from localStorage', () => {
    const { result, rerender } = renderHook(
      ({ content }) => useAutoSave(content, { postId: 'test-3', interval: 100 }),
      { initialProps: { content: '' } },
    );

    rerender({ content: 'persisted content' });
    expect(result.current.isDirty).toBe(true);

    act(() => {
      vi.advanceTimersByTime(100);
    });

    expect(result.current.status).toBe('saved');

    // Simulate fresh hook load - should find saved draft
    const loaded = restoreDraft('test-3');
    expect(loaded).toBe('persisted content');
  });

  it('should remove draft when removeDraft is called', () => {
    const { result, rerender } = renderHook(
      ({ content }) => useAutoSave(content, { postId: 'test-4', interval: 100 }),
      { initialProps: { content: '' } },
    );

    rerender({ content: 'to be removed' });

    act(() => {
      vi.advanceTimersByTime(100);
    });

    expect(result.current.hasDraft).toBe(true);

    act(() => {
      result.current.removeDraft();
    });

    expect(result.current.hasDraft).toBe(false);
    expect(restoreDraft('test-4')).toBeNull();
  });

  it('should handle expired drafts', () => {
    // Save an expired draft
    const expiredDate = new Date(Date.now() - 8 * 24 * 60 * 60 * 1000).toISOString();
    localStorage.setItem(
      'md_autosave_expired-test',
      JSON.stringify({ content: 'expired', savedAt: expiredDate }),
    );

    const loaded = restoreDraft('expired-test');
    expect(loaded).toBeNull();
  });

  it('should clean up all drafts', () => {
    const { rerender } = renderHook(
      ({ content }) => useAutoSave(content, { postId: 'clean-test', interval: 100 }),
      { initialProps: { content: '' } },
    );

    rerender({ content: 'draft 1' });

    act(() => {
      vi.advanceTimersByTime(100);
    });

    clearAllDrafts();
    expect(restoreDraft('clean-test')).toBeNull();
  });

  it('should force save when forceSave is called', () => {
    const { result } = renderHook(() =>
      useAutoSave('force content', { postId: 'force-test', interval: 30000 }),
    );

    act(() => {
      result.current.forceSave();
    });

    expect(result.current.status).toBe('saved');
    expect(restoreDraft('force-test')).toBe('force content');
  });
});
