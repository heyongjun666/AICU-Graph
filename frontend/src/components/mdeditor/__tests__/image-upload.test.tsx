// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import React from 'react';
import ImageUpload from '../image-upload';
import ImagePanel from '../image-panel';

afterEach(cleanup);

describe('ImageUpload', () => {
  it('should render upload zone', () => {
    render(
      <ImageUpload
        onUpload={async () => ''}
        onSuccess={() => {}}
      />,
    );
    expect(screen.getByTestId('image-upload')).toBeDefined();
    expect(screen.getByText(/点击上传/)).toBeDefined();
  });

  it('should validate file type and reject invalid', async () => {
    const onError = vi.fn();
    const { container } = render(
      <ImageUpload
        onUpload={async () => ''}
        onSuccess={() => {}}
        onError={onError}
        accept="image/png"
      />,
    );

    const input = container.querySelector('[data-testid="image-upload-input"]') as HTMLInputElement;
    expect(input).not.toBeNull();
    const file = new File([''], 'test.txt', { type: 'text/plain' });

    fireEvent.change(input, { target: { files: [file] } });
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ code: 'INVALID_IMAGE_TYPE' }),
    );
  });

  it('should validate file size and reject too large', () => {
    const onError = vi.fn();
    const { container } = render(
      <ImageUpload
        onUpload={async () => ''}
        onSuccess={() => {}}
        onError={onError}
        maxFileSize={100} // 100 bytes
      />,
    );

    const input = container.querySelector('[data-testid="image-upload-input"]') as HTMLInputElement;
    expect(input).not.toBeNull();
    const file = new File(['x'.repeat(200)], 'test.png', { type: 'image/png' });

    fireEvent.change(input, { target: { files: [file] } });
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ code: 'FILE_TOO_LARGE' }),
    );
  });

  it('should call onUpload and onSuccess for valid file', async () => {
    const onUpload = vi.fn().mockResolvedValue('https://example.com/img.png');
    const onSuccess = vi.fn();

    const { container: container3 } = render(
      <ImageUpload
        onUpload={onUpload}
        onSuccess={onSuccess}
        maxFileSize={1000000}
      />,
    );

    const input3 = container3.querySelector('[data-testid="image-upload-input"]') as HTMLInputElement;
    const file = new File(['fake-image'], 'test.png', { type: 'image/png' });

    fireEvent.change(input3, { target: { files: [file] } });

    // Wait for async upload to complete
    await vi.waitFor(() => {
      expect(onUpload).toHaveBeenCalledWith(file);
      expect(onSuccess).toHaveBeenCalledWith('https://example.com/img.png');
    });
  });

  it('should support drag and drop', () => {
    const onUpload = vi.fn().mockResolvedValue('url');
    const onSuccess = vi.fn();

    const { container: container4 } = render(
      <ImageUpload
        onUpload={onUpload}
        onSuccess={onSuccess}
      />,
    );

    const dropZone = container4.querySelector('[data-testid="image-upload"]') as HTMLElement;
    const file = new File(['img'], 'test.png', { type: 'image/png' });

    fireEvent.dragOver(dropZone);
    fireEvent.drop(dropZone, { dataTransfer: { files: [file] } });

    expect(onUpload).toHaveBeenCalledWith(file);
  });
});

describe('ImagePanel', () => {
  const mockImages = [
    { url: 'https://example.com/1.png', name: 'image1.png', uploadedAt: '2026-01-01' },
    { url: 'https://example.com/2.png', name: 'image2.png', uploadedAt: '2026-01-02' },
  ];

  it('should render empty state when no images', () => {
    render(<ImagePanel images={[]} onInsertImage={() => {}} />);
    expect(screen.getByText('暂无上传的图片')).toBeDefined();
  });

  it('should render image list', () => {
    render(<ImagePanel images={mockImages} onInsertImage={() => {}} />);
    expect(screen.getByTestId('image-item-0')).toBeDefined();
    expect(screen.getByTestId('image-item-1')).toBeDefined();
  });

  it('should call onInsertImage when image is clicked', () => {
    const onInsert = vi.fn();
    render(<ImagePanel images={mockImages} onInsertImage={onInsert} />);

    fireEvent.click(screen.getByTestId('image-item-0'));
    expect(onInsert).toHaveBeenCalledWith('https://example.com/1.png');
  });
});
