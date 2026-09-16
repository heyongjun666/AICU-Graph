import React, { useEffect, useMemo, useState } from 'react';
import { Plus, Workflow } from 'lucide-react';
import GraphCanvas from './components/GraphCanvas';
import NodeInspector from './components/NodeInspector';
import SessionSidebar from './components/SessionSidebar';
import SharedWorkspacePanel from './components/SharedWorkspacePanel';
import SessionWorkspaceDialog from './components/SessionWorkspaceDialog';
import EventsDialog from './components/EventsDialog';
import CandidateRolesDialog from './components/CandidateRolesDialog';
import TerminalPanel from './components/TerminalPanel';
import WorkbenchHeader from './components/WorkbenchHeader';
import WorkbenchSettings from './components/WorkbenchSettings';
import {
  NewNodeDialog,
  NewSessionDialog,
  confirmSessionDelete,
  LinkCandidateLoadState,
} from './components/WorkbenchDialogs';
import SSEProvider from './providers/SSEProvider';
import { useGraphStore } from './stores/GraphStore';
import { useSessionStore } from './stores/SessionStore';
import { useUIStateStore } from './stores/UIStateStore';
import { useEventStore } from './stores/EventStore';
import { LinkCandidate, NodeConfig, SessionSummary } from './types';
import { createSessionNode, getGlobalConfig, getGraphSnapshot, getLinkCandidates } from './utils/api';
import { useI18n } from './i18n';
import ToastContainer from './components/ToastContainer';
import './workbench.css';

const App: React.FC = () => {
  const { t } = useI18n();
  const setLanguage = useUIStateStore((state) => state.setLanguage);
  const sessions = useSessionStore((state) => state.sessions);
  const activeSessionId = useSessionStore((state) => state.activeSessionId);
  const loading = useSessionStore((state) => state.loading);
  const loadError = useSessionStore((state) => state.error);
  const loadSessions = useSessionStore((state) => state.loadSessions);
  const refreshSessions = useSessionStore((state) => state.refreshSessions);
  const selectSession = useSessionStore((state) => state.selectSession);
  const createSession = useSessionStore((state) => state.createSession);
  const renameSession = useSessionStore((state) => state.renameSession);
  const deleteSession = useSessionStore((state) => state.deleteSession);
  const resetGraph = useGraphStore((state) => state.resetGraph);
  const addNode = useGraphStore((state) => state.addNode);
  const batchUpdate = useGraphStore((state) => state.batchUpdate);
  const nodes = useGraphStore((state) => state.nodes);
  const selectedNodeId = useGraphStore((state) => state.selectedNodeId);
  const clientStatus = useUIStateStore((state) => state.clientStatus);
  const loadEvents = useEventStore((state) => state.loadEvents);
  const clearEvents = useEventStore((state) => state.clear);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [sharedPanelOpen, setSharedPanelOpen] = useState(false);
  const [sharedPanelWidth, setSharedPanelWidth] = useState(320);
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  const [newNodeOpen, setNewNodeOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [terminalOpen, setTerminalOpen] = useState(false);
  const [workspaceSession, setWorkspaceSession] = useState<SessionSummary | null>(null);
  const [eventsOpen, setEventsOpen] = useState(false);
  const [candidatePoolOpen, setCandidatePoolOpen] = useState(false);
  const [dialogBusy, setDialogBusy] = useState(false);
  const [linkCandidates, setLinkCandidates] = useState<LinkCandidate[]>([]);
  const [linkCandidateStatus, setLinkCandidateStatus] = useState<LinkCandidateLoadState>('idle');
  const [linkCandidateError, setLinkCandidateError] = useState('');

  useEffect(() => { loadSessions(); }, [loadSessions]);
  useEffect(() => { resetGraph(); }, [activeSessionId, resetGraph]);
  useEffect(() => {
    getGlobalConfig()
      .then((res) => {
        if (res.config?.language) setLanguage(res.config.language);
      })
      .catch(() => {});
  }, [setLanguage]);
  useEffect(() => {
    if (activeSessionId) {
      loadEvents(activeSessionId);
    } else {
      clearEvents();
    }
  }, [activeSessionId, loadEvents, clearEvents]);
  useEffect(() => {
    if (!newNodeOpen || !activeSessionId) {
      if (!newNodeOpen) {
        setLinkCandidates([]);
        setLinkCandidateStatus('idle');
        setLinkCandidateError('');
      }
      return;
    }
    let cancelled = false;
    setLinkCandidateStatus('loading');
    setLinkCandidateError('');
    getLinkCandidates(activeSessionId)
      .then((nextCandidates) => {
        if (cancelled) return;
        setLinkCandidates(nextCandidates);
        setLinkCandidateStatus(nextCandidates.length ? 'ready' : 'empty');
      })
      .catch((reason) => {
        if (cancelled) return;
        // Keep the last authoritative list while exposing the request error;
        // never turn an error into an all-nodes fallback.
        setLinkCandidateStatus('error');
        setLinkCandidateError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => { cancelled = true; };
  }, [activeSessionId, newNodeOpen]);

  const activeSession = useMemo(
    () => sessions.find((session) => session.id === activeSessionId) || null,
    [activeSessionId, sessions],
  );

  const createNewSession = async (title: string) => {
    setDialogBusy(true);
    try {
      await createSession(title);
      setNewSessionOpen(false);
      setSidebarOpen(false);
    } finally {
      setDialogBusy(false);
    }
  };

  const addNewNode = async (payload: {
    task: string;
    role: string;
    exec_type: 'manager' | 'worker';
    parent_id?: string;
    dependencies?: string[];
    config?: NodeConfig;
  }) => {
    if (!activeSessionId) return;
    setDialogBusy(true);
    try {
      if (linkCandidateStatus === 'loading' || linkCandidateStatus === 'error') {
        throw new Error(linkCandidateError || t('dialog.candidatesLoadFailed'));
      }
      const node = await createSessionNode(activeSessionId, payload);
      // 后端会为新节点生成 parent/dependency 边（随机 UUID，前端无法本地预测），
      // 拉一次权威快照让结构/依赖连线立即出现；快照失败时降级为仅显示节点。
      try {
        const snapshot = await getGraphSnapshot(activeSessionId);
        batchUpdate(snapshot.nodes, snapshot.edges);
      } catch {
        addNode(node);
      }
      setNewNodeOpen(false);
      await refreshSessions();
    } finally {
      setDialogBusy(false);
    }
  };

  const handleRename = async (session: SessionSummary) => {
    const title = window.prompt(t('session.rename'), session.title)?.trim();
    if (title && title !== session.title) await renameSession(session.id, title);
  };

  const handleDelete = async (session: SessionSummary) => {
    if (!confirmSessionDelete(session)) return;
    await deleteSession(session.id);
  };

  const SIDEBAR_WIDTH = 276;
  const SIDEBAR_COLLAPSED_WIDTH = 56;
  const sidebarWidth = sidebarCollapsed ? SIDEBAR_COLLAPSED_WIDTH : SIDEBAR_WIDTH;
  const leftOffset = sidebarWidth + (sharedPanelOpen ? sharedPanelWidth : 0);

  return (
    <div
      className="app-shell"
      style={{
        '--sidebar-width-current': `${sidebarWidth}px`,
        '--shared-panel-width': `${sharedPanelOpen ? sharedPanelWidth : 0}px`,
        '--left-offset': `${leftOffset}px`,
      } as React.CSSProperties}
    >
      <SessionSidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        open={sidebarOpen}
        collapsed={sidebarCollapsed}
        onToggleCollapse={() => setSidebarCollapsed((value) => !value)}
        onCreate={() => setNewSessionOpen(true)}
        onSelect={(id) => { selectSession(id); setSidebarOpen(false); }}
        onRename={handleRename}
        onDelete={handleDelete}
        onOpenSettings={() => setSettingsOpen(true)}
        onOpenWorkspace={(session) => setWorkspaceSession(session)}
      />
      {sidebarOpen && <button className="sidebar-scrim" onClick={() => setSidebarOpen(false)} aria-label={t('session.closeSessions')} />}

      <SharedWorkspacePanel
        sessionId={activeSessionId}
        open={sharedPanelOpen}
        width={sharedPanelWidth}
        onToggle={() => setSharedPanelOpen((value) => !value)}
        onResize={setSharedPanelWidth}
      />

      <main className={`workspace ${selectedNodeId ? 'has-inspector' : ''}`}>
        {loading && !sessions.length ? (
          <div className="workspace-state"><span className="loading-ring" /><p>{t('session.loading')}</p></div>
        ) : activeSession ? (
          <SSEProvider key={activeSession.id} sessionId={activeSession.id}>
            <WorkbenchHeader
              session={activeSession}
              clientStatus={clientStatus}
              onOpenSessions={() => setSidebarOpen(true)}
              onAddNode={() => setNewNodeOpen(true)}
              outputsOpen={sharedPanelOpen}
              onToggleOutputs={() => setSharedPanelOpen((value) => !value)}
              onOpenEvents={() => setEventsOpen(true)}
              onRefresh={refreshSessions}
              terminalOpen={terminalOpen}
              onToggleTerminal={() => setTerminalOpen((value) => !value)}
              onOpenCandidatePool={() => setCandidatePoolOpen(true)}
            />
            <div className="graph-workspace">
              <GraphCanvas />
              <NodeInspector />
            </div>
            <TerminalPanel sessionId={activeSession.id} open={terminalOpen} />
          </SSEProvider>
        ) : (
          <div className="empty-workspace">
            <div className="empty-symbol"><Workflow size={28} /></div>
            <h1>{t('session.startGraph')}</h1>
            <p>{t('session.startGraphDesc')}</p>
            {loadError && <div className="form-error">{loadError}</div>}
            <button className="primary-button" onClick={() => setNewSessionOpen(true)}><Plus size={17} /> {t('session.newSession')}</button>
          </div>
        )}
      </main>

      <NewSessionDialog open={newSessionOpen} busy={dialogBusy} onClose={() => setNewSessionOpen(false)} onSubmit={createNewSession} />
      <NewNodeDialog
        open={newNodeOpen}
        busy={dialogBusy}
        nodes={Array.from(nodes.values())}
        candidates={linkCandidates}
        candidateStatus={linkCandidateStatus}
        candidateError={linkCandidateError}
        onClose={() => setNewNodeOpen(false)}
        onSubmit={addNewNode}
      />
      <WorkbenchSettings open={settingsOpen} onClose={() => setSettingsOpen(false)} onSaved={refreshSessions} />
      <SessionWorkspaceDialog open={Boolean(workspaceSession)} session={workspaceSession} onClose={() => setWorkspaceSession(null)} onSaved={refreshSessions} />
      <EventsDialog open={eventsOpen} sessionId={activeSessionId} onClose={() => setEventsOpen(false)} />
      <CandidateRolesDialog
        open={candidatePoolOpen}
        sessionId={activeSessionId}
        onClose={() => setCandidatePoolOpen(false)}
        onSaved={refreshSessions}
      />
      <ToastContainer />
    </div>
  );
};

export default App;
