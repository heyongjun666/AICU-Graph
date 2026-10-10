import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft,
  ChevronRight,
  Copy,
  FileText,
  PackageOpen,
  Pause,
  Pencil,
  Play,
  RefreshCw,
  Save,
  ScrollText,
  Square,
  Trash2,
  X,
  Zap,
} from 'lucide-react';
import { useGraphStore } from '../stores/GraphStore';
import { useSessionStore } from '../stores/SessionStore';
import { useUIStateStore } from '../stores/UIStateStore';
import { useEventStore } from '../stores/EventStore';
import { GraphNode, LogEntry, LogLevel, NodeConfig, NodeStatus, TASK_HISTORY_REASONS, WorkspaceFile, WorkspaceListing } from '../types';
import {
  continueNode,
  controlSessionNode,
  deleteSessionNode,
  getSessionNode,
  getSessionNodeLogs,
  listNodeResults,
  readNodeResultFile,
  reviseSessionNode,
  updateSessionNode,
} from '../utils/api';
import { formatBytes } from '../utils/format';
import { useI18n } from '../i18n';
import { useAvailableClis } from '../hooks/useAvailableClis';
import { useMcpCompatWarning } from '../hooks/useMcpCompatWarning';
import { useWorkspaceDrop } from '../hooks/useWorkspaceDrop';
import WorkspaceEntryRow from './WorkspaceEntryRow';
import { CliOptions } from './CliOptions';

type Tab = 'overview' | 'logs' | 'artifact' | 'events';

const EMPTY_LOGS: LogEntry[] = [];
const EVENT_STATUS_LABEL: Record<string, string> = {
  routed: 'ROUTED',
  delivered: 'DELIVERED',
  consumed: 'CONSUMED',
};
/** 错误提示自动关闭时限（与 toast 默认时长一致），避免提示无限期挂起在页面上。 */
const ERROR_AUTO_DISMISS_MS = 3000;
const tabs: Array<{ id: Tab; label: string; icon: React.ReactNode }> = [
  { id: 'overview', label: 'inspector.overview', icon: <FileText size={15} /> },
  { id: 'logs', label: 'inspector.logs', icon: <ScrollText size={15} /> },
  { id: 'artifact', label: 'inspector.output', icon: <PackageOpen size={15} /> },
  { id: 'events', label: 'inspector.event', icon: <Zap size={15} /> },
];

const NodeInspector: React.FC = () => {
  const { t } = useI18n();
  const sessionId = useSessionStore((state) => state.activeSessionId);
  const selectedNodeId = useGraphStore((state) => state.selectedNodeId);
  const storeNode = useGraphStore((state) => selectedNodeId ? state.nodes.get(selectedNodeId) : undefined);
  const graphNodes = useGraphStore((state) => state.nodes);
  const logs = useGraphStore((state) => selectedNodeId ? state.logsByNode.get(selectedNodeId) || EMPTY_LOGS : EMPTY_LOGS);
  const setNodeLogs = useGraphStore((state) => state.setNodeLogs);
  const updateNode = useGraphStore((state) => state.updateNode);
  const removeNode = useGraphStore((state) => state.removeNode);
  const selectNode = useGraphStore((state) => state.selectNode);
  const addToast = useUIStateStore((state) => state.addToast);
  const events = useEventStore((state) => state.events);
  const eventsLoading = useEventStore((state) => state.loading);
  const loadEvents = useEventStore((state) => state.loadEvents);
  const highlightEvent = useEventStore((state) => state.highlightEvent);
  const highlightedEventId = useEventStore((state) => state.highlightedEventId);
  const [node, setNode] = useState<GraphNode | null>(null);
  const [tab, setTab] = useState<Tab>('overview');
  const [expandedEventId, setExpandedEventId] = useState<string | null>(null);
  const [logLevel, setLogLevel] = useState<'ALL' | LogLevel>('ALL');
  const [followLogs, setFollowLogs] = useState(true);
  const [visibleLogStart, setVisibleLogStart] = useState(0);
  const logViewerRef = useRef<HTMLDivElement>(null);
  const nodeRequestGeneration = useRef(0);
  const logsRequestGeneration = useRef(0);
  const [workspace, setWorkspace] = useState<WorkspaceListing | null>(null);
  const [workspaceFile, setWorkspaceFile] = useState<WorkspaceFile | null>(null);
  const [workspacePath, setWorkspacePath] = useState('');
  const [workspaceBusy, setWorkspaceBusy] = useState(false);
  const [workspaceError, setWorkspaceError] = useState('');
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [revising, setRevising] = useState(false);
  const [reviseTask, setReviseTask] = useState('');
  const [task, setTask] = useState('');
  const [role, setRole] = useState('');
  const [cliName, setCliName] = useState<NodeConfig['cli_name']>('internal');
  const [model, setModel] = useState('');
  const [apiBaseUrl, setApiBaseUrl] = useState('');
  const [apiKeyEnv, setApiKeyEnv] = useState('');
  const [skillIds, setSkillIds] = useState('');
  const [skipPermissions, setSkipPermissions] = useState(true);
  const [error, setError] = useState('');
  const { clis, defaultCli } = useAvailableClis();
  // 角色声明了 mcp、而这个 CLI 接不了时，编辑面板的 CLI 下拉下面说一句。原因来自后端。
  // 挂在下面那个 `if (!node) return null` 之前：hook 不能排在条件返回后面。
  const mcpWarning = useMcpCompatWarning({
    enabled: editing,
    roleType: node?.exec_type === 'M' ? 'manager' : 'worker',
    roleId: role,
    cliId: cliName || '',
    clis,
  });

  // 上传完要把当前目录重列一遍。用计数器而不是直接调 load：取数逻辑在下面那个
  // effect 里（依赖 sessionId/nodeId/tab/path），在这儿复刻一遍就会有两份真相。
  const [workspaceReload, setWorkspaceReload] = useState(0);

  const drop = useWorkspaceDrop({
    sessionId: sessionId ?? '',
    scope: 'results',
    nodeId: selectedNodeId ?? undefined,
    currentDir: workspacePath,
    onUploaded: () => setWorkspaceReload((count) => count + 1),
  });

  useEffect(() => {
    setNode(storeNode || null);
    if (storeNode) {
      const config = storeNode.config || {};
      setTask(storeNode.task || '');
      setRole(storeNode.role || 'default');
      setCliName(config.cli_name || '');
      setModel(config.model || '');
      setApiBaseUrl(config.api_base_url || '');
      setApiKeyEnv(config.api_key_env || '');
      setSkillIds((config.skills || []).join(', '));
      setSkipPermissions(config.skip_permissions ?? true);
    }
  }, [storeNode]);

  useEffect(() => {
    setWorkspace(null);
    setWorkspaceFile(null);
    setWorkspacePath('');
    setWorkspaceError('');
    setError('');
  }, [selectedNodeId]);

  // 错误提示自动消失：到达时限后清空，避免提示长期挂起；error 变化时重置计时。
  useEffect(() => {
    if (!error) return;
    const timer = window.setTimeout(() => setError(''), ERROR_AUTO_DISMISS_MS);
    return () => window.clearTimeout(timer);
  }, [error]);

  // Artifact 页浏览错误（workspaceError）同样自动消失，避免同类提示长期挂起；变化时重置计时。
  useEffect(() => {
    if (!workspaceError) return;
    const timer = window.setTimeout(() => setWorkspaceError(''), ERROR_AUTO_DISMISS_MS);
    return () => window.clearTimeout(timer);
  }, [workspaceError]);

  useEffect(() => {
    const requestId = ++nodeRequestGeneration.current;
    if (!sessionId || !selectedNodeId) return;
    let cancelled = false;
    const requestedSessionId = sessionId;
    const requestedNodeId = selectedNodeId;
    getSessionNode(requestedSessionId, requestedNodeId).then((detail) => {
      if (
        !cancelled &&
        nodeRequestGeneration.current === requestId &&
        useSessionStore.getState().activeSessionId === requestedSessionId &&
        useGraphStore.getState().selectedNodeId === requestedNodeId
      ) {
        setNode(detail);
        // REST details and sparse SSE/snapshot patches share the same merge
        // semantics, so lifecycle fields cannot be lost on the next refresh.
        updateNode(detail.id, detail);
      }
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [sessionId, selectedNodeId, updateNode]);

  // Hydrate historical logs as soon as a node is selected, independently of
  // the active tab. The Logs tab effect below only handles live polling.
  useEffect(() => {
    const requestId = ++logsRequestGeneration.current;
    if (!sessionId || !selectedNodeId) return;
    let cancelled = false;
    const requestedSessionId = sessionId;
    const requestedNodeId = selectedNodeId;
    getSessionNodeLogs(requestedSessionId, requestedNodeId)
      .then((entries) => {
        if (
          !cancelled &&
          logsRequestGeneration.current === requestId &&
          useSessionStore.getState().activeSessionId === requestedSessionId &&
          useGraphStore.getState().selectedNodeId === requestedNodeId
        ) setNodeLogs(requestedNodeId, entries);
      })
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, [sessionId, selectedNodeId, setNodeLogs]);

  useEffect(() => {
    if (!sessionId || !selectedNodeId || tab !== 'logs') return;
    let cancelled = false;
    const requestedSessionId = sessionId;
    const requestedNodeId = selectedNodeId;
    const loadLogs = () => getSessionNodeLogs(requestedSessionId, requestedNodeId)
      .then((entries) => {
        if (
          !cancelled &&
          useSessionStore.getState().activeSessionId === requestedSessionId &&
          useGraphStore.getState().selectedNodeId === requestedNodeId
        ) setNodeLogs(requestedNodeId, entries);
      })
      .catch(() => undefined);
    const timer = node?.status === NodeStatus.Running ? window.setInterval(loadLogs, 1500) : undefined;
    return () => { cancelled = true; if (timer) window.clearInterval(timer); };
  }, [sessionId, selectedNodeId, tab, setNodeLogs, node?.status]);

  useEffect(() => {
    if (!sessionId || !selectedNodeId || tab !== 'artifact') return;
    let cancelled = false;
    setWorkspaceBusy(true);
    setWorkspaceError('');
    setWorkspaceFile(null);
    listNodeResults(sessionId, selectedNodeId, workspacePath)
      .then((listing) => { if (!cancelled) setWorkspace(listing); })
      .catch((reason) => {
        if (!cancelled) setWorkspaceError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => { if (!cancelled) setWorkspaceBusy(false); });
    return () => { cancelled = true; };
  }, [sessionId, selectedNodeId, tab, workspacePath, workspaceReload]);

  useEffect(() => {
    if (!sessionId || tab !== 'events') return;
    loadEvents(sessionId);
  }, [sessionId, tab, loadEvents]);

  const nodeEvents = useMemo(() => {
    if (!node) return [];
    // 只展示该节点「接收到的」事件（作为消费方）；自己发出的不展示。
    return events
      .filter((event) => event.targets.some((t) => t.node_id === node.id))
      .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  }, [events, node]);

  const visibleLogs = useMemo(
    () => logs.slice(visibleLogStart).filter((entry) => logLevel === 'ALL' || entry.level === logLevel),
    [logs, logLevel, visibleLogStart],
  );

  /**
   * 任务链：原始任务在最上面，之后每被改写一次就在下面补一条，当前任务收尾。
   *
   * 后端存的是「被顶替掉的那条」—— `entry.at` 是**它被换掉**的时间，`entry.reason`
   * 是**它被换掉**的原因。所以某一条的「到达时间 / 到达原因」是它**前一条**的
   * at / reason，不是它自己的。照搬 entry.reason 当这一条的标签会张冠李戴：
   * 一次「自动重试」会被标在**被换掉的旧**任务上，而该标的是重试换来的**新**任务。
   *
   * 原始任务没有到达原因，单独给标签。`task_history` 为空（旧会话、从未改过任务）
   * 时返回 null，调用方退回单条任务渲染 —— 那种节点与今天完全一致。
   */
  const taskChain = useMemo(() => {
    const chain = node?.task_history || [];
    if (chain.length === 0) return null;
    // 认不出的 slug 直接显示 slug 本身。宁可显示 `resequenced` 这种生词，
    // 也不要显示 `inspector.taskReason.resequenced` 这种内部 key。
    const reasonLabel = (reason: string) => (
      TASK_HISTORY_REASONS.includes(reason as (typeof TASK_HISTORY_REASONS)[number])
        ? t(`inspector.taskReason.${reason}`)
        : reason
    );
    const rows = chain.map((entry, index) => ({
      key: `${entry.at}-${index}`,
      label: index === 0 ? t('inspector.taskOrigin') : reasonLabel(chain[index - 1].reason),
      at: index === 0 ? node?.created_at : chain[index - 1].at,
      task: entry.task,
      current: false,
    }));
    const last = chain[chain.length - 1];
    rows.push({
      key: 'current',
      label: reasonLabel(last.reason),
      at: last.at,
      task: node?.task || '',
      current: true,
    });
    return rows;
  }, [node, t]);

  useEffect(() => {
    setVisibleLogStart(0);
  }, [selectedNodeId]);

  useEffect(() => {
    if (tab === 'logs' && followLogs && logViewerRef.current) {
      logViewerRef.current.scrollTop = logViewerRef.current.scrollHeight;
    }
  }, [tab, followLogs, visibleLogs.length]);

  if (!sessionId || !selectedNodeId || !node) return null;

  const isManager = node.exec_type === 'M';

  const runAction = async (action: 'pause' | 'resume' | 'stop') => {
    setBusy(true);
    setError('');
    try {
      await controlSessionNode(sessionId, node.id, action);
      const messages: Record<string, string> = {
        pause: t('inspector.nodePaused'),
        resume: t('inspector.nodeResumed'),
        stop: t('inspector.nodeStopped'),
      };
      addToast({ type: 'success', message: messages[action] });
      const updated = await getSessionNode(sessionId, node.id);
      setNode(updated);
      updateNode(updated.id, updated);
    } catch (reason) {
      const msg = reason instanceof Error ? reason.message : String(reason);
      setError(msg);
      addToast({ type: 'error', message: msg });
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    setBusy(true);
    setError('');
    try {
      const config: NodeConfig = {
        ...(node.config || {}),
        cli_name: cliName || undefined,
        model: model.trim() || undefined,
        api_base_url: apiBaseUrl.trim() || undefined,
        api_key_env: apiKeyEnv.trim() || undefined,
        skills: skillIds.split(',').map((item) => item.trim()).filter(Boolean),
        skip_permissions: skipPermissions,
      };
      const updated = await updateSessionNode(sessionId, node.id, {
        task: task.trim(),
        role: role.trim(),
        config,
      });
      setNode(updated);
      updateNode(updated.id, updated);
      setEditing(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    // 删除保护：清理子节点是严重操作，只有用户在前端手动逐叶子删除。
    // 有子节点 → 拦截（不发 API），提示先删除其子节点（后端也不支持级联删除）。
    const childCount = [...graphNodes.values()].filter((n) => n.parent_id === node.id).length;
    if (childCount > 0) {
      const msg = t('inspector.hasChildren', { id: node.id.slice(0, 8), count: childCount });
      setError(msg);
      addToast({ type: 'error', message: msg });
      return;
    }

    if (!window.confirm(t('inspector.deleteNodeConfirm', { id: node.id.slice(0, 8) }))) return;
    setBusy(true);
    try {
      await deleteSessionNode(sessionId, node.id);
      removeNode(node.id);
      selectNode(null);
      addToast({ type: 'success', message: t('inspector.nodeDeleted') });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      addToast({ type: 'error', message: reason instanceof Error ? reason.message : String(reason) });
    } finally {
      setBusy(false);
    }
  };

  const handleContinue = async () => {
    setBusy(true);
    setError('');
    try {
      await continueNode(sessionId, node.id);
      addToast({ type: 'success', message: t('inspector.nodeContinued') });
      const updated = await getSessionNode(sessionId, node.id);
      setNode(updated);
      updateNode(updated.id, updated);
    } catch (reason) {
      const msg = reason instanceof Error ? reason.message : String(reason);
      setError(msg);
      addToast({ type: 'error', message: msg });
    } finally {
      setBusy(false);
    }
  };

  const handleRevise = async () => {
    if (!reviseTask.trim()) return;
    setBusy(true);
    setError('');
    try {
      await reviseSessionNode(sessionId, node.id, reviseTask.trim());
      addToast({ type: 'success', message: isManager ? t('inspector.managerRevising') : t('inspector.workerRevising') });
      setRevising(false);
      setReviseTask('');
      const updated = await getSessionNode(sessionId, node.id);
      setNode(updated);
      updateNode(updated.id, updated);
    } catch (reason) {
      const msg = reason instanceof Error ? reason.message : String(reason);
      setError(msg);
      addToast({ type: 'error', message: msg });
    } finally {
      setBusy(false);
    }
  };

  const openWorkspaceEntry = async (entry: WorkspaceListing['entries'][number]) => {
    if (entry.type === 'directory') {
      setWorkspacePath(entry.path === '.' ? '' : entry.path);
      setWorkspaceFile(null);
      return;
    }
    setWorkspaceBusy(true);
    setWorkspaceError('');
    try {
      setWorkspaceFile(await readNodeResultFile(sessionId, node.id, entry.path));
    } catch (reason) {
      setWorkspaceError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setWorkspaceBusy(false);
    }
  };

  const pathSegments = workspacePath ? workspacePath.split('/').filter(Boolean) : [];
  const goToWorkspacePath = (index: number) => {
    setWorkspaceFile(null);
    setWorkspacePath(pathSegments.slice(0, index + 1).join('/'));
  };

  /**
   * 有产出目录才谈得上「落」。
   *
   * 两种没有目录的形态（后端 `_get_node_result_root` 的两种返回）：`artifact_path` 为空
   * （还没跑、或没声明产出）→ `result_root === null`；产出本身是**单个文件** →
   * `artifact_kind === 'file'`。两种都不渲染落点 —— 落到别处就是所见非所得，
   * 而用户定的正是「落在产出目录」。
   */
  const canAcceptDrop = Boolean(workspace?.artifact_path) && workspace?.artifact_kind !== 'file';

  const editable = node.status === NodeStatus.Waiting || node.status === NodeStatus.Paused;

  return (
    <aside className="node-inspector" aria-label={t('inspector.nodeDetails')}>
      <div className="inspector-header">
        <div className="inspector-title">
          <span className={`node-status-icon status-${node.status.toLowerCase()}`} />
          <div><strong>{node.role || node.exec_type}</strong><span>{node.id.slice(0, 8)}</span></div>
        </div>
        <button className="icon-button" onClick={() => selectNode(null)} title={t('inspector.closeInspector')}><X size={18} /></button>
      </div>

      <div className="inspector-status-row">
        <span className={`status-pill status-${node.status.toLowerCase()}`}>{node.status}</span>
        <span>{isManager ? t('dialog.manager') : t('dialog.worker')}</span>
        {node.started_at && <span>{new Date(node.started_at).toLocaleTimeString()}</span>}
      </div>

      <div className="inspector-actions">
        {node.status === NodeStatus.Running && <button onClick={() => runAction('pause')} disabled={busy}><Pause size={15} /> {t('control.pause')}</button>}
        {node.status === NodeStatus.Paused && <button onClick={() => runAction('resume')} disabled={busy}><Play size={15} /> {t('control.resume')}</button>}
        {[NodeStatus.Running, NodeStatus.Paused, NodeStatus.Ready, NodeStatus.Waiting].includes(node.status) && (
          <button onClick={() => runAction('stop')} disabled={busy}><Square size={15} /> {t('common.stop')}</button>
        )}
        {[NodeStatus.Failed, NodeStatus.Stopped].includes(node.status) && (
          <button onClick={handleContinue} disabled={busy}><Play size={15} /> {t('inspector.continue')}</button>
        )}
        {[NodeStatus.Completed, NodeStatus.Failed, NodeStatus.Stopped, NodeStatus.Paused].includes(node.status) && (
          <button onClick={() => setRevising(true)} disabled={busy} title={isManager ? t('inspector.reviseTitle') : t('inspector.reviseTitleWorker')}><RefreshCw size={15} /> {t('inspector.revise')}</button>
        )}
        <button onClick={() => setEditing(true)} disabled={!editable || busy} title={editable ? t('inspector.editNode') : t('inspector.pauseBeforeEdit')}><Pencil size={15} /> {t('inspector.editNode')}</button>
        <button className="danger-action" onClick={remove} disabled={node.status === NodeStatus.Running || busy}><Trash2 size={15} /></button>
      </div>

      {error && (
        <div className="inspector-error">
          <span className="inspector-error-message">{error}</span>
          <button
            type="button"
            className="inspector-error-close"
            onClick={() => setError('')}
            title={t('common.close')}
            aria-label={t('common.close')}
          >
            <X size={14} />
          </button>
        </div>
      )}

      <div className="inspector-tabs">
        {tabs.map((item) => (
          <button key={item.id} className={tab === item.id ? 'is-active' : ''} onClick={() => setTab(item.id)} title={t(item.label)}>
            {item.icon}<span>{t(item.label)}</span>
          </button>
        ))}
      </div>

      <div className="inspector-content">
        {tab === 'overview' && (
          <div className="detail-stack">
            <section>
              <h3>{t('node.task')}</h3>
              {/*
                `task` 是「现在该执行什么」，会被 Revise / 增量复用 / 评审修复 /
                界面改任务 整条盖掉。被盖掉的那条只存在 task_history 里 —— 没有它，
                一个被改过三次的节点看上去就是凭空冒出来的一条增量任务，看不出本来
                干什么的。所以整条链一起渲染：原始任务打头，后续每条补在后面，
                当前任务收尾。**不给折叠** —— 折起来的恰恰是最该看见的「这活本来是什么」。
              */}
              {taskChain ? (
                <ol className="task-chain">
                  {taskChain.map((row) => (
                    <li key={row.key} className={row.current ? 'is-current' : ''}>
                      <div className="task-chain-meta">
                        <span className="task-chain-label">{row.label}</span>
                        {row.at && <time>{new Date(row.at).toLocaleString()}</time>}
                        {row.current && <span className="task-chain-badge">{t('inspector.taskCurrent')}</span>}
                      </div>
                      <p className="task-copy">{row.task}</p>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="task-copy">{node.task || t('inspector.noTaskDescription')}</p>
              )}
            </section>
            <section><h3>{t('inspector.configuration')}</h3><pre>{JSON.stringify(node.config || {}, null, 2)}</pre></section>
            {node.error && <section className="error-section"><h3>{t('common.error')}</h3><p>{node.error}</p></section>}
            <section className="metadata-grid">
              <div><span>{t('inspector.created')}</span><strong>{node.created_at ? new Date(node.created_at).toLocaleString() : '-'}</strong></div>
              <div><span>{t('inspector.started')}</span><strong>{node.started_at ? new Date(node.started_at).toLocaleString() : '-'}</strong></div>
              <div><span>{t('inspector.finished')}</span><strong>{node.finished_at ? new Date(node.finished_at).toLocaleString() : '-'}</strong></div>
              <div><span>{t('node.parent')}</span><strong>{node.parent_id?.slice(0, 8) || t('inspector.root')}</strong></div>
            </section>
          </div>
        )}
        {tab === 'logs' && (
          <div className="log-panel">
            {!!node.cc_activity && Object.keys(node.cc_activity).length > 0 && <div className="cc-activity-grid">{Object.values(node.cc_activity).sort((a, b) => a.cc_id.localeCompare(b.cc_id)).map((activity) => <div className={`cc-activity cc-${activity.status}`} key={activity.cc_id}><div className="cc-activity-head"><strong>{activity.cc_id}</strong><span>{activity.status}</span></div><div className="cc-activity-phase">{activity.phase || 'idle'}</div><div className="cc-activity-task">{activity.task || node.task}</div>{activity.latest_output && <pre>{activity.latest_output}</pre>}</div>)}</div>}
            <div className="log-toolbar"><label>{t('inspector.level')}<select value={logLevel} onChange={(event) => setLogLevel(event.target.value as 'ALL' | LogLevel)}><option value="ALL">{t('common.all')}</option><option value={LogLevel.DEBUG}>{t('inspector.debug')}</option><option value={LogLevel.INFO}>{t('inspector.info')}</option><option value={LogLevel.WARN}>{t('inspector.warn')}</option><option value={LogLevel.ERROR}>{t('common.error')}</option></select></label><label className="log-follow"><input type="checkbox" checked={followLogs} onChange={(event) => setFollowLogs(event.target.checked)} /> {t('inspector.follow')}</label><button className="secondary-button" onClick={() => setVisibleLogStart(logs.length)}>{t('inspector.clearView')}</button></div>
            <div className="log-viewer" ref={logViewerRef}>
              {!visibleLogs.length && <div className="detail-empty">{logs.length ? t('inspector.noLogsMatch') : t('inspector.noLogsYet')}</div>}
              {visibleLogs.map((entry) => <div className={`log-line log-${String(entry.level).toLowerCase()}`} key={`${entry.id ?? 'local'}-${entry.timestamp}-${entry.level}-${entry.cc_id ?? ''}-${entry.phase ?? ''}-${entry.event ?? ''}-${entry.task ?? ''}`}><time>{new Date(entry.timestamp).toLocaleTimeString()}</time><span className="log-meta">{entry.cc_id && <strong className="log-source">{entry.cc_id}</strong>}{entry.phase && <span>{t('inspector.phase')}: {entry.phase}</span>}{entry.event && <span>{t('inspector.eventName')}: {entry.event}</span>}{entry.task && <span className="log-task" title={entry.task}>{entry.task}</span>}</span><span className="log-message">{entry.message}</span></div>)}
            </div>
          </div>
        )}
        {tab === 'artifact' && (
          <div className="workspace-browser">
            <div className="workspace-breadcrumbs">
              <button onClick={() => { setWorkspacePath(''); setWorkspaceFile(null); }} disabled={!workspacePath && !workspaceFile}>{t('inspector.output')}</button>
              {pathSegments.map((segment, index) => <React.Fragment key={`${segment}-${index}`}><ChevronRight size={13} /><button onClick={() => goToWorkspacePath(index)}>{segment}</button></React.Fragment>)}
              {canAcceptDrop && (
                <button
                  className="icon-button workspace-breadcrumb-action"
                  title={t('files.copyPath')}
                  aria-label={t('files.copyPath')}
                  onClick={() => drop.onCopyPath(workspacePath)}
                >
                  <Copy size={12} />
                </button>
              )}
            </div>
            {drop.progress && (
              <div className="workspace-drop-status">
                {t('files.uploading', { done: drop.progress.done, total: drop.progress.total })}
              </div>
            )}
            {!!drop.failures.length && (
              <div className="workspace-drop-failures">
                {drop.failures.slice(0, 3).map((item) => <div key={item.name}>{item.name}: {item.reason}</div>)}
                {drop.failures.length > 3 && <div>+{drop.failures.length - 3}</div>}
              </div>
            )}
            {workspaceError && (
              <div className="inspector-error">
                <span className="inspector-error-message">{workspaceError}</span>
                <button
                  type="button"
                  className="inspector-error-close"
                  onClick={() => setWorkspaceError('')}
                  title={t('common.close')}
                  aria-label={t('common.close')}
                >
                  <X size={14} />
                </button>
              </div>
            )}
            {workspaceBusy && <div className="detail-empty"><RefreshCw size={15} className="spin" /> {t('inspector.loadingOutput')}</div>}
            {!workspaceBusy && workspaceFile && (
              <div className="workspace-preview">
                <div className="workspace-preview-header">
                  <button className="secondary-button" onClick={() => setWorkspaceFile(null)}><ArrowLeft size={14} /> {t('common.back')}</button>
                  <span>{workspaceFile.path}</span>
                  <small>{formatBytes(workspaceFile.size)}</small>
                  <button
                    className="icon-button"
                    title={t('files.copyPath')}
                    aria-label={t('files.copyPath')}
                    onClick={() => drop.onCopyPath(workspaceFile.path)}
                  >
                    <Copy size={13} />
                  </button>
                </div>
                {workspaceFile.content === null
                  ? <div className="detail-empty">{t('outputs.binaryFile')}</div>
                  : <pre>{workspaceFile.content}</pre>}
              </div>
            )}
            {!workspaceBusy && !workspaceFile && workspace && (
              <div
                className={`workspace-list${canAcceptDrop && drop.dropTarget === workspacePath ? ' is-drop-target' : ''}`}
                {...(canAcceptDrop ? drop.containerProps : {})}
              >
                {workspace.artifact_path === null && <div className="detail-empty">{t('inspector.noDeclaredOutput')}</div>}
                {/* 空目录提示与「能不能落」无关：产出是单个文件时照样要说明这里是空的。 */}
                {workspace.artifact_path !== null && !workspace.entries.length && (
                  <div className="detail-empty">
                    {t('inspector.emptyOutputDir')}
                    {canAcceptDrop && <div className="workspace-drop-hint">{t('files.dropHint')}</div>}
                  </div>
                )}
                {workspace.entries.map((entry) => (
                  <WorkspaceEntryRow
                    key={entry.path}
                    entry={entry}
                    sessionId={sessionId}
                    scope="results"
                    nodeId={node.id}
                    onOpen={(target) => { void openWorkspaceEntry(target); }}
                    onCopyPath={(target) => drop.onCopyPath(target.path)}
                    onReveal={(target) => drop.onReveal(target.path)}
                    dropTarget={canAcceptDrop ? drop.dropTarget : null}
                    onDropTargetChange={drop.setDropTarget}
                    onDrop={canAcceptDrop ? drop.upload : () => undefined}
                  />
                ))}
              </div>
            )}
          </div>
        )}
        {tab === 'events' && (
          <div className="detail-stack node-events">
            <section>
              <h3>{t('inspector.events')}</h3>
              {eventsLoading && !nodeEvents.length && <div className="detail-empty">{t('events.loadingEvents')}</div>}
              {!eventsLoading && !nodeEvents.length && <div className="detail-empty">{t('inspector.noEventsReceived')}</div>}
              {nodeEvents.map((event) => {
                const isExpanded = expandedEventId === event.event_id;
                const objectiveText = typeof event.objective === 'string'
                  ? event.objective
                  : (event.objective ? JSON.stringify(event.objective) : '(empty)');
                return (
                  <div
                    className={`event-item node-event-item ${event.event_id === highlightedEventId ? 'is-highlighted' : ''}`}
                    key={event.event_id}
                    onClick={() => highlightEvent(event)}
                    role="button"
                    tabIndex={0}
                    title={t('inspector.clickToHighlight')}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        highlightEvent(event);
                      }
                    }}
                  >
                    <div className="event-item-header">
                      <span className="event-icon"><Zap size={13} /></span>
                      <span className="event-id" title={event.event_id}>{event.event_id}</span>
                      <span className={`event-status event-status-${event.status}`}>{EVENT_STATUS_LABEL[event.status] || event.status}</span>
                      {event.intent && event.intent !== 'notification' && <span className="event-meta-tag">intent: {event.intent}</span>}
                      {event.scope && event.scope !== 'related' && <span className="event-meta-tag">scope: {event.scope}</span>}
                      {event.action && <span className="event-meta-tag">action: {event.action}</span>}
                      <span className="event-time">{event.created_at}</span>
                    </div>
                    <div className="node-event-role">
                      <span className="node-event-role-label">{t('inspector.producer')}</span>
                      <span className="event-node-link">{event.producer_label || event.producer}</span>
                    </div>
                    <div className="node-event-role">
                      <span className="node-event-role-label">{t('inspector.consumers')}</span>
                      {event.targets.length ? event.targets.map((target) => (
                        <span className="event-node-link" key={target.node_id}>{target.label}</span>
                      )) : <span className="event-target-none">{t('events.none')}</span>}
                    </div>
                    <button
                      className={`event-content ${isExpanded ? 'event-content-expanded' : ''}`}
                      onClick={(e) => { e.stopPropagation(); setExpandedEventId(isExpanded ? null : event.event_id); }}
                      title={isExpanded ? t('events.collapse') : t('events.expand')}
                    >
                      {objectiveText}
                    </button>
                  </div>
                );
              })}
            </section>
          </div>
        )}
      </div>

      {editing && (
        <div className="inspector-editor">
          <div className="dialog-header"><h2>{t('inspector.editNode')}</h2><button className="icon-button" onClick={() => setEditing(false)}><X size={17} /></button></div>
          <div className="form-grid two-columns"><label>{t('node.role')}<input value={role} onChange={(event) => setRole(event.target.value)} /></label><label>{t('dialog.cli')}<select value={cliName} onChange={(event) => setCliName(event.target.value as NodeConfig['cli_name'])}><CliOptions clis={clis} defaultCli={defaultCli} />{cliName && !clis.some((cli) => cli.id === cliName) ? <option value={cliName}>{cliName}</option> : null}</select>{mcpWarning && <div className="form-notice is-warning">{mcpWarning}</div>}</label></div>
          <label>{t('dialog.task')}<textarea value={task} onChange={(event) => setTask(event.target.value)} rows={7} /></label>
          <div className="form-grid two-columns"><label>{t('dialog.model')}<input value={model} onChange={(event) => setModel(event.target.value)} placeholder={t('dialog.useSessionDefault')} /></label><label>{t('dialog.apiKeyEnv')}<input value={apiKeyEnv} onChange={(event) => setApiKeyEnv(event.target.value)} placeholder="ANTHROPIC_API_KEY" /></label><label className="full-field">{t('dialog.apiBaseUrl')}<input value={apiBaseUrl} onChange={(event) => setApiBaseUrl(event.target.value)} /></label><label className="full-field">{t('inspector.skillIds')}<input value={skillIds} onChange={(event) => setSkillIds(event.target.value)} placeholder="skill-one, focused-review" /></label></div>
          <label className="toggle-field"><input type="checkbox" checked={skipPermissions} onChange={(event) => setSkipPermissions(event.target.checked)} /><span>{t('dialog.runWithoutPerms')}</span></label>
          <div className="dialog-actions"><button className="secondary-button" onClick={() => setEditing(false)}>{t('common.cancel')}</button><button className="primary-button" onClick={save} disabled={!task.trim() || !role.trim() || busy}><Save size={15} /> {t('common.save')}</button></div>
        </div>
      )}

      {revising && (
        <div className="inspector-editor">
          <div className="dialog-header"><h2>{isManager ? t('inspector.reviseManager') : t('inspector.reviseWorker')}</h2><button className="icon-button" onClick={() => setRevising(false)}><X size={17} /></button></div>
          <p className="detail-hint">{isManager ? t('inspector.reviseHint') : t('inspector.reviseHintWorker')}</p>
          <label>{t('inspector.incrementalTask')}<textarea value={reviseTask} onChange={(event) => setReviseTask(event.target.value)} rows={7} placeholder={t('inspector.revisePlaceholder')} /></label>
          <div className="dialog-actions"><button className="secondary-button" onClick={() => setRevising(false)}>{t('common.cancel')}</button><button className="primary-button" onClick={handleRevise} disabled={!reviseTask.trim() || busy}><RefreshCw size={15} /> {t('inspector.revise')}</button></div>
        </div>
      )}
    </aside>
  );
};

export default NodeInspector;
