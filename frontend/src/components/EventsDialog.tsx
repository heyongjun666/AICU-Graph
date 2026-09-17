import React, { useCallback, useEffect, useState } from 'react';
import { RefreshCw, X, Zap } from 'lucide-react';
import { AgentEvent, AgentEventStatus } from '../types';
import { getAgentEvents } from '../utils/api';
import { useI18n } from '../i18n';

interface EventsDialogProps {
  open: boolean;
  sessionId: string | null;
  onClose: () => void;
}

const STATUS_META: Record<AgentEventStatus, { label: string; titleKey: string }> = {
  routed: { label: 'ROUTED', titleKey: 'events.status.routed.title' },
  delivered: { label: 'DELIVERED', titleKey: 'events.status.delivered.title' },
  consumed: { label: 'CONSUMED', titleKey: 'events.status.consumed.title' },
};

/**
 * EventsDialog — Agent 事件日志查看（方案三 Event 状态 + 路由记录）。
 * 打开即拉取 /agent-events；展示 producer、状态、路由目标与事件内容。
 */
const EventsDialog: React.FC<EventsDialogProps> = ({ open, sessionId, onClose }) => {
  const { t } = useI18n();
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!sessionId) return;
    setLoading(true);
    setError('');
    getAgentEvents(sessionId)
      .then(setEvents)
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setLoading(false));
  }, [sessionId]);

  useEffect(() => {
    if (open) {
      setEvents([]);
      setExpanded(null);
      setError('');
      load();
    }
  }, [open, load]);

  if (!open) return null;

  const toggleExpand = (eventId: string) => {
    setExpanded((current) => (current === eventId ? null : eventId));
  };

  return (
    <div className="dialog-backdrop" onMouseDown={onClose}>
      <div className="workbench-dialog events-dialog" onMouseDown={(event) => event.stopPropagation()}>
        <div className="dialog-header">
          <div><h2>{t('events.title')}</h2><p>{t('events.desc')}</p></div>
          <div className="dialog-header-actions">
            {sessionId && (
              <button className="secondary-button" onClick={load} disabled={loading} title={t('common.refresh')}>
                <RefreshCw size={14} className={loading ? 'spin' : ''} /> {t('common.refresh')}
              </button>
            )}
            <button className="icon-button" onClick={onClose} title={t('common.close')}><X size={18} /></button>
          </div>
        </div>

        {!sessionId ? (
          <div className="detail-empty">{t('events.selectSession')}</div>
        ) : (
          <div className="events-browser">
            {error && <div className="inspector-error">{error}</div>}
            {loading && !events.length && <div className="detail-empty"><RefreshCw size={15} className="spin" /> {t('events.loadingEvents')}</div>}
            {!loading && !events.length && !error && <div className="detail-empty">{t('events.noEvents')}</div>}
            {events.length > 0 && (
              <div className="events-list">
                {events.map((event) => {
                  const meta = STATUS_META[event.status] || STATUS_META.routed;
                  const isExpanded = expanded === event.event_id;
                  // 防御：objective 声明为字符串，但历史/异常事件可能为对象，渲染前兜底字符串化，避免整页白屏。
                  const objectiveText = typeof event.objective === 'string'
                    ? event.objective
                    : (event.objective ? JSON.stringify(event.objective) : '(empty)');
                  return (
                    <div className="event-item" key={event.event_id}>
                      <div className="event-item-header">
                        <span className="event-icon"><Zap size={13} /></span>
                        <span className="event-id" title={event.event_id}>{event.event_id}</span>
                        <span className="event-producer" title={event.producer}>{event.producer_label}</span>
                        <span className={`event-status event-status-${event.status}`} title={t(meta.titleKey)}>{meta.label}</span>
                        {event.intent && event.intent !== 'notification' && <span className="event-meta-tag">intent: {event.intent}</span>}
                        {event.scope && event.scope !== 'related' && <span className="event-meta-tag">scope: {event.scope}</span>}
                        {event.action && <span className="event-meta-tag">action: {event.action}</span>}
                        <span className="event-time">{event.created_at}</span>
                      </div>
                      <div className="event-targets">
                        <span className="event-targets-label">{t('events.targets')}</span>
                        {event.targets.length ? event.targets.map((target) => (
                          <span className="event-target" key={target.node_id} title={target.node_id}>{target.label}</span>
                        )) : <span className="event-target event-target-none">{t('events.none')}</span>}
                      </div>
                      <button className={`event-content ${isExpanded ? 'event-content-expanded' : ''}`} onClick={() => toggleExpand(event.event_id)} title={isExpanded ? t('events.collapse') : t('events.expand')}>
                        {objectiveText}
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
};

export default EventsDialog;
