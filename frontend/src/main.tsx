import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { I18nProvider } from './i18n';

// ============================================================================
// 全局 CSS
// ============================================================================

const globalStyle = `
  * {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
  }

  html, body, #root {
    height: 100%;
    width: 100%;
    overflow: hidden;
    font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  }

  @keyframes pulse {
    0%, 100% { opacity: 0.4; }
    50% { opacity: 1; }
  }

  @keyframes slideIn {
    from { transform: translateX(100%); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
  }

  /* ReactFlow 自定义样式覆盖 */
  .react-flow__node {
    cursor: pointer !important;
  }

  .react-flow__node.selected {
    cursor: pointer !important;
  }

  .react-flow__controls {
    display: flex;
    gap: 2px;
  }

  .react-flow__controls-button {
    width: 28px;
    height: 28px;
    border: 1px solid #E5E7EB;
    background: #FFF;
    border-radius: 6px;
    cursor: pointer;
  }

  ::-webkit-scrollbar {
    width: 6px;
    height: 6px;
  }

  ::-webkit-scrollbar-track {
    background: transparent;
  }

  ::-webkit-scrollbar-thumb {
    background: #D1D5DB;
    border-radius: 3px;
  }

  ::-webkit-scrollbar-thumb:hover {
    background: #9CA3AF;
  }
`;

// Inject global styles
const styleElement = document.createElement('style');
styleElement.textContent = globalStyle;
document.head.appendChild(styleElement);

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <I18nProvider>
      <App />
    </I18nProvider>
  </React.StrictMode>,
);
