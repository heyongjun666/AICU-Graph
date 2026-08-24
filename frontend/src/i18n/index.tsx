// ============================================================================
// i18n — 轻量中英文切换
// 语言状态存于 UIStateStore（zustand），持久化经 updateGlobalConfig。
// 组件内用 useI18n()；非 React 环境（如 api 错误处理）用 translate()。
// ============================================================================

import React, { createContext, useContext, useMemo } from 'react';
import { useUIStateStore } from '../stores/UIStateStore';
import { updateGlobalConfig } from '../utils/api';
import { zh } from './zh';
import { en } from './en';

export type Language = 'zh' | 'en';
export type Dict = Record<string, string>;

const dictionaries: Record<Language, Dict> = { zh, en };

export function getLanguage(): Language {
  return useUIStateStore.getState().language;
}

export function translate(
  key: string,
  params?: Record<string, string | number>,
): string {
  const lang = getLanguage();
  let template = dictionaries[lang][key];
  if (template === undefined) {
    template = dictionaries[lang === 'zh' ? 'en' : 'zh'][key] ?? key;
  }
  if (params) {
    for (const [name, value] of Object.entries(params)) {
      template = template.replace(new RegExp(`\\{${name}\\}`, 'g'), String(value));
    }
  }
  return template;
}

export function setLanguage(lang: Language): void {
  useUIStateStore.getState().setLanguage(lang);
  void updateGlobalConfig({ language: lang }).catch(() => {
    /* 持久化失败仅保留本地状态，不打断切换 */
  });
}

interface I18nContextValue {
  lang: Language;
  t: (key: string, params?: Record<string, string | number>) => string;
  setLang: (lang: Language) => void;
}

const I18nContext = createContext<I18nContextValue>({
  lang: 'zh',
  t: translate,
  setLang: setLanguage,
});

export const I18nProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const lang = useUIStateStore((state) => state.language);
  const value = useMemo<I18nContextValue>(
    () => ({ lang, t: translate, setLang: setLanguage }),
    [lang],
  );
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
};

export function useI18n(): I18nContextValue {
  return useContext(I18nContext);
}
