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

interface NodeDialogProps {
  open: boolean;
  nodes: GraphNode[];
  candidates: LinkCandidate[];
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

export const NewNodeDialog: React.FC<NodeDialogProps> = ({ open, nodes, candidates, busy, onClose, onSubmit }) => {
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
  const [error, setError] = useState('');

  useEffect(() => {
    if (open) {
      setTask('');
      setRole('default');
      setExecType('worker');
      setParentId(nodes[0]?.id || '');
      setDependencies([]);
      setSelectedSkills([]);
      setCliName('claude');
      setModel('');
      setApiBaseUrl('');
      setApiKeyEnv('');
      setSkipPermissions(true);
      setError('');
      Promise.all([getRoles(), getSkills()])
        .then(([nextRoles, nextSkills]) => {
          setRoles(nextRoles);
          setSkills(nextSkills);
        })
        .catch(() => {
          setRoles([]);
          setSkills([]);
        });
    }
  }, [open, nodes]);

  if (!open) return null;

  const availableCandidates = candidates.length ? candidates : nodes.map((node) => ({
    id: node.id,
    label: `${node.role || node.exec_type} ${node.id.slice(0, 8)}`,
    task: node.task || '',
    status: node.status,
    eligible_as_parent: true,
    eligible_as_dependency: true,
    reachable: false,
  }));
  const parentCandidates = availableCandidates.filter((candidate) => candidate.eligible_as_parent);
  const dependencyCandidates = availableCandidates.filter((candidate) => candidate.eligible_as_dependency && candidate.id !== parentId);
  const availableRoles = roles.filter((item) => !item.type || item.type === execType);

  const toggleDependency = (id: string, checked: boolean) => {
    setDependencies((current) => checked ? [...current, id] : current.filter((item) => item !== id));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!task.trim() || !role.trim()) return;
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
          <label>{t('dialog.parent')}<select value={parentId} onChange={(event) => setParentId(event.target.value)}><option value="">{t('dialog.noParent')}</option>{parentCandidates.map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.label}</option>)}</select></label>
          <label>{t('dialog.cli')}<select value={cliName} onChange={(event) => setCliName(event.target.value as NodeConfig['cli_name'])}><option value="claude">Claude Code</option></select></label>
        </div>
        <label>{t('dialog.task')}<textarea autoFocus value={task} onChange={(event) => setTask(event.target.value)} rows={5} placeholder={t('dialog.taskPlaceholder')} /></label>
        <div className="node-dialog-section"><div className="node-dialog-section-title">{t('dialog.dependencies')}</div><div className="dependency-picker">{dependencyCandidates.length ? dependencyCandidates.map((candidate) => <label key={candidate.id}><input type="checkbox" checked={dependencies.includes(candidate.id)} onChange={(event) => toggleDependency(candidate.id, event.target.checked)} /><span>{candidate.label}</span></label>) : <span className="detail-empty">{t('dialog.noEligibleNodes')}</span>}</div></div>
        {skills.length > 0 && <div className="node-dialog-section"><div className="node-dialog-section-title">{t('dialog.skills')}</div><div className="dependency-picker skill-picker-list">{skills.map((skill) => <label key={skill.id}><input type="checkbox" checked={selectedSkills.includes(skill.id)} onChange={(event) => setSelectedSkills((current) => event.target.checked ? [...current, skill.id] : current.filter((item) => item !== skill.id))} /><span>{skill.name}</span></label>)}</div></div>}
        <div className="form-grid two-columns">
          <label>{t('dialog.model')}<input value={model} onChange={(event) => setModel(event.target.value)} placeholder={t('dialog.useSessionDefault')} /></label>
          <label>{t('dialog.apiKeyEnv')}<input value={apiKeyEnv} onChange={(event) => setApiKeyEnv(event.target.value)} placeholder="ANTHROPIC_API_KEY" /></label>
          <label className="full-field">{t('dialog.apiBaseUrl')}<input value={apiBaseUrl} onChange={(event) => setApiBaseUrl(event.target.value)} placeholder={t('dialog.useProviderDefault')} /></label>
        </div>
        <label className="toggle-field"><input type="checkbox" checked={skipPermissions} onChange={(event) => setSkipPermissions(event.target.checked)} /><span>{t('dialog.runWithoutPerms')}</span></label>
        {error && <div className="form-error">{error}</div>}
        <div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>{t('common.cancel')}</button><button className="primary-button" disabled={!task.trim() || !role.trim() || busy}>{busy ? t('dialog.adding') : t('dialog.addNode')}</button></div>
      </form>
    </div>
  );
};

export function confirmSessionDelete(session: SessionSummary): boolean {
  return window.confirm(translate('dialog.deleteConfirm', { title: session.title }));
}
