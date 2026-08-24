// ===== src/components/mdeditor/image-upload.tsx =====
// 图片上传组件 — 支持点击选择文件和拖拽上传

import React, { useState, useRef, DragEvent } from 'react';
import type { MDEditorError } from './types';

export interface ImageUploadProps {
  /** 上传回调 (返回上传后的 URL) */
  onUpload: (file: File) => Promise<string>;
  /** 上传成功回调 */
  onSuccess: (url: string) => void;
  /** 错误回调 */
  onError?: (error: MDEditorError) => void;
  /** 最大文件大小 (bytes)，默认 5MB */
  maxFileSize?: number;
  /** 接受的图片类型 */
  accept?: string;
  /** 主题 */
  theme?: 'light' | 'dark';
}

const DEFAULT_ACCEPT = 'image/jpeg,image/png,image/gif,image/webp,image/svg+xml';
const DEFAULT_MAX_SIZE = 5 * 1024 * 1024; // 5MB

/**
 * ImageUpload — 图片上传组件
 *
 * 支持点击选择和拖拽上传两种方式。上传后触发 onSuccess 回调返回图片 URL。
 */
const ImageUpload: React.FC<ImageUploadProps> = ({
  onUpload,
  onSuccess,
  onError,
  maxFileSize = DEFAULT_MAX_SIZE,
  accept = DEFAULT_ACCEPT,
  theme = 'light',
}) => {
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [progressMessage, setProgressMessage] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  const validateFile = (file: File): boolean => {
    // 检查类型
    const acceptedTypes = accept.split(',');
    const isValidType = acceptedTypes.some((t) => {
      const trimmed = t.trim();
      if (trimmed.endsWith('/*')) {
        const category = trimmed.split('/')[0];
        return file.type.startsWith(`${category}/`);
      }
      return file.type === trimmed;
    });

    if (!isValidType) {
      onError?.({
        code: 'INVALID_IMAGE_TYPE',
        message: `不支持的文件类型: ${file.type}。支持的格式: ${accept}`,
      });
      return false;
    }

    // 检查大小
    if (file.size > maxFileSize) {
      const maxMB = Math.round(maxFileSize / (1024 * 1024));
      onError?.({
        code: 'FILE_TOO_LARGE',
        message: `文件大小 ${(file.size / (1024 * 1024)).toFixed(1)}MB 超过限制 ${maxMB}MB`,
      });
      return false;
    }

    return true;
  };

  const handleUpload = async (file: File) => {
    if (!validateFile(file)) return;

    setIsUploading(true);
    setProgressMessage(`正在上传 ${file.name}...`);

    try {
      const url = await onUpload(file);
      onSuccess(url);
      setProgressMessage('上传成功');
    } catch (err) {
      onError?.({
        code: 'UPLOAD_FAILED',
        message: err instanceof Error ? err.message : '图片上传失败',
      });
      setProgressMessage('上传失败');
    } finally {
      setIsUploading(false);
      setTimeout(() => setProgressMessage(''), 2000);
    }
  };

  const handleClick = () => {
    inputRef.current?.click();
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) {
      handleUpload(file);
    }
    // 重置 input，允许重复选择同一文件
    if (inputRef.current) {
      inputRef.current.value = '';
    }
  };

  const handleDragOver = (e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  };

  const handleDragLeave = (e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  };

  const handleDrop = (e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);

    const file = e.dataTransfer.files?.[0];
    if (file) {
      handleUpload(file);
    }
  };

  const dropZoneStyle: React.CSSProperties = {
    border: `2px dashed ${isDragging ? '#0066cc' : theme === 'dark' ? '#555' : '#ccc'}`,
    borderRadius: '8px',
    padding: '20px',
    textAlign: 'center',
    cursor: 'pointer',
    backgroundColor: isDragging
      ? theme === 'dark'
        ? '#1a3a5c'
        : '#e8f0fe'
      : 'transparent',
    transition: 'all 0.2s',
    color: theme === 'dark' ? '#ccc' : '#666',
  };

  return (
    <div
      className="md-image-upload"
      onClick={handleClick}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      style={dropZoneStyle}
      data-testid="image-upload"
    >
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        style={{ display: 'none' }}
        onChange={handleFileChange}
        data-testid="image-upload-input"
      />

      {isUploading ? (
        <div>
          <div style={{ marginBottom: '8px' }}>
            <span style={{ fontSize: '24px' }}>⏳</span>
          </div>
          <div>{progressMessage}</div>
        </div>
      ) : (
        <div>
          <div style={{ marginBottom: '8px' }}>
            <span style={{ fontSize: '24px' }}>📁</span>
          </div>
          <div>
            <strong>点击上传</strong> 或拖拽图片到此处
          </div>
          <div style={{ fontSize: '12px', marginTop: '4px', opacity: 0.7 }}>
            支持 JPG/PNG/GIF/WebP/SVG，最大 {Math.round(maxFileSize / (1024 * 1024))}MB
          </div>
        </div>
      )}
    </div>
  );
};

export default ImageUpload;
