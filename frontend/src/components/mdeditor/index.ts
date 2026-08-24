// ===== src/components/mdeditor/index.ts =====
// MDEditor 模块统一导出

export { default as MDEditor } from './mdeditor';
export { MDEditorToolbar } from './mdeditor';
export type { MDEditorProps, MDEditorRef, MDEditorError, ShortcutMap } from './types';
export { default as PreviewPane } from './preview-pane';
export type { PreviewPaneProps } from './preview-pane';
export { default as ImageUpload } from './image-upload';
export type { ImageUploadProps } from './image-upload';
export { default as ImagePanel } from './image-panel';
export type { ImageItem, ImagePanelProps } from './image-panel';
export { useAutoSave, restoreDraft, clearAllDrafts } from './auto-save';
export type { AutoSaveOptions, AutoSaveState } from './auto-save';
export { lightTheme, darkTheme, getTheme } from './editor-theme';
export type { EditorTheme } from './editor-theme';
