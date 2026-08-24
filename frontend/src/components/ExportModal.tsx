// ============================================================================
// ExportModal — 导出弹窗
// 支持 JSON/Markdown/ZIP，Collecting→Packing→Downloading→Done 四阶段
// ============================================================================

import React, { useState } from 'react';
import { useUIStateStore } from '../stores/UIStateStore';
import { useGraphStore } from '../stores/GraphStore';
import { ModalType, ExportPhase } from '../types';
import { useI18n } from '../i18n';

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
  width: 480,
  boxShadow: '0 20px 60px rgba(0,0,0,0.2)',
};

const PHASES: { key: ExportPhase; label: string }[] = [
  { key: ExportPhase.Collecting, label: 'export.collecting' },
  { key: ExportPhase.Packing, label: 'export.packing' },
  { key: ExportPhase.Downloading, label: 'export.downloading' },
  { key: ExportPhase.Done, label: 'export.done' },
];

const ExportModal: React.FC = () => {
  const { t } = useI18n();
  const isOpen = useUIStateStore((state) => state.modals[ModalType.Export]);
  const closeModal = useUIStateStore((state) => state.closeModal);
  const addToast = useUIStateStore((state) => state.addToast);
  const exportPhase = useUIStateStore((state) => state.exportPhase);
  const setExportPhase = useUIStateStore((state) => state.setExportPhase);
  const nodes = useGraphStore((state) => state.nodes);
  const edges = useGraphStore((state) => state.edges);
  const stats = useGraphStore((state) => state.stats);

  const [fileType, setFileType] = useState<'json' | 'markdown' | 'zip'>('json');
  const [format, setFormat] = useState<'full' | 'structure' | 'logs'>('full');
  const [exporting, setExporting] = useState(false);

  if (!isOpen) return null;

  const triggerDownload = (content: string, filename: string, mimeType: string) => {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleExport = async () => {
    setExporting(true);
    setExportPhase(ExportPhase.Collecting);

    // Simulate collecting
    await new Promise((r) => setTimeout(r, 500));
    setExportPhase(ExportPhase.Packing);

    // Simulate packing
    await new Promise((r) => setTimeout(r, 500));
    setExportPhase(ExportPhase.Downloading);

    try {
      const nodesArray = Array.from(nodes.values());
      const edgesArray = Array.from(edges.values());
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-');

      if (fileType === 'json') {
        const data = JSON.stringify({ nodes: nodesArray, edges: edgesArray, stats, exported_at: timestamp }, null, 2);
        triggerDownload(data, `graph-export-${timestamp}.json`, 'application/json');
      } else if (fileType === 'markdown') {
        let md = `# Graph Export (${timestamp})\n\n`;
        md += `## Stats\n\n`;
        md += `| Status | Count |\n| --- | --- |\n`;
        md += `| Total | ${stats.total} |\n`;
        md += `| Waiting | ${stats.waiting} |\n`;
        md += `| Ready | ${stats.ready} |\n`;
        md += `| Running | ${stats.running} |\n`;
        md += `| Completed | ${stats.completed} |\n`;
        md += `| Failed | ${stats.failed} |\n`;
        md += `| Stopped | ${stats.stopped} |\n`;
        md += `| Paused | ${stats.paused} |\n\n`;
        md += `## Nodes\n\n`;
        md += `| ID | Label | Status | Type | Role |\n| --- | --- | --- | --- | --- |\n`;
        for (const node of nodesArray) {
          md += `| ${node.id} | ${node.label} | ${node.status} | ${node.exec_type} | ${node.role || '-'} |\n`;
        }
        triggerDownload(md, `graph-export-${timestamp}.md`, 'text/markdown');
      } else {
        // ZIP placeholder — just download as JSON
        const data = JSON.stringify({ nodes: nodesArray, edges: edgesArray, stats, exported_at: timestamp }, null, 2);
        triggerDownload(data, `graph-export-${timestamp}.json`, 'application/json');
      }

      setExportPhase(ExportPhase.Done);
      addToast({ type: 'success', message: t('export.completed') });

      setTimeout(() => {
        setExportPhase(ExportPhase.Idle);
        closeModal(ModalType.Export);
      }, 1500);
    } catch (err) {
      addToast({ type: 'error', message: t('export.failed') });
      setExportPhase(ExportPhase.Idle);
    } finally {
      setExporting(false);
    }
  };

  return (
    <div style={overlayStyle} onClick={() => closeModal(ModalType.Export)}>
      <div style={modalStyle} onClick={(e) => e.stopPropagation()}>
        <h2 style={{ fontSize: 18, fontWeight: 600, color: '#111827', margin: '0 0 16px' }}>
          {t('export.title')}
        </h2>

        {/* File type */}
        <div style={{ marginBottom: 16 }}>
          <label style={{ fontSize: 13, fontWeight: 600, color: '#374151', marginBottom: 4, display: 'block' }}>
            {t('export.fileType')}
          </label>
          <div style={{ display: 'flex', gap: 8 }}>
            {(['json', 'markdown', 'zip'] as const).map((type) => (
              <button
                key={type}
                onClick={() => setFileType(type)}
                style={{
                  padding: '6px 16px',
                  borderRadius: 8,
                  border: `1px solid ${fileType === type ? '#6366F1' : '#D1D5DB'}`,
                  backgroundColor: fileType === type ? '#EEF2FF' : '#FFF',
                  color: fileType === type ? '#6366F1' : '#374151',
                  cursor: 'pointer',
                  fontSize: 13,
                  fontWeight: fileType === type ? 600 : 400,
                }}
              >
                {type === 'json' ? t('export.json') : type === 'markdown' ? t('export.markdown') : t('export.zip')}
              </button>
            ))}
          </div>
        </div>

        {/* Format */}
        <div style={{ marginBottom: 20 }}>
          <label style={{ fontSize: 13, fontWeight: 600, color: '#374151', marginBottom: 4, display: 'block' }}>
            {t('export.format')}
          </label>
          <div style={{ display: 'flex', gap: 8 }}>
            {(['full', 'structure', 'logs'] as const).map((fmt) => (
              <button
                key={fmt}
                onClick={() => setFormat(fmt)}
                style={{
                  padding: '6px 16px',
                  borderRadius: 8,
                  border: `1px solid ${format === fmt ? '#6366F1' : '#D1D5DB'}`,
                  backgroundColor: format === fmt ? '#EEF2FF' : '#FFF',
                  color: format === fmt ? '#6366F1' : '#374151',
                  cursor: 'pointer',
                  fontSize: 13,
                  fontWeight: format === fmt ? 600 : 400,
                }}
              >
                {fmt === 'full' ? t('export.full') : fmt === 'structure' ? t('export.structure') : t('export.logs')}
              </button>
            ))}
          </div>
        </div>

        {/* Progress */}
        {exporting && (
          <div style={{ marginBottom: 20 }}>
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                marginBottom: 8,
              }}
            >
              {PHASES.map((phase) => {
                const phaseOrder = Object.values(ExportPhase).indexOf(phase.key);
                const currentOrder = Object.values(ExportPhase).indexOf(exportPhase);
                const isActive = phaseOrder === currentOrder;
                const isDone = phaseOrder < currentOrder;

                return (
                  <div
                    key={phase.key}
                    style={{
                      fontSize: 11,
                      fontWeight: isActive ? 600 : 400,
                      color: isDone ? '#10B981' : isActive ? '#6366F1' : '#D1D5DB',
                      textAlign: 'center',
                      flex: 1,
                    }}
                  >
                    <div
                      style={{
                        width: 20,
                        height: 20,
                        borderRadius: '50%',
                        backgroundColor: isDone ? '#10B981' : isActive ? '#6366F1' : '#E5E7EB',
                        color: '#FFF',
                        fontSize: 10,
                        fontWeight: 700,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        margin: '0 auto 4px',
                      }}
                    >
                      {isDone ? '✓' : isActive ? '●' : '○'}
                    </div>
                    {t(phase.label)}
                  </div>
                );
              })}
            </div>
            <div
              style={{
                width: '100%',
                height: 4,
                backgroundColor: '#E5E7EB',
                borderRadius: 2,
                overflow: 'hidden',
              }}
            >
              <div
                style={{
                  width: `${(Object.values(ExportPhase).indexOf(exportPhase) / (PHASES.length - 1)) * 100}%`,
                  height: '100%',
                  backgroundColor: '#6366F1',
                  borderRadius: 2,
                  transition: 'width 0.3s ease',
                }}
              />
            </div>
          </div>
        )}

        {/* Buttons */}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button
            onClick={() => closeModal(ModalType.Export)}
            disabled={exporting}
            style={{
              padding: '8px 20px',
              borderRadius: 8,
              border: '1px solid #D1D5DB',
              backgroundColor: '#FFF',
              cursor: exporting ? 'not-allowed' : 'pointer',
              fontSize: 13,
              fontWeight: 500,
              color: '#374151',
            }}
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={handleExport}
            disabled={exporting}
            style={{
              padding: '8px 20px',
              borderRadius: 8,
              border: 'none',
              backgroundColor: exporting ? '#9CA3AF' : '#6366F1',
              color: '#FFF',
              cursor: exporting ? 'not-allowed' : 'pointer',
              fontSize: 13,
              fontWeight: 600,
            }}
          >
            {exporting ? t('export.exporting') : t('export.export')}
          </button>
        </div>
      </div>
    </div>
  );
};

export default ExportModal;
