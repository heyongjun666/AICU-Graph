import React, { FormEvent, useEffect, useRef, useState } from 'react';
import { X } from 'lucide-react';
import { GraphNode, LinkCandidate, NodeConfig, RoleInfo, RolePool, SessionSummary, SkillInfo } from '../types';
import { getRole, getRolePools, getRoles, getSkills } from '../utils/api';
import { useAvailableClis } from '../hooks/useAvailableClis';
import { useMcpCompatWarning } from '../hooks/useMcpCompatWarning';
import { useI18n, translate } from '../i18n';
import SearchCheckboxPicker from './SearchCheckboxPicker';
import { CliOptions } from './CliOptions';
import { errorMessage } from '../utils/errorMessage';

interface NewSessionDialogProps {
  open: boolean;
  busy: boolean;
  onClose: () => void;
  /** candidateRoles 为空/缺省 = 不限制，Manager 看到全部角色。 */
  onSubmit: (title: string, candidateRoles?: string[]) => Promise<void>;
}

export const NewSessionDialog: React.FC<NewSessionDialogProps> = ({ open, busy, onClose, onSubmit }) => {
  const { t } = useI18n();
  const [title, setTitle] = useState('');
  const [error, setError] = useState('');
  const [pools, setPools] = useState<RolePool[]>([]);
  const [poolId, setPoolId] = useState('');

  useEffect(() => {
    if (!open) return;
    setTitle('');
    setError('');
    setPoolId('');
    setPools([]);
    // 角色池是可选项：拉取失败只是不显示选择器，不能挡住建会话。
    let cancelled = false;
    getRolePools()
      .then((next) => { if (!cancelled) setPools(next); })
      .catch(() => { if (!cancelled) setPools([]); });
    return () => { cancelled = true; };
  }, [open]);

  if (!open) return null;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!title.trim()) return;
    // 写入会话的是角色 id 快照，之后改池/删池不影响本会话。
    const selectedPool = pools.find((pool) => pool.id === poolId);
    try {
      setError('');
      await onSubmit(title.trim(), selectedPool?.roles.length ? selectedPool.roles : undefined);
    } catch (reason) {
      setError(errorMessage(reason));
    }
  };

  return (
    <div className="dialog-backdrop" onMouseDown={onClose}>
      <form className="workbench-dialog" onSubmit={submit} onMouseDown={(event) => event.stopPropagation()}>
        <div className="dialog-header">
          <div><h2>{t('dialog.newSession')}</h2><p>{t('dialog.newSessionDesc')}</p></div>
          <button type="button" className="icon-button" onClick={onClose} title={t('common.close')}><X size={18} /></button>
        </div>
        <label>{t('dialog.sessionName')}<input autoFocus value={title} onChange={(event) => setTitle(event.target.value)} placeholder={t('dialog.sessionNamePlaceholder')} /></label>
        {pools.length > 0 && (
          <label>{t('dialog.newSessionPool')}
            <select value={poolId} onChange={(event) => setPoolId(event.target.value)}>
              <option value="">{t('dialog.newSessionPoolNone')}</option>
              {pools.map((pool) => <option key={pool.id} value={pool.id}>{`${pool.name} (${pool.roles.length})`}</option>)}
            </select>
            <small>{t('dialog.newSessionPoolHint')}</small>
          </label>
        )}
        {error && <div className="form-error">{error}</div>}
        <div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>{t('common.cancel')}</button><button className="primary-button" disabled={!title.trim() || busy}>{busy ? t('dialog.creating') : t('dialog.createSession')}</button></div>
      </form>
    </div>
  );
};

export type LinkCandidateLoadState = 'idle' | 'loading' | 'ready' | 'empty' | 'error';

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
  const [skillsLoading, setSkillsLoading] = useState(false);
  const roleRequestId = useRef(0);
  const [cliName, setCliName] = useState<NodeConfig['cli_name']>('claude');
  const [model, setModel] = useState('');
  const [apiBaseUrl, setApiBaseUrl] = useState('');
  const [apiKeyEnv, setApiKeyEnv] = useState('');
  const [skipPermissions, setSkipPermissions] = useState(true);
  const [dependencyQuery, setDependencyQuery] = useState('');
  const [skillQuery, setSkillQuery] = useState('');
  const [error, setError] = useState('');
  const { clis, defaultCli } = useAvailableClis();
  // 角色声明了 mcp、而这个 CLI 接不了时，在 CLI 下拉下面说一句。原因短语来自后端。
  const mcpWarning = useMcpCompatWarning({
    enabled: open, roleType: execType, roleId: role, cliId: cliName || '', clis,
  });

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

  // 角色详情里的 effective_config 是节点新建时应回显的最终配置；请求按角色和类型绑定，
  // 避免切换角色后旧响应把用户刚选中的 skills 覆盖掉。
  useEffect(() => {
    const requestId = ++roleRequestId.current;
    if (!open || !role.trim()) {
      setSkillsLoading(false);
      if (open && !role.trim()) setSelectedSkills([]);
      return;
    }

    let cancelled = false;
    setSkillsLoading(true);
    setError('');
    getRole(execType, role)
      .then((detail) => {
        if (cancelled || requestId !== roleRequestId.current) return;
        const config = detail.effective_config ?? detail.config ?? {};
        const declared = config.skills;
        setSelectedSkills(Array.isArray(declared)
          ? declared.filter((item): item is string => typeof item === 'string')
          : []);
        setSkillsLoading(false);
      })
      .catch((reason) => {
        if (cancelled || requestId !== roleRequestId.current) return;
        setSelectedSkills([]);
        setSkillsLoading(false);
        setError(errorMessage(reason));
      });
    return () => {
      cancelled = true;
      if (requestId === roleRequestId.current) setSkillsLoading(false);
    };
  }, [open, execType, role]);

  // CLI 选项可能晚于对话框打开到达：保留用户选择，仅当当前值不在权威列表中时回退为"自动（会话默认）"。
  useEffect(() => {
    if (clis.length === 0) return;
    setCliName((current) => (current && clis.some((cli) => cli.id === current) ? current : ''));
  }, [clis]);

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
    if (skillsLoading) {
      setError(t('dialog.waitForRoleConfig'));
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
        cli_name: cliName || undefined,
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
      setError(errorMessage(reason));
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
          <label>{t('dialog.type')}<select value={execType} onChange={(event) => {
            setExecType(event.target.value as 'manager' | 'worker');
            setRole('default');
          }}><option value="worker">{t('dialog.worker')}</option><option value="manager">{t('dialog.manager')}</option></select></label>
          <label>{t('node.role')}{availableRoles.length ? <select value={role} onChange={(event) => setRole(event.target.value)}><option value="default">default</option>{availableRoles.map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select> : <input value={role} onChange={(event) => setRole(event.target.value)} />}</label>
        </div>
        <div className="form-grid two-columns">
          <label>{t('dialog.parent')}<select value={parentId} disabled={relationDisabled} onChange={(event) => {
            const nextParentId = event.target.value;
            setParentId(nextParentId);
            setDependencies((current) => current.filter((id) => id !== nextParentId));
          }}><option value="">{t('dialog.noParent')}</option>{parentCandidates.map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.label}</option>)}</select></label>
          <label>{t('dialog.cli')}<select value={cliName} onChange={(event) => setCliName(event.target.value as NodeConfig['cli_name'])}><CliOptions clis={clis} defaultCli={defaultCli} /></select>{mcpWarning && <div className="form-notice is-warning">{mcpWarning}</div>}</label>
        </div>
        {/* 任务正文通常是一整段需求（背景 + 验收标准，几屏长）。默认 5 行只够看个开头，
            写到一半就没法通读自己写的东西了。这里给足高度。 */}
        <label>{t('dialog.task')}<textarea autoFocus value={task} onChange={(event) => setTask(event.target.value)} rows={14} placeholder={t('dialog.taskPlaceholder')} /></label>
        <div className="node-dialog-section"><div className="node-dialog-section-title">{t('dialog.dependencies')}</div>{candidateStatus === 'loading' && <div className="picker-status">{t('dialog.loadingCandidates')}</div>}{candidateStatus === 'error' && <div className="picker-status is-error">{candidateError || t('dialog.candidatesLoadFailed')}</div>}<SearchCheckboxPicker
          items={dependencyCandidates.map((candidate) => ({ id: candidate.id, label: candidate.label, detail: candidate.task, description: candidate.task }))}
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
          items={skills.map((skill) => ({ id: skill.id, label: skill.name || skill.id, detail: skill.name !== skill.id ? skill.id : undefined, description: skill.description }))}
          selected={selectedSkills}
          onToggle={toggleSkill}
          query={skillQuery}
          onQueryChange={setSkillQuery}
          searchPlaceholder={t('dialog.searchSkills')}
          emptyLabel={t('dialog.noMatchingSkills')}
          selectedLabel={t('dialog.selectedCount')}
          disabled={skillsLoading}
        /></div>}
        <div className="form-grid two-columns">
          <label>{t('dialog.model')}<input value={model} onChange={(event) => setModel(event.target.value)} placeholder={t('dialog.useSessionDefault')} /></label>
          <label>{t('dialog.apiKeyEnv')}<input value={apiKeyEnv} onChange={(event) => setApiKeyEnv(event.target.value)} placeholder="ANTHROPIC_API_KEY" /></label>
          <label className="full-field">{t('dialog.apiBaseUrl')}<input value={apiBaseUrl} onChange={(event) => setApiBaseUrl(event.target.value)} placeholder={t('dialog.useProviderDefault')} /></label>
        </div>
        <label className="toggle-field"><input type="checkbox" checked={skipPermissions} onChange={(event) => setSkipPermissions(event.target.checked)} /><span>{t('dialog.runWithoutPerms')}</span></label>
        {error && <div className="form-error">{error}</div>}
        <div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>{t('common.cancel')}</button><button className="primary-button" disabled={!task.trim() || !role.trim() || busy || relationDisabled || skillsLoading}>{busy ? t('dialog.adding') : t('dialog.addNode')}</button></div>
      </form>
    </div>
  );
};

export function confirmSessionDelete(session: SessionSummary): boolean {
  return window.confirm(translate('dialog.deleteConfirm', { title: session.title }));
}
