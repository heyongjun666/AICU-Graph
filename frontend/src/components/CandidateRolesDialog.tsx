import React, { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import { RoleInfo, RolePool } from '../types';
import { getRolePools, getRoles, getSessionSettings, updateSessionSettings } from '../utils/api';
import { loadRoleHoverDetail } from '../utils/roleHoverDetail';
import { useI18n } from '../i18n';
import SearchCheckboxPicker from './SearchCheckboxPicker';
import { errorMessage } from '../utils/errorMessage';

/** 候选段固定按 manager / worker 分两个 picker，类型在调用处就是常量。 */
type RoleType = NonNullable<RoleInfo['type']>;

interface CandidateRolesDialogProps {
  open: boolean;
  /** null = 当前无活动会话；与 EventsDialog 一致，由本组件内部兜底不渲染。 */
  sessionId: string | null;
  onClose: () => void;
  onSaved: () => void;
}

type PoolMode = 'pool' | 'manual';

/**
 * 会话候选角色池选择面板。
 *
 * 两种绑定方式：选一个已保存的池，或临时手选一批角色。写入会话配置的是**角色 id 快照**
 * （candidate_roles），不是池 id —— 因此之后改池/删池不会影响已绑定的会话。
 * 手选只对当前会话生效，建池/编辑池在「设置 → 角色池」。
 */
export const CandidateRolesDialog: React.FC<CandidateRolesDialogProps> = ({
  open,
  sessionId,
  onClose,
  onSaved,
}) => {
  const { t } = useI18n();
  const [pools, setPools] = useState<RolePool[]>([]);
  const [roles, setRoles] = useState<RoleInfo[]>([]);
  const [boundRoles, setBoundRoles] = useState<string[]>([]);
  const [mode, setMode] = useState<PoolMode>('pool');
  const [selectedPoolId, setSelectedPoolId] = useState('');
  const [manualRoles, setManualRoles] = useState<string[]>([]);
  const [managerQuery, setManagerQuery] = useState('');
  const [workerQuery, setWorkerQuery] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open || !sessionId) return;
    setError('');
    setManagerQuery('');
    setWorkerQuery('');
    setSelectedPoolId('');
    setManualRoles([]);
    setBoundRoles([]);
    setLoading(true);

    let cancelled = false;
    Promise.all([getRolePools(), getRoles(), getSessionSettings(sessionId)])
      .then(([nextPools, nextRoles, settings]) => {
        if (cancelled) return;
        const bound = settings.config?.candidate_roles ?? [];
        setPools(nextPools);
        setRoles(nextRoles);
        setBoundRoles(bound);
        setManualRoles(bound);
        // 反查：绑定内容恰好等于某个池时回显该池，否则按手选回填，
        // 避免用户一打开面板就把现有绑定悄悄换成另一个池。
        const matched = nextPools.find(
          (pool) => pool.roles.length > 0
            && pool.roles.length === bound.length
            && pool.roles.every((role) => bound.includes(role)),
        );
        if (matched) {
          setMode('pool');
        } else if (bound.length) {
          setMode('manual');
        } else {
          setMode(nextPools.length ? 'pool' : 'manual');
        }
        // 下拉不留空：能反查到池就选中它，否则直接落在第一个池上，
        // 让用户不必先手动挑一次才有内容可应用。
        setSelectedPoolId(matched?.id ?? nextPools[0]?.id ?? '');
      })
      .catch((reason) => {
        if (cancelled) return;
        setPools([]);
        setRoles([]);
        setError(errorMessage(reason));
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, [open, sessionId]);

  if (!open || !sessionId) return null;

  // 收窄后取常量：两个保存回调是在闭包里用的，直接引 sessionId 会丢掉非空收窄。
  const targetSessionId = sessionId;
  const selectedPool = pools.find((pool) => pool.id === selectedPoolId);
  const resolved = mode === 'pool' ? (selectedPool?.roles ?? []) : manualRoles;
  const managerRoles = roles.filter((role) => role.type === 'manager');
  const workerRoles = roles.filter((role) => role.type !== 'manager');

  const toggleManualRole = (id: string, checked: boolean) => {
    setManualRoles((current) => checked
      ? (current.includes(id) ? current : [...current, id])
      : current.filter((item) => item !== id));
  };

  const apply = async () => {
    if (!resolved.length) {
      setError(t('dialog.candidatePoolEmpty'));
      return;
    }
    try {
      setSaving(true);
      setError('');
      await updateSessionSettings(targetSessionId, { candidate_roles: resolved });
      onSaved();
      onClose();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  };

  const clear = async () => {
    try {
      setSaving(true);
      setError('');
      await updateSessionSettings(targetSessionId, { candidate_roles: [] });
      onSaved();
      onClose();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  };

  // 描述只进悬浮卡片：行内塞长描述会被省略号截断，也把清单挤得很乱。
  // 角色类型无需重复表达——两个 picker 已按 manager / worker 分段。
  // 卡片的详情懒加载：列表接口不带 YAML 配置，悬停时再按类型和 id 取一次。
  const toItems = (list: RoleInfo[], type: RoleType) =>
    list.map((role) => ({
      id: role.id,
      label: role.display_name || role.id,
      description: role.description,
      detailLoader: () => loadRoleHoverDetail(type, role.id),
    }));

  return (
    <div className="dialog-backdrop" onMouseDown={onClose}>
      <div className="workbench-dialog" onMouseDown={(event) => event.stopPropagation()}>
        <div className="dialog-header">
          <div>
            <h2>{t('dialog.candidatePoolTitle')}</h2>
            <p>{t('dialog.candidatePoolDesc')}</p>
          </div>
          <button type="button" className="icon-button" onClick={onClose} title={t('common.close')}>
            <X size={18} />
          </button>
        </div>

        <div className="picker-status">
          {boundRoles.length
            ? t('dialog.candidatePoolCurrent', { count: boundRoles.length })
            : t('dialog.candidatePoolUnbound')}
        </div>

        {/* 三种模式互斥：单选决定候选范围来源，只有选中那一项的内容会展开。 */}
        <div className="candidate-pool-modes" role="radiogroup" aria-label={t('dialog.bindingMode')}>
          <label className="candidate-pool-mode">
            <input
              type="radio"
              name="candidate-pool-mode"
              checked={mode === 'pool'}
              onChange={() => setMode('pool')}
            />
            <span>{t('dialog.selectPool')}</span>
          </label>
          <label className="candidate-pool-mode">
            <input
              type="radio"
              name="candidate-pool-mode"
              checked={mode === 'manual'}
              onChange={() => setMode('manual')}
            />
            <span>{t('dialog.manualSelect')}</span>
          </label>
          <label className="candidate-pool-mode is-disabled">
            <input type="radio" name="candidate-pool-mode" disabled checked={false} readOnly />
            <span>
              {t('dialog.smartRecommend')}
              <small>{t('dialog.smartRecommendSoon')}</small>
            </span>
          </label>
        </div>

        {mode === 'pool' && (
          <div className="node-dialog-section">
            {pools.length > 0 ? (
              <select
                value={selectedPoolId}
                onChange={(event) => setSelectedPoolId(event.target.value)}
              >
                {pools.map((pool) => (
                  <option key={pool.id} value={pool.id}>
                    {`${pool.name} (${pool.roles.length})`}
                  </option>
                ))}
              </select>
            ) : (
              <p className="picker-status">{t('dialog.noPoolAvailable')}</p>
            )}
          </div>
        )}

        {mode === 'manual' && (
          <div className="node-dialog-section">
            <p className="picker-status">{t('dialog.manualSelectHint')}</p>
            {managerRoles.length > 0 && (
              <SearchCheckboxPicker
                items={toItems(managerRoles, 'manager')}
                selected={manualRoles}
                onToggle={toggleManualRole}
                query={managerQuery}
                onQueryChange={setManagerQuery}
                searchPlaceholder={t('dialog.searchRoles')}
                emptyLabel={t('dialog.noMatchingRoles')}
                selectedLabel={t('dialog.selectedCount')}
                disabled={loading}
              />
            )}
            <SearchCheckboxPicker
              items={toItems(workerRoles, 'worker')}
              selected={manualRoles}
              onToggle={toggleManualRole}
              query={workerQuery}
              onQueryChange={setWorkerQuery}
              searchPlaceholder={t('dialog.searchRoles')}
              emptyLabel={t('dialog.noMatchingRoles')}
              selectedLabel={t('dialog.selectedCount')}
              disabled={loading}
            />
          </div>
        )}

        {error && <div className="form-error">{error}</div>}
        <div className="dialog-actions">
          <button type="button" className="secondary-button" onClick={onClose}>
            {t('common.cancel')}
          </button>
          {boundRoles.length > 0 && (
            <button type="button" className="secondary-button" onClick={() => void clear()} disabled={saving}>
              {t('dialog.clearCandidatePool')}
            </button>
          )}
          <button
            type="button"
            className="primary-button"
            onClick={() => void apply()}
            disabled={saving || loading || !resolved.length}
          >
            {t('dialog.applyCandidatePool')}
          </button>
        </div>
      </div>
    </div>
  );
};

export default CandidateRolesDialog;
