// @vitest-environment jsdom
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import WorkbenchSettings from '../WorkbenchSettings';
import * as api from '../../utils/api';
import { RoleField } from '../../types';

vi.mock('../../utils/api', () => ({
  createRole: vi.fn(),
  createSkill: vi.fn(),
  getGlobalConfig: vi.fn(),
  getRole: vi.fn(),
  getRoleSchema: vi.fn(),
  getRoles: vi.fn(),
  getSkill: vi.fn(),
  getSkills: vi.fn(),
  updateGlobalConfig: vi.fn(),
  updateRole: vi.fn(),
  updateSkill: vi.fn(),
}));

const fieldNames = [
  'summary', 'description', 'suitable_for', 'not_for',
  'num_ccs', 'model', 'cli_name', 'audit_rounds',
  'skills_dir', 'skills', 'output_file', 'final_file',
  'modular_output', 'main_doc', 'index_file', 'generate_instruction',
  'revise_instruction', 'review_checklist', 'adversarial_role',
  'audit_role', 'audit_angles', 'role_md',
];

const fields: RoleField[] = fieldNames.map((name) => ({
  name,
  label: name.replace(/_/g, ' '),
  type: ['suitable_for', 'not_for', 'skills', 'review_checklist', 'audit_angles'].includes(name)
    ? 'list'
    : name === 'num_ccs' || name === 'audit_rounds'
      ? 'number'
      : name === 'modular_output'
        ? 'boolean'
        : name.includes('instruction') || ['adversarial_role', 'audit_role', 'role_md'].includes(name)
          ? 'multiline'
          : 'text',
  default: null,
  has_default: !['description', 'main_doc', 'index_file'].includes(name),
}));

beforeEach(() => {
  vi.mocked(api.getGlobalConfig).mockResolvedValue({
    config: { workspace_root: null, cli_name: null, model: null, api_base_url: null, api_key_env: null },
    clis: [],
    default_cli: 'internal',
  });
  vi.mocked(api.getRoles).mockResolvedValue([]);
  vi.mocked(api.getSkills).mockResolvedValue([]);
  vi.mocked(api.getRoleSchema).mockResolvedValue({
    role_type: 'worker',
    defaults: {},
    fields,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('WorkbenchSettings roles form', () => {
  it('renders every schema field, including fields absent from default.yaml', async () => {
    const { container } = render(
      <WorkbenchSettings
        open
        onClose={() => {}}
        onSaved={() => {}}
      />,
    );

    await waitFor(() => expect(api.getRoleSchema).toHaveBeenCalledWith('worker'));
    fireEvent.click(screen.getByText('Roles'));

    expect(await screen.findByText('22 supported fields for this role type. Override only the values this role needs.')).toBeDefined();
    expect(screen.getByText('main_doc')).toBeDefined();
    expect(screen.getByText('index_file')).toBeDefined();
    expect(screen.getAllByText('description')).toHaveLength(2);
    expect(container.querySelectorAll('.role-field')).toHaveLength(22);
  });
});
