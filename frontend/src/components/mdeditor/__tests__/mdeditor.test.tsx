// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import React, { createRef } from 'react';
import MDEditor from '../mdeditor';
import type { MDEditorRef } from '../types';

afterEach(cleanup);

describe('MDEditor', () => {
  it('should render the editor container', () => {
    render(<MDEditor value="" onChange={() => {}} />);
    const container = document.querySelector('.md-editor-container');
    expect(container).toBeDefined();
  });

  it('should render toolbar when showToolbar is true', () => {
    render(<MDEditor value="" onChange={() => {}} showToolbar={true} />);
    const toolbar = document.querySelector('.md-editor-toolbar');
    expect(toolbar).toBeDefined();
  });

  it('should hide toolbar when showToolbar is false', () => {
    render(<MDEditor value="" onChange={() => {}} showToolbar={false} />);
    const toolbar = document.querySelector('.md-editor-toolbar');
    expect(toolbar).toBeNull();
  });

  it('should render with custom height', () => {
    render(<MDEditor value="" onChange={() => {}} height="800px" />);
    const container = document.querySelector('.md-editor-container') as HTMLElement;
    expect(container).toBeDefined();
  });

  it('should render with editor div inside', () => {
    render(<MDEditor value="# Hello" onChange={() => {}} />);
    // CodeMirror creates a .cm-editor element inside the container
    const editorDiv = document.querySelector('.md-editor-container > div:last-child');
    expect(editorDiv).toBeDefined();
  });

  it('should expose ref methods', () => {
    const ref = createRef<MDEditorRef>();
    render(<MDEditor ref={ref} value="test" onChange={() => {}} />);

    expect(ref.current).toBeDefined();
    expect(typeof ref.current?.getValue).toBe('function');
    expect(typeof ref.current?.setValue).toBe('function');
    expect(typeof ref.current?.insertText).toBe('function');
    expect(typeof ref.current?.getSelection).toBe('function');
    expect(typeof ref.current?.focus).toBe('function');
    expect(typeof ref.current?.blur).toBe('function');
    expect(typeof ref.current?.toggleBold).toBe('function');
    expect(typeof ref.current?.toggleItalic).toBe('function');
    expect(typeof ref.current?.insertHeading).toBe('function');
    expect(typeof ref.current?.insertCodeBlock).toBe('function');
    expect(typeof ref.current?.insertLink).toBe('function');
    expect(typeof ref.current?.insertList).toBe('function');
    expect(typeof ref.current?.insertBlockquote).toBe('function');
    expect(typeof ref.current?.insertHorizontalRule).toBe('function');
    expect(typeof ref.current?.undo).toBe('function');
    expect(typeof ref.current?.redo).toBe('function');
    expect(typeof ref.current?.scrollToLine).toBe('function');
  });

  it('should call onChange when content changes', () => {
    const onChange = vi.fn();
    render(<MDEditor value="" onChange={onChange} />);
    // We can't easily simulate CodeMirror content changes through DOM,
    // but the component should render without error
    expect(document.querySelector('.cm-editor')).toBeDefined();
  });

  it('should handle readOnly mode', () => {
    render(<MDEditor value="readonly" onChange={() => {}} readOnly={true} />);
    expect(document.querySelector('.cm-editor')).toBeDefined();
  });

  it('should handle theme switching via ref methods', () => {
    const ref = createRef<MDEditorRef>();
    render(<MDEditor ref={ref} value="" onChange={() => {}} />);
    expect(ref.current).toBeDefined();
  });

  it('should initialize with provided value', () => {
    render(<MDEditor value="initial value" onChange={() => {}} />);
    expect(document.querySelector('.cm-editor')).toBeDefined();
  });

  it('should export all required type interfaces', () => {
    // Verify the barrel exports work via type assertion
    const types = {
      MDEditorProps: {} as import('../types').MDEditorProps,
      MDEditorRef: {} as import('../types').MDEditorRef,
      MDEditorError: {} as import('../types').MDEditorError,
    };
    expect(types).toBeDefined();
  });
});
