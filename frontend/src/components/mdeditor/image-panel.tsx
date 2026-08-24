// ===== src/components/mdeditor/image-panel.tsx =====
// 图片资源管理面板 — 展示已上传的图片列表

import React from 'react';

export interface ImageItem {
  /** 图片 URL */
  url: string;
  /** 图片文件名 */
  name: string;
  /** 上传时间 */
  uploadedAt: string;
  /** 图片大小 (bytes) */
  size?: number;
}

export interface ImagePanelProps {
  /** 已上传图片列表 */
  images: ImageItem[];
  /** 点击图片回调（插入到编辑器） */
  onInsertImage: (url: string) => void;
  /** 主题 */
  theme?: 'light' | 'dark';
}

/**
 * ImagePanel — 图片资源管理面板
 *
 * 展示用户已上传的图片列表，点击即可将图片 Markdown 插入编辑器。
 */
const ImagePanel: React.FC<ImagePanelProps> = ({
  images,
  onInsertImage,
  theme = 'light',
}) => {
  if (images.length === 0) {
    return (
      <div
        style={{
          padding: '16px',
          textAlign: 'center',
          color: theme === 'dark' ? '#888' : '#999',
          fontSize: '14px',
        }}
      >
        暂无上传的图片
      </div>
    );
  }

  return (
    <div
      className="md-image-panel"
      style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fill, minmax(120px, 1fr))',
        gap: '8px',
        padding: '8px',
      }}
      data-testid="image-panel"
    >
      {images.map((img, index) => (
        <div
          key={`${img.url}-${index}`}
          onClick={() => onInsertImage(img.url)}
          style={{
            cursor: 'pointer',
            borderRadius: '6px',
            overflow: 'hidden',
            border: `1px solid ${theme === 'dark' ? '#3c3c3c' : '#e0e0e0'}`,
            transition: 'border-color 0.2s',
            position: 'relative',
            aspectRatio: '16 / 10',
            backgroundColor: theme === 'dark' ? '#2d2d2d' : '#f5f5f5',
          }}
          title={`点击插入: ${img.name}`}
          data-testid={`image-item-${index}`}
        >
          <img
            src={img.url}
            alt={img.name}
            style={{
              width: '100%',
              height: '100%',
              objectFit: 'cover',
            }}
            loading="lazy"
          />
          <div
            style={{
              position: 'absolute',
              bottom: 0,
              left: 0,
              right: 0,
              padding: '4px 6px',
              backgroundColor: 'rgba(0,0,0,0.6)',
              color: '#fff',
              fontSize: '11px',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {img.name}
          </div>
        </div>
      ))}
    </div>
  );
};

export default ImagePanel;
