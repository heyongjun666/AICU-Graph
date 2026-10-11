import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, Boxes, Download, FolderTree, HelpCircle, Languages, Pencil, Plug, Plus, Save, Server, Sparkles, Trash2, Upload, Users, X } from 'lucide-react';
import { CliInfo, EffectiveBaseUrlResponse, GlobalConfig, McpRoleRef, McpServerInfo, McpServerInput, ProviderPreset, RoleField, RoleInfo, RolePool, RolePoolImportResult, RoleSchema, SkillInfo } from '../types';
import { useI18n } from '../i18n';
import { useUIStateStore } from '../stores/UIStateStore';
import { useSessionStore } from '../stores/SessionStore';
import SearchCheckboxPicker from './SearchCheckboxPicker';
import { RolePoolImportDialog, describeBundleWarning } from './RolePoolImportDialog';
import { CliOptions } from './CliOptions';
import {
  createMcpServer,
  createRole,
  createRolePool,
  createSkill,
  deleteMcpServer,
  deleteRole,
  deleteRolePool,
  deleteSkill,
  exportRolePool,
  getApiKey,
  getEffectiveBaseUrl,
  getGlobalConfig,
  getMcpServers,
  getRole,
  getRolePools,
  getRoleSchema,
  getRoles,
  getSkill,
  getSkills,
  importSkill,
  previewRolePoolExport,
  probeMcpServer,
  saveApiKey,
  testConnection,
  updateGlobalConfig,
  updateMcpServer,
  updateRole,
  updateRolePool,
  updateSkill,
} from '../utils/api';
import { clearRoleHoverDetailCache, loadRoleHoverDetail } from '../utils/roleHoverDetail';
import { errorMessage } from '../utils/errorMessage';

type SettingsTab = 'connection' | 'workspace' | 'roles' | 'pools' | 'skills' | 'mcp' | 'language';
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
  { id: 'mcp', label: 'settings.tab.mcp', icon: <Plug size={16} /> },
  { id: 'language', label: 'settings.tab.language', icon: <Languages size={16} /> },
];

const emptyConfig: GlobalConfig = {
  workspace_root: null,
  cli_name: null,
  model: null,
  api_base_url: null,
  api_key_env: null,
};

const MAX_CONCURRENCY_MIN = 1;
const MAX_CONCURRENCY_MAX = 16;

/** Provider 下拉里「手动配置」那一项的取值。预置 id 全是厂商名（openai / deepseek…），
 *  用双下划线包起来不会撞上。取值只有两种：某个预置 id，或它 —— 没有「不指定」那一项，
 *  因为「什么都没选」和「我要自己填」在界面上长得一样，用户却以为是两回事。 */
const MANUAL_PROVIDER = '__manual__';

type ProviderRoute = NonNullable<ProviderPreset['routes']>[string];

function providerRouteFor(preset: ProviderPreset | undefined, cliName: string | null | undefined): ProviderRoute | undefined {
  if (!preset || !cliName) return undefined;
  const route = preset.routes?.[cliName];
  if (route?.enabled === false) return undefined;
  return route;
}

function routeApiKeyEnv(route: ProviderRoute | undefined, preset: ProviderPreset | undefined): string | null {
  return route?.auth_env || route?.api_key_env || preset?.api_key_env || null;
}

const clampConcurrency = (value: unknown): number => {
  const parsed = Number.parseInt(String(value ?? ''), 10);
  if (Number.isNaN(parsed)) return MAX_CONCURRENCY_MIN;
  return Math.min(MAX_CONCURRENCY_MAX, Math.max(MAX_CONCURRENCY_MIN, parsed));
};

const ROLE_FIELD_SECTIONS: Record<string, string> = {
  summary: 'Identity', description: 'Identity', suitable_for: 'Identity', not_for: 'Identity',
  model: 'Execution', cli_name: 'Execution',
  skills_dir: 'Skills', skills: 'Skills', mcp: 'Skills',
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
    || (field.type === 'multiline'
      ? 'Instructions'
      : 'Advanced');
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) return value.join('\n');
  return String(value);
}

function normalizeStringList(value: unknown): string[] {
  if (typeof value === 'string') return value.trim() ? [value] : [];
  if (Array.isArray(value)) return value.map(String);
  return [];
}

function coerceField(field: RoleField, value: string): unknown {
  if (field.type === 'number') return value === '' ? undefined : Number(value);
  if (field.type === 'boolean') return value === 'true';
  if (field.type === 'list') return value.split(/[,\n]/).map((item) => item.trim()).filter(Boolean);
  return value;
}

/** 资源编辑区的显隐状态：idle = 什么都没选，view = 只读查看，edit = 改已有条目，create = 新建。 */
type ResourceMode = 'idle' | 'view' | 'edit' | 'create';

/**
 * MCP server 的两种传输，对应界面上那组互斥的表单字段。
 *
 * ``stdio`` = 本地子进程（命令 + 参数 + 环境变量），``http`` = 远程 Streamable HTTP
 * （地址 + 请求头）。**只是表单的界面状态**：存进注册表的是 url，有没有它就是哪种传输。
 * SSE 型的远程 server 不在其中 —— 后端只支持 Streamable HTTP（见 mcp_manager.validate_server）。
 */
type McpTransport = 'stdio' | 'http';

/** 由「是否在编辑」与「当前选中项」推导，避免两者各存一份而互相打架。 */
const deriveMode = (editing: boolean, selected: string): ResourceMode =>
  editing ? (selected ? 'edit' : 'create') : (selected ? 'view' : 'idle');

/**
 * 多行文本 → 参数数组，一行一个。
 *
 * 刻意不按空格分：arg 本身可以含空格（`--config "a b.json"`），按空格分会把它劈成两个
 * 参数**且不报错** —— 用户只能从 server 那边一句莫名其妙的报错反推回来。
 */
const parseArgLines = (text: string): string[] =>
  text.split('\n').map((line) => line.trim()).filter(Boolean);

/** 多行「KEY=VALUE」→ env 对象；有一行不是这个形状就返回 null（由调用方去报错）。 */
function parseEnvLines(text: string): Record<string, string> | null {
  const env: Record<string, string> = {};
  for (const line of text.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const separator = trimmed.indexOf('=');
    if (separator <= 0) return null;   // 没有 =，或 = 打头（那是个没有名字的变量）
    env[trimmed.slice(0, separator).trim()] = trimmed.slice(separator + 1).trim();
  }
  return env;
}

/** 表单里的 env 文本：一行一个 KEY=VALUE，与 parseEnvLines 互为逆运算。 */
const envToLines = (env: Record<string, string>): string =>
  Object.entries(env).map(([key, value]) => `${key}=${value}`).join('\n');

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

/**
 * 下拉里的一项。
 *
 * ``id`` 是存进配置的取值，``label`` 是给人看的名字 —— 两者未必相同（skill 的 id 与
 * 显示名就常常不一样）。``keywords`` 只参与搜索、不上屏：一句描述放在行里会把菜单撑变形，
 * 但它恰恰是「该选哪个」的主要依据，所以搜索要认它。
 */
type SelectOption = {
  id: string;
  label: string;
  hint?: string;
  title?: string;
  keywords?: string;
};

/**
 * 可搜索的下拉多选框：勾选/取消，支持按名字或关键字过滤。
 *
 * 做成通用件而不是照抄两份：skill 与 MCP server 的差别只有「一行显示什么、搜索认哪些字」，
 * 而这份实现里有几处不显眼但要紧的东西（外部点击关闭、`is-checked` 的样式钩子、chips
 * 的溢出处理）。抄一份出来，改了其中一处另一处就会与它悄悄分叉。
 */
const MultiSelect: React.FC<{
  options: SelectOption[];
  value: string[];
  onChange: (ids: string[]) => void;
  placeholder: string;
  searchPlaceholder: string;
  emptyText: string;
  disabled?: boolean;
}> = ({ options, value, onChange, placeholder, searchPlaceholder, emptyText, disabled = false }) => {
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

  const needle = query.toLowerCase();
  const filtered = options.filter((option) =>
    !needle
    || `${option.label} ${option.id} ${option.keywords || ''}`.toLowerCase().includes(needle),
  );
  const selected = options.filter((option) => value.includes(option.id));
  const toggle = (id: string) => onChange(
    value.includes(id) ? value.filter((item) => item !== id) : [...value, id],
  );

  return (
    <div className="skill-select" ref={boxRef}>
      <button type="button" className="skill-select-trigger" disabled={disabled} onClick={() => !disabled && setOpen((v) => !v)}>
        {selected.length > 0 ? (
          <span className="skill-select-chips">
            {selected.map((option) => <span key={option.id} className="skill-select-chip">{option.label}</span>)}
          </span>
        ) : (
          <span className="skill-select-placeholder">{placeholder}</span>
        )}
        <ChevronDown size={14} className={open ? 'is-open' : ''} />
      </button>
      {open && (
        <div className="skill-select-menu">
          <div className="skill-select-search">
            <input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder={searchPlaceholder} />
          </div>
          <div className="skill-select-list">
            {filtered.map((option) => (
              <label key={option.id} className={`skill-select-item ${value.includes(option.id) ? 'is-checked' : ''}`} title={option.title}>
                <input type="checkbox" checked={value.includes(option.id)} onChange={() => toggle(option.id)} disabled={disabled} />
                <span className="skill-select-name">{option.label}</span>
                {option.hint && <code className="skill-select-id">{option.hint}</code>}
              </label>
            ))}
            {filtered.length === 0 && <div className="skill-select-empty">{emptyText}</div>}
          </div>
        </div>
      )}
    </div>
  );
};

/** 列出所有 skill，勾选/取消。 */
const SkillMultiSelect: React.FC<{
  options: SkillInfo[];
  value: string[];
  onChange: (ids: string[]) => void;
  disabled?: boolean;
}> = ({ options, value, onChange, disabled = false }) => {
  const { t } = useI18n();
  return (
    <MultiSelect
      options={options.map((skill) => ({
        id: skill.id,
        label: skill.name || skill.id,
        // 显示名与 id 不同时才把 id 也摆出来：相同的话那是同一句话说两遍。
        hint: skill.name !== skill.id ? skill.id : undefined,
      }))}
      value={value}
      onChange={onChange}
      placeholder={t('settings.selectSkill')}
      searchPlaceholder={t('settings.searchSkill')}
      emptyText={t('settings.noMatchingSkill')}
      disabled={disabled}
    />
  );
};

/** 列出所有 MCP server，勾选/取消。 */
const McpMultiSelect: React.FC<{
  options: McpServerInfo[];
  value: string[];
  onChange: (ids: string[]) => void;
  disabled?: boolean;
}> = ({ options, value, onChange, disabled = false }) => {
  const { t } = useI18n();
  return (
    <MultiSelect
      options={options.map((server) => ({
        id: server.name,
        label: server.name,
        // 描述只做悬浮提示与搜索关键字，不上屏 —— 它可能很长，摆进行里会把菜单撑变形。
        title: server.description || undefined,
        keywords: server.description,
      }))}
      value={value}
      onChange={onChange}
      placeholder={t('settings.selectMcp')}
      searchPlaceholder={t('settings.searchMcp')}
      emptyText={t('settings.noMatchingMcp')}
      disabled={disabled}
    />
  );
};

const RoleFieldEditor: React.FC<{
  field: RoleField;
  value: unknown;
  overridden: boolean;
  onChange: (value: unknown) => void;
  onReset: () => void;
  skillOptions?: SkillInfo[];
  mcpOptions?: McpServerInfo[];
  /** 只读查看态。必须显式透传：控件的禁用条件是 `!overridden`，
   *  而 roleConfig 恰好就是被覆盖字段的集合，不单独判只读会让"查看"态里
   *  最该锁住的那些字段反而可改。 */
  readOnly?: boolean;
}> = ({ field, value, overridden, onChange, onReset, skillOptions, mcpOptions, readOnly = false }) => {
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
  const isMcpPicker = field.name === 'mcp' && Boolean(mcpOptions);
  // 两个选择器共用的取值：存的就是名字/ id 的字符串数组，回退到字段默认值。
  const currentList = normalizeStringList(value === undefined ? field.default : value);

  if (isSkillPicker || isMcpPicker) {
    // 共用同一段外壳（标签 + 说明 + 空态提示），只有控件与文案不同。
    const emptyHint = isMcpPicker ? t('settings.noMcpSelected') : t('settings.noSkillSelected');
    return (
      <div className="role-field is-overridden">
        <div className="role-field-header">
          <div className="role-field-label">
            <label>{fieldLabel}</label>
            <code>{field.name}</code>
            {fieldDescription && <HelpTip text={fieldDescription} title={fieldLabel} />}
          </div>
        </div>
        {isMcpPicker
          ? <McpMultiSelect options={mcpOptions!} value={currentList} onChange={onChange} disabled={readOnly} />
          : <SkillMultiSelect options={skillOptions!} value={currentList} onChange={onChange} disabled={readOnly} />}
        {currentList.length === 0 && <small className="field-default">{emptyHint}</small>}
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

  // 「当前生效」的那一行。取选中会话的实际工作区根 —— 它可能与全局默认不同：
  // 全局根改过之后，老会话仍用创建时定下的那个目录。
  const activeSession = useSessionStore((state) =>
    state.sessions.find((item) => item.id === state.activeSessionId));
  const effectiveWorkspace = activeSession?.workspace_root || config.workspace_root || '';
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
  const [poolImportFile, setPoolImportFile] = useState<File | null>(null);
  const poolImportInput = useRef<HTMLInputElement>(null);
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
  // MCP 注册表：列表 + 表单。args / env / headers 在表单里是**多行文本**（见
  // parseArgLines / parseEnvLines），保存和测试时才解析 —— 边敲边解析的话，用户敲一个
  // 换行就会把半截参数吞掉。
  //
  // 传输是本地的界面状态而不是表单字段：条目里存的是 url，有没有它就是哪种传输（与后端
  // mcp_manager.transport_of 同一条判据）。界面上再存一份能跟 url 打架的选择，正是本项目
  // 反复防的那类「看起来两样都在、运行时按哪个走看读的那一方」。
  const [mcpServers, setMcpServers] = useState<McpServerInfo[]>([]);
  const [editingMcp, setEditingMcp] = useState(false);
  const [selectedMcp, setSelectedMcp] = useState('');
  const [mcpName, setMcpName] = useState('');
  const [mcpTransport, setMcpTransport] = useState<McpTransport>('stdio');
  const [mcpCommand, setMcpCommand] = useState('');
  const [mcpArgs, setMcpArgs] = useState('');
  const [mcpEnv, setMcpEnv] = useState('');
  const [mcpUrl, setMcpUrl] = useState('');
  const [mcpHeaders, setMcpHeaders] = useState('');
  const [mcpDescription, setMcpDescription] = useState('');
  const [mcpRequired, setMcpRequired] = useState(true);
  const [mcpTools, setMcpTools] = useState<string[] | null>(null);
  const [mcpTesting, setMcpTesting] = useState(false);
  const mcpProbeGeneration = useRef(0);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [error, setError] = useState('');
  const [mcpError, setMcpError] = useState('');
  const [notice, setNotice] = useState('');
  // 成功提示 banner 自动消除（与 Toast 默认 3000ms 对齐），避免"已删除"等信息常驻不清。
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(''), 3000);
    return () => clearTimeout(timer);
  }, [notice]);
  const [apiKey, setApiKey] = useState('');
  const [apiKeyVendor, setApiKeyVendor] = useState<string | null>(null);
  // 后端算出的「生效地址」（非手动模式下地址框显示的就是它，随模型厂商变）。
  const [effectiveBaseUrl, setEffectiveBaseUrl] = useState<EffectiveBaseUrlResponse | null>(null);

  const load = async () => {
    setBusy(true);
    setError('');
    setMcpError('');
    setMcpServers([]);
    setMcpFormEmpty();
    void getMcpServers()
      .then((nextMcp) => {
        setMcpServers(nextMcp);
        if (nextMcp[0]) fillMcpForm(nextMcp[0]);
      })
      .catch((reason) => {
        setMcpServers([]);
        setMcpError(errorMessage(reason));
      });
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
        (preset) => preset.available !== false
          && (!preset.cli_names?.length
            ? preset.cli_name === presetCliId
            : preset.cli_names.includes(presetCliId || '')),
      );
      const initialPreset = providerPresets.find((preset) => preset.id === globalResp.config.provider_id)
        ?? usablePresets.find((preset) => preset.model === globalResp.config.model)
        ?? usablePresets.find(
          (preset) => globalResp.config.api_base_url && preset.api_base_url === globalResp.config.api_base_url,
        );
      // 反查不到任何预置就落回「手动配置」：这一态下模型是自由输入、地址交还用户，用户
      // 自己架了网关时还能把已存的模型/地址原样带走；若在这里擅自挑一个预置，界面就会
      // 宣称一套用户从没选过的参数。仓库里没有任何 config.json 迁移代码
      //（session_manager.py:388-401 对未知键静默丢弃、不做归一），兼容只能在这做。
      setSelectedProviderId(initialPreset?.id ?? MANUAL_PROVIDER);
      setConfig((current) => ({
        ...current,
        provider_id: initialPreset?.id ?? globalResp.config.provider_id ?? null,
      }));
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
      setError(errorMessage(reason));
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
    setMcpError('');
    setRoleType('worker');
    setRoleConfig({});
    setPoolFormEmpty();
    setSelectedSkill('');
    // 打开面板一律从只读态开始，且清掉上一次的编辑开关。
    setEditingRole(false);
    setEditingPool(false);
    setEditingSkill(false);
    setEditingMcp(false);
    setMcpFormEmpty();
    setAdvancedOpen(false);
    void load();
  }, [open]);

  useEffect(() => () => {
    mcpProbeGeneration.current += 1;
  }, [open]);

  // 回显当前模型厂商的 API Key。
  const loadCredential = async (
    model: string | null,
    providerId: string | null = selectedProviderId,
  ) => {
    if (!model) { setApiKey(''); setApiKeyVendor(null); return; }
    try {
      const resp = await getApiKey(model, providerId === MANUAL_PROVIDER ? null : providerId);
      setApiKey(resp.api_key || '');
      setApiKeyVendor(resp.vendor);
    } catch { setApiKey(''); setApiKeyVendor(null); }
  };

  useEffect(() => {
    if (!open) return;
    void loadCredential(config.model, selectedProviderId);
  }, [open, config.model, selectedProviderId]);

  const setGlobal = <K extends keyof GlobalConfig>(key: K, value: GlobalConfig[K]) =>
    setConfig((current) => ({ ...current, [key]: value }));

  /** Provider 下拉：选预置则自动填充 CLI/模型/API 参数；选「手动配置」则把端点交还用户。
   *  取值只有预置 id 与 MANUAL_PROVIDER 两种（与旁边的 CLI 下拉取值一致，不是 display_name）。 */
  const selectedProvider = providerPresets.find((preset) => preset.id === selectedProviderId);
  const chooseProvider = (value: string) => {
    setSelectedProviderId(value);
    if (value === MANUAL_PROVIDER) {
      // 必须清空地址：预置残留值若恰好等于某个预置地址，后端会按模型厂商把它改写掉
      //（cli_adapters.py:216-217），界面上写着「手动配置」而实际地址被换掉。
      setConfig((current) => ({ ...current, api_base_url: null, provider_id: null }));
      return;
    }
    const preset = providerPresets.find((item) => item.id === value);
    if (!preset || preset.available === false) return;  // 认不出的取值：各字段保持原样
    const allowedClis = preset.cli_names?.length ? preset.cli_names : (preset.cli_name ? [preset.cli_name] : []);
    const nextCli = allowedClis.includes(config.cli_name || '')
      ? config.cli_name
      : (preset.cli_name || allowedClis[0] || config.cli_name);
    const route = providerRouteFor(preset, nextCli);
    setConfig((current) => ({
      ...current,
      provider_id: preset.id,
      cli_name: nextCli,
      model: preset.model || preset.models?.[0] || current.model,
      api_base_url: route?.api_base_url || preset.api_base_url || null,
      api_key_env: routeApiKeyEnv(route, preset),
    }));
  };

  const chooseCli = (value: string) => {
    const route = providerRouteFor(selectedProvider, value);
    setConfig((current) => ({
      ...current,
      cli_name: value || null,
      ...(selectedProvider ? {
        api_base_url: route?.api_base_url || selectedProvider.api_base_url || null,
        api_key_env: routeApiKeyEnv(route, selectedProvider),
      } : {}),
    }));
  };

  const manualMode = selectedProviderId === MANUAL_PROVIDER;
  // 模型能不能手输 = 端点是不是用户自己的：手动配置下名单无从算起，只能让他填；
  // 其余情形（某个预置）名单是确定的，就不该让人把 gpt-5.6 敲进 claude 的下拉里。
  const modelIsFreeText = manualMode || Boolean(selectedProvider?.allow_custom_model);
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
    getEffectiveBaseUrl({
      cliName: config.cli_name,
      model: config.model,
      apiBaseUrl: config.api_base_url,
      providerId: selectedProviderId === MANUAL_PROVIDER ? null : selectedProviderId,
    })
      .then((resp) => { if (!cancelled) setEffectiveBaseUrl(resp); })
      // 问不到就退回显示已存地址（下面 displayedBaseUrl 的 `??`）：宁可少说，不可说错。
      .catch(() => { if (!cancelled) setEffectiveBaseUrl(null); });
    return () => { cancelled = true; };
  }, [open, addressLocked, config.cli_name, config.model, config.api_base_url, selectedProviderId]);

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

  // 当前 Provider 若声明了兼容 CLI，只在下拉中展示这些 CLI；旧预置仍按 cli_name 工作。
  const providerCliIds = selectedProvider?.routes
    ? Object.entries(selectedProvider.routes)
      .filter(([, route]) => route.enabled !== false)
      .map(([cliId]) => cliId)
    : selectedProvider?.cli_names?.length
      ? selectedProvider.cli_names
      : (selectedProvider?.cli_name ? [selectedProvider.cli_name] : null);
  const visibleClis = providerCliIds
    ? clis.filter((cli) => providerCliIds.includes(cli.id))
    : clis;
  // 当前 CLI 的已知模型清单：显式选择时用该 CLI 的 models；
  // Auto-detect 时用默认 CLI 的 models；都无法确定则用全部 CLI 模型的并集（去重）。
  const selectedCliId = config.cli_name || defaultCli;
  // 找不到当前 CLI（例如配置里存着已从下拉隐藏的 CLI id）时退回默认 CLI，而不是落到
  // 下面的「全部并集」—— 那会把别家 CLI 才能用的模型全带回来。
  const selectedCli = visibleClis.find((cli) => cli.id === selectedCliId)
    ?? visibleClis.find((cli) => cli.id === defaultCli)
    ?? clis.find((cli) => cli.id === selectedCliId);
  const modelOptions = selectedCli?.models?.length
    ? selectedCli.models
    : Array.from(new Set(clis.flatMap((cli) => cli.models || [])));

  // 厂商预置的模型只在「预置自己的 CLI == 当前 CLI」时才并入候选。否则选了 OpenAI
  // 预置、再把 CLI 切成 claude 时，那几个 gpt-* 仍会留在 claude 的下拉里 —— 而
  // OpenAI 没有 /v1/messages 端点，选中必然连不上。
  const providerModelOptions = selectedProvider
    && (selectedProvider.cli_names?.includes(selectedCliId || '') || selectedProvider.cli_name === selectedCliId)
    ? selectedProvider.models || []
    : [];

  // 模型可输入（combobox）：把厂商预置模型与 CLI 已知模型合并为建议项，仍允许手输任意模型名。
  const allModelOptions = Array.from(new Set([...providerModelOptions, ...modelOptions]));

  // 严格下拉时，已保存的 model 可能落在候选之外（换过 CLI、或目录里删过该 id）。不把它
  // 作为一个 option 注入的话，<select value> 匹配不到任何项、浏览器会显示成空白 —— 用户
  // 看到的和他实际保存的不是一回事，他动一下别的字段再保存，这个值就被悄悄换掉了。
  // 不自动清空它：清掉用户已存的值比留着更烦人。
  const modelIsForeign = Boolean(config.model) && !allModelOptions.includes(config.model as string);
  const refreshAfterSave = async (isCurrent = () => true) => {
    try {
      await onSaved();
    } catch {
      if (isCurrent()) addToast({ type: 'warning', message: t('settings.sessionRefreshFailed') });
    }
  };
  const saveConnection = async () => {
    setBusy(true); setError(''); setNotice('');
    try {
      if (config.model) {
        await saveApiKey(
          config.model,
          apiKey,
          selectedProviderId === MANUAL_PROVIDER ? null : selectedProviderId,
        );
      }
      await updateGlobalConfig({
        cli_name: config.cli_name,
        model: config.model,
        api_base_url: config.api_base_url,
        api_key_env: config.api_key_env,
        provider_id: selectedProviderId === MANUAL_PROVIDER ? null : selectedProviderId,
      });
      const message = t('settings.connectionSaved');
      setNotice(message); addToast({ type: 'success', message });
      await refreshAfterSave();
    } catch (reason) {
      const message = errorMessage(reason);
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
        provider_id: selectedProviderId === MANUAL_PROVIDER ? null : selectedProviderId,
      });
      if (resp.ok) {
        const message = resp.result ? t('settings.connectionSuccessResult', { result: resp.result.slice(0, 200) }) : (resp.message || t('settings.connectionSuccess'));
        setNotice(message); addToast({ type: 'success', message });
      } else {
        const message = resp.message || t('settings.connectionFailed');
        setError(message); addToast({ type: 'error', message });
      }
    } catch (reason) {
      const message = errorMessage(reason);
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
      const message = errorMessage(reason);
      setError(message); addToast({ type: 'error', message });
    }
    finally { setBusy(false); }
  };

  const saveExecution = async () => {
    setBusy(true); setError(''); setNotice('');
    try {
      await updateGlobalConfig({
        max_concurrency: clampConcurrency(config.max_concurrency),
      });
      const message = t('settings.executionSaved');
      setNotice(message); addToast({ type: 'success', message });
      await refreshAfterSave();
    } catch (reason) {
      const message = errorMessage(reason);
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
      setError(errorMessage(reason));
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
      const message = errorMessage(reason);
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
      setError(errorMessage(reason));
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
    } catch (reason) { setError(errorMessage(reason)); }
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
      const message = errorMessage(reason);
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
    } catch (reason) { setError(errorMessage(reason)); }
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

  const formatRoleReferences = (refs: McpRoleRef[]) => {
    const wrap = lang === 'en' ? (value: string) => `(${value})` : (value: string) => `（${value}）`;
    return refs
      .map((item) => `${item.id}${wrap(roleTypeLabel(item.type))}`)
      .join(lang === 'en' ? '; ' : '、');
  };

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
      reportPoolError(errorMessage(reason));
    } finally { setBusy(false); }
  };

  const exportCurrentPool = async () => {
    const target = pools.find((item) => item.id === selectedPool);
    if (!target) return;
    setBusy(true); setError(''); setNotice('');
    try {
      // 导出前先预览：MCP 的 env / headers 是原样导出的，用户必须在下载前知道哪些服务带着凭据。
      const preview = await previewRolePoolExport(target.id);
      const details: string[] = [];
      if (preview.sensitive_mcp.length) {
        details.push(`${t('poolBundle.exportCredentials')}\n${preview.sensitive_mcp
          .map((item) => `  - ${item.name}: ${item.keys.join(', ')}`).join('\n')}`);
      }
      if (preview.personal_skills.length) {
        details.push(`${t('poolBundle.exportPersonal')}\n${preview.personal_skills.map((id) => `  - ${id}`).join('\n')}`);
      }
      if (preview.warnings.length) {
        details.push(`${t('poolBundle.exportWarnings')}\n${preview.warnings
          .map((warning) => `  - ${describeBundleWarning(t, warning)}`).join('\n')}`);
      }
      if (details.length && !window.confirm(t('poolBundle.exportConfirm', { name: target.name, details: details.join('\n\n') }))) return;
      await exportRolePool(target.id, target.name);
      const message = t('poolBundle.exportDone', { name: target.name });
      setNotice(message); addToast({ type: 'success', message });
    } catch (reason) {
      reportPoolError(errorMessage(reason));
    } finally { setBusy(false); }
  };

  const onPoolImported = async (done: RolePoolImportResult) => {
    // 导入同时动了角色 / skill / MCP / 池四处，一并重新拉；悬浮详情缓存里是旧的角色内容。
    clearRoleHoverDetailCache();
    try {
      const [nextRoles, nextSkills, nextPools, nextMcp] = await Promise.all([
        getRoles(), getSkills(), getRolePools(), getMcpServers(),
      ]);
      setRoles(nextRoles); setSkills(nextSkills); setPools(nextPools); setMcpServers(nextMcp);
      const imported = nextPools.find((item) => item.name === done.pool.name);
      if (imported && !editingPool) fillPoolForm(imported);
      const counts = done.counts;
      const message = t('poolBundle.resultNotice', {
        created: counts?.created ?? 0,
        overwritten: counts?.overwritten ?? 0,
        skipped: counts?.skipped ?? 0,
        unchanged: counts?.unchanged ?? 0,
      });
      setNotice(message); addToast({ type: 'success', message });
    } catch (reason) {
      setError(errorMessage(reason));
    }
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
      reportPoolError(errorMessage(reason));
    } finally { setBusy(false); }
  };

  const deleteCurrentSkill = async () => {
    const targetId = selectedSkill || skillId;
    if (!targetId) return;
    const target = skills.find((item) => item.id === targetId);
    const name = target?.name || targetId;
    setBusy(true); setError(''); setNotice('');
    try {
      const detail = await getSkill(targetId);
      const refs = detail.referencing_roles || [];
      const roles = formatRoleReferences(refs);
      const warning = refs.length
        ? `\n${t('settings.skillReferencedWarning', { count: refs.length, roles })}`
        : `\n${t('settings.skillReferenceScope')}`;
      if (!window.confirm(`${t('settings.deleteSkillConfirm', { name })}${warning}`)) return;
      await deleteSkill(targetId);
      const nextSkills = await getSkills();
      setSkills(nextSkills);
      setNotice(t('settings.skillDeleted'));
      addToast({ type: 'success', message: t('settings.skillDeleted') });
      const next = nextSkills[0];
      setEditingSkill(false); setZipFile(null);
      if (next) await chooseSkill(next.id);
      else { setSelectedSkill(''); setSkillId(''); setSkillContent('# Skill\n\n'); setSkillDetail(null); }
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  };

  const reportMcpError = (message: string) => {
    setError(message); addToast({ type: 'error', message });
  };

  const setMcpFormEmpty = () => {
    setSelectedMcp(''); setMcpName(''); setMcpTransport('stdio');
    setMcpCommand(''); setMcpArgs(''); setMcpEnv(''); setMcpUrl(''); setMcpHeaders('');
    setMcpDescription(''); setMcpRequired(true); setMcpTools(null);
  };

  const fillMcpForm = (server: McpServerInfo) => {
    // 有没有 url 就是传输本身，不另读一个字段（见 mcpTransport state 的说明）。
    setMcpTransport(server.url ? 'http' : 'stdio');
    setSelectedMcp(server.name); setMcpName(server.name); setMcpCommand(server.command || '');
    setMcpArgs((server.args || []).join('\n')); setMcpEnv(envToLines(server.env || {}));
    setMcpUrl(server.url || ''); setMcpHeaders(envToLines(server.headers || {}));
    setMcpDescription(server.description); setMcpRequired(server.required); setMcpTools(null);
  };

  /** 入参为空串表示切到「新建」态。 */
  const invalidateMcpProbe = () => {
    mcpProbeGeneration.current += 1;
  };

  const chooseMcp = (name: string) => {
    invalidateMcpProbe();
    setError(''); setNotice('');
    const server = name ? mcpServers.find((item) => item.name === name) : undefined;
    if (server) fillMcpForm(server); else setMcpFormEmpty();
  };

  /**
   * 屏幕上这套值 → 请求体；本地能判出来的问题在这里拦下，其余交给后端。
   *
   * 刻意不在这里复刻名字的正则：那条规则的成品是「会进 CLI 配置键的标识符」，它归
   * mcp_manager.validate_server 所有，后端也已经把违规的名字与原因拼成一句可直接展示的话。
   * 前端再写一份，两边就会各自漂移。header 名与换行的规则同理，也不在这里复刻。
   *
   * 但 env / headers 的**行格式**是界面自己的语法（一行一个 NAME=VALUE），后端看到的只是
   * 解析结果 —— 一行漏了 `=` 会被静默丢掉，用户以为存进去了。所以这一条必须在这里拦。
   *
   * 两种传输各自只提交自己那组字段：远程条目带上 command/args/env 会被后端**报错**（不是
   * 忽略），而界面上那些框已经藏起来了，用户看不到自己在提交什么。
   */
  const mcpPayload = (): McpServerInput | null => {
    const name = mcpName.trim();
    if (!name) { reportMcpError(t('settings.mcpNameRequired')); return null; }
    const common = { name, description: mcpDescription.trim(), required: mcpRequired };
    if (mcpTransport === 'http') {
      const url = mcpUrl.trim();
      if (!url) { reportMcpError(t('settings.mcpUrlRequired')); return null; }
      // 只判协议前缀，不判 URL 合不合法：这条判据的成品是「后端 validate_server 会收的
      // 那两种 scheme」，两边不一样的后果是用户敲完保存才在另一个进程里看到报错。
      if (!/^https?:\/\//i.test(url)) { reportMcpError(t('settings.mcpUrlInvalid')); return null; }
      const headers = parseEnvLines(mcpHeaders);
      if (!headers) { reportMcpError(t('settings.mcpHeadersInvalid')); return null; }
      return { ...common, url, headers };
    }
    const command = mcpCommand.trim();
    if (!command) { reportMcpError(t('settings.mcpCommandRequired')); return null; }
    const env = parseEnvLines(mcpEnv);
    if (!env) { reportMcpError(t('settings.mcpEnvInvalid')); return null; }
    return { ...common, command, args: parseArgLines(mcpArgs), env };
  };

  /** 传输切换：清掉另一组字段，免得「切过去又切回来」时旧值悄悄留着被一起提交。 */
  const switchMcpTransport = (next: McpTransport) => {
    if (next === mcpTransport) return;
    invalidateMcpProbe();
    if (next === 'http') { setMcpCommand(''); setMcpArgs(''); setMcpEnv(''); }
    else { setMcpUrl(''); setMcpHeaders(''); }
    setMcpTools(null);
    setMcpTransport(next);
  };

  /**
   * 「测试」「保存」两个按钮的禁用条件：名字 + **当前传输那一组**的必填项。
   *
   * 按传输取对应的那个输入，而不是恒看 command —— 远程表单里 command 恒为空，
   * 恒看它会让两个按钮在填好 URL 之后依然点不动。这里只判「空没空」，格式对不对
   * 交给 mcpPayload（点下去会给出具体原因，比一个灰按钮好懂）。
   */
  const mcpFormIncomplete = !mcpName.trim() || (mcpTransport === 'http' ? !mcpUrl.trim() : !mcpCommand.trim());

  const saveMcp = async () => {
    const payload = mcpPayload();
    if (!payload) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const saved = selectedMcp
        ? await updateMcpServer(selectedMcp, payload)
        : await createMcpServer(payload);
      // 重新拉列表而不是把响应塞回去：referencing_roles 只有列表接口才算得出来。
      const next = await getMcpServers();
      setMcpServers(next);
      fillMcpForm(next.find((item) => item.name === saved.name) || saved);
      setEditingMcp(false);
      const message = t('settings.mcpSaved');
      setNotice(message); addToast({ type: 'success', message });
    } catch (reason) {
      reportMcpError(errorMessage(reason));
    } finally { setBusy(false); }
  };

  const deleteCurrentMcp = async () => {
    if (!selectedMcp) return;
    const target = mcpServers.find((item) => item.name === selectedMcp);
    // 引用它的角色要一个个列出来：删掉之后那些角色下次启动会带着「可用 server 列表」当场
    // 报错，这一行字是用户唯一的判断依据。
    const refs = target?.referencing_roles || [];
    const warning = refs.length
      ? `\n${t('settings.mcpReferencedWarning', {
          count: refs.length,
          roles: formatRoleReferences(refs),
        })}`
      : '';
    if (!window.confirm(`${t('settings.mcpDeleteConfirm', { name: target?.name || mcpName })}${warning}`)) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await deleteMcpServer(selectedMcp);
      const next = await getMcpServers();
      setMcpServers(next);
      setEditingMcp(false);
      if (next[0]) fillMcpForm(next[0]); else setMcpFormEmpty();
      setNotice(t('settings.mcpDeleted'));
      addToast({ type: 'success', message: t('settings.mcpDeleted') });
    } catch (reason) {
      reportMcpError(errorMessage(reason));
    } finally { setBusy(false); }
  };

  /** 测试连接：连的是屏幕上这套**还没保存**的值，所以走 payload 而不是 selectedMcp。 */
  const runMcpProbe = async () => {
    const payload = mcpPayload();
    if (!payload) return;
    const generation = mcpProbeGeneration.current + 1;
    mcpProbeGeneration.current = generation;
    setMcpTesting(true); setError(''); setNotice(''); setMcpTools(null);
    try {
      const tools = await probeMcpServer(payload);
      if (mcpProbeGeneration.current !== generation) return;
      setMcpTools(tools);
      setNotice(tools.length ? t('settings.mcpTestOk') : t('settings.mcpTestOkNoTools'));
    } catch (reason) {
      if (mcpProbeGeneration.current !== generation) return;
      // 失败原句来自后端（含命令名、超时、stderr 尾部），前端不重写。
      const message = errorMessage(reason);
      setError(message); addToast({ type: 'error', message });
    } finally {
      if (mcpProbeGeneration.current === generation) setMcpTesting(false);
    }
  };

  const cancelEditMcp = () => {
    invalidateMcpProbe();
    setEditingMcp(false); setError(''); setNotice(''); setMcpTools(null);
    const server = mcpServers.find((item) => item.name === selectedMcp);
    if (server) fillMcpForm(server); else setMcpFormEmpty();
  };

  const currentMcpName = () => mcpServers.find((item) => item.name === selectedMcp)?.name || mcpName;

  const requestSelectMcp = (name: string) => {
    if (!confirmSwitchAway(editingMcp, name, selectedMcp, currentMcpName())) return;
    setEditingMcp(false);
    chooseMcp(name);
  };

  const startCreateMcp = () => {
    if (!confirmSwitchAway(editingMcp, '', selectedMcp, currentMcpName())) return;
    setEditingMcp(true);
    chooseMcp('');
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
   * 四个 tab 同一时刻只有一个在编辑，所以按 tab 取当前条目的名字，提示里能指名道姓。
   */
  const requestClose = () => {
    invalidateMcpProbe();
    if (!editingRole && !editingPool && !editingSkill && !editingMcp) { onClose(); return; }
    const name = editingRole ? currentRoleName()
      : editingPool ? currentPoolName()
        : editingSkill ? currentSkillName()
          : currentMcpName();
    if (!window.confirm(t('settings.discardEditsConfirm', { name: name || t('settings.unsavedEdits') }))) return;
    onClose();
  };

  if (!open) return null;
  const roleMode = deriveMode(editingRole, selectedRole);
  const poolMode = deriveMode(editingPool, selectedPool);
  const skillMode = deriveMode(editingSkill, selectedSkill);
  const mcpMode = deriveMode(editingMcp, selectedMcp);
  // 用不了 MCP 的 CLI。原因短语直接取后端下发的 mcp_unwired_reason，前端不重写成因 ——
  // 成因会变（pi 现在是「装个扩展就行」，agy / grok 是「AICU 还没接」），前端各写一份的话
  // 后端改了口径这边不会跟着变，用户读到的就是一句与实际不符的话。只列已安装的：没装的
  // CLI 在下拉里选不到，写进来只是噪音。
  const unwiredClis = clis.filter((cli) => cli.installed && !cli.supports_mcp);
  const mcpCliHint = unwiredClis.length
    ? t('settings.mcpCliHint', {
        clis: unwiredClis
          .map((cli) => (cli.mcp_unwired_reason ? `${cli.display_name} — ${cli.mcp_unwired_reason}` : cli.display_name))
          .join(lang === 'en' ? '; ' : '；'),
      })
    : t('settings.mcpCliAllWired');
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
          mcpOptions={field.name === 'mcp' ? mcpServers : undefined}
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
          <nav className="settings-nav">{tabs.map((item) => <button key={item.id} className={tab === item.id ? 'is-active' : ''} onClick={() => { if (item.id !== 'mcp') invalidateMcpProbe(); setTab(item.id); setEditingRole(false); setEditingPool(false); setEditingSkill(false); setEditingMcp(false); }}>{item.icon}<span>{t(item.label)}</span></button>)}</nav>
          <div className="settings-body">
            {error && <div className="settings-message is-error">{error}</div>}
            {notice && <div className="settings-message is-success">{notice}</div>}
            {tab === 'mcp' && mcpError && <div className="settings-message is-error">{mcpError}</div>}
            {tab === 'connection' && <div className="settings-section"><div className="section-heading"><div><h3>{t('settings.connectionTitle')}</h3><p>{t('settings.connectionDesc')}</p></div></div><div className="form-grid"><label>{t('settings.provider')}<select id="provider-select" value={selectedProviderId} onChange={(event) => chooseProvider(event.target.value)}><option value={MANUAL_PROVIDER}>{t('settings.providerManual')}</option>{providerPresets.filter((preset) => preset.available !== false).map((preset) => <option key={preset.id} value={preset.id}>{preset.display_name}</option>)}</select><small>{manualMode ? t('settings.providerManualHint') : t('settings.providerHint')}</small></label><label>{t('settings.cli')}<select id="cli-select" value={config.cli_name || ''} onChange={(event) => chooseCli(event.target.value)}><CliOptions clis={visibleClis} defaultCli={defaultCli} /></select></label><label>{t('settings.model')}{modelIsFreeText ? <><input list="model-options" value={config.model || ''} onChange={(event) => setGlobal('model', event.target.value || null)} placeholder={t('settings.selectOrTypeModel')} /><datalist id="model-options">{allModelOptions.map((model) => <option key={model} value={model} />)}</datalist></> : <select id="model-select" value={config.model || ''} onChange={(event) => setGlobal('model', event.target.value || null)}>{modelIsForeign && <option value={config.model as string}>{t('settings.modelNotInList', { model: config.model as string })}</option>}{allModelOptions.map((model) => <option key={model} value={model}>{model}</option>)}</select>}</label><label>{t('settings.apiBaseUrl')}<input id="api-base-url" value={displayedBaseUrl} disabled={addressLocked} onChange={(event) => setGlobal('api_base_url', event.target.value || null)} placeholder={t('common.optional')} /><small>{baseUrlHint}</small></label><label>{t('settings.apiKey')}<input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={t('settings.apiKeyPlaceholder')} /><small>{apiKeyVendor ? t('settings.vendor', { vendor: apiKeyVendor }) : ''}{t('settings.localPlaintextNote')}</small></label></div><div className="settings-actions"><button className="secondary-button" disabled={testing || busy} onClick={runConnectionTest}>{testing ? t('settings.testing') : t('settings.testConnection')}</button><button className="primary-button" disabled={busy} onClick={saveConnection}><Save size={15} /> {t('settings.saveConnection')}</button></div></div>}
            {tab === 'workspace' && (
              <div className="settings-section">
                <div className="section-heading">
                  <div>
                    <h3>{t('settings.workspaceTitle')}</h3>
                    <p>{t('settings.workspaceDesc')}</p>
                  </div>
                </div>
                <label>
                  {t('settings.workspaceRoot')}
                  <input
                    value={config.workspace_root || ''}
                    onChange={(event) => setGlobal('workspace_root', event.target.value || null)}
                    placeholder={t('settings.workspaceRootPlaceholder')}
                  />
                </label>
                {/*
                  生效值只**只读展示**，绝不填进上面那个输入框。
                  输入框是「新建会话的全局根」，留空 = 不覆盖。把当前会话的路径填进去，
                  用户顺手一保存就把它变成了全局根，此后所有新会话共用一个目录（共享区
                  互相覆盖）—— 一次「查看」悄悄变成了破坏性配置。
                  何况未设置时每个会话各用各的 {root_dir}/<会话id>/workspace，
                  根本不存在「一个」当前目录可以填。
                  与此处同构的先例见 SessionWorkspaceDialog 的「当前：{path}」。
                */}
                {effectiveWorkspace && (
                  <small>{t('settings.workspaceEffective', { path: effectiveWorkspace })}</small>
                )}
                <div className="settings-actions">
                  <button className="primary-button" disabled={busy} onClick={saveWorkspace}>
                    <Save size={15} /> {t('settings.saveWorkspace')}
                  </button>
                </div>
              </div>
            )}
            {tab === 'workspace' && <div className="settings-section">
              <div className="section-heading"><div><h3>{t('settings.executionTitle')}</h3><p>{t('settings.executionDesc')}</p></div></div>
              <label>
                {t('settings.maxConcurrency')}
                <input
                  aria-label={t('settings.maxConcurrency')}
                  aria-describedby="max-concurrency-help"
                  type="number" min={MAX_CONCURRENCY_MIN} max={MAX_CONCURRENCY_MAX} step={1}
                  value={config.max_concurrency ?? MAX_CONCURRENCY_MIN}
                  disabled={busy}
                  onChange={(event) => setGlobal('max_concurrency', clampConcurrency(event.target.value))}
                />
                <small id="max-concurrency-help">{t('settings.maxConcurrencyHelp')}</small>
              </label>
              <div className="settings-actions"><button className="primary-button" disabled={busy} onClick={saveExecution}><Save size={15} /> {t('settings.saveExecution')}</button></div>
            </div>}
            {tab === 'roles' && <div className="settings-section roles-editor"><div className="section-heading"><div><h3>{t('settings.rolesTitle')}</h3><p>{t('settings.rolesDesc', { count: roleFields.length })}</p></div><div className="section-heading-actions">{!editingRole && <button className="secondary-button" disabled={!selectedRole || busy} onClick={() => setEditingRole(true)}><Pencil size={15} /> {t('common.edit')}</button>}<button className="secondary-button" onClick={startCreateRole}><Plus size={15} /> {t('settings.newRole')}</button></div></div><div className="role-editor-layout"><aside className="resource-list"><ul className="resource-list-items">{roles.map((item) => { const value = `${item.type}:${item.id}`; return <li key={value}><button type="button" className={selectedRole === value ? 'is-active' : ''} disabled={busy} onClick={() => requestSelectRole(value)}><span className="resource-list-type">{roleTypeLabel(item.type)}</span><span className="resource-list-name">{item.display_name}</span></button></li>; })}</ul><div className="resource-list-note">{t('settings.customRoles', { count: roles.length })}</div></aside><div className="role-form">
              {roleMode === 'idle' ? <p className="detail-empty">{t('settings.selectOrCreateRole')}</p> : <>
                <div className="form-grid"><label>{t('settings.roleId')}<input value={roleId} onChange={(event) => setRoleId(event.target.value)} placeholder="lowercase_role_id" disabled={Boolean(selectedRole)} title={selectedRole ? t('settings.identityLocked') : undefined} /></label><label>{t('settings.roleType')}<select value={roleType} onChange={(event) => void chooseRoleType(event.target.value as RoleType)} disabled={Boolean(selectedRole)} title={selectedRole ? t('settings.identityLocked') : undefined}><option value="manager">{t('dialog.manager')}</option><option value="worker">{t('dialog.worker')}</option></select></label></div>
                {Object.entries(basicSections).map(([section, fields]) => renderFieldset(section, fields))}
                {advancedFields.length > 0 && <div className="advanced-config"><button type="button" className="advanced-config-toggle" onClick={() => setAdvancedOpen((value) => !value)} aria-expanded={advancedOpen}><ChevronDown size={14} className={advancedOpen ? 'is-open' : ''} /><span>{t('settings.advancedConfig')}</span><span className="advanced-config-count">{advancedFields.length}</span></button>{advancedOpen && <div className="advanced-config-body"><p className="advanced-config-desc">{t('settings.advancedConfigDesc')}</p>{Object.entries(advancedSections).map(([section, fields]) => renderFieldset(section, fields))}</div>}</div>}
                {roleMode !== 'view' && <div className="settings-actions">{roleMode === 'edit' && <button className="danger-button" disabled={busy} onClick={deleteCurrentRole}><Trash2 size={15} /> {t('settings.deleteRole')}</button>}<button className="secondary-button" disabled={busy} onClick={() => void cancelEditRole()}>{t('common.cancel')}</button><button className="primary-button" disabled={busy || !roleId.trim()} onClick={saveRole}><Save size={15} /> {t('settings.saveRole')}</button></div>}
              </>}
            </div></div></div>}
            {tab === 'pools' && <div className="settings-section pools-editor"><div className="section-heading"><div><h3>{t('settings.poolsTitle')}</h3><p>{t('settings.poolsDesc')}</p></div><div className="section-heading-actions">{!editingPool && <button className="secondary-button" disabled={!selectedPool || busy} onClick={() => setEditingPool(true)}><Pencil size={15} /> {t('common.edit')}</button>}{!editingPool && <button className="secondary-button" disabled={!selectedPool || busy} onClick={() => void exportCurrentPool()}><Download size={15} /> {t('poolBundle.export')}</button>}<button className="secondary-button" disabled={busy} onClick={() => poolImportInput.current?.click()}><Upload size={15} /> {t('poolBundle.import')}</button><input ref={poolImportInput} type="file" accept=".zip,application/zip" hidden data-testid="pool-import-input" onChange={(event) => { setPoolImportFile(event.target.files?.[0] || null); event.target.value = ''; }} /><button className="secondary-button" onClick={startCreatePool}><Plus size={15} /> {t('settings.newPool')}</button></div></div><div className="role-editor-layout"><aside className="resource-list"><ul className="resource-list-items">{pools.map((item) => <li key={item.id}><button type="button" className={selectedPool === item.id ? 'is-active' : ''} disabled={busy} onClick={() => requestSelectPool(item.id)}><span className="resource-list-name">{item.name}</span><span className="resource-list-type">{t('settings.poolCount', { count: item.roles.length })}</span></button></li>)}</ul><div className="resource-list-note">{pools.length ? t('settings.poolCount', { count: pools.length }) : t('settings.noPools')}</div></aside><div className="role-form">
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
            {tab === 'mcp' && <div className="settings-section mcp-editor"><div className="section-heading"><div><h3>{t('settings.mcpTitle')}</h3><p>{t('settings.mcpDesc')}</p></div><div className="section-heading-actions">{!editingMcp && <button className="secondary-button" disabled={!selectedMcp || busy} onClick={() => setEditingMcp(true)}><Pencil size={15} /> {t('common.edit')}</button>}<button className="secondary-button" onClick={startCreateMcp}><Plus size={15} /> {t('settings.newMcp')}</button></div></div><p className="mcp-cli-hint">{mcpCliHint}</p><div className="role-editor-layout"><aside className="resource-list"><ul className="resource-list-items">{mcpServers.map((item) => <li key={item.name}><button type="button" className={selectedMcp === item.name ? 'is-active' : ''} disabled={busy} onClick={() => requestSelectMcp(item.name)}><span className="resource-list-name">{item.name}</span>{(item.referencing_roles?.length ?? 0) > 0 && <span className="resource-list-type">{t('settings.mcpUsedBy', { count: item.referencing_roles!.length })}</span>}</button></li>)}</ul><div className="resource-list-note">{mcpServers.length ? t('settings.mcpCount', { count: mcpServers.length }) : t('settings.noMcp')}</div></aside><div className="mcp-form">
              {mcpMode === 'idle' ? <p className="detail-empty">{t('settings.selectOrCreateMcp')}</p> : <>
                <div className="form-grid"><label>{t('settings.mcpName')}<input value={mcpName} onChange={(event) => setMcpName(event.target.value)} placeholder={t('settings.mcpNamePlaceholder')} disabled={Boolean(selectedMcp)} title={selectedMcp ? t('settings.mcpIdentityLocked') : undefined} /><small>{t('settings.mcpNameHelp')}</small></label><label>{t('settings.mcpTransport')}<select value={mcpTransport} onChange={(event) => switchMcpTransport(event.target.value as McpTransport)} disabled={mcpMode === 'view'}><option value="stdio">{t('settings.mcpTransportStdio')}</option><option value="http">{t('settings.mcpTransportHttp')}</option></select><small>{t('settings.mcpTransportHelp')}</small></label></div>
                {mcpTransport === 'http' ? <>
                  <label>{t('settings.mcpUrl')}<input value={mcpUrl} onChange={(event) => setMcpUrl(event.target.value)} placeholder={t('settings.mcpUrlPlaceholder')} disabled={mcpMode === 'view'} /><small>{t('settings.mcpUrlHint')}</small></label>
                  <label>{t('settings.mcpHeaders')}<textarea rows={3} value={mcpHeaders} onChange={(event) => setMcpHeaders(event.target.value)} placeholder={t('settings.mcpHeadersPlaceholder')} disabled={mcpMode === 'view'} /><small>{t('settings.mcpHeadersHint')}</small></label>
                </> : <>
                  <label>{t('settings.mcpCommand')}<input value={mcpCommand} onChange={(event) => setMcpCommand(event.target.value)} placeholder={t('settings.mcpCommandPlaceholder')} disabled={mcpMode === 'view'} /><small>{t('settings.mcpCommandHint')}</small></label>
                  <label>{t('settings.mcpArgs')}<textarea rows={3} value={mcpArgs} onChange={(event) => setMcpArgs(event.target.value)} placeholder={t('settings.mcpArgsPlaceholder')} disabled={mcpMode === 'view'} /><small>{t('settings.mcpArgsHint')}</small></label>
                  <label>{t('settings.mcpEnv')}<textarea rows={3} value={mcpEnv} onChange={(event) => setMcpEnv(event.target.value)} placeholder={t('settings.mcpEnvPlaceholder')} disabled={mcpMode === 'view'} /><small>{t('settings.mcpEnvHint')}</small></label>
                </>}
                {/* 「照这样填」放在字段**下面**：会填的人不该每次都被一段示例挡在表单前面。
                    只在能编辑时出现 —— 查看态里字段本身就摆着真实值，再放一份示例是噪音。 */}
                {mcpMode !== 'view' && <div className="mcp-example">
                  <span className="mcp-example-title">{t('settings.mcpExampleTitle')}</span>
                  {mcpTransport === 'http' ? <>
                    <p className="mcp-example-row"><span>{t('settings.mcpUrl')}</span><code>{t('settings.mcpExampleUrlHttp')}</code></p>
                    <p className="mcp-example-row"><span>{t('settings.mcpHeaders')}</span><code>{t('settings.mcpExampleHeadersHttp')}</code></p>
                    <p className="mcp-example-note">{t('settings.mcpExampleNoteHttp')}</p>
                  </> : <>
                    <p className="mcp-example-row"><span>{t('settings.mcpCommand')}</span><code>npx</code></p>
                    <p className="mcp-example-row"><span>{t('settings.mcpArgs')}</span><code>{t('settings.mcpExampleArgsStdio')}</code></p>
                    <p className="mcp-example-note">{t('settings.mcpExampleNoteStdio')}</p>
                  </>}
                </div>}
                <label>{t('settings.mcpDescription')}<input value={mcpDescription} onChange={(event) => setMcpDescription(event.target.value)} placeholder={t('settings.mcpDescriptionPlaceholder')} disabled={mcpMode === 'view'} /></label>
                <label className="settings-toggle"><input type="checkbox" checked={mcpRequired} disabled={mcpMode === 'view'} onChange={(event) => setMcpRequired(event.target.checked)} /><span>{t('settings.mcpRequired')}</span><HelpTip text={t('settings.mcpRequiredHelp')} title={t('settings.mcpRequired')} /></label>
                {mcpTools && mcpTools.length > 0 && <div className="mcp-tools"><span className="mcp-tools-label">{t('settings.mcpTools', { count: mcpTools.length })}</span><div className="skill-tags">{mcpTools.map((tool) => <code key={tool}>{tool}</code>)}</div></div>}
                <p className="mcp-test-note">{mcpTransport === 'http' ? t('settings.mcpTestNoteHttp') : t('settings.mcpTestNote')}</p>
                {mcpMode !== 'view' && <div className="settings-actions">{mcpMode === 'edit' && <button className="danger-button" disabled={busy} onClick={() => void deleteCurrentMcp()}><Trash2 size={15} /> {t('settings.mcpDelete')}</button>}<button className="secondary-button" disabled={busy} onClick={cancelEditMcp}>{t('common.cancel')}</button><button className="secondary-button" disabled={busy || mcpTesting || mcpFormIncomplete} onClick={() => void runMcpProbe()}>{mcpTesting ? t('settings.mcpTesting') : t('settings.mcpTest')}</button><button className="primary-button" disabled={busy || mcpTesting || mcpFormIncomplete} onClick={() => void saveMcp()}><Save size={15} /> {t('settings.mcpSave')}</button></div>}
              </>}
            </div></div></div>}
            {tab === 'language' && <div className="settings-section language-switcher"><div className="section-heading"><div><h3>{t('lang.switchTitle')}</h3><p>{t('lang.switchDesc')}</p></div></div><div className="language-options"><button type="button" className={lang === 'zh' ? 'is-active' : ''} onClick={() => setLang('zh')}>中文</button><button type="button" className={lang === 'en' ? 'is-active' : ''} onClick={() => setLang('en')}>English</button></div></div>}
          </div>
        </div>
      </section>
      <RolePoolImportDialog file={poolImportFile} onClose={() => setPoolImportFile(null)} onImported={(done) => void onPoolImported(done)} />
    </div>
  );
};

export default WorkbenchSettings;
