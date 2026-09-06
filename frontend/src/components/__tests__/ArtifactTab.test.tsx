// @vitest-environment jsdom
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import ArtifactTab from '../ArtifactTab';
import * as api from '../../utils/api';

vi.mock('../../stores/SessionStore', () => ({
  useSessionStore: (selector: (state: { activeSessionId: string }) => unknown) => (
    selector({ activeSessionId: 'session-1' })
  ),
}));

vi.mock('../../utils/api', () => ({
  listNodeResults: vi.fn(),
  readNodeResultFile: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(api.listNodeResults).mockReset();
  vi.mocked(api.readNodeResultFile).mockReset();
});

afterEach(cleanup);

describe('ArtifactTab', () => {
  it('loads declared results and previews the selected file', async () => {
    vi.mocked(api.listNodeResults).mockResolvedValue({
      artifact_path: '/workspace/output',
      artifact_kind: 'directory',
      result_root: '/workspace/output',
      path: '.',
      entries: [{
        name: 'report.md',
        path: 'report.md',
        type: 'file',
        size: 8,
        modified_at: 0,
      }],
    });
    vi.mocked(api.readNodeResultFile).mockResolvedValue({
      path: 'report.md',
      size: 8,
      content: '# Report',
    });

    render(<ArtifactTab nodeId="node-1" />);

    const report = await screen.findByText('report.md');
    expect(api.listNodeResults).toHaveBeenCalledWith('session-1', 'node-1', '');
    fireEvent.click(report);
    await waitFor(() => {
      expect(api.readNodeResultFile).toHaveBeenCalledWith('session-1', 'node-1', 'report.md');
      expect(screen.getByText('# Report')).toBeDefined();
    });
  });

  it('shows an explicit empty state when no artifact is declared', async () => {
    vi.mocked(api.listNodeResults).mockResolvedValue({
      artifact_path: null,
      artifact_kind: null,
      result_root: null,
      path: '.',
      entries: [],
    });

    render(<ArtifactTab nodeId="node-2" />);

    expect(await screen.findByText('此节点没有声明的产出结果。')).toBeDefined();
  });
});
