// @vitest-environment jsdom
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import SessionOutputsDialog from '../SessionOutputsDialog';
import * as api from '../../utils/api';

vi.mock('../../utils/api', () => ({
  listSessionShared: vi.fn(),
  readSessionSharedFile: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(api.listSessionShared).mockReset();
  vi.mocked(api.readSessionSharedFile).mockReset();
});

afterEach(cleanup);

const partitionListing = {
  path: '.',
  truncated: false,
  entries: [{
    name: 'backend',
    path: 'backend',
    type: 'directory',
    kind: 'artifact_partition',
    size: 0,
    modified_at: 0,
    producer: {
      node_id: 'node_1',
      role: 'backend',
      step_ref: 'backend_impl',
      artifact_type: 'source_code',
      key_files: ['src/api.py'],
    },
    delivery: { state: 'validated' },
  }],
};

describe('SessionOutputsDialog', () => {
  it('renders a partition row with producer badge and delivery badge', async () => {
    vi.mocked(api.listSessionShared).mockResolvedValue(partitionListing);

    render(<SessionOutputsDialog open sessionId="s1" onClose={() => undefined} />);

    expect(await screen.findByText('backend')).toBeDefined();
    expect(screen.getByText('backend · source_code · backend_impl')).toBeDefined();
    expect(screen.getByText('validated')).toBeDefined();
    expect(api.listSessionShared).toHaveBeenCalledWith('s1', '');
  });

  it('drills down into a partition directory and lists sub-entries', async () => {
    vi.mocked(api.listSessionShared).mockResolvedValueOnce(partitionListing);
    vi.mocked(api.listSessionShared).mockResolvedValueOnce({
      path: 'backend',
      truncated: false,
      entries: [{
        name: 'api.py',
        path: 'backend/api.py',
        type: 'file',
        kind: 'file',
        size: 17,
        modified_at: 0,
        producer: null,
        delivery: null,
      }],
    });

    render(<SessionOutputsDialog open sessionId="s1" onClose={() => undefined} />);

    const backend = await screen.findByText('backend');
    fireEvent.click(backend);
    await waitFor(() => {
      expect(api.listSessionShared).toHaveBeenCalledWith('s1', 'backend');
    });
    expect(await screen.findByText('api.py')).toBeDefined();
  });

  it('previews a previewable file', async () => {
    vi.mocked(api.listSessionShared).mockResolvedValue({
      path: '.',
      truncated: false,
      entries: [{
        name: 'report.md',
        path: 'report.md',
        type: 'file',
        kind: 'file',
        size: 9,
        modified_at: 0,
        producer: null,
        delivery: null,
      }],
    });
    vi.mocked(api.readSessionSharedFile).mockResolvedValue({
      path: 'report.md',
      size: 9,
      mime_type: 'text/markdown',
      previewable: true,
      content: '# Report',
      truncated: false,
    });

    render(<SessionOutputsDialog open sessionId="s1" onClose={() => undefined} />);

    fireEvent.click(await screen.findByText('report.md'));
    await waitFor(() => {
      expect(api.readSessionSharedFile).toHaveBeenCalledWith('s1', 'report.md');
      expect(screen.getByText('# Report')).toBeDefined();
    });
  });

  it('shows a placeholder for non-previewable files', async () => {
    vi.mocked(api.listSessionShared).mockResolvedValue({
      path: '.',
      truncated: false,
      entries: [{
        name: 'blob.bin',
        path: 'blob.bin',
        type: 'file',
        kind: 'file',
        size: 4,
        modified_at: 0,
        producer: null,
        delivery: null,
      }],
    });
    vi.mocked(api.readSessionSharedFile).mockResolvedValue({
      path: 'blob.bin',
      size: 4,
      mime_type: null,
      previewable: false,
      content: null,
      truncated: false,
    });

    render(<SessionOutputsDialog open sessionId="s1" onClose={() => undefined} />);

    fireEvent.click(await screen.findByText('blob.bin'));
    expect(await screen.findByText('二进制或不可预览文件。')).toBeDefined();
  });

  it('refresh re-fetches the current layer', async () => {
    vi.mocked(api.listSessionShared).mockResolvedValue(partitionListing);

    render(<SessionOutputsDialog open sessionId="s1" onClose={() => undefined} />);

    await screen.findByText('backend');
    fireEvent.click(screen.getByText('刷新'));
    await waitFor(() => {
      expect(api.listSessionShared).toHaveBeenCalledTimes(2);
    });
    expect(api.listSessionShared).toHaveBeenLastCalledWith('s1', '');
  });

  it('prompts to select a session when none is active', () => {
    render(<SessionOutputsDialog open sessionId={null} onClose={() => undefined} />);
    expect(screen.getByText('选择会话以查看其产出。')).toBeDefined();
  });
});
