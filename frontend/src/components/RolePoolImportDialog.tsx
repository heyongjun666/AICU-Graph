import React, { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import {
  BundleConflictPolicy,
  BundleDecisions,
  BundleReadiness,
  BundleWarning,
  RolePoolImportResult,
} from '../types';
import { importRolePool } from '../utils/api';
import { useI18n } from '../i18n';
import { errorMessage } from '../utils/errorMessage';

type Translate = (key: string, params?: Record<string, string | number>) => string;

/** 后端 warnings 的 kind 是开放集合（包可能来自更新版本），未知 kind 退化成「kind：ref」。 */
export function describeBundleWarning(t: Translate, warning: BundleWarning): string {
  const key = `poolBundle.warn.${warning.kind}`;
  const known = t(key, { ref: warning.ref, role: warning.role }) !== key;
  return known
    ? t(key, { ref: warning.ref, role: warning.role })
    : t('poolBundle.warn.unknown', { kind: warning.kind, ref: warning.ref });
}

function describeIssue(t: Translate, issue: BundleReadiness['issues'][number]): string {
  const key = `poolBundle.issue.${issue.kind}`;
  const params = { ref: issue.ref, detail: issue.detail };
  return t(key, params) !== key
    ? t(key, params)
    : t('poolBundle.issue.unknown', { kind: issue.kind, ref: issue.ref });
}

const NAME_LIMIT = 3;

/** 已经是空的就原样返回：换一个新的 {} 会让预览白白重跑一次。 */
const clearDecisions = (current: BundleDecisions): BundleDecisions =>
  Object.keys(current).length ? {} : current;

function nameList(t: Translate, names: string[]): string {
  return names.length > NAME_LIMIT
    ? t('poolBundle.namesMore', { names: names.slice(0, NAME_LIMIT).join(', '), count: names.length })
    : names.join(', ');
}

interface RolePoolImportDialogProps {
  /** 选中的 .zip；null = 不显示。 */
  file: File | null;
  onClose: () => void;
  /** 真正导入成功后触发，调用方据此重新拉取 roles / skills / MCP / pools。 */
  onImported: (result: RolePoolImportResult) => void;
}

/**
 * 角色池导入面板：先 dry_run 预览（确认前不写任何东西），有冲突时让用户选跳过 / 覆盖，
 * 确认后真正导入，并展示导入后的可用性检查。全局选项给冲突项一个默认处理，每个冲突行还能单独改；
 * 任何一处改变都会重新预览 —— 可用性取决于冲突项最终用哪个版本，不能用上一次的结论。
 */
export const RolePoolImportDialog: React.FC<RolePoolImportDialogProps> = ({
  file,
  onClose,
  onImported,
}) => {
  const { t } = useI18n();
  const [policy, setPolicy] = useState<BundleConflictPolicy>('skip');
  const [decisions, setDecisions] = useState<BundleDecisions>({});
  const [plan, setPlan] = useState<RolePoolImportResult | null>(null);
  const [result, setResult] = useState<RolePoolImportResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    setPolicy('skip');
    setDecisions(clearDecisions);
    setPlan(null);
    setResult(null);
    setError('');
  }, [file]);

  useEffect(() => {
    if (!file) return;
    let cancelled = false;
    setLoading(true);
    setError('');
    importRolePool(file, { dryRun: true, onConflict: policy, decisions })
      .then((next) => { if (!cancelled) setPlan(next); })
      .catch((reason) => {
        if (cancelled) return;
        setPlan(null);
        setError(errorMessage(reason));
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [file, policy, decisions]);

  if (!file) return null;

  const shown = result ?? plan;
  const close = () => { if (!importing) onClose(); };

  const confirm = async () => {
    setImporting(true);
    setError('');
    try {
      const done = await importRolePool(file, { dryRun: false, onConflict: policy, decisions });
      setResult(done);
      onImported(done);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setImporting(false);
    }
  };

  const badge = (item: { status: string; action?: string }) => {
    const text = result && item.action
      ? t(`poolBundle.action.${item.action}`)
      : t(`poolBundle.status.${item.status}`);
    return <span className="pool-bundle-badge" data-status={item.status}>{text}</span>;
  };

  const busy = loading || importing;
  const setDefaultPolicy = (value: BundleConflictPolicy) => {
    setDecisions(clearDecisions);
    setPolicy(value);
  };

  /** 冲突行右侧的单项开关；预览阶段才出现，导入完成后只留结果徽标。 */
  const conflictSelect = (item: { key: string; status: string; policy?: BundleConflictPolicy }, name: string) =>
    !result && item.status === 'conflict' ? (
      <select
        className="pool-bundle-select"
        aria-label={t('poolBundle.rowPolicy', { name })}
        value={item.policy ?? policy}
        disabled={busy}
        onChange={(event) => setDecisions({ ...decisions, [item.key]: event.target.value as BundleConflictPolicy })}
      >
        <option value="skip">{t('poolBundle.rowSkip')}</option>
        <option value="overwrite">{t('poolBundle.rowOverwrite')}</option>
      </select>
    ) : null;
  const usage = (key: string, names?: string[]) =>
    !result && names && names.length > 0
      ? <div className="pool-bundle-detail">{t(key, { names: nameList(t, names) })}</div>
      : null;

  const overwriteCount = plan
    ? [...plan.roles, ...plan.skills, ...plan.mcp, plan.pool]
        .filter((item) => item.status === 'conflict' && item.policy === 'overwrite').length
    : 0;

  const readiness = shown?.readiness ?? [];
  const readyCount = readiness.filter((item) => item.ready).length;
  const attention = readiness.filter((item) => !item.ready);

  return (
    <div className="dialog-backdrop" onMouseDown={close}>
      <div
        className="workbench-dialog pool-bundle-dialog"
        role="dialog"
        aria-label={t('poolBundle.title')}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-header">
          <div>
            <h2>{result ? t('poolBundle.resultTitle') : t('poolBundle.title')}</h2>
            <p>{t(result ? 'poolBundle.resultDesc' : 'poolBundle.desc', { file: file.name })}</p>
          </div>
          <button type="button" className="icon-button" onClick={close} title={t('common.close')}>
            <X size={18} />
          </button>
        </div>

        {!result && <div className="pool-bundle-trust" role="note">{t('poolBundle.trustNotice')}</div>}

        {loading && !shown && <div className="picker-status">{t('poolBundle.loading')}</div>}

        {shown && (
          <>
            {result?.counts && (
              <div className="picker-status">{t('poolBundle.resultSummary', {
                created: result.counts.created,
                overwritten: result.counts.overwritten,
                skipped: result.counts.skipped,
                unchanged: result.counts.unchanged,
              })}</div>
            )}

            <div className="node-dialog-section">
              <strong>{t('poolBundle.pool')}</strong>
              <ul className="pool-bundle-list">
                <li>
                  <span className="pool-bundle-name">{shown.pool.name}</span>
                  <span className="pool-bundle-meta">{shown.pool.roles.join(', ')}</span>
                  {conflictSelect(shown.pool, shown.pool.name)}
                  {badge(shown.pool)}
                  {usage('poolBundle.localPoolRoles', shown.pool.local_roles)}
                </li>
              </ul>
            </div>

            {shown.roles.length > 0 && (
              <div className="node-dialog-section">
                <strong>{t('poolBundle.roles')}</strong>
                <ul className="pool-bundle-list">
                  {shown.roles.map((item) => (
                    <li key={`${item.type}:${item.id}`}>
                      <span className="pool-bundle-name">{item.id}</span>
                      <span className="pool-bundle-meta">{item.type}</span>
                      {conflictSelect(item, item.id)}
                      {badge(item)}
                      {usage('poolBundle.usedByPools', item.used_by)}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {shown.skills.length > 0 && (
              <div className="node-dialog-section">
                <strong>{t('poolBundle.skills')}</strong>
                <ul className="pool-bundle-list">
                  {shown.skills.map((item) => (
                    <li key={item.id}>
                      <span className="pool-bundle-name">{item.id}</span>
                      <span className="pool-bundle-meta">
                        {t('poolBundle.fileCount', { count: item.file_count })}
                        {item.script_count > 0 && ` · ${t('poolBundle.scriptCount', { count: item.script_count })}`}
                      </span>
                      {conflictSelect(item, item.id)}
                      {badge(item)}
                      {usage('poolBundle.usedByRoles', item.used_by)}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {shown.mcp.length > 0 && (
              <div className="node-dialog-section">
                <strong>{t('poolBundle.mcp')}</strong>
                <ul className="pool-bundle-list">
                  {shown.mcp.map((item) => (
                    <li key={item.name} className="pool-bundle-mcp">
                      <span className="pool-bundle-name">{item.name}</span>
                      {conflictSelect(item, item.name)}
                      {badge(item)}
                      {usage('poolBundle.usedByRoles', item.used_by)}
                      {/* 导入后这条命令会在本机被执行 —— 必须让用户在确认前看到。 */}
                      <div className="pool-bundle-detail">
                        {item.command
                          ? <>{t('poolBundle.mcpExecutes')} <code>{[item.command, ...item.args].join(' ')}</code></>
                          : <code>{t('poolBundle.mcpRemote', { url: item.url })}</code>}
                      </div>
                      {item.sensitive_keys.length > 0 && (
                        <div className="pool-bundle-detail">
                          {t('poolBundle.mcpCredentials', { keys: item.sensitive_keys.join(', ') })}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {!result && plan?.has_conflict && (
              <div className="node-dialog-section">
                <strong>{t('poolBundle.conflictTitle')}</strong>
                <p className="picker-status">{t('poolBundle.conflictHint')}</p>
                <div className="candidate-pool-modes" role="radiogroup" aria-label={t('poolBundle.conflictTitle')}>
                  {(['skip', 'overwrite'] as const).map((value) => (
                    <label key={value} className="candidate-pool-mode">
                      <input
                        type="radio"
                        name="pool-bundle-policy"
                        checked={policy === value}
                        disabled={busy}
                        onChange={() => setDefaultPolicy(value)}
                      />
                      <span>{t(`poolBundle.policy.${value}`)}</span>
                    </label>
                  ))}
                </div>
              </div>
            )}

            {readiness.length > 0 && (
              <div className="node-dialog-section">
                <strong>{t('poolBundle.readinessTitle')}</strong>
                <p className="picker-status">{t('poolBundle.readySummary', {
                  ready: readyCount,
                  attention: attention.length,
                })}</p>
                {attention.length > 0 && (
                  <ul className="pool-bundle-list">
                    {attention.map((item) => (
                      <li key={`${item.type}:${item.id}`} className="pool-bundle-mcp">
                        <span className="pool-bundle-name">{item.id}</span>
                        <span className="pool-bundle-badge" data-status="conflict">{t('poolBundle.needsAttention')}</span>
                        {item.issues.map((issue, index) => (
                          <div key={index} className="pool-bundle-detail">{describeIssue(t, issue)}</div>
                        ))}
                      </li>
                    ))}
                  </ul>
                )}
                <p className="picker-status">{t('poolBundle.readinessNote')}</p>
              </div>
            )}

            {shown.warnings.length > 0 && (
              <div className="node-dialog-section">
                <strong>{t('poolBundle.warningsTitle')}</strong>
                <ul className="pool-bundle-notes">
                  {shown.warnings.map((warning, index) => (
                    <li key={index}>{describeBundleWarning(t, warning)}</li>
                  ))}
                </ul>
              </div>
            )}

            {result && shown.mcp.length > 0 && <small>{t('poolBundle.testMcpHint')}</small>}
          </>
        )}

        {error && <div className="form-error">{error}</div>}
        <div className="dialog-actions">
          <button type="button" className="secondary-button" onClick={close} disabled={importing}>
            {result ? t('common.close') : t('common.cancel')}
          </button>
          {!result && (
            <button
              type="button"
              className="primary-button"
              onClick={() => void confirm()}
              disabled={!plan || loading || importing}
            >
              {importing
                ? t('poolBundle.importing')
                : overwriteCount > 0
                  ? t('poolBundle.confirmOverwrite', { count: overwriteCount })
                  : t('poolBundle.confirm')}
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

export default RolePoolImportDialog;
