// ===== src/components/mdeditor/mdeditor.tsx =====
// MDEditor 核心组件 — 基于 CodeMirror 6 的 Markdown 编辑器
// 严格遵循 design.md §6.2~§6.4 接口规范

import React, {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from 'react';
import { EditorState, Compartment } from '@codemirror/state';
import { EditorView, keymap, placeholder as placeholderExt } from '@codemirror/view';
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { syntaxHighlighting, defaultHighlightStyle } from '@codemirror/language';
import { Prec } from '@codemirror/state';

import type { MDEditorProps, MDEditorRef, MDEditorError } from './types';
import { getTheme, type EditorTheme } from './editor-theme';

// ============================================================
// 状态更新 compartment：用于创建和替换 editor state 更新监听
// ============================================================
const updateCompartment = new Compartment();
const editableCompartment = new Compartment();

// ============================================================
// 快捷键定义
// ============================================================
function buildKeymap(props: MDEditorProps) {
  const shortcuts = [];

  if (props.enableShortcuts !== false) {
    if (props.onSave) {
      shortcuts.push({
        key: 'Mod-s',
        run: () => {
          props.onSave?.(props.value);
          return true;
        },
      });
    }

    // Ctrl/Cmd+B → toggle bold
    shortcuts.push({
      key: 'Mod-b',
      run: (view: EditorView) => {
        insertFormatting(view, '**', '**');
        return true;
      },
    });

    // Ctrl/Cmd+I → toggle italic
    shortcuts.push({
      key: 'Mod-i',
      run: (view: EditorView) => {
        insertFormatting(view, '*', '*');
        return true;
      },
    });
  }

  return shortcuts;
}

// ============================================================
// 格式化辅助函数
// ============================================================
function insertFormatting(view: EditorView, before: string, after: string) {
  const selection = view.state.selection.main;
  const text = view.state.sliceDoc(selection.from, selection.to);
  view.dispatch({
    changes: {
      from: selection.from,
      to: selection.to,
      insert: `${before}${text || 'text'}${after}`,
    },
    selection: {
      anchor: selection.from + before.length,
      head: selection.from + before.length + (text.length || 4),
    },
  });
}

// ============================================================
// 编辑器主题 compartment（支持运行时切换）
// ============================================================
const themeCompartment = new Compartment();

// ============================================================
// MDEditor 组件
// ============================================================
const MDEditor = forwardRef<MDEditorRef, MDEditorProps>((props, ref) => {
  const {
    value,
    onChange,
    readOnly = false,
    height = '500px',
    placeholder: placeholderText = '开始写作...',
    maxLength = 100000,
    showToolbar = true,
    enablePreview = true,
    enableShortcuts = true,
    enableImageUpload = true,
    onImageUpload,
    onSave,
    onError,
    onFocusChange,
  } = props;

  const editorRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const isUpdatingFromOutside = useRef(false);
  const [theme, setTheme] = useState<EditorTheme>('light');

  // --- 初始化 EditorView ---
  useEffect(() => {
    if (!editorRef.current) return;

    const updateListener = EditorView.updateListener.of((update) => {
      if (update.docChanged && !isUpdatingFromOutside.current) {
        const newValue = update.state.doc.toString();
        onChange(newValue);
      }
      if (update.focusChanged) {
        onFocusChange?.(update.view.hasFocus);
      }
    });

    // 验证内容长度
    const contentValidator = EditorState.changeFilter.of((tr) => {
      let valid = true;
      tr.changes.iterChanges((_fromA, _toA, _fromB, _toB, inserted) => {
        const newLen = tr.state.doc.length + inserted.length - (_toA - _fromA);
        if (newLen > maxLength) {
          valid = false;
        }
      });
      return valid;
    });

    const startState = EditorState.create({
      doc: value,
      extensions: [
        markdown({ base: markdownLanguage }),
        syntaxHighlighting(defaultHighlightStyle),
        history(),
        keymap.of([...defaultKeymap, ...historyKeymap]),
        Prec.high(keymap.of(buildKeymap(props))),
        updateCompartment.of(updateListener),
        themeCompartment.of(getTheme(theme)),
        placeholderExt(placeholderText),
        contentValidator,
        editableCompartment.of(EditorView.editable.of(!readOnly)),
        EditorView.lineWrapping,
      ],
    });

    const view = new EditorView({
      state: startState,
      parent: editorRef.current,
    });

    viewRef.current = view;

    return () => {
      view.destroy();
      viewRef.current = null;
    };
  }, []); // 只初始化一次

  // --- 同步外部 value 到编辑器 ---
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;

    const currentValue = view.state.doc.toString();
    if (currentValue !== value) {
      isUpdatingFromOutside.current = true;
      view.dispatch({
        changes: {
          from: 0,
          to: currentValue.length,
          insert: value,
        },
      });
      isUpdatingFromOutside.current = false;
    }
  }, [value]);

  // --- 同步 readOnly ---
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: editableCompartment.reconfigure(EditorView.editable.of(!readOnly)),
    });
  }, [readOnly]);

  // --- 暴露 ref 方法 ---
  useImperativeHandle(
    ref,
    (): MDEditorRef => ({
      getValue(): string {
        return viewRef.current?.state.doc.toString() ?? '';
      },

      setValue(val: string): void {
        const view = viewRef.current;
        if (!view) return;
        isUpdatingFromOutside.current = true;
        const current = view.state.doc.toString();
        view.dispatch({
          changes: { from: 0, to: current.length, insert: val },
        });
        isUpdatingFromOutside.current = false;
      },

      insertText(text: string): void {
        const view = viewRef.current;
        if (!view) return;
        const sel = view.state.selection.main;
        view.dispatch({
          changes: { from: sel.from, to: sel.to, insert: text },
          selection: { anchor: sel.from + text.length },
        });
        view.focus();
      },

      getSelection(): string {
        const view = viewRef.current;
        if (!view) return '';
        const sel = view.state.selection.main;
        return view.state.sliceDoc(sel.from, sel.to);
      },

      replaceSelection(text: string): void {
        const view = viewRef.current;
        if (!view) return;
        const sel = view.state.selection.main;
        view.dispatch({
          changes: { from: sel.from, to: sel.to, insert: text },
          selection: { anchor: sel.from + text.length },
        });
        view.focus();
      },

      focus(): void {
        viewRef.current?.focus();
      },

      blur(): void {
        viewRef.current?.contentDOM.blur();
      },

      scrollToLine(line: number): void {
        const view = viewRef.current;
        if (!view) return;
        const lineObj = view.state.doc.line(Math.max(1, Math.min(line, view.state.doc.lines)));
        view.dispatch({
          effects: EditorView.scrollIntoView(lineObj.from, { y: 'center' }),
        });
      },

      async insertImage(): Promise<void> {
        if (!onImageUpload) return;
        try {
          const url = await onImageUpload(new File([], ''));
          const view = viewRef.current;
          if (view) {
            const sel = view.state.selection.main;
            view.dispatch({
              changes: { from: sel.from, to: sel.to, insert: `![alt](${url})` },
              selection: { anchor: sel.from + 4 },
            });
            view.focus();
          }
        } catch (err) {
          onError?.({
            code: 'UPLOAD_FAILED',
            message: err instanceof Error ? err.message : 'Image upload failed',
          });
        }
      },

      toggleBold(): void {
        const view = viewRef.current;
        if (!view) return;
        insertFormatting(view, '**', '**');
        view.focus();
      },

      toggleItalic(): void {
        const view = viewRef.current;
        if (!view) return;
        insertFormatting(view, '*', '*');
        view.focus();
      },

      insertHeading(level: 1 | 2 | 3 | 4 | 5 | 6): void {
        const view = viewRef.current;
        if (!view) return;
        const sel = view.state.selection.main;
        const line = view.state.doc.lineAt(sel.from);
        const prefix = '#'.repeat(level) + ' ';
        view.dispatch({
          changes: { from: line.from, to: line.from, insert: prefix },
          selection: { anchor: line.from + prefix.length },
        });
        view.focus();
      },

      insertCodeBlock(language?: string): void {
        const view = viewRef.current;
        if (!view) return;
        const sel = view.state.selection.main;
        const code = '```' + (language || '') + '\n\n```';
        view.dispatch({
          changes: { from: sel.from, to: sel.to, insert: code },
          selection: { anchor: sel.from + code.length - 4 },
        });
        view.focus();
      },

      insertLink(url: string, text?: string): void {
        const view = viewRef.current;
        if (!view) return;
        const sel = view.state.selection.main;
        const linkText = text || view.state.sliceDoc(sel.from, sel.to) || 'link';
        view.dispatch({
          changes: { from: sel.from, to: sel.to, insert: `[${linkText}](${url})` },
          selection: { anchor: sel.from + 1 },
        });
        view.focus();
      },

      insertList(type: 'unordered' | 'ordered'): void {
        const view = viewRef.current;
        if (!view) return;
        const sel = view.state.selection.main;
        const line = view.state.doc.lineAt(sel.from);
        const marker = type === 'ordered' ? '1. ' : '- ';
        view.dispatch({
          changes: { from: line.from, to: line.from, insert: marker },
          selection: { anchor: line.from + marker.length },
        });
        view.focus();
      },

      insertBlockquote(): void {
        const view = viewRef.current;
        if (!view) return;
        const sel = view.state.selection.main;
        const line = view.state.doc.lineAt(sel.from);
        view.dispatch({
          changes: { from: line.from, to: line.from, insert: '> ' },
          selection: { anchor: line.from + 2 },
        });
        view.focus();
      },

      insertHorizontalRule(): void {
        const view = viewRef.current;
        if (!view) return;
        const sel = view.state.selection.main;
        view.dispatch({
          changes: { from: sel.from, to: sel.from, insert: '\n---\n' },
          selection: { anchor: sel.from + 5 },
        });
        view.focus();
      },

      undo(): void {
        const view = viewRef.current;
        if (!view) return;
        view.dispatch({ ...(view.state as any) });
        // Trigger undo via history keymap
        const cmd = historyKeymap.find((k) => k.key === 'Mod-z');
        cmd?.run?.(view);
      },

      redo(): void {
        const view = viewRef.current;
        if (!view) return;
        const cmd = historyKeymap.find((k) => k.key === 'Mod-Shift-z' || k.key === 'Mod-y');
        cmd?.run?.(view);
      },
    }),
    [onImageUpload, onError, onSave],
  );

  // --- 主题切换 ---
  const switchTheme = useCallback((newTheme: EditorTheme) => {
    setTheme(newTheme);
    const view = viewRef.current;
    if (view) {
      view.dispatch({
        effects: themeCompartment.reconfigure(getTheme(newTheme)),
      });
    }
  }, []);

  // --- 计算编辑器容器高度 ---
  const containerStyle = useMemo(
    () => ({
      height: typeof height === 'number' ? `${height}px` : height,
      border: '1px solid #d0d0d0',
      borderRadius: '6px',
      overflow: 'hidden',
      display: 'flex',
      flexDirection: 'column' as const,
    }),
    [height],
  );

  const editorAreaStyle = useMemo(
    () => ({
      flex: 1,
      overflow: 'auto',
      position: 'relative' as const,
    }),
    [],
  );

  return (
    <div className="md-editor-container" style={containerStyle}>
      {showToolbar && (
        <MDEditorToolbar
          onToggleBold={() => (ref as React.RefObject<MDEditorRef>)?.current?.toggleBold()}
          onToggleItalic={() => (ref as React.RefObject<MDEditorRef>)?.current?.toggleItalic()}
          onInsertHeading={(level) =>
            (ref as React.RefObject<MDEditorRef>)?.current?.insertHeading(level)
          }
          onInsertCodeBlock={() =>
            (ref as React.RefObject<MDEditorRef>)?.current?.insertCodeBlock()
          }
          onInsertLink={() =>
            (ref as React.RefObject<MDEditorRef>)?.current?.insertLink('')
          }
          onInsertImage={() =>
            (ref as React.RefObject<MDEditorRef>)?.current?.insertImage()
          }
          onInsertList={(type) =>
            (ref as React.RefObject<MDEditorRef>)?.current?.insertList(type)
          }
          onInsertBlockquote={() =>
            (ref as React.RefObject<MDEditorRef>)?.current?.insertBlockquote()
          }
          onInsertHorizontalRule={() =>
            (ref as React.RefObject<MDEditorRef>)?.current?.insertHorizontalRule()
          }
          onUndo={() => (ref as React.RefObject<MDEditorRef>)?.current?.undo()}
          onRedo={() => (ref as React.RefObject<MDEditorRef>)?.current?.redo()}
          theme={theme}
          onThemeToggle={() => switchTheme(theme === 'light' ? 'dark' : 'light')}
        />
      )}
      <div ref={editorRef} style={editorAreaStyle} />
    </div>
  );
});

MDEditor.displayName = 'MDEditor';

// ============================================================
// 工具栏子组件（在此文件中定义，避免循环依赖）
// ============================================================
interface ToolbarProps {
  onToggleBold: () => void;
  onToggleItalic: () => void;
  onInsertHeading: (level: 1 | 2 | 3) => void;
  onInsertCodeBlock: () => void;
  onInsertLink: () => void;
  onInsertImage: () => void;
  onInsertList: (type: 'unordered' | 'ordered') => void;
  onInsertBlockquote: () => void;
  onInsertHorizontalRule: () => void;
  onUndo: () => void;
  onRedo: () => void;
  theme: EditorTheme;
  onThemeToggle: () => void;
}

function MDEditorToolbar({
  onToggleBold,
  onToggleItalic,
  onInsertHeading,
  onInsertCodeBlock,
  onInsertLink,
  onInsertImage,
  onInsertList,
  onInsertBlockquote,
  onInsertHorizontalRule,
  onUndo,
  onRedo,
  theme,
  onThemeToggle,
}: ToolbarProps) {
  const toolbarStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: '2px',
    padding: '4px 8px',
    borderBottom: '1px solid #d0d0d0',
    backgroundColor: theme === 'dark' ? '#2d2d2d' : '#fafafa',
    flexWrap: 'wrap',
  };

  const btnStyle = (active = false): React.CSSProperties => ({
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: '30px',
    height: '28px',
    border: 'none',
    borderRadius: '4px',
    backgroundColor: active ? '#e0e0e0' : 'transparent',
    color: theme === 'dark' ? '#ccc' : '#333',
    cursor: 'pointer',
    fontSize: '14px',
    fontWeight: '500',
    transition: 'background-color 0.15s',
  });

  const separatorStyle: React.CSSProperties = {
    width: '1px',
    height: '20px',
    backgroundColor: theme === 'dark' ? '#555' : '#d0d0d0',
    margin: '0 4px',
  };

  return (
    <div style={toolbarStyle} className="md-editor-toolbar">
      <button
        style={btnStyle()}
        onClick={onUndo}
        title="撤销 (Ctrl+Z)"
        type="button"
      >
        ↶
      </button>
      <button
        style={btnStyle()}
        onClick={onRedo}
        title="重做 (Ctrl+Shift+Z)"
        type="button"
      >
        ↷
      </button>

      <div style={separatorStyle} />

      <button
        style={btnStyle()}
        onClick={onToggleBold}
        title="粗体 (Ctrl+B)"
        type="button"
      >
        <strong>B</strong>
      </button>
      <button
        style={btnStyle()}
        onClick={onToggleItalic}
        title="斜体 (Ctrl+I)"
        type="button"
      >
        <em>I</em>
      </button>

      <div style={separatorStyle} />

      <button
        style={btnStyle()}
        onClick={() => onInsertHeading(1)}
        title="标题1"
        type="button"
      >
        H1
      </button>
      <button
        style={btnStyle()}
        onClick={() => onInsertHeading(2)}
        title="标题2"
        type="button"
      >
        H2
      </button>
      <button
        style={btnStyle()}
        onClick={() => onInsertHeading(3)}
        title="标题3"
        type="button"
      >
        H3
      </button>

      <div style={separatorStyle} />

      <button
        style={btnStyle()}
        onClick={() => onInsertList('unordered')}
        title="无序列表"
        type="button"
      >
        •≡
      </button>
      <button
        style={btnStyle()}
        onClick={() => onInsertList('ordered')}
        title="有序列表"
        type="button"
      >
        1.
      </button>
      <button
        style={btnStyle()}
        onClick={onInsertBlockquote}
        title="引用"
        type="button"
      >
        ❝
      </button>

      <div style={separatorStyle} />

      <button
        style={btnStyle()}
        onClick={onInsertCodeBlock}
        title="代码块"
        type="button"
      >
        {'</>'}
      </button>
      <button
        style={btnStyle()}
        onClick={onInsertLink}
        title="链接 (Ctrl+K)"
        type="button"
      >
        🔗
      </button>
      <button
        style={btnStyle()}
        onClick={onInsertImage}
        title="图片 (Ctrl+Shift+K)"
        type="button"
      >
        🖼
      </button>
      <button
        style={btnStyle()}
        onClick={onInsertHorizontalRule}
        title="分割线"
        type="button"
      >
        ―
      </button>

      <div style={{ flex: 1 }} />

      <button
        style={btnStyle()}
        onClick={onThemeToggle}
        title={theme === 'light' ? '切换暗色主题' : '切换亮色主题'}
        type="button"
      >
        {theme === 'light' ? '🌙' : '☀'}
      </button>
    </div>
  );
}

export { MDEditorToolbar };
export default MDEditor;
