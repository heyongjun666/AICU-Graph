import React, { FormEvent, useEffect, useState } from 'react';
import { X } from 'lucide-react';
import { GraphNode, LinkCandidate, NodeConfig, RoleInfo, SessionSummary, SkillInfo } from '../types';
import { getRoles, getSkills } from '../utils/api';
import { useI18n, translate } from '../i18n';

interface NewSessionDialogProps {
  open: boolean;
  busy: boolean;
  onClose: () => void;
  onSubmit: (task: string, title?: string) => Promise<void>;
}

export const NewSessionDialog: React.FC<NewSessionDialogProps> = ({ open, busy, onClose, onSubmit }) => {
  const { t } = useI18n();
  const [title, setTitle] = useState('');
  const [task, setTask] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    if (open) {
      setTitle('');
      setTask('');
      setError('');
    }
  }, [open]);

  if (!open) return null;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!task.trim()) return;
    try {
      setError('');
      await onSubmit(task.trim(), title.trim() || undefined);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  return (
    <div className="dialog-backdrop" onMouseDown={onClose}>
      <form className="workbench-dialog" onSubmit={submit} onMouseDown={(event) => event.stopPropagation()}>
        <div className="dialog-header">
          <div><h2>{t('dialog.newSession')}</h2><p>{t('dialog.newSessionDesc')}</p></div>
          <button type="button" className="icon-button" onClick={onClose} title={t('common.close')}><X size={18} /></button>
        </div>
        <label>{t('dialog.sessionName')} <span className="optional">{t('common.optional')}</span><input value={title} onChange={(event) => setTitle(event.target.value)} placeholder={t('dialog.sessionNamePlaceholder')} /></label>
        <label>{t('dialog.rootTask')}<textarea autoFocus value={task} onChange={(event) => setTask(event.target.value)} placeholder={t('dialog.rootTaskPlaceholder')} rows={6} /></label>
        {error && <div className="form-error">{error}</div>}
        <div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>{t('common.cancel')}</button><button className="primary-button" disabled={!task.trim() || busy}>{busy ? t('dialog.creating') : t('dialog.createSession')}</button></div>
      </form>
    </div>
  );
};

export type LinkCandidateLoadState = 'idle' | 'loading' | 'ready' | 'empty' | 'error';

interface SearchCheckboxPickerProps {
  items: Array<{ id: string; label: string; detail?: string }>;
  selected: string[];
  onToggle: (id: string, checked: boolean) => void;
  query: string;
  onQueryChange: (query: string) => void;
  searchPlaceholder: string;
  emptyLabel: string;
  selectedLabel: string;
  disabled?: boolean;
  className?: string;
}

const SearchCheckboxPicker: React.FC<SearchCheckboxPickerProps> = ({
  items,
  selected,
  onToggle,
  query,
  onQueryChange,
  searchPlaceholder,
  emptyLabel,
  selectedLabel,
  disabled = false,
  className = '',
}) => {
  const normalizedQuery = query.trim().toLowerCase();
  const filtered = items.filter((item) =>
    !normalizedQuery
    || item.label.toLowerCase().includes(normalizedQuery)
    || item.id.toLowerCase().includes(normalizedQuery)
    || item.detail?.toLowerCase().includes(normalizedQuery),
  );
  return (
    <div className={`search-checkbox-picker ${className}`}>
      <div className="picker-toolbar">
        <input
          type="search"
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder={searchPlaceholder}
          disabled={disabled}
          aria-label={searchPlaceholder}
        />
        <span className="picker-count">{selectedLabel.replace('{count}', String(selected.length))}</span>
      </div>
      <div className="dependency-picker">
        {filtered.length ? filtered.map((item) => (
          <label key={item.id}>
            <input
              type="checkbox"
              checked={selected.includes(item.id)}
              onChange={(event) => onToggle(item.id, event.target.checked)}
              disabled={disabled}
            />
            <span className="picker-item-label">{item.label}</span>
            {item.detail && <small>{item.detail}</small>}
          </label>
        )) : <span className="detail-empty">{emptyLabel}</span>}
      </div>
    </div>
  );
};

interface NodeDialogProps {
  open: boolean;
  nodes: GraphNode[];
  candidates: LinkCandidate[];
  candidateStatus?: LinkCandidateLoadState;
  candidateError?: string;
  busy: boolean;
  onClose: () => void;
  onSubmit: (payload: {
    task: string;
    role: string;
    exec_type: 'manager' | 'worker';
    parent_id?: string;
    dependencies?: string[];
    config?: NodeConfig;
  }) => Promise<void>;
}

export const NewNodeDialog: React.FC<NodeDialogProps> = ({
  open,
  nodes,
  candidates,
  candidateStatus = candidates.length ? 'ready' : 'empty',
  candidateError,
  busy,
  onClose,
  onSubmit,
}) => {
  const { t } = useI18n();
  const [task, setTask] = useState('');
  const [role, setRole] = useState('default');
  const [execType, setExecType] = useState<'manager' | 'worker'>('worker');
  const [parentId, setParentId] = useState('');
  const [dependencies, setDependencies] = useState<string[]>([]);
  const [roles, setRoles] = useState<RoleInfo[]>([]);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [cliName, setCliName] = useState<NodeConfig['cli_name']>('claude');
  const [model, setModel] = useState('');
  const [apiBaseUrl, setApiBaseUrl] = useState('');
  const [apiKeyEnv, setApiKeyEnv] = useState('');
  const [skipPermissions, setSkipPermissions] = useState(true);
  const [dependencyQuery, setDependencyQuery] = useState('');
  const [skillQuery, setSkillQuery] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    setTask('');
    setRole('default');
    setExecType('worker');
    setParentId(nodes.find((node) => node.exec_type === 'M')?.id || '');
    setDependencies([]);
    setSelectedSkills([]);
    setCliName('claude');
    setModel('');
    setApiBaseUrl('');
    setApiKeyEnv('');
    setSkipPermissions(true);
    setDependencyQuery('');
    setSkillQuery('');
    setError('');

    let cancelled = false;
    Promise.all([getRoles(), getSkills()])
      .then(([nextRoles, nextSkills]) => {
        if (cancelled) return;
        setRoles(nextRoles);
        setSkills(nextSkills);
      })
      .catch(() => {
        if (cancelled) return;
        setRoles([]);
        setSkills([]);
      });
    return () => { cancelled = true; };
  }, [open]);

  const availableCandidates = candidates;
  const parentCandidates = availableCandidates.filter(
    (candidate) => candidate.exec_type === 'manager' && candidate.eligible_as_parent,
  );
  const dependencyCandidates = availableCandidates.filter(
    (candidate) => candidate.eligible_as_dependency && candidate.id !== parentId,
  );
  const availableRoles = roles.filter((item) => !item.type || item.type === execType);
  const relationDisabled = candidateStatus === 'loading' || candidateStatus === 'error';

  // Options may arrive after the dialog opened. Preserve user input, but drop
  // only selections that the authoritative ready response marks as invalid.
  useEffect(() => {
    if (candidateStatus === 'empty') {
      setParentId('');
      setDependencies([]);
      return;
    }
    if (candidateStatus !== 'ready') return;
    const validParents = new Set(parentCandidates.map((candidate) => candidate.id));
    const validDependencies = new Set(dependencyCandidates.map((candidate) => candidate.id));
    setParentId((current) => current && validParents.has(current) ? current : '');
    setDependencies((current) => current.filter((id) => validDependencies.has(id)));
  }, [candidateStatus, candidates, parentId]);

  if (!open) return null;

  const toggleDependency = (id: string, checked: boolean) => {
    setDependencies((current) => checked
      ? (current.includes(id) ? current : [...current, id])
      : current.filter((item) => item !== id));
  };

  const toggleSkill = (id: string, checked: boolean) => {
    setSelectedSkills((current) => checked
      ? (current.includes(id) ? current : [...current, id])
      : current.filter((item) => item !== id));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!task.trim() || !role.trim()) return;
    if (candidateStatus === 'loading') {
      setError(t('dialog.waitForCandidates'));
      return;
    }
    if (candidateStatus === 'error') {
      setError(candidateError || t('dialog.candidatesLoadFailed'));
      return;
    }
    if (candidateStatus === 'ready') {
      const parentIds = new Set(parentCandidates.map((candidate) => candidate.id));
      const dependencyIds = new Set(dependencyCandidates.map((candidate) => candidate.id));
      if (parentId && !parentIds.has(parentId)) {
        setError(t('dialog.invalidParent'));
        return;
      }
      if (dependencies.some((id) => !dependencyIds.has(id))) {
        setError(t('dialog.invalidDependencies'));
        return;
      }
    }
    try {
      setError('');
      const config: NodeConfig = {
        cli_name: cliName,
        model: model.trim() || undefined,
        api_base_url: apiBaseUrl.trim() || undefined,
        api_key_env: apiKeyEnv.trim() || undefined,
        skills: selectedSkills,
        skip_permissions: skipPermissions,
      };
      await onSubmit({
        task: task.trim(),
        role: role.trim(),
        exec_type: execType,
        parent_id: parentId || undefined,
        dependencies,
        config,
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  return (
    <div className="dialog-backdrop" onMouseDown={onClose}>
      <form className="workbench-dialog compact node-dialog" onSubmit={submit} onMouseDown={(event) => event.stopPropagation()}>
        <div className="dialog-header">
          <div><h2>{t('dialog.addNode')}</h2><p>{t('dialog.addNodeDesc')}</p></div>
          <button type="button" className="icon-button" onClick={onClose} title={t('common.close')}><X size={18} /></button>
        </div>
        <div className="form-grid two-columns">
          <label>{t('dialog.type')}<select value={execType} onChange={(event) => setExecType(event.target.value as 'manager' | 'worker')}><option value="worker">{t('dialog.worker')}</option><option value="manager">{t('dialog.manager')}</option></select></label>
          <label>{t('node.role')}{availableRoles.length ? <select value={role} onChange={(event) => setRole(event.target.value)}><option value="default">default</option>{availableRoles.map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select> : <input value={role} onChange={(event) => setRole(event.target.value)} />}</label>
        </div>
        <div className="form-grid two-columns">
          <label>{t('dialog.parent')}<select value={parentId} disabled={relationDisabled} onChange={(event) => {
            const nextParentId = event.target.value;
            setParentId(nextParentId);
            setDependencies((current) => current.filter((id) => id !== nextParentId));
          }}><option value="">{t('dialog.noParent')}</option>{parentCandidates.map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.label}</option>)}</select></label>
          <label>{t('dialog.cli')}<select value={cliName} onChange={(event) => setCliName(event.target.value as NodeConfig['cli_name'])}><option value="claude">Claude Code</option></select></label>
        </div>
        <label>{t('dialog.task')}<textarea autoFocus value={task} onChange={(event) => setTask(event.target.value)} rows={5} placeholder={t('dialog.taskPlaceholder')} /></label>
        <div className="node-dialog-section"><div className="node-dialog-section-title">{t('dialog.dependencies')}</div>{candidateStatus === 'loading' && <div className="picker-status">{t('dialog.loadingCandidates')}</div>}{candidateStatus === 'error' && <div className="picker-status is-error">{candidateError || t('dialog.candidatesLoadFailed')}</div>}<SearchCheckboxPicker
          items={dependencyCandidates.map((candidate) => ({ id: candidate.id, label: candidate.label, detail: candidate.task }))}
          selected={dependencies}
          onToggle={toggleDependency}
          query={dependencyQuery}
          onQueryChange={setDependencyQuery}
          searchPlaceholder={t('dialog.searchNodes')}
          emptyLabel={candidateStatus === 'empty' ? t('dialog.noEligibleNodes') : t('dialog.noMatchingNodes')}
          selectedLabel={t('dialog.selectedCount')}
          disabled={relationDisabled}
        /></div>
        {skills.length > 0 && <div className="node-dialog-section"><div className="node-dialog-section-title">{t('dialog.skills')}</div><SearchCheckboxPicker
          items={skills.map((skill) => ({ id: skill.id, label: skill.name || skill.id, detail: skill.name !== skill.id ? skill.id : undefined }))}
          selected={selectedSkills}
          onToggle={toggleSkill}
          query={skillQuery}
          onQueryChange={setSkillQuery}
          searchPlaceholder={t('dialog.searchSkills')}
          emptyLabel={t('dialog.noMatchingSkills')}
          selectedLabel={t('dialog.selectedCount')}
        /></div>}
        <div className="form-grid two-columns">
          <label>{t('dialog.model')}<input value={model} onChange={(event) => setModel(event.target.value)} placeholder={t('dialog.useSessionDefault')} /></label>
          <label>{t('dialog.apiKeyEnv')}<input value={apiKeyEnv} onChange={(event) => setApiKeyEnv(event.target.value)} placeholder="ANTHROPIC_API_KEY" /></label>
          <label className="full-field">{t('dialog.apiBaseUrl')}<input value={apiBaseUrl} onChange={(event) => setApiBaseUrl(event.target.value)} placeholder={t('dialog.useProviderDefault')} /></label>
        </div>
        <label className="toggle-field"><input type="checkbox" checked={skipPermissions} onChange={(event) => setSkipPermissions(event.target.checked)} /><span>{t('dialog.runWithoutPerms')}</span></label>
        {error && <div className="form-error">{error}</div>}
        <div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>{t('common.cancel')}</button><button className="primary-button" disabled={!task.trim() || !role.trim() || busy || relationDisabled}>{busy ? t('dialog.adding') : t('dialog.addNode')}</button></div>
      </form>
    </div>
  );
};

export function confirmSessionDelete(session: SessionSummary): boolean {
  return window.confirm(translate('dialog.deleteConfirm', { title: session.title }));
}
