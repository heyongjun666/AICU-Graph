import React, { useMemo, useState } from 'react';
import {
  FolderTree,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Plus,
  Search,
  Settings,
  Trash2,
  Workflow,
} from 'lucide-react';
import { NodeStatus, SessionSummary } from '../types';
import { useI18n } from '../i18n';

interface SessionSidebarProps {
  sessions: SessionSummary[];
  activeSessionId: string | null;
  open: boolean;
  collapsed: boolean;
  onToggleCollapse: () => void;
  onCreate: () => void;
  onSelect: (sessionId: string) => void;
  onRename: (session: SessionSummary) => void;
  onDelete: (session: SessionSummary) => void;
  onOpenSettings: () => void;
  onOpenWorkspace: (session: SessionSummary) => void;
}

const statusClass: Record<string, string> = {
  [NodeStatus.Running]: 'is-running',
  [NodeStatus.Completed]: 'is-completed',
  [NodeStatus.Failed]: 'is-failed',
  [NodeStatus.Paused]: 'is-paused',
  [NodeStatus.Stopped]: 'is-stopped',
};

const SessionSidebar: React.FC<SessionSidebarProps> = ({
  sessions,
  activeSessionId,
  open,
  collapsed,
  onToggleCollapse,
  onCreate,
  onSelect,
  onRename,
  onDelete,
  onOpenSettings,
  onOpenWorkspace,
}) => {
  const { t } = useI18n();
  const [query, setQuery] = useState('');
  const [menuId, setMenuId] = useState<string | null>(null);
  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return normalized
      ? sessions.filter((session) => session.title.toLowerCase().includes(normalized))
      : sessions;
  }, [query, sessions]);

  return (
    <aside className={`session-sidebar ${open ? 'is-open' : ''} ${collapsed ? 'is-collapsed' : ''}`} aria-label={t('sidebar.sessions')}>
      <div className="sidebar-brand">
        {collapsed ? (
          <button className="icon-button sidebar-collapse" onClick={onToggleCollapse} title={t('sidebar.expandSessions')}>
            <PanelLeftOpen size={18} />
          </button>
        ) : (
          <>
            <span className="brand-mark"><Workflow size={18} /></span>
            <span className="brand-title">Graph Studio</span>
            <button className="icon-button sidebar-collapse" onClick={onToggleCollapse} title={t('sidebar.collapseSessions')}>
              <PanelLeftClose size={18} />
            </button>
          </>
        )}
      </div>

      {!collapsed && (<>
        <button className="new-session-button" onClick={onCreate}>
          <Plus size={17} />
          {t('sidebar.newSession')}
        </button>

      <label className="session-search">
        <Search size={15} />
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t('sidebar.searchSessions')}
          aria-label={t('sidebar.searchSessions')}
        />
      </label>

      <div className="session-list" role="list">
        <div className="sidebar-section-label">{t('sidebar.recent')}</div>
        {filtered.map((session) => {
          const progress = session.node_count
            ? Math.round((session.completed_count / session.node_count) * 100)
            : 0;
          return (
            <div
              className={`session-row ${session.id === activeSessionId ? 'is-active' : ''}`}
              key={session.id}
              role="listitem"
            >
              <button className="session-main" onClick={() => onSelect(session.id)}>
                <span className={`status-dot ${statusClass[session.status] || ''}`} />
                <span className="session-copy">
                  <span className="session-title">{session.title}</span>
                  <span className="session-meta">
                    {t('sidebar.nodes', { count: session.node_count })}
                    <span aria-hidden="true">·</span>
                    {session.status === NodeStatus.Running ? t('sidebar.running', { count: session.running_count }) : t('sidebar.complete', { progress })}
                  </span>
                </span>
              </button>
              <button
                className="icon-button session-menu-button"
                title={t('sidebar.sessionActions')}
                onClick={() => setMenuId(menuId === session.id ? null : session.id)}
              >
                <MoreHorizontal size={16} />
              </button>
              {menuId === session.id && (
                <div className="session-menu">
                  <button onClick={() => { setMenuId(null); onOpenWorkspace(session); }}>
                    <FolderTree size={14} /> {t('sidebar.workspace')}
                  </button>
                  <button onClick={() => { setMenuId(null); onRename(session); }}>
                    <Pencil size={14} /> {t('sidebar.rename')}
                  </button>
                  <button className="danger" onClick={() => { setMenuId(null); onDelete(session); }}>
                    <Trash2 size={14} /> {t('sidebar.delete')}
                  </button>
                </div>
              )}
            </div>
          );
        })}
        {!filtered.length && <div className="sidebar-empty">{t('sidebar.noMatchingSessions')}</div>}
      </div>

      <div className="sidebar-footer">
        <button onClick={onOpenSettings} title={t('sidebar.workbenchSettings')}>
          <Settings size={16} />
          <span>{t('sidebar.settings')}</span>
        </button>
      </div>
      </>)}
    </aside>
  );
};

export default SessionSidebar;
