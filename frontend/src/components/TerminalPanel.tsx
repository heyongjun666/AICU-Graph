import React, { useEffect, useRef } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import { useI18n } from '../i18n';

interface TerminalPanelProps {
  sessionId: string;
  open: boolean;
}

const TerminalPanel: React.FC<TerminalPanelProps> = ({ sessionId, open }) => {
  const { t } = useI18n();
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);

  useEffect(() => {
    if (!open || !containerRef.current) return;

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 12,
      lineHeight: 1.2,
      fontFamily: "'JetBrains Mono', Menlo, Monaco, 'Courier New', monospace",
      theme: {
        background: '#0b1220',
        foreground: '#d6deeb',
        cursor: '#80a4c2',
        selectionBackground: '#26374f',
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(containerRef.current);
    termRef.current = term;

    // 与 utils/api.ts 保持一致：默认相对路径 /v1/，走 Vite dev 代理（ws: true）。
    const apiBase: string = (import.meta as any).env?.VITE_API_BASE_URL || '/v1/';
    const base = apiBase.endsWith('/') ? apiBase : `${apiBase}/`;
    let wsUrl: string;
    if (/^https?:\/\//.test(base)) {
      const url = new URL(base);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      wsUrl = `${url.origin}${url.pathname}sessions/${sessionId}/terminal`;
    } else {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      wsUrl = `${protocol}//${window.location.host}${base}sessions/${sessionId}/terminal`;
    }

    const ws = new WebSocket(wsUrl);

    const sendResize = () => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
      }
    };

    ws.onopen = () => {
      term.writeln(`\x1b[32m${t('terminal.connected')}\x1b[0m`);
      sendResize();
    };
    ws.onmessage = (event) => {
      const text =
        typeof event.data === 'string'
          ? event.data
          : new TextDecoder().decode(event.data as ArrayBuffer);
      term.write(text);
    };
    ws.onerror = () => term.writeln(`\r\n\x1b[31m${t('terminal.error')}\x1b[0m`);
    ws.onclose = () => term.writeln(`\r\n\x1b[33m${t('terminal.disconnected')}\x1b[0m`);

    const dataSub = term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(data);
    });
    const resizeSub = term.onResize(sendResize);

    const fitNow = () => {
      try {
        fit.fit();
      } catch {
        /* container not laid out yet */
      }
    };
    // 首次布局稳定后再 fit，避免拿到 0 尺寸
    const raf = requestAnimationFrame(fitNow);
    window.addEventListener('resize', fitNow);

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', fitNow);
      dataSub.dispose();
      resizeSub.dispose();
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
        ws.close();
      }
      term.dispose();
      termRef.current = null;
    };
  }, [sessionId, open]);

  if (!open) return null;

  return (
    <div className="terminal-panel">
      <div className="terminal-panel-header">
        <span className="terminal-panel-title">{t('terminal.title')}</span>
        <span className="terminal-panel-meta">{t('terminal.sharedWorkspaceMeta', { id: sessionId.slice(0, 8) })}</span>
      </div>
      <div className="terminal-panel-body" ref={containerRef} />
    </div>
  );
};

export default TerminalPanel;
