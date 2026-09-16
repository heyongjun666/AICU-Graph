import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, Boxes, FolderTree, HelpCircle, Languages, Pencil, Plus, Save, Server, Sparkles, Trash2, Upload, Users, X } from 'lucide-react';
import { CliInfo, EffectiveBaseUrlResponse, GlobalConfig, ProviderPreset, RoleField, RoleInfo, RolePool, RoleSchema, SkillInfo } from '../types';
import { useI18n } from '../i18n';
import { useUIStateStore } from '../stores/UIStateStore';
import SearchCheckboxPicker from './SearchCheckboxPicker';
import {
  createRole,
  createRolePool,
  createSkill,
  deleteRole,
  deleteRolePool,
  deleteSkill,
  getApiKey,
  getInternalUsername,
  getEffectiveBaseUrl,
  getGlobalConfig,
  getRole,
  getRolePools,
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
  updateRolePool,
  updateSkill,
} from '../utils/api';
import { clearRoleHoverDetailCache, loadRoleHoverDetail } from '../utils/roleHoverDetail';

type SettingsTab = 'connection' | 'workspace' | 'roles' | 'pools' | 'skills' | 'language';
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
  { id: 'pools', label: 'settings.tab.pools', icon: <Boxes size={16} /> },
  { id: 'skills', label: 'settings.tab.skills', icon: <Sparkles size={16} /> },
  { id: 'language', label: 'settings.tab.language', icon: <Languages size={16} /> },
];

const emptyConfig: GlobalConfig = {
  workspace_root: null,
  cli_name: null,
  model: null,
  api_base_url: null,
  api_key_env: null,
  dynamic_num_ccs: true,
  default_num_ccs: 1,
};

/** 单节点 agent 数量在关闭动态配置时的合法区间（与后端 plan_validator 对齐）。 */
const NUM_CCS_MIN = 1;
const NUM_CCS_MAX = 4;

/** Provider 下拉里「手动配置」那一项的取值。预置 id 全是厂商名（openai / deepseek…），
 *  用双下划线包起来不会撞上。取值只有两种：某个预置 id，或它 —— 没有「不指定」那一项，
 *  因为「什么都没选」和「我要自己填」在界面上长得一样，用户却以为是两回事。 */
const MANUAL_PROVIDER = '__manual__';

const clampNumCcs = (value: unknown): number => {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  if (Number.isNaN(parsed)) return NUM_CCS_MIN;
  return Math.min(NUM_CCS_MAX, Math.max(NUM_CCS_MIN, parsed));
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

/** 资源编辑区的显隐状态：idle = 什么都没选，view = 只读查看，edit = 改已有条目，create = 新建。 */
type ResourceMode = 'idle' | 'view' | 'edit' | 'create';

/** 由「是否在编辑」与「当前选中项」推导，避免两者各存一份而互相打架。 */
const deriveMode = (editing: boolean, selected: string): ResourceMode =>
  editing ? (selected ? 'edit' : 'create') : (selected ? 'view' : 'idle');

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
  disabled?: boolean;
}> = ({ options, value, onChange, disabled = false }) => {
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
      <button type="button" className="skill-select-trigger" disabled={disabled} onClick={() => !disabled && setOpen((v) => !v)}>
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
                <input type="checkbox" checked={value.includes(skill.id)} onChange={() => toggle(skill.id)} disabled={disabled} />
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
  /** 只读查看态。必须显式透传：控件的禁用条件是 `!overridden`，
   *  而 roleConfig 恰好就是被覆盖字段的集合，不单独判只读会让"查看"态里
   *  最该锁住的那些字段反而可改。 */
  readOnly?: boolean;
}> = ({ field, value, overridden, onChange, onReset, skillOptions, readOnly = false }) => {
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
        <SkillMultiSelect options={skillOptions!} value={currentSkills} onChange={onChange} disabled={readOnly} />
        {currentSkills.length === 0 && <small className="field-default">{t('settings.noSkillSelected')}</small>}
      </div>
    );
  }

  const control = field.type === 'boolean' ? (
    <select value={String(value)} onChange={(event) => onChange(event.target.value === 'true')} disabled={readOnly || !overridden}>
      <option value="true">{t('common.enabled')}</option>
      <option value="false">{t('common.disabled')}</option>
    </select>
  ) : field.type === 'multiline' || field.type === 'list' ? (
    <textarea
      value={displayValue(value)}
      onChange={(event) => onChange(coerceField(field, event.target.value))}
      placeholder={fieldPlaceholder || defaultText || t('settings.enterValue')}
      rows={field.type === 'list' ? 3 : 5}
      disabled={readOnly || !overridden}
    />
  ) : (
    <input
      type={field.type === 'number' ? 'number' : 'text'}
      value={displayValue(value)}
      onChange={(event) => onChange(coerceField(field, event.target.value))}
      placeholder={fieldPlaceholder || defaultText || t('settings.usesDefaultWhenEmpty')}
      disabled={readOnly || !overridden}
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
        <label className="default-toggle"><input type="checkbox" checked={overridden} disabled={readOnly} onChange={(event) => event.target.checked ? onChange(initialOverrideValue) : onReset()} /> {t('settings.override')}</label>
      </div>
      {control}
      {!overridden && <small className="field-default">{hasDefault ? t('settings.default', { value: defaultText || t('settings.empty') }) : t('settings.optionalNoDefault')}</small>}
      {overridden && !readOnly && <button type="button" className="inline-reset" onClick={onReset}>{hasDefault ? t('settings.useDefault') : t('settings.removeOverride')}</button>}
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
  // 加载完成前先当作「手动配置」：此时什么都没读到，摆一个预置出来是无中生有，
  // 而手动模式是唯一不宣称任何事实的那一态。
  const [selectedProviderId, setSelectedProviderId] = useState(MANUAL_PROVIDER);
  const [defaultCli, setDefaultCli] = useState<string | null>(null);
  const [roles, setRoles] = useState<RoleInfo[]>([]);
  const [pools, setPools] = useState<RolePool[]>([]);
  const [selectedPool, setSelectedPool] = useState('');
  const [poolName, setPoolName] = useState('');
  const [poolRoles, setPoolRoles] = useState<string[]>([]);
  const [poolQuery, setPoolQuery] = useState('');
  // 三个资源 tab 的编辑开关。不另设 mode 枚举：selectedX === '' 本来就是「新建」的判据，
  // 再存一份 mode 会出现「编辑中但没有选中项」这类无法自洽的状态。
  const [editingRole, setEditingRole] = useState(false);
  const [editingPool, setEditingPool] = useState(false);
  const [editingSkill, setEditingSkill] = useState(false);
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
  // 后端算出的「生效地址」（非手动模式下地址框显示的就是它，随模型厂商变）。
  const [effectiveBaseUrl, setEffectiveBaseUrl] = useState<EffectiveBaseUrlResponse | null>(null);

  const load = async () => {
    setBusy(true);
    setError('');
    try {
      const [globalResp, nextRoles, nextSkills, schema, nextPools] = await Promise.all([
        getGlobalConfig(), getRoles(), getSkills(), getRoleSchema('worker'), getRolePools(),
      ]);
      setConfig(globalResp.config);
      setClis(globalResp.clis);
      const providerPresets = globalResp.provider_presets || [];
      setProviderPresets(providerPresets);
      // 反查预置：先按「模型 + 该预置自己声称的 CLI」，再按已存的 api_base_url。
      // 两条都要比对 cli_name —— 预置是「把某个 CLI 配起来」的一组参数，CLI 对不上时
      // 它就不是当前这套配置的来源（用户选了预置又改了 CLI 就会这样）。
      const presetCliId = globalResp.config.cli_name || globalResp.default_cli;
      const usablePresets = providerPresets.filter(
        (preset) => preset.available !== false && preset.cli_name === presetCliId,
      );
      const initialPreset = usablePresets.find((preset) => preset.model === globalResp.config.model)
        ?? usablePresets.find(
          (preset) => globalResp.config.api_base_url && preset.api_base_url === globalResp.config.api_base_url,
        );
      // 反查不到任何预置就落回「手动配置」：这一态下模型是自由输入、地址交还用户，用户
      // 自己架了网关时还能把已存的模型/地址原样带走；若在这里擅自挑一个预置，界面就会
      // 宣称一套用户从没选过的参数。仓库里没有任何 config.json 迁移代码
      //（session_manager.py:388-401 对未知键静默丢弃、不做归一），兼容只能在这做。
      setSelectedProviderId(initialPreset?.id ?? MANUAL_PROVIDER);
      // 预置是靠地址认出来的、而模型是空的时候（手改过 config.json），把预置的模型带上：
      // 严格下拉里没有「空」这一项，不补值用户看到的就是一个空框。
      if (initialPreset && !globalResp.config.model) {
        setConfig((current) => ({ ...current, model: initialPreset.model ?? null }));
      }
      setDefaultCli(globalResp.default_cli);
      setRoles(nextRoles);
      setSkills(nextSkills);
      setRoleSchema(schema);
      setPools(nextPools);
      // 与角色/skill 一致：默认选中第一个角色池，而非停留在「新建」态。
      const firstPool = nextPools[0];
      if (firstPool) fillPoolForm(firstPool);
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
    setPoolFormEmpty();
    setSelectedSkill('');
    // 打开面板一律从只读态开始，且清掉上一次的编辑开关。
    setEditingRole(false);
    setEditingPool(false);
    setEditingSkill(false);
    setAdvancedOpen(false);
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

  /** Provider 下拉：选预置则自动填充 CLI/模型/API 参数；选「手动配置」则把端点交还用户。
   *  取值只有预置 id 与 MANUAL_PROVIDER 两种（与旁边的 CLI 下拉取值一致，不是 display_name）。 */
  const chooseProvider = (value: string) => {
    setSelectedProviderId(value);
    if (value === MANUAL_PROVIDER) {
      // 必须清空地址：预置残留值若恰好等于某个预置地址，后端会按模型厂商把它改写掉
      //（cli_adapters.py:216-217），界面上写着「手动配置」而实际地址被换掉。
      setConfig((current) => ({ ...current, api_base_url: null }));
      return;
    }
    const preset = providerPresets.find((item) => item.id === value);
    if (!preset || preset.available === false) return;  // 认不出的取值：各字段保持原样
    setConfig((current) => ({
      ...current,
      cli_name: preset.cli_name || current.cli_name,
      model: preset.model || preset.models?.[0] || current.model,
      api_base_url: preset.api_base_url || null,
      api_key_env: preset.api_key_env || null,
    }));
  };

  const manualMode = selectedProviderId === MANUAL_PROVIDER;
  const selectedProvider = providerPresets.find((preset) => preset.id === selectedProviderId);
  // 模型能不能手输 = 端点是不是用户自己的：手动配置下名单无从算起，只能让他填；
  // 其余情形（某个预置）名单是确定的，就不该让人把 gpt-5.6 敲进 claude 的下拉里。
  const modelIsFreeText = manualMode;
  // 地址框只在手动模式下可编辑。其余模式下框架里显示的是后端算出的「生效地址」——
  // 它随模型厂商变，用户改它没有意义（存进去也会被后端按模型改写掉）。
  const addressLocked = Boolean(selectedProvider);

  // 生效地址随 (CLI, 模型, 已存地址) 变，所以每次变化都去后端问一次 —— 规则只有后端那一份
  //（cli_adapters.effective_base_url），前端不复刻，免得改了这边忘了那边。
  useEffect(() => {
    if (!open || !addressLocked) {
      setEffectiveBaseUrl(null);
      return;
    }
    let cancelled = false;
    getEffectiveBaseUrl({ cliName: config.cli_name, model: config.model, apiBaseUrl: config.api_base_url })
      .then((resp) => { if (!cancelled) setEffectiveBaseUrl(resp); })
      // 问不到就退回显示已存地址（下面 displayedBaseUrl 的 `??`）：宁可少说，不可说错。
      .catch(() => { if (!cancelled) setEffectiveBaseUrl(null); });
    return () => { cancelled = true; };
  }, [open, addressLocked, config.cli_name, config.model, config.api_base_url]);

  // 非手动模式下这个框是只读的「生效地址」，值不来自 config（config 里存的是预置地址，
  // 后端会按模型把它改写掉）——所以显示与落盘本来就会不一致，显示的那个才是真的。
  const displayedBaseUrl = manualMode
    ? config.api_base_url || ''
    : effectiveBaseUrl?.base_url ?? config.api_base_url ?? '';
  const baseUrlHint = manualMode
    ? t('settings.directConnectNote')
    : effectiveBaseUrl?.source === 'model_vendor'
      ? t('settings.apiBaseUrlFromModel')
      : effectiveBaseUrl?.source === 'cli_default'
        ? t('settings.apiBaseUrlCliDefault')
        : t('settings.apiBaseUrlLocked');

  // 当前 CLI 的已知模型清单：显式选择时用该 CLI 的 models；
  // Auto-detect 时用默认 CLI 的 models；都无法确定则用全部 CLI 模型的并集（去重）。
  const selectedCliId = config.cli_name || defaultCli;
  // 找不到当前 CLI（例如配置里存着已从下拉隐藏的 CLI id）时退回默认 CLI，而不是落到
  // 下面的「全部并集」—— 那会把别家 CLI 才能用的模型全带回来。
  const selectedCli = clis.find((cli) => cli.id === selectedCliId)
    ?? clis.find((cli) => cli.id === defaultCli);
  const modelOptions = selectedCli?.models?.length
    ? selectedCli.models
    : Array.from(new Set(clis.flatMap((cli) => cli.models || [])));

  // 厂商预置的模型只在「预置自己的 CLI == 当前 CLI」时才并入候选。否则选了 OpenAI
  // 预置、再把 CLI 切成 claude 时，那几个 gpt-* 仍会留在 claude 的下拉里 —— 而
  // OpenAI 没有 /v1/messages 端点，选中必然连不上。
  const providerModelOptions = selectedProvider && selectedProvider.cli_name === selectedCliId
    ? selectedProvider.models || []
    : [];

  // 模型可输入（combobox）：把厂商预置模型与 CLI 已知模型合并为建议项，仍允许手输任意模型名。
  const allModelOptions = Array.from(new Set([...providerModelOptions, ...modelOptions]));

  // 严格下拉时，已保存的 model 可能落在候选之外（换过 CLI、或目录里删过该 id）。不把它
  // 作为一个 option 注入的话，<select value> 匹配不到任何项、浏览器会显示成空白 —— 用户
  // 看到的和他实际保存的不是一回事，他动一下别的字段再保存，这个值就被悄悄换掉了。
  // 不自动清空它：清掉用户已存的值比留着更烦人。
  const modelIsForeign = Boolean(config.model) && !allModelOptions.includes(config.model as string);
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

  const saveExecution = async () => {
    setBusy(true); setError(''); setNotice('');
    try {
      await updateGlobalConfig({
        dynamic_num_ccs: config.dynamic_num_ccs !== false,
        default_num_ccs: clampNumCcs(config.default_num_ccs),
      });
      const message = t('settings.executionSaved');
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
    setSelectedRole(value); setError(''); setAdvancedOpen(false);
    if (!value) {
      setRoleId(''); setRoleType('worker'); setRoleConfig({}); setRoleSchema(await getRoleSchema('worker')); return;
    }
    const [type, id] = value.split(':') as [RoleType, string];
    try {
      const detail = await getRole(type, id);
      setRoleId(detail.id); setRoleType(type); setRoleConfig(detail.config || {}); setRoleSchema(detail.schema || await getRoleSchema(type));
    } catch (reason) {
      // 详情拉不到就清空表单，否则新的高亮行下面还挂着上一个角色的配置。
      setRoleId(''); setRoleConfig({});
      setError(reason instanceof Error ? reason.message : String(reason));
    }
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
      // 角色内容变了，悬停卡片里缓存的旧人格要丢掉，否则池编辑处悬停还看到改之前的。
      clearRoleHoverDetailCache();
      setRoles(await getRoles()); setSelectedRole(`${roleType}:${payload.role_id}`);
      setEditingRole(false);
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
    catch (reason) {
      // 同 chooseRole：拉不到详情就清空，别把上一个 skill 的内容留在新的高亮行下面。
      setSkillId(''); setSkillContent('# Skill\n\n'); setSkillDetail(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    }
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
      setEditingSkill(false);
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
      setEditingSkill(false);
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
      clearRoleHoverDetailCache();
      const nextRoles = await getRoles();
      setRoles(nextRoles);
      setNotice(t('settings.roleDeleted'));
      addToast({ type: 'success', message: t('settings.roleDeleted') });
      const next = nextRoles[0];
      setEditingRole(false); setAdvancedOpen(false);
      if (next) await chooseRole(`${next.type}:${next.id}`);
      else { setSelectedRole(''); setRoleId(''); setRoleType('worker'); setRoleConfig({}); setRoleSchema(await getRoleSchema('worker')); }
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };

  const reportPoolError = (message: string) => {
    setError(message); addToast({ type: 'error', message });
  };

  const setPoolFormEmpty = () => { setSelectedPool(''); setPoolName(''); setPoolRoles([]); setPoolQuery(''); };

  const fillPoolForm = (pool: RolePool) => {
    setSelectedPool(pool.id); setPoolName(pool.name); setPoolRoles(pool.roles); setPoolQuery('');
  };

  /** 入参为空串表示切到「新建」态。 */
  const choosePool = (id: string) => {
    setError('');
    const pool = id ? pools.find((item) => item.id === id) : undefined;
    if (pool) fillPoolForm(pool); else setPoolFormEmpty();
  };

  /** 角色类型的显示名。列表 pill 与悬浮卡片的短标签共用，省得各处再抄一遍三元表达式。 */
  const roleTypeLabel = (type?: RoleInfo['type']) =>
    (type === 'manager' ? t('dialog.manager') : t('dialog.worker'));

  const togglePoolRole = (id: string, checked: boolean) => {
    setPoolRoles((current) => checked
      ? (current.includes(id) ? current : [...current, id])
      : current.filter((item) => item !== id));
  };

  const savePool = async () => {
    const name = poolName.trim();
    if (!name) return reportPoolError(t('settings.poolNameRequired'));
    if (!poolRoles.length) return reportPoolError(t('settings.poolRolesRequired'));
    setBusy(true); setError(''); setNotice('');
    try {
      const payload = { name, roles: poolRoles };
      const saved = selectedPool
        ? await updateRolePool(selectedPool, payload)
        : await createRolePool(payload);
      setPools(await getRolePools());
      fillPoolForm(saved);
      setEditingPool(false);
      const message = t('settings.poolSaved');
      setNotice(message); addToast({ type: 'success', message });
    } catch (reason) {
      reportPoolError(reason instanceof Error ? reason.message : String(reason));
    } finally { setBusy(false); }
  };

  const deleteCurrentPool = async () => {
    if (!selectedPool) return;
    const target = pools.find((item) => item.id === selectedPool);
    if (!window.confirm(t('settings.deletePoolConfirm', { name: target?.name || poolName }))) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await deleteRolePool(selectedPool);
      const nextPools = await getRolePools();
      setPools(nextPools);
      setEditingPool(false);
      if (nextPools[0]) fillPoolForm(nextPools[0]); else setPoolFormEmpty();
      setNotice(t('settings.poolDeleted'));
      addToast({ type: 'success', message: t('settings.poolDeleted') });
    } catch (reason) {
      reportPoolError(reason instanceof Error ? reason.message : String(reason));
    } finally { setBusy(false); }
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
      setEditingSkill(false); setZipFile(null);
      if (next) await chooseSkill(next.id);
      else { setSelectedSkill(''); setSkillId(''); setSkillContent('# Skill\n\n'); setSkillDetail(null); }
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };

  /**
   * 当前选中条目在库里的名字，用于确认框里指名道姓。
   * 必须是**已存的那一份**，不能直接用表单里的值：用户把名字改成草稿后，
   * 确认框会拿草稿名去问他要不要放弃「草稿」，等于没说清要丢的是哪一条。
   * 只有在条目还没落库（新建态，选中为空）时才回退到表单值。
   */
  const currentRoleName = () =>
    roles.find((item) => `${item.type}:${item.id}` === selectedRole)?.display_name || roleId;
  const currentPoolName = () => pools.find((item) => item.id === selectedPool)?.name || poolName;
  const currentSkillName = () => skills.find((item) => item.id === selectedSkill)?.name || skillId;

  /**
   * 用户主动切换条目：编辑中会丢改动，先问一句。
   * 保存/删除后的内部重选走 chooseRole / chooseSkill / fillPoolForm 等裸加载函数，不经过这里，
   * 否则「存完自动跳回只读」会自己弹自己一个确认框。
   *
   * 有两条路能离开编辑态：左侧列表点另一条，和标题栏的「新建」。两条都走这里，
   * 所以「新建」按钮在编辑态下仍然显示——它被藏起来的话，用户就只能先点「取消」，
   * 而这个守卫也就成了永远走不到的死代码。
   */
  const confirmSwitchAway = (editing: boolean, nextValue: string, currentValue: string, currentName: string) => {
    if (!editing || nextValue === currentValue) return true;
    return window.confirm(t('settings.discardEditsConfirm', { name: currentName }));
  };

  const requestSelectRole = (value: string) => {
    if (!confirmSwitchAway(editingRole, value, selectedRole, currentRoleName())) return;
    setEditingRole(false);
    void chooseRole(value);
  };

  const requestSelectPool = (id: string) => {
    if (!confirmSwitchAway(editingPool, id, selectedPool, currentPoolName())) return;
    setEditingPool(false);
    choosePool(id);
  };

  const requestSelectSkill = (id: string) => {
    if (!confirmSwitchAway(editingSkill, id, selectedSkill, currentSkillName())) return;
    setEditingSkill(false);
    void chooseSkill(id);
  };

  const startCreateRole = () => {
    if (!confirmSwitchAway(editingRole, '', selectedRole, currentRoleName())) return;
    setEditingRole(true);
    void chooseRole('');
  };

  const startCreatePool = () => {
    if (!confirmSwitchAway(editingPool, '', selectedPool, currentPoolName())) return;
    setEditingPool(true);
    choosePool('');
  };

  const startCreateSkill = () => {
    if (!confirmSwitchAway(editingSkill, '', selectedSkill, currentSkillName())) return;
    setEditingSkill(true);
    void chooseSkill('');
  };

  /** 取消编辑：丢掉改动，重新按权威数据回填，再回到只读态。 */
  const cancelEditRole = async () => {
    setEditingRole(false); setAdvancedOpen(false); setError('');
    if (selectedRole) await chooseRole(selectedRole);
    else { setRoleId(''); setRoleType('worker'); setRoleConfig({}); setRoleSchema(await getRoleSchema('worker')); }
  };

  const cancelEditPool = () => {
    setEditingPool(false); setError('');
    const pool = pools.find((item) => item.id === selectedPool);
    if (pool) fillPoolForm(pool); else setPoolFormEmpty();
  };

  const cancelEditSkill = async () => {
    setEditingSkill(false); setError(''); setZipFile(null);
    if (selectedSkill) await chooseSkill(selectedSkill);
    else { setSkillId(''); setSkillContent('# Skill\n\n'); setSkillDetail(null); }
  };

  /**
   * 关闭设置面板时若正在编辑，提醒一句，避免点右上角的关闭直接丢掉改动。
   * 三个 tab 同一时刻只有一个在编辑，所以按 tab 取当前条目的名字，提示里能指名道姓。
   */
  const requestClose = () => {
    if (!editingRole && !editingPool && !editingSkill) { onClose(); return; }
    const name = editingRole ? currentRoleName() : editingPool ? currentPoolName() : currentSkillName();
    if (!window.confirm(t('settings.discardEditsConfirm', { name: name || t('settings.unsavedEdits') }))) return;
    onClose();
  };

  if (!open) return null;
  const roleMode = deriveMode(editingRole, selectedRole);
  const poolMode = deriveMode(editingPool, selectedPool);
  const skillMode = deriveMode(editingSkill, selectedSkill);
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
          readOnly={roleMode === 'view' || roleMode === 'idle'}
        />
      ))}
    </fieldset>
  );

  return (
    <div className="dialog-backdrop settings-backdrop" role="presentation">
      <section className="settings-dialog" role="dialog" aria-modal="true" aria-label={t('settings.title')}>
        <header className="dialog-header"><div><span className="eyebrow">WORKBENCH</span><h2>{t('settings.title')}</h2></div><button className="icon-button" onClick={requestClose} title={t('settings.close')}><X size={18} /></button></header>
        <div className="settings-layout">
          <nav className="settings-nav">{tabs.map((item) => <button key={item.id} className={tab === item.id ? 'is-active' : ''} onClick={() => { setTab(item.id); setEditingRole(false); setEditingPool(false); setEditingSkill(false); }}>{item.icon}<span>{t(item.label)}</span></button>)}</nav>
          <div className="settings-body">
            {error && <div className="settings-message is-error">{error}</div>}
            {notice && <div className="settings-message is-success">{notice}</div>}
            {tab === 'connection' && <div className="settings-section"><div className="section-heading"><div><h3>{t('settings.connectionTitle')}</h3><p>{t('settings.connectionDesc')}</p></div></div><div className="form-grid"><label>{t('settings.provider')}<select id="provider-select" value={selectedProviderId} onChange={(event) => chooseProvider(event.target.value)}><option value={MANUAL_PROVIDER}>{t('settings.providerManual')}</option>{providerPresets.filter((preset) => preset.available !== false).map((preset) => <option key={preset.id} value={preset.id}>{preset.display_name}</option>)}</select><small>{manualMode ? t('settings.providerManualHint') : t('settings.providerHint')}</small></label><label>{t('settings.cli')}<select id="cli-select" value={config.cli_name || ''} onChange={(event) => setGlobal('cli_name', event.target.value || null)}><option value="">{t('settings.autoDetect')}{defaultCli ? ` (${defaultCli})` : ''}</option>{clis.map((cli) => <option key={cli.id} value={cli.id}>{cli.display_name}{cli.installed ? '' : t('settings.notInstalled')}{cli.supports_mcp ? '' : t('settings.mcpNotWired')}</option>)}</select></label><label>{t('settings.model')}{modelIsFreeText ? <><input list="model-options" value={config.model || ''} onChange={(event) => setGlobal('model', event.target.value || null)} placeholder={t('settings.selectOrTypeModel')} /><datalist id="model-options">{allModelOptions.map((model) => <option key={model} value={model} />)}</datalist></> : <select id="model-select" value={config.model || ''} onChange={(event) => setGlobal('model', event.target.value || null)}>{modelIsForeign && <option value={config.model as string}>{t('settings.modelNotInList', { model: config.model as string })}</option>}{allModelOptions.map((model) => <option key={model} value={model}>{model}</option>)}</select>}</label><label>{t('settings.apiBaseUrl')}<input id="api-base-url" value={displayedBaseUrl} disabled={addressLocked} onChange={(event) => setGlobal('api_base_url', event.target.value || null)} placeholder={t('common.optional')} /><small>{baseUrlHint}</small></label>{(config.cli_name || defaultCli) === 'internal' ? <label>{t('settings.accountName')}<input value={internalUsername} onChange={(event) => setInternalUsername(event.target.value)} placeholder={t('settings.accountNamePlaceholder')} /><small>{t('settings.accountNameNote')}</small></label> : <label>{t('settings.apiKey')}<input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={t('settings.apiKeyPlaceholder')} /><small>{apiKeyVendor ? t('settings.vendor', { vendor: apiKeyVendor }) : ''}{t('settings.localPlaintextNote')}</small></label>}</div><div className="settings-actions"><button className="secondary-button" disabled={testing || busy} onClick={runConnectionTest}>{testing ? t('settings.testing') : t('settings.testConnection')}</button><button className="primary-button" disabled={busy} onClick={saveConnection}><Save size={15} /> {t('settings.saveConnection')}</button></div></div>}
            {tab === 'workspace' && <div className="settings-section"><div className="section-heading"><div><h3>{t('settings.workspaceTitle')}</h3><p>{t('settings.workspaceDesc')}</p></div></div><label>{t('settings.workspaceRoot')}<input value={config.workspace_root || ''} onChange={(event) => setGlobal('workspace_root', event.target.value || null)} placeholder={t('settings.workspaceRootPlaceholder')} /></label><div className="settings-actions"><button className="primary-button" disabled={busy} onClick={saveWorkspace}><Save size={15} /> {t('settings.saveWorkspace')}</button></div></div>}
            {tab === 'workspace' && <div className="settings-section"><div className="section-heading"><div><h3>{t('settings.executionTitle')}</h3><p>{t('settings.executionDesc')}</p></div></div><label className="settings-toggle"><input type="checkbox" checked={config.dynamic_num_ccs !== false} onChange={(event) => setGlobal('dynamic_num_ccs', event.target.checked)} /><span>{t('settings.dynamicNumCcs')}</span><HelpTip text={t('settings.dynamicNumCcsHelp')} title={t('settings.dynamicNumCcs')} /></label>{config.dynamic_num_ccs === false && <label>{t('settings.defaultNumCcs')}<input type="number" min={NUM_CCS_MIN} max={NUM_CCS_MAX} step={1} value={config.default_num_ccs ?? NUM_CCS_MIN} disabled={busy} onChange={(event) => setGlobal('default_num_ccs', clampNumCcs(event.target.value))} /><small>{t('settings.defaultNumCcsHelp', { min: NUM_CCS_MIN, max: NUM_CCS_MAX })}</small></label>}<div className="settings-actions"><button className="primary-button" disabled={busy} onClick={saveExecution}><Save size={15} /> {t('settings.saveExecution')}</button></div></div>}
            {tab === 'roles' && <div className="settings-section roles-editor"><div className="section-heading"><div><h3>{t('settings.rolesTitle')}</h3><p>{t('settings.rolesDesc', { count: roleFields.length })}</p></div><div className="section-heading-actions">{!editingRole && <button className="secondary-button" disabled={!selectedRole || busy} onClick={() => setEditingRole(true)}><Pencil size={15} /> {t('common.edit')}</button>}<button className="secondary-button" onClick={startCreateRole}><Plus size={15} /> {t('settings.newRole')}</button></div></div><div className="role-editor-layout"><aside className="resource-list"><ul className="resource-list-items">{roles.map((item) => { const value = `${item.type}:${item.id}`; return <li key={value}><button type="button" className={selectedRole === value ? 'is-active' : ''} disabled={busy} onClick={() => requestSelectRole(value)}><span className="resource-list-type">{roleTypeLabel(item.type)}</span><span className="resource-list-name">{item.display_name}</span></button></li>; })}</ul><div className="resource-list-note">{t('settings.customRoles', { count: roles.length })}</div></aside><div className="role-form">
              {roleMode === 'idle' ? <p className="detail-empty">{t('settings.selectOrCreateRole')}</p> : <>
                <div className="form-grid"><label>{t('settings.roleId')}<input value={roleId} onChange={(event) => setRoleId(event.target.value)} placeholder="lowercase_role_id" disabled={Boolean(selectedRole)} title={selectedRole ? t('settings.identityLocked') : undefined} /></label><label>{t('settings.roleType')}<select value={roleType} onChange={(event) => void chooseRoleType(event.target.value as RoleType)} disabled={Boolean(selectedRole)} title={selectedRole ? t('settings.identityLocked') : undefined}><option value="manager">{t('dialog.manager')}</option><option value="worker">{t('dialog.worker')}</option></select></label></div>
                {Object.entries(basicSections).map(([section, fields]) => renderFieldset(section, fields))}
                {advancedFields.length > 0 && <div className="advanced-config"><button type="button" className="advanced-config-toggle" onClick={() => setAdvancedOpen((value) => !value)} aria-expanded={advancedOpen}><ChevronDown size={14} className={advancedOpen ? 'is-open' : ''} /><span>{t('settings.advancedConfig')}</span><span className="advanced-config-count">{advancedFields.length}</span></button>{advancedOpen && <div className="advanced-config-body"><p className="advanced-config-desc">{t('settings.advancedConfigDesc')}</p>{Object.entries(advancedSections).map(([section, fields]) => renderFieldset(section, fields))}</div>}</div>}
                {roleMode !== 'view' && <div className="settings-actions">{roleMode === 'edit' && <button className="danger-button" disabled={busy} onClick={deleteCurrentRole}><Trash2 size={15} /> {t('settings.deleteRole')}</button>}<button className="secondary-button" disabled={busy} onClick={() => void cancelEditRole()}>{t('common.cancel')}</button><button className="primary-button" disabled={busy || !roleId.trim()} onClick={saveRole}><Save size={15} /> {t('settings.saveRole')}</button></div>}
              </>}
            </div></div></div>}
            {tab === 'pools' && <div className="settings-section pools-editor"><div className="section-heading"><div><h3>{t('settings.poolsTitle')}</h3><p>{t('settings.poolsDesc')}</p></div><div className="section-heading-actions">{!editingPool && <button className="secondary-button" disabled={!selectedPool || busy} onClick={() => setEditingPool(true)}><Pencil size={15} /> {t('common.edit')}</button>}<button className="secondary-button" onClick={startCreatePool}><Plus size={15} /> {t('settings.newPool')}</button></div></div><div className="role-editor-layout"><aside className="resource-list"><ul className="resource-list-items">{pools.map((item) => <li key={item.id}><button type="button" className={selectedPool === item.id ? 'is-active' : ''} disabled={busy} onClick={() => requestSelectPool(item.id)}><span className="resource-list-name">{item.name}</span><span className="resource-list-type">{t('settings.poolCount', { count: item.roles.length })}</span></button></li>)}</ul><div className="resource-list-note">{pools.length ? t('settings.poolCount', { count: pools.length }) : t('settings.noPools')}</div></aside><div className="role-form">
              {poolMode === 'idle' ? <p className="detail-empty">{t('settings.selectOrCreatePool')}</p> : <>
                <label>{t('settings.poolName')}<input value={poolName} onChange={(event) => setPoolName(event.target.value)} placeholder={t('settings.poolNamePlaceholder')} maxLength={60} disabled={poolMode === 'view'} /></label>
                <div className="node-dialog-section"><div className="node-dialog-section-title">{t('settings.poolRoles')}</div><p className="picker-status">{t('settings.poolRolesHint')}</p><SearchCheckboxPicker items={roles.map((item) => ({ id: item.id, label: item.display_name || item.id, detail: roleTypeLabel(item.type), badge: roleTypeLabel(item.type), description: item.description, detailLoader: () => loadRoleHoverDetail(item.type, item.id) }))} selected={poolRoles} onToggle={togglePoolRole} query={poolQuery} onQueryChange={setPoolQuery} searchPlaceholder={t('dialog.searchRoles')} emptyLabel={t('dialog.noMatchingRoles')} selectedLabel={t('dialog.selectedCount')} disabled={poolMode === 'view'} /></div>
                {poolMode !== 'view' && <div className="settings-actions">{poolMode === 'edit' && <button className="danger-button" disabled={busy} onClick={() => void deleteCurrentPool()}><Trash2 size={15} /> {t('settings.deletePool')}</button>}<button className="secondary-button" disabled={busy} onClick={cancelEditPool}>{t('common.cancel')}</button><button className="primary-button" disabled={busy || !poolName.trim() || !poolRoles.length} onClick={() => void savePool()}><Save size={15} /> {t('settings.savePool')}</button></div>}
              </>}
            </div></div></div>}
            {tab === 'skills' && <div className="settings-section skills-editor"><div className="section-heading"><div><h3>{t('settings.skillsTitle')}</h3><p>{t('settings.skillsDesc')}</p></div><div className="section-heading-actions">{!editingSkill && <button className="secondary-button" disabled={!selectedSkill || busy} onClick={() => setEditingSkill(true)}><Pencil size={15} /> {t('common.edit')}</button>}<button className="secondary-button" onClick={startCreateSkill}><Plus size={15} /> {t('settings.newSkill')}</button></div></div><div className="skill-editor-layout"><aside className="resource-list"><ul className="resource-list-items">{skills.map((item) => <li key={item.id}><button type="button" className={selectedSkill === item.id ? 'is-active' : ''} disabled={busy} onClick={() => requestSelectSkill(item.id)}><span className="resource-list-name">{item.name || item.id}</span></button></li>)}</ul><div className="resource-list-note">{t('settings.availableSkills', { count: skills.length })}</div></aside><div className="skill-form">
              {skillMode === 'idle' ? <p className="detail-empty">{t('settings.selectOrCreateSkill')}</p> : <>
                <label>{t('settings.skillId')}<input value={skillId} onChange={(event) => setSkillId(event.target.value)} placeholder="lowercase-skill-id" disabled={Boolean(selectedSkill)} title={selectedSkill ? t('settings.identityLocked') : undefined} /></label>
                <label>{t('settings.instructions')}<textarea className="skill-content-editor" value={skillContent} onChange={(event) => setSkillContent(event.target.value)} rows={18} placeholder={t('settings.skillContentPlaceholder')} disabled={skillMode === 'view'} /></label>
                {skillDetail && <div className="skill-meta">{skillDetail.description && <p className="skill-meta-desc">{skillDetail.description}</p>}{skillDetail.license && <div className="skill-meta-row"><span>{t('settings.license')}</span><code>{skillDetail.license}</code></div>}{(skillDetail.allowed_tools?.length ?? 0) > 0 && <div className="skill-meta-row"><span>{t('settings.allowedTools')}</span><div className="skill-tags">{skillDetail.allowed_tools!.map((tool) => <code key={tool}>{tool}</code>)}</div></div>}{(skillDetail.files?.length ?? 0) > 0 && <div className="skill-meta-row"><span>{t('settings.files', { count: skillDetail.files!.length })}</span><div className="skill-files">{skillDetail.files!.map((file) => <code key={file}>{file}</code>)}</div></div>}</div>}
                {/* 导入只会新建一个 skill，放在查看/编辑态容易被当成覆盖当前 skill，故只在新建态出现。 */}
                {skillMode === 'create' && <div className="skill-import"><div className="skill-import-title"><Upload size={14} /> {t('settings.importFromZip')}<HelpTip text={t('settings.importHelp')} /></div><div className="skill-import-row"><input type="file" accept=".zip,application/zip" onChange={(event) => setZipFile(event.target.files?.[0] || null)} />{zipFile && <code className="skill-import-file">{zipFile.name}</code>}<button className="secondary-button" disabled={busy || !zipFile} onClick={importZip}>{t('settings.import')}</button></div><small>{t('settings.importNote')}</small></div>}
                {skillMode !== 'view' && <div className="settings-actions">{skillMode === 'edit' && <button className="danger-button" disabled={busy} onClick={deleteCurrentSkill}><Trash2 size={15} /> {t('settings.deleteSkill')}</button>}<button className="secondary-button" disabled={busy} onClick={() => void cancelEditSkill()}>{t('common.cancel')}</button><button className="primary-button" disabled={busy || !skillId.trim() || !skillContent.trim()} onClick={saveSkill}><Save size={15} /> {t('settings.saveSkill')}</button></div>}
              </>}
            </div></div></div>}
            {tab === 'language' && <div className="settings-section language-switcher"><div className="section-heading"><div><h3>{t('lang.switchTitle')}</h3><p>{t('lang.switchDesc')}</p></div></div><div className="language-options"><button type="button" className={lang === 'zh' ? 'is-active' : ''} onClick={() => setLang('zh')}>中文</button><button type="button" className={lang === 'en' ? 'is-active' : ''} onClick={() => setLang('en')}>English</button></div></div>}
          </div>
        </div>
      </section>
    </div>
  );
};

export default WorkbenchSettings;
