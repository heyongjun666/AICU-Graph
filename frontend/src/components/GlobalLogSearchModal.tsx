// ============================================================================
// GlobalLogSearchModal — 全局日志搜索弹窗
// 支持级别筛选、节点 ID 过滤、关键词搜索、分页、定位到节点
// ============================================================================

import React, { useState, useEffect } from 'react';
import { useUIStateStore } from '../stores/UIStateStore';
import { useGraphStore } from '../stores/GraphStore';
import { useSessionStore } from '../stores/SessionStore';
import { ModalType, LogLevel, LogEntry } from '../types';
import { searchLogs } from '../utils/api';
import { useI18n } from '../i18n';

const PAGE_SIZE = 50;

const overlayStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  backgroundColor: 'rgba(0,0,0,0.4)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  zIndex: 100,
};

const modalStyle: React.CSSProperties = {
  backgroundColor: '#FFF',
  borderRadius: 16,
  padding: 24,
  width: 700,
  maxHeight: '80vh',
  display: 'flex',
  flexDirection: 'column',
  boxShadow: '0 20px 60px rgba(0,0,0,0.2)',
};

const LEVELS = ['ALL', ...Object.values(LogLevel)] as const;

const GlobalLogSearchModal: React.FC = () => {
  const { t } = useI18n();
  const isOpen = useUIStateStore((state) => state.modals[ModalType.GlobalLogSearch]);
  const closeModal = useUIStateStore((state) => state.closeModal);
  const addToast = useUIStateStore((state) => state.addToast);
  const selectNode = useGraphStore((state) => state.selectNode);
  const nodes = useGraphStore((state) => state.nodes);
  const sessionId = useSessionStore((state) => state.activeSessionId);

  const [level, setLevel] = useState<string>('ALL');
  const [nodeIdFilter, setNodeIdFilter] = useState('');
  const [keyword, setKeyword] = useState('');
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) closeModal(ModalType.GlobalLogSearch);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [isOpen, closeModal]);

  if (!isOpen) return null;

  const doSearch = async (newOffset = 0) => {
    if (!sessionId) return;
    setLoading(true);
    try {
      const result = await searchLogs(sessionId, {
        node_id: nodeIdFilter || undefined,
        level: level === 'ALL' ? undefined : (level as LogLevel),
        keyword: keyword || undefined,
        offset: newOffset,
        limit: PAGE_SIZE,
      });
      setLogs(result.logs);
      setTotal(result.total);
      setOffset(newOffset);
    } catch (err) {
      addToast({ type: 'error', message: t('logs.failedSearch') });
    } finally {
      setLoading(false);
    }
  };

  const handleSearch = () => doSearch(0);

  const handleLocateNode = (nodeId: string) => {
    selectNode(nodeId);
    closeModal(ModalType.GlobalLogSearch);
    addToast({ type: 'info', message: t('logs.locatedToNode', { nodeId }) });
  };

  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <div style={overlayStyle} onClick={() => closeModal(ModalType.GlobalLogSearch)}>
      <div style={modalStyle} onClick={(e) => e.stopPropagation()}>
        <h2 style={{ fontSize: 18, fontWeight: 600, color: '#111827', margin: '0 0 16px' }}>
          {t('logs.title')}
        </h2>

        {/* Filters */}
        <div style={{ display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
          <select
            value={level}
            onChange={(e) => setLevel(e.target.value)}
            style={{
              padding: '6px 10px',
              borderRadius: 8,
              border: '1px solid #D1D5DB',
              fontSize: 12,
            }}
          >
            {LEVELS.map((l) => (
              <option key={l} value={l}>{l}</option>
            ))}
          </select>
          <input
            value={nodeIdFilter}
            onChange={(e) => setNodeIdFilter(e.target.value)}
            placeholder={t('logs.nodeIdFilter')}
            style={{
              padding: '6px 10px',
              borderRadius: 8,
              border: '1px solid #D1D5DB',
              fontSize: 12,
              width: 140,
              outline: 'none',
            }}
          />
          <input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder={t('logs.keyword')}
            style={{
              padding: '6px 10px',
              borderRadius: 8,
              border: '1px solid #D1D5DB',
              fontSize: 12,
              flex: 1,
              outline: 'none',
            }}
            onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
          />
          <button
            onClick={handleSearch}
            disabled={loading}
            style={{
              padding: '6px 16px',
              borderRadius: 8,
              border: 'none',
              backgroundColor: loading ? '#9CA3AF' : '#6366F1',
              color: '#FFF',
              cursor: loading ? 'not-allowed' : 'pointer',
              fontSize: 12,
              fontWeight: 600,
            }}
          >
            {loading ? t('logs.searching') : t('logs.search')}
          </button>
        </div>

        {/* Log list */}
        <div style={{ flex: 1, overflow: 'auto', marginBottom: 12 }}>
          {logs.length === 0 ? (
            <div style={{ textAlign: 'center', color: '#9CA3AF', padding: 40, fontSize: 13 }}>
              {loading ? t('logs.loading') : t('logs.noLogsFound')}
            </div>
          ) : (
            logs.map((log) => {
              const levelColors: Record<string, string> = {
                DEBUG: '#6B7280',
                INFO: '#3B82F6',
                WARN: '#F59E0B',
                ERROR: '#EF4444',
              };
              return (
                <div
                  key={log.id}
                  style={{
                    padding: '4px 8px',
                    borderBottom: '1px solid #F3F4F6',
                    fontSize: 12,
                    display: 'flex',
                    gap: 8,
                    alignItems: 'flex-start',
                  }}
                >
                  <span style={{ color: '#9CA3AF', whiteSpace: 'nowrap', minWidth: 80 }}>
                    {new Date(log.timestamp).toLocaleTimeString()}
                  </span>
                  <span
                    style={{
                      fontWeight: 600,
                      color: levelColors[log.level] || '#6B7280',
                      minWidth: 40,
                    }}
                  >
                    {log.level}
                  </span>
                  <span
                    style={{ color: '#6366F1', cursor: 'pointer', minWidth: 60 }}
                    onClick={() => handleLocateNode(log.node_id)}
                    title={t('logs.locateNode')}
                  >
                    [{log.node_id}]
                  </span>
                  <span style={{ color: '#374151', flex: 1 }}>{log.message}</span>
                </div>
              );
            })
          )}
        </div>

        {/* Pagination */}
        {total > PAGE_SIZE && (
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              marginBottom: 12,
            }}
          >
            <span style={{ fontSize: 12, color: '#6B7280' }}>
              {offset + 1}-{Math.min(offset + PAGE_SIZE, total)} of {total}
            </span>
            <div style={{ display: 'flex', gap: 4 }}>
              <button
                onClick={() => doSearch(Math.max(0, offset - PAGE_SIZE))}
                disabled={offset === 0}
                style={{
                  padding: '4px 10px',
                  borderRadius: 6,
                  border: '1px solid #D1D5DB',
                  backgroundColor: '#FFF',
                  cursor: offset === 0 ? 'not-allowed' : 'pointer',
                  fontSize: 12,
                  color: offset === 0 ? '#D1D5DB' : '#374151',
                }}
              >
                {t('logs.prev')}
              </button>
              <span style={{ fontSize: 12, color: '#6B7280', padding: '4px 8px' }}>
                {currentPage}/{totalPages}
              </span>
              <button
                onClick={() => doSearch(offset + PAGE_SIZE)}
                disabled={offset + PAGE_SIZE >= total}
                style={{
                  padding: '4px 10px',
                  borderRadius: 6,
                  border: '1px solid #D1D5DB',
                  backgroundColor: '#FFF',
                  cursor: offset + PAGE_SIZE >= total ? 'not-allowed' : 'pointer',
                  fontSize: 12,
                  color: offset + PAGE_SIZE >= total ? '#D1D5DB' : '#374151',
                }}
              >
                {t('logs.next')}
              </button>
            </div>
          </div>
        )}

        {/* Footer */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ fontSize: 11, color: '#9CA3AF' }}>
            {t('logs.totalEntries', { total })}
          </span>
          <button
            onClick={() => closeModal(ModalType.GlobalLogSearch)}
            style={{
              padding: '8px 20px',
              borderRadius: 8,
              border: '1px solid #D1D5DB',
              backgroundColor: '#FFF',
              cursor: 'pointer',
              fontSize: 13,
              fontWeight: 500,
              color: '#374151',
            }}
          >
            {t('common.close')}
          </button>
        </div>
      </div>
    </div>
  );
};

export default GlobalLogSearchModal;
