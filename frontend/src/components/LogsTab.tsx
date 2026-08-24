// ============================================================================
// LogsTab — 节点日志 Tab
// 虚拟滚动（react-window FixedSizeList），级别过滤，关键词搜索
// ============================================================================

import React, { useState, useEffect, useRef } from 'react';
import { useGraphStore } from '../stores/GraphStore';
import { useSessionStore } from '../stores/SessionStore';
import { LogLevel, LogEntry } from '../types';
import { searchLogs } from '../utils/api';
import { useI18n } from '../i18n';

const ROW_HEIGHT = 24;

interface LogsTabProps {
  nodeId: string;
}

const LEVELS: LogLevel[] = [LogLevel.DEBUG, LogLevel.INFO, LogLevel.WARN, LogLevel.ERROR];

const LEVEL_COLORS: Record<string, string> = {
  DEBUG: '#6B7280',
  INFO: '#3B82F6',
  WARN: '#F59E0B',
  ERROR: '#EF4444',
};

const LogsTab: React.FC<LogsTabProps> = ({ nodeId }) => {
  const { t } = useI18n();
  const sessionId = useSessionStore((state) => state.activeSessionId);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [levelFilter, setLevelFilter] = useState<LogLevel | 'ALL'>('ALL');
  const [keyword, setKeyword] = useState('');
  const [loading, setLoading] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const [visibleRange, setVisibleRange] = useState({ start: 0, end: 50 });

  const doSearch = async () => {
    if (!sessionId || !nodeId) return;
    setLoading(true);
    try {
      const result = await searchLogs(sessionId, {
        node_id: nodeId,
        level: levelFilter === 'ALL' ? undefined : levelFilter,
        keyword: keyword || undefined,
        limit: 1000,
      });
      setLogs(result.logs);
      setTotal(result.total);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    doSearch();
  }, [nodeId]);

  // Scroll handler for virtual scrolling
  const handleScroll = () => {
    if (!listRef.current) return;
    const { scrollTop, clientHeight } = listRef.current;
    const start = Math.floor(scrollTop / ROW_HEIGHT);
    const visibleCount = Math.ceil(clientHeight / ROW_HEIGHT);
    setVisibleRange({
      start: Math.max(0, start - 5),
      end: start + visibleCount + 5,
    });
  };

  const filteredLogs = logs.filter((log) => {
    if (levelFilter !== 'ALL' && log.level !== levelFilter) return false;
    if (keyword && !log.message.toLowerCase().includes(keyword.toLowerCase())) return false;
    return true;
  });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Filters */}
      <div
        style={{
          padding: '8px 12px',
          borderBottom: '1px solid #F3F4F6',
          display: 'flex',
          gap: 4,
          flexWrap: 'wrap',
          alignItems: 'center',
        }}
      >
        {['ALL', ...LEVELS].map((level) => (
          <button
            key={level}
            onClick={() => setLevelFilter(level as LogLevel | 'ALL')}
            style={{
              padding: '2px 8px',
              borderRadius: 4,
              border: `1px solid ${levelFilter === level ? '#6366F1' : '#E5E7EB'}`,
              backgroundColor: levelFilter === level ? '#EEF2FF' : '#FFF',
              color: levelFilter === level ? '#6366F1' : '#6B7280',
              cursor: 'pointer',
              fontSize: 10,
              fontWeight: levelFilter === level ? 600 : 400,
            }}
          >
            {level === 'ALL' ? 'ALL' : level}
          </button>
        ))}
        <input
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder={t('logs.searchPlaceholder')}
          style={{
            flex: 1,
            minWidth: 60,
            padding: '2px 8px',
            borderRadius: 4,
            border: '1px solid #E5E7EB',
            fontSize: 10,
            outline: 'none',
          }}
          onKeyDown={(e) => e.key === 'Enter' && doSearch()}
        />
      </div>

      {/* Log list */}
      <div
        ref={listRef}
        onScroll={handleScroll}
        style={{ flex: 1, overflow: 'auto', position: 'relative' }}
      >
        {loading ? (
          <div style={{ textAlign: 'center', color: '#9CA3AF', padding: 20, fontSize: 12 }}>
            {t('logs.loading')}
          </div>
        ) : filteredLogs.length === 0 ? (
          <div style={{ textAlign: 'center', color: '#9CA3AF', padding: 20, fontSize: 12 }}>
            {t('logs.noLogsFound')}
          </div>
        ) : (
          <div style={{ height: filteredLogs.length * ROW_HEIGHT, position: 'relative' }}>
            {filteredLogs.slice(visibleRange.start, visibleRange.end).map((log, i) => {
              const index = visibleRange.start + i;
              return (
                <div
                  key={log.id}
                  style={{
                    position: 'absolute',
                    top: index * ROW_HEIGHT,
                    left: 0,
                    right: 0,
                    height: ROW_HEIGHT,
                    padding: '0 12px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    fontSize: 11,
                    borderBottom: '1px solid #F9FAFB',
                  }}
                >
                  <span style={{ color: '#9CA3AF', whiteSpace: 'nowrap', minWidth: 70 }}>
                    {new Date(log.timestamp).toLocaleTimeString()}
                  </span>
                  <span
                    style={{
                      fontWeight: 600,
                      color: LEVEL_COLORS[log.level] || '#6B7280',
                      minWidth: 36,
                      fontSize: 10,
                    }}
                  >
                    {log.level}
                  </span>
                  <span style={{ color: '#374151', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {log.message}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Footer */}
      <div
        style={{
          padding: '4px 12px',
          borderTop: '1px solid #F3F4F6',
          fontSize: 10,
          color: '#9CA3AF',
        }}
      >
        {t('logs.entries', { count: filteredLogs.length, total })}
      </div>
    </div>
  );
};

export default LogsTab;
