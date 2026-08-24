// ===== src/components/mdeditor/editor-theme.ts =====
// CodeMirror 6 编辑器主题定义，支持亮色/暗色切换

import { EditorView } from '@codemirror/view';

/** 亮色主题 */
export const lightTheme = EditorView.theme(
  {
    '&': {
      backgroundColor: '#ffffff',
      color: '#1a1a1a',
    },
    '.cm-content': {
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
      fontSize: '14px',
      lineHeight: '1.6',
      padding: '12px',
    },
    '.cm-gutters': {
      backgroundColor: '#f5f5f5',
      borderRight: '1px solid #e0e0e0',
      color: '#999',
    },
    '.cm-activeLineGutter': {
      backgroundColor: '#e8f0fe',
    },
    '.cm-activeLine': {
      backgroundColor: '#f0f7ff',
    },
    '.cm-cursor': {
      borderLeftColor: '#0066cc',
    },
    '&.cm-focused .cm-selectionBackground': {
      backgroundColor: '#d4e2f0',
    },
    '.cm-selectionBackground': {
      backgroundColor: '#e0e7ef',
    },
    '.cm-matchingBracket': {
      backgroundColor: '#d4e2f0',
      outline: '1px solid #0066cc',
    },
    '.cm-placeholder': {
      color: '#adb5bd',
    },
    '.cm-scroller': {
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
    },
  },
  { dark: false },
);

/** 暗色主题 */
export const darkTheme = EditorView.theme(
  {
    '&': {
      backgroundColor: '#1e1e1e',
      color: '#d4d4d4',
    },
    '.cm-content': {
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
      fontSize: '14px',
      lineHeight: '1.6',
      padding: '12px',
    },
    '.cm-gutters': {
      backgroundColor: '#252526',
      borderRight: '1px solid #3c3c3c',
      color: '#858585',
    },
    '.cm-activeLineGutter': {
      backgroundColor: '#2a2d2e',
    },
    '.cm-activeLine': {
      backgroundColor: '#2a2d2e',
    },
    '.cm-cursor': {
      borderLeftColor: '#569cd6',
    },
    '&.cm-focused .cm-selectionBackground': {
      backgroundColor: '#264f78',
    },
    '.cm-selectionBackground': {
      backgroundColor: '#3a3d41',
    },
    '.cm-matchingBracket': {
      backgroundColor: '#264f78',
      outline: '1px solid #569cd6',
    },
    '.cm-placeholder': {
      color: '#6e6e6e',
    },
    '.cm-scroller': {
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
    },
  },
  { dark: true },
);

export type EditorTheme = 'light' | 'dark';

export function getTheme(theme: EditorTheme) {
  return theme === 'dark' ? [darkTheme] : [lightTheme];
}
