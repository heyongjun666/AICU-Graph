import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, FolderTree, HelpCircle, Languages, Plus, Save, Server, Sparkles, Trash2, Upload, Users, X } from 'lucide-react';
import { CliInfo, GlobalConfig, ProviderPreset, RoleField, RoleInfo, RoleSchema, SkillInfo } from '../types';
import { useI18n } from '../i18n';
import { useUIStateStore } from '../stores/UIStateStore';
import {
  createRole,
  createSkill,
  deleteRole,
  deleteSkill,
  getApiKey,
  getInternalUsername,
  getGlobalConfig,
  getRole,
  getRoleSchema,
  getRoles,
  getSkill,
  getSkills,
  importSkill,
  saveApiKey,
  saveInternalUsername,
  testConnection,
  updateGlobalConfig,
  updateRole,
  updateSkill,
} from '../utils/api';

type SettingsTab = 'connection' | 'workspace' | 'roles' | 'skills' | 'language';
type RoleType = NonNullable<RoleInfo['type']>;

interface WorkbenchSettingsProps {
  open: boolean;
  onClose: () => void;
  onSaved: () => void | Promise<void>;
}

const tabs: Array<{ id: SettingsTab; label: string; icon: React.ReactNode }> = [
  { id: 'connection', label: 'settings.tab.connection', icon: <Server size={16} /> },
  { id: 'workspace', label: 'settings.tab.workspace', icon: <FolderTree size={16} /> },
  { id: 'roles', label: 'settings.tab.roles', icon: <Users size={16} /> },
  { id: 'skills', label: 'settings.tab.skills', icon: <Sparkles size={16} /> },
  { id: 'language', label: 'settings.tab.language', icon: <Languages size={16} /> },
];

const emptyConfig: GlobalConfig = {
  workspace_root: null,
  cli_name: null,
  model: null,
  api_base_url: null,
  api_key_env: null,
};

const ROLE_FIELD_SECTIONS: Record<string, string> = {
  summary: 'Identity', description: 'Identity', suitable_for: 'Identity', not_for: 'Identity',
  num_ccs: 'Execution', model: 'Execution', cli_name: 'Execution', audit_rounds: 'Execution',
  skills_dir: 'Skills', skills: 'Skills',
  output_file: 'Output', final_file: 'Output', modular_output: 'Output', main_doc: 'Output', index_file: 'Output',
};

const SECTION_KEYS: Record<string, string> = {
  Identity: 'settings.sectionIdentity',
  Execution: 'settings.sectionExecution',
  Skills: 'settings.sectionSkills',
  Output: 'settings.sectionOutput',
  Instructions: 'settings.sectionInstructions',
  Advanced: 'settings.sectionAdvanced',
};

function roleFieldSection(field: RoleField): string {
  return ROLE_FIELD_SECTIONS[field.name]
    || (field.type === 'multiline' || field.name.includes('audit') || field.name.includes('review')
      ? 'Instructions'
      : 'Advanced');
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) return value.join('\n');
  return String(value);
}

function coerceField(field: RoleField, value: string): unknown {
  if (field.type === 'number') return value === '' ? undefined : Number(value);
  if (field.type === 'boolean') return value === 'true';
  if (field.type === 'list') return value.split(/[,\n]/).map((item) => item.trim()).filter(Boolean);
  return value;
}

/** 悬浮（hover）或点击（focus）显示说明气泡的帮助图标。 */
const HelpTip: React.FC<{ text: string; title?: string }> = ({ text, title }) => {
  if (!text) return null;
  return (
    <span className="help-tip" tabIndex={0} role="button" aria-label={title || text}>
      <HelpCircle size={14} />
      <span className="help-tip-pop" role="tooltip">{text}</span>
    </span>
  );
};

/** 可搜索的下拉多选框：列出所有 skill，勾选/取消、支持按名称或 id 过滤。 */
const SkillMultiSelect: React.FC<{
  options: SkillInfo[];
  value: string[];
  onChange: (ids: string[]) => void;
}> = ({ options, value, onChange }) => {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (event: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const filtered = options.filter((skill) =>
    !query
    || skill.name.toLowerCase().includes(query.toLowerCase())
    || skill.id.toLowerCase().includes(query.toLowerCase()),
  );
  const selected = options.filter((skill) => value.includes(skill.id));
  const toggle = (id: string) => onChange(
    value.includes(id) ? value.filter((item) => item !== id) : [...value, id],
  );

  return (
    <div className="skill-select" ref={boxRef}>
      <button type="button" className="skill-select-trigger" onClick={() => setOpen((v) => !v)}>
        {selected.length > 0 ? (
          <span className="skill-select-chips">
            {selected.map((skill) => <span key={skill.id} className="skill-select-chip">{skill.name || skill.id}</span>)}
          </span>
        ) : (
          <span className="skill-select-placeholder">{t('settings.selectSkill')}</span>
        )}
        <ChevronDown size={14} className={open ? 'is-open' : ''} />
      </button>
      {open && (
        <div className="skill-select-menu">
          <div className="skill-select-search">
            <input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t('settings.searchSkill')} />
          </div>
          <div className="skill-select-list">
            {filtered.map((skill) => (
              <label key={skill.id} className={`skill-select-item ${value.includes(skill.id) ? 'is-checked' : ''}`}>
                <input type="checkbox" checked={value.includes(skill.id)} onChange={() => toggle(skill.id)} />
                <span className="skill-select-name">{skill.name || skill.id}</span>
                {skill.name !== skill.id && <code className="skill-select-id">{skill.id}</code>}
              </label>
            ))}
            {filtered.length === 0 && <div className="skill-select-empty">{t('settings.noMatchingSkill')}</div>}
          </div>
        </div>
      )}
    </div>
  );
};

const RoleFieldEditor: React.FC<{
  field: RoleField;
  value: unknown;
  overridden: boolean;
  onChange: (value: unknown) => void;
  onReset: () => void;
  skillOptions?: SkillInfo[];
}> = ({ field, value, overridden, onChange, onReset, skillOptions }) => {
  const { t, lang } = useI18n();
  const fieldLabel = lang === 'en' ? field.label_en || field.label : field.label;
  const fieldDescription = lang === 'en' ? field.description_en || field.description : field.description;
  const fieldPlaceholder = lang === 'en' ? field.placeholder_en || field.placeholder : field.placeholder;
  const hasDefault = field.has_default !== false;
  const defaultText = displayValue(field.default);
  const initialOverrideValue = hasDefault
    ? field.default
    : field.type === 'boolean'
      ? false
      : field.type === 'list'
        ? []
        : '';
  const isSkillPicker = field.name === 'skills' && Boolean(skillOptions);
  const currentSkills = (Array.isArray(value) ? value : Array.isArray(field.default) ? field.default : []).map(String);

  if (isSkillPicker) {
    return (
      <div className="role-field is-overridden">
        <div className="role-field-header">
          <div className="role-field-label">
            <label>{fieldLabel}</label>
            <code>{field.name}</code>
            {fieldDescription && <HelpTip text={fieldDescription} title={fieldLabel} />}
          </div>
        </div>
        <SkillMultiSelect options={skillOptions!} value={currentSkills} onChange={onChange} />
        {currentSkills.length === 0 && <small className="field-default">{t('settings.noSkillSelected')}</small>}
      </div>
    );
  }

  const control = field.type === 'boolean' ? (
    <select value={String(value)} onChange={(event) => onChange(event.target.value === 'true')} disabled={!overridden}>
      <option value="true">{t('common.enabled')}</option>
      <option value="false">{t('common.disabled')}</option>
    </select>
  ) : field.type === 'multiline' || field.type === 'list' ? (
    <textarea
      value={displayValue(value)}
      onChange={(event) => onChange(coerceField(field, event.target.value))}
      placeholder={fieldPlaceholder || defaultText || t('settings.enterValue')}
      rows={field.type === 'list' ? 3 : 5}
      disabled={!overridden}
    />
  ) : (
    <input
      type={field.type === 'number' ? 'number' : 'text'}
      value={displayValue(value)}
      onChange={(event) => onChange(coerceField(field, event.target.value))}
      placeholder={fieldPlaceholder || defaultText || t('settings.usesDefaultWhenEmpty')}
      disabled={!overridden}
    />
  );

  return (
    <div className={`role-field ${overridden ? 'is-overridden' : 'uses-default'}`}>
      <div className="role-field-header">
        <div className="role-field-label">
          <label>{fieldLabel}</label>
          <code>{field.name}</code>
          {fieldDescription && <HelpTip text={fieldDescription} title={fieldLabel} />}
        </div>
        <label className="default-toggle"><input type="checkbox" checked={overridden} onChange={(event) => event.target.checked ? onChange(initialOverrideValue) : onReset()} /> {t('settings.override')}</label>
      </div>
      {control}
      {!overridden && <small className="field-default">{hasDefault ? t('settings.default', { value: defaultText || t('settings.empty') }) : t('settings.optionalNoDefault')}</small>}
      {overridden && <button type="button" className="inline-reset" onClick={onReset}>{hasDefault ? t('settings.useDefault') : t('settings.removeOverride')}</button>}
    </div>
  );
};

const WorkbenchSettings: React.FC<WorkbenchSettingsProps> = ({ open, onClose, onSaved }) => {
  const { t, lang, setLang } = useI18n();
  const addToast = useUIStateStore((state) => state.addToast);
  const [tab, setTab] = useState<SettingsTab>('connection');
  const [config, setConfig] = useState<GlobalConfig>(emptyConfig);
  const [clis, setClis] = useState<CliInfo[]>([]);
  const [providerPresets, setProviderPresets] = useState<ProviderPreset[]>([]);
  const [selectedProviderId, setSelectedProviderId] = useState('');
  const [providerInput, setProviderInput] = useState('');
  const [defaultCli, setDefaultCli] = useState<string | null>(null);
  const [roles, setRoles] = useState<RoleInfo[]>([]);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [selectedRole, setSelectedRole] = useState('');
  const [roleId, setRoleId] = useState('');
  const [roleType, setRoleType] = useState<RoleType>('worker');
  const [roleSchema, setRoleSchema] = useState<RoleSchema | null>(null);
  const [roleConfig, setRoleConfig] = useState<Record<string, unknown>>({});
  const [selectedSkill, setSelectedSkill] = useState('');
  const [skillId, setSkillId] = useState('');
  const [skillContent, setSkillContent] = useState('# Skill\n\n');
  const [skillDetail, setSkillDetail] = useState<SkillInfo | null>(null);
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  // 成功提示 banner 自动消除（与 Toast 默认 3000ms 对齐），避免"已删除"等信息常驻不清。
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(''), 3000);
    return () => clearTimeout(timer);
  }, [notice]);
  const [apiKey, setApiKey] = useState('');
  const [apiKeyVendor, setApiKeyVendor] = useState<string | null>(null);
  const [internalUsername, setInternalUsername] = useState('');

  const load = async () => {
    setBusy(true);
    setError('');
    try {
      const [globalResp, nextRoles, nextSkills, schema] = await Promise.all([
        getGlobalConfig(), getRoles(), getSkills(), getRoleSchema('worker'),
      ]);
      setConfig(globalResp.config);
      setClis(globalResp.clis);
      setProviderPresets(globalResp.provider_presets || []);
      const initialPreset = (globalResp.provider_presets || []).find((preset) => preset.available !== false && preset.model === globalResp.config.model);
      setSelectedProviderId(initialPreset?.id || '');
      setProviderInput(initialPreset?.display_name || '');
      setDefaultCli(globalResp.default_cli);
      setRoles(nextRoles);
      setSkills(nextSkills);
      setRoleSchema(schema);
      // 进入页面默认选中第一个角色 / 第一个 skill，而非停留在「新建」态。
      const firstRole = nextRoles[0];
      if (firstRole) {
        const type = firstRole.type || 'worker';
        try {
          const detail = await getRole(type, firstRole.id);
          setSelectedRole(`${type}:${firstRole.id}`);
          setRoleId(detail.id);
          setRoleType(type);
          setRoleConfig(detail.config || {});
          setRoleSchema(detail.schema || schema);
        } catch { /* 首个角色详情加载失败不阻塞设置面板 */ }
      }
      const firstSkill = nextSkills[0];
      if (firstSkill) {
        try {
          const detail = await getSkill(firstSkill.id);
          setSelectedSkill(firstSkill.id);
          setSkillId(detail.id);
          setSkillContent(detail.content || '');
          setSkillDetail(detail);
        } catch { /* 首个 skill 详情加载失败不阻塞设置面板 */ }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (!open) return;
    setTab('connection');
    setNotice('');
    setSelectedRole('');
    setRoleId('');
    setRoleType('worker');
    setRoleConfig({});
    setSelectedSkill('');
    void load();
  }, [open]);

  // 按当前 CLI 回显 Internal 账号或模型厂商 API Key。
  const loadCredential = async (cliName: string | null, model: string | null, fallbackCli: string | null) => {
    if ((cliName || fallbackCli) === 'internal') {
      setApiKey('');
      setApiKeyVendor(null);
      try {
        const resp = await getInternalUsername();
        setInternalUsername(resp.username || '');
      } catch { setInternalUsername(''); }
      return;
    }
    setInternalUsername('');
    if (!model) { setApiKey(''); setApiKeyVendor(null); return; }
    try {
      const resp = await getApiKey(model);
      setApiKey(resp.api_key || '');
      setApiKeyVendor(resp.vendor);
    } catch { setApiKey(''); setApiKeyVendor(null); }
  };

  useEffect(() => {
    if (!open) return;
    void loadCredential(config.cli_name, config.model, defaultCli);
  }, [open, config.cli_name, config.model, defaultCli]);

  const setGlobal = <K extends keyof GlobalConfig>(key: K, value: GlobalConfig[K]) =>
    setConfig((current) => ({ ...current, [key]: value }));

  /** Provider 输入：精确匹配预置厂商则自动填充 CLI/模型/API 参数；否则视为手动自定义，保留用户已填字段。 */
  const applyProviderInput = (raw: string) => {
    const value = raw.trim();
    const preset = providerPresets.find((item) => item.id === value)
      || providerPresets.find((item) => item.display_name === value);
    if (!preset || preset.available === false) {
      setSelectedProviderId('');
      setProviderInput(value);
      return;
    }
    setSelectedProviderId(preset.id);
    setProviderInput(preset.display_name);
    setConfig((current) => ({
      ...current,
      cli_name: preset.cli_name || current.cli_name,
      model: preset.model || preset.models?.[0] || current.model,
      api_base_url: preset.api_base_url || null,
      api_key_env: preset.api_key_env || null,
    }));
  };

  const selectedProvider = providerPresets.find((preset) => preset.id === selectedProviderId);
  const providerModelOptions = selectedProvider?.models || [];

  // 当前 CLI 的已知模型清单：显式选择时用该 CLI 的 models；
  // Auto-detect 时用默认 CLI 的 models；都无法确定则用全部 CLI 模型的并集（去重）。
  const selectedCliId = config.cli_name || defaultCli;
  const selectedCli = clis.find((cli) => cli.id === selectedCliId);
  const modelOptions = selectedCli?.models?.length
    ? selectedCli.models
    : Array.from(new Set(clis.flatMap((cli) => cli.models || [])));
  // 模型可输入（combobox）：把厂商预置模型与 CLI 已知模型合并为建议项，仍允许手输任意模型名。
  const allModelOptions = Array.from(new Set([...providerModelOptions, ...modelOptions]));
  // 已知模型也可能来自预置 provider，因此不能据此清空或禁用自定义地址。
  const modelIsKnown = Boolean(config.model) && modelOptions.includes(config.model as string);
  const refreshAfterSave = async () => {
    try {
      await onSaved();
    } catch {
      addToast({ type: 'warning', message: t('settings.sessionRefreshFailed') });
    }
  };
  const saveConnection = async () => {
    setBusy(true); setError(''); setNotice('');
    try {
      if ((config.cli_name || defaultCli) === 'internal') {
        await saveInternalUsername(internalUsername);
      } else if (config.model) {
        await saveApiKey(config.model, apiKey);
      }
      await updateGlobalConfig({
        cli_name: config.cli_name,
        model: config.model,
        api_base_url: config.api_base_url,
        api_key_env: config.api_key_env,
      });
      const message = t('settings.connectionSaved');
      setNotice(message); addToast({ type: 'success', message });
      await refreshAfterSave();
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(message); addToast({ type: 'error', message });
    }
    finally { setBusy(false); }
  };

  const runConnectionTest = async () => {
    setTesting(true); setError(''); setNotice('');
    try {
      const resp = await testConnection({
        cli_name: config.cli_name || defaultCli,
        model: config.model,
        api_base_url: config.api_base_url,
        api_key_env: config.api_key_env,
      });
      if (resp.ok) {
        const message = resp.result ? t('settings.connectionSuccessResult', { result: resp.result.slice(0, 200) }) : (resp.message || t('settings.connectionSuccess'));
        setNotice(message); addToast({ type: 'success', message });
      } else {
        const message = resp.message || t('settings.connectionFailed');
        setError(message); addToast({ type: 'error', message });
      }
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(message); addToast({ type: 'error', message });
    } finally { setTesting(false); }
  };

  const saveWorkspace = async () => {
    setBusy(true); setError(''); setNotice('');
    try {
      await updateGlobalConfig({ workspace_root: config.workspace_root || null });
      const message = t('settings.workspaceSaved');
      setNotice(message); addToast({ type: 'success', message });
      await refreshAfterSave();
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(message); addToast({ type: 'error', message });
    }
    finally { setBusy(false); }
  };

  const chooseRoleType = async (nextType: RoleType) => {
    setRoleType(nextType);
    setRoleConfig({});
    setRoleSchema(await getRoleSchema(nextType));
  };

  const chooseRole = async (value: string) => {
    setSelectedRole(value); setError('');
    if (!value) {
      setRoleId(''); setRoleType('worker'); setRoleConfig({}); setRoleSchema(await getRoleSchema('worker')); return;
    }
    const [type, id] = value.split(':') as [RoleType, string];
    try {
      const detail = await getRole(type, id);
      setRoleId(detail.id); setRoleType(type); setRoleConfig(detail.config || {}); setRoleSchema(detail.schema || await getRoleSchema(type));
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  };

  const saveRole = async () => {
    if (!roleId.trim()) {
      const message = t('settings.roleIdRequired');
      setError(message); addToast({ type: 'error', message });
      return;
    }
    setBusy(true); setError(''); setNotice('');
    try {
      const payload = { role_id: roleId.trim(), role_type: roleType, config: roleConfig };
      if (selectedRole) await updateRole(payload); else await createRole(payload);
      setRoles(await getRoles()); setSelectedRole(`${roleType}:${payload.role_id}`);
      const message = t('settings.roleSaved');
      setNotice(message); addToast({ type: 'success', message });
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(message); addToast({ type: 'error', message });
    }
    finally { setBusy(false); }
  };

  const chooseSkill = async (id: string) => {
    setSelectedSkill(id); setError(''); setZipFile(null);
    if (!id) { setSkillId(''); setSkillContent('# Skill\n\n'); setSkillDetail(null); return; }
    try { const detail = await getSkill(id); setSkillId(detail.id); setSkillContent(detail.content || ''); setSkillDetail(detail); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  };

  const importZip = async () => {
    if (!zipFile) { setError(t('settings.chooseZipFirst')); return; }
    const id = skillId.trim() || zipFile.name.replace(/\.zip$/i, '').toLowerCase().replace(/[^a-z0-9_-]/g, '-');
    if (!id) { setError(t('settings.skillIdRequired')); return; }
    setBusy(true); setError(''); setNotice('');
    try {
      const detail = await importSkill(id, zipFile);
      setSkills(await getSkills());
      setSelectedSkill(detail.id); setSkillId(detail.id); setSkillContent(detail.content || ''); setSkillDetail(detail);
      setZipFile(null);
      setNotice(t('settings.skillImported'));
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };

  const saveSkill = async () => {
    if (!skillId.trim() || !skillContent.trim()) { setError(t('settings.skillIdAndContentRequired')); return; }
    setBusy(true); setError(''); setNotice('');
    try {
      const payload = { skill_id: skillId.trim(), content: skillContent };
      if (selectedSkill) await updateSkill(payload); else await createSkill(payload);
      setSkills(await getSkills()); setSelectedSkill(payload.skill_id);
      const message = t('settings.skillSaved');
      setNotice(message); addToast({ type: 'success', message });
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(message); addToast({ type: 'error', message });
    }
    finally { setBusy(false); }
  };

  const deleteCurrentRole = async () => {
    if (!selectedRole || !roleId) return;
    const target = roles.find((item) => `${item.type}:${item.id}` === selectedRole);
    const name = target?.display_name || roleId;
    if (!window.confirm(t('settings.deleteRoleConfirm', { name }))) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await deleteRole(roleType, roleId);
      const nextRoles = await getRoles();
      setRoles(nextRoles);
      setNotice(t('settings.roleDeleted'));
      addToast({ type: 'success', message: t('settings.roleDeleted') });
      const next = nextRoles[0];
      if (next) await chooseRole(`${next.type}:${next.id}`);
      else { setSelectedRole(''); setRoleId(''); setRoleType('worker'); setRoleConfig({}); setRoleSchema(await getRoleSchema('worker')); }
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };

  const deleteCurrentSkill = async () => {
    if (!selectedSkill || !skillId) return;
    const target = skills.find((item) => item.id === selectedSkill);
    const name = target?.name || skillId;
    if (!window.confirm(t('settings.deleteSkillConfirm', { name }))) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await deleteSkill(skillId);
      const nextSkills = await getSkills();
      setSkills(nextSkills);
      setNotice(t('settings.skillDeleted'));
      addToast({ type: 'success', message: t('settings.skillDeleted') });
      const next = nextSkills[0];
      if (next) await chooseSkill(next.id);
      else { setSelectedSkill(''); setSkillId(''); setSkillContent('# Skill\n\n'); setSkillDetail(null); }
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };

  if (!open) return null;
  const roleFields = roleSchema?.fields || [];
  const basicFields = roleFields.filter((field) => !field.advanced);
  const advancedFields = roleFields.filter((field) => field.advanced);
  const groupFields = (fields: RoleField[]) => fields.reduce<Record<string, RoleField[]>>((groups, field) => {
    const section = roleFieldSection(field);
    (groups[section] ||= []).push(field);
    return groups;
  }, {});
  const basicSections = groupFields(basicFields);
  const advancedSections = groupFields(advancedFields);

  const renderFieldset = (section: string, fields: RoleField[]) => (
    <fieldset className="config-section" key={section}>
      <legend>{t(SECTION_KEYS[section] || section)} ({fields.length})</legend>
      {fields.map((field) => (
        <RoleFieldEditor
          key={field.name}
          field={field}
          value={roleConfig[field.name]}
          overridden={Object.prototype.hasOwnProperty.call(roleConfig, field.name)}
          onChange={(value) => setRoleConfig((current) => ({ ...current, [field.name]: value }))}
          onReset={() => setRoleConfig((current) => { const next = { ...current }; delete next[field.name]; return next; })}
          skillOptions={field.name === 'skills' ? skills : undefined}
        />
      ))}
    </fieldset>
  );

  return (
    <div className="dialog-backdrop settings-backdrop" role="presentation">
      <section className="settings-dialog" role="dialog" aria-modal="true" aria-label={t('settings.title')}>
        <header className="dialog-header"><div><span className="eyebrow">WORKBENCH</span><h2>{t('settings.title')}</h2></div><button className="icon-button" onClick={onClose} title={t('settings.close')}><X size={18} /></button></header>
        <div className="settings-layout">
          <nav className="settings-nav">{tabs.map((item) => <button key={item.id} className={tab === item.id ? 'is-active' : ''} onClick={() => setTab(item.id)}>{item.icon}<span>{t(item.label)}</span></button>)}</nav>
          <div className="settings-body">
            {error && <div className="settings-message is-error">{error}</div>}
            {notice && <div className="settings-message is-success">{notice}</div>}
            {tab === 'connection' && <div className="settings-section"><div className="section-heading"><div><h3>{t('settings.connectionTitle')}</h3><p>{t('settings.connectionDesc')}</p></div></div><div className="form-grid"><label>Provider 厂商<input list="provider-options" value={providerInput} onChange={(event) => applyProviderInput(event.target.value)} placeholder="手动配置（输入厂商名，或从列表选择预置厂商）" /><datalist id="provider-options">{providerPresets.filter((preset) => preset.available !== false).map((preset) => <option key={preset.id} value={preset.display_name} />)}</datalist><small>选择预置厂商会自动填充 CLI/模型/API 地址；也可手动输入自定义厂商后自行配置下方各项。</small></label><label>{t('settings.cli')}<select value={config.cli_name || ''} onChange={(event) => setGlobal('cli_name', event.target.value || null)}><option value="">{t('settings.autoDetect')}{defaultCli ? ` (${defaultCli})` : ''}</option>{clis.map((cli) => <option key={cli.id} value={cli.id}>{cli.display_name}{cli.installed ? '' : t('settings.notInstalled')}</option>)}</select></label><label>{t('settings.model')}<input list="model-options" value={config.model || ''} onChange={(event) => setGlobal('model', event.target.value || null)} placeholder={selectedCli ? t('settings.selectOrTypeModel') : t('settings.usesRuntimeDefault')} /><datalist id="model-options">{allModelOptions.map((model) => <option key={model} value={model} />)}</datalist></label><label>{t('settings.apiBaseUrl')}<input value={config.api_base_url || ''} onChange={(event) => setGlobal('api_base_url', event.target.value || null)} placeholder={t('common.optional')} /><small>{t('settings.directConnectNote')}</small></label>{(config.cli_name || defaultCli) === 'internal' ? <label>账号名称<input value={internalUsername} onChange={(event) => setInternalUsername(event.target.value)} placeholder="请输入账号名称" /><small>Internal 使用账号名称认证。</small></label> : <label>{t('settings.apiKey')}<input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={t('settings.apiKeyPlaceholder')} /><small>{apiKeyVendor ? t('settings.vendor', { vendor: apiKeyVendor }) : ''}{t('settings.localPlaintextNote')}</small></label>}</div><div className="settings-actions"><button className="secondary-button" disabled={testing || busy} onClick={runConnectionTest}>{testing ? t('settings.testing') : t('settings.testConnection')}</button><button className="primary-button" disabled={busy} onClick={saveConnection}><Save size={15} /> {t('settings.saveConnection')}</button></div></div>}
            {tab === 'workspace' && <div className="settings-section"><div className="section-heading"><div><h3>{t('settings.workspaceTitle')}</h3><p>{t('settings.workspaceDesc')}</p></div></div><label>{t('settings.workspaceRoot')}<input value={config.workspace_root || ''} onChange={(event) => setGlobal('workspace_root', event.target.value || null)} placeholder={t('settings.workspaceRootPlaceholder')} /></label><div className="settings-actions"><button className="primary-button" disabled={busy} onClick={saveWorkspace}><Save size={15} /> {t('settings.saveWorkspace')}</button></div></div>}
            {tab === 'roles' && <div className="settings-section roles-editor"><div className="section-heading"><div><h3>{t('settings.rolesTitle')}</h3><p>{t('settings.rolesDesc', { count: roleFields.length })}</p></div><button className="secondary-button" onClick={() => void chooseRole('')}><Plus size={15} /> {t('settings.newRole')}</button></div><div className="role-editor-layout"><aside className="resource-list"><ul className="resource-list-items">{roles.map((item) => { const value = `${item.type}:${item.id}`; return <li key={value}><button type="button" className={selectedRole === value ? 'is-active' : ''} onClick={() => void chooseRole(value)}><span className="resource-list-type">{item.type === 'manager' ? t('dialog.manager') : t('dialog.worker')}</span><span className="resource-list-name">{item.display_name}</span></button></li>; })}</ul><div className="resource-list-note">{t('settings.customRoles', { count: roles.length })}</div></aside><div className="role-form"><div className="form-grid"><label>{t('settings.roleId')}<input value={roleId} onChange={(event) => setRoleId(event.target.value)} placeholder="lowercase_role_id" disabled={Boolean(selectedRole)} /></label><label>{t('settings.roleType')}<select value={roleType} onChange={(event) => void chooseRoleType(event.target.value as RoleType)} disabled={Boolean(selectedRole)}><option value="manager">{t('dialog.manager')}</option><option value="worker">{t('dialog.worker')}</option></select></label></div>{Object.entries(basicSections).map(([section, fields]) => renderFieldset(section, fields))}{advancedFields.length > 0 && <div className="advanced-config"><button type="button" className="advanced-config-toggle" onClick={() => setAdvancedOpen((value) => !value)} aria-expanded={advancedOpen}><ChevronDown size={14} className={advancedOpen ? 'is-open' : ''} /><span>{t('settings.advancedConfig')}</span><span className="advanced-config-count">{advancedFields.length}</span></button>{advancedOpen && <div className="advanced-config-body"><p className="advanced-config-desc">{t('settings.advancedConfigDesc')}</p>{Object.entries(advancedSections).map(([section, fields]) => renderFieldset(section, fields))}</div>}</div>}<div className="settings-actions">{selectedRole && <button className="danger-button" disabled={busy} onClick={deleteCurrentRole}><Trash2 size={15} /> {t('settings.deleteRole')}</button>}<button className="primary-button" disabled={busy || !roleId.trim()} onClick={saveRole}><Save size={15} /> {t('settings.saveRole')}</button></div></div></div></div>}
            {tab === 'skills' && <div className="settings-section skills-editor"><div className="section-heading"><div><h3>{t('settings.skillsTitle')}</h3><p>{t('settings.skillsDesc')}</p></div><button className="secondary-button" onClick={() => void chooseSkill('')}><Plus size={15} /> {t('settings.newSkill')}</button></div><div className="skill-editor-layout"><aside className="resource-list"><ul className="resource-list-items">{skills.map((item) => <li key={item.id}><button type="button" className={selectedSkill === item.id ? 'is-active' : ''} onClick={() => void chooseSkill(item.id)}><span className="resource-list-name">{item.name || item.id}</span></button></li>)}</ul><div className="resource-list-note">{t('settings.availableSkills', { count: skills.length })}</div></aside><div className="skill-form"><label>{t('settings.skillId')}<input value={skillId} onChange={(event) => setSkillId(event.target.value)} placeholder="lowercase-skill-id" disabled={Boolean(selectedSkill)} /></label><label>{t('settings.instructions')}<textarea className="skill-content-editor" value={skillContent} onChange={(event) => setSkillContent(event.target.value)} rows={18} placeholder={t('settings.skillContentPlaceholder')} /></label>{skillDetail && <div className="skill-meta">{skillDetail.description && <p className="skill-meta-desc">{skillDetail.description}</p>}{skillDetail.license && <div className="skill-meta-row"><span>{t('settings.license')}</span><code>{skillDetail.license}</code></div>}{(skillDetail.allowed_tools?.length ?? 0) > 0 && <div className="skill-meta-row"><span>{t('settings.allowedTools')}</span><div className="skill-tags">{skillDetail.allowed_tools!.map((tool) => <code key={tool}>{tool}</code>)}</div></div>}{(skillDetail.files?.length ?? 0) > 0 && <div className="skill-meta-row"><span>{t('settings.files', { count: skillDetail.files!.length })}</span><div className="skill-files">{skillDetail.files!.map((file) => <code key={file}>{file}</code>)}</div></div>}</div>}<div className="skill-import"><div className="skill-import-title"><Upload size={14} /> {t('settings.importFromZip')}<HelpTip text={t('settings.importHelp')} /></div><div className="skill-import-row"><input type="file" accept=".zip,application/zip" onChange={(event) => setZipFile(event.target.files?.[0] || null)} />{zipFile && <code className="skill-import-file">{zipFile.name}</code>}<button className="secondary-button" disabled={busy || !zipFile} onClick={importZip}>{t('settings.import')}</button></div><small>{t('settings.importNote')}</small></div><div className="settings-actions">{selectedSkill && <button className="danger-button" disabled={busy} onClick={deleteCurrentSkill}><Trash2 size={15} /> {t('settings.deleteSkill')}</button>}<button className="primary-button" disabled={busy || !skillId.trim() || !skillContent.trim()} onClick={saveSkill}><Save size={15} /> {t('settings.saveSkill')}</button></div></div></div></div>}
            {tab === 'language' && <div className="settings-section language-switcher"><div className="section-heading"><div><h3>{t('lang.switchTitle')}</h3><p>{t('lang.switchDesc')}</p></div></div><div className="language-options"><button type="button" className={lang === 'zh' ? 'is-active' : ''} onClick={() => setLang('zh')}>中文</button><button type="button" className={lang === 'en' ? 'is-active' : ''} onClick={() => setLang('en')}>English</button></div></div>}
          </div>
        </div>
      </section>
    </div>
  );
};

export default WorkbenchSettings;
