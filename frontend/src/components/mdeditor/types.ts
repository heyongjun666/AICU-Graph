// ===== src/components/mdeditor/types.ts =====
// MDEditor 组件类型定义 — 严格遵循 design.md §6.2~§6.4 接口规范

export interface MDEditorError {
  code: 'UPLOAD_FAILED' | 'FILE_TOO_LARGE' | 'INVALID_IMAGE_TYPE' | 'CONTENT_TOO_LONG';
  message: string;
}

export interface MDEditorProps {
  // ----- 数据 -----
  /** 初始 Markdown 内容 */
  value: string;
  /** 内容变更回调 */
  onChange: (value: string) => void;

  // ----- 行为 -----
  /** 是否只读（预览模式） */
  readOnly?: boolean;
  /** 编辑器高度 */
  height?: string | number;
  /** 占位文本 */
  placeholder?: string;
  /** 最大字符数 */
  maxLength?: number;

  // ----- 功能开关 -----
  /** 是否启用工具栏 */
  showToolbar?: boolean;
  /** 是否启用实时预览（分栏模式） */
  enablePreview?: boolean;
  /** 是否启用快捷键 */
  enableShortcuts?: boolean;
  /** 是否启用图片上传 */
  enableImageUpload?: boolean;

  // ----- 回调 -----
  /** 图片上传回调 (返回上传后的 URL) */
  onImageUpload?: (file: File) => Promise<string>;
  /** 保存快捷键回调 (Ctrl/Cmd + S) */
  onSave?: (value: string) => void;
  /** 错误回调 */
  onError?: (error: MDEditorError) => void;
  /** 焦点变化回调 */
  onFocusChange?: (focused: boolean) => void;
}

export interface MDEditorRef {
  /** 获取当前 Markdown 内容 */
  getValue(): string;

  /** 设置 Markdown 内容 */
  setValue(value: string): void;

  /** 在光标位置插入文本 */
  insertText(text: string): void;

  /** 获取选中文本 */
  getSelection(): string;

  /** 替换选中文本 */
  replaceSelection(text: string): void;

  /** 聚焦编辑器 */
  focus(): void;

  /** 失焦 */
  blur(): void;

  /** 滚动到指定行 */
  scrollToLine(line: number): void;

  /** 插入图片标记 (自动调用 onImageUpload) */
  insertImage(): Promise<void>;

  /** 切换粗体 */
  toggleBold(): void;

  /** 切换斜体 */
  toggleItalic(): void;

  /** 插入标题 */
  insertHeading(level: 1 | 2 | 3 | 4 | 5 | 6): void;

  /** 插入代码块 */
  insertCodeBlock(language?: string): void;

  /** 插入链接 */
  insertLink(url: string, text?: string): void;

  /** 插入列表 */
  insertList(type: 'unordered' | 'ordered'): void;

  /** 插入引用 */
  insertBlockquote(): void;

  /** 插入分割线 */
  insertHorizontalRule(): void;

  /** 撤销 */
  undo(): void;

  /** 重做 */
  redo(): void;
}

// 快捷键映射定义
export interface ShortcutMap {
  'Mod-b': () => void;      // Bold
  'Mod-i': () => void;      // Italic
  'Mod-k': () => void;      // Insert link
  'Mod-Shift-k': () => void; // Insert image
  'Mod-s': () => void;      // Save (trigger onSave)
  'Mod-Shift-p': () => void; // Toggle preview
}
