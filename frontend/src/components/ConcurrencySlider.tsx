// ============================================================================
// ConcurrencySlider — 并发度滑块（1-10）
// ============================================================================

import React, { useState } from 'react';
import { useI18n } from '../i18n';

const ConcurrencySlider: React.FC = () => {
  const { t } = useI18n();
  const [value, setValue] = useState(3);

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <span style={{ fontSize: 11, color: '#6B7280', whiteSpace: 'nowrap' }}>
        {t('concurrency.label')}
      </span>
      <input
        type="range"
        min={1}
        max={10}
        value={value}
        onChange={(e) => setValue(Number(e.target.value))}
        style={{ width: 80, height: 4, cursor: 'pointer' }}
        title={t('concurrency.title', { value })}
      />
      <span
        style={{
          fontSize: 11,
          fontWeight: 600,
          color: '#374151',
          minWidth: 16,
          textAlign: 'center',
        }}
      >
        {value}
      </span>
    </div>
  );
};

export default ConcurrencySlider;
