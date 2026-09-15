// ============================================================================
// UIStateStore — UI 状态管理（Zustand）
// 管理弹窗、Toast、可访问性、性能模式、客户端连接状态等
// ============================================================================

import { create } from 'zustand';
import { ModalType, SSEClientStatus, ExportPhase, Toast } from '../types';

// ============================================================================
// 状态接口
// ============================================================================

interface UIState {
  /** 弹窗可见性记录 */
  modals: Record<ModalType, boolean>;
  /** Toast 列表 */
  toasts: Toast[];
  /** 是否显示依赖边 */
  showDependencyEdges: boolean;
  /** 性能降级模式 */
  performanceMode: boolean;
  /** 色盲模式 */
  colorBlindMode: boolean;
  /** 高对比度模式 */
  highContrastMode: boolean;
  /** SSE 客户端状态 */
  clientStatus: SSEClientStatus;
  /** 导出阶段 */
  exportPhase: ExportPhase;
  /** 当前任务文本（用于提交弹窗回显） */
  currentTaskText: string;
  /** 全局搜索关键词 */
  globalSearchKeyword: string;
  /** 界面语言 */
  language: 'zh' | 'en';
}

// ============================================================================
// 操作接口
// ============================================================================

interface UIActions {
  /** 打开弹窗 */
  openModal: (type: ModalType) => void;
  /** 关闭弹窗 */
  closeModal: (type: ModalType) => void;
  /** 切换弹窗 */
  toggleModal: (type: ModalType) => void;
  /** 关闭所有弹窗 */
  closeAllModals: () => void;

  /** 添加 Toast */
  addToast: (toast: Omit<Toast, 'id'>) => void;
  /** 移除 Toast */
  removeToast: (id: string) => void;
  /** 清空所有 Toast */
  clearToasts: () => void;

  /** 设置依赖边可见性 */
  setShowDependencyEdges: (show: boolean) => void;
  /** 切换依赖边可见性 */
  toggleDependencyEdges: () => void;

  /** 设置性能模式 */
  setPerformanceMode: (mode: boolean) => void;

  /** 设置色盲模式 */
  setColorBlindMode: (mode: boolean) => void;
  /** 设置高对比度模式 */
  setHighContrastMode: (mode: boolean) => void;

  /** 设置客户端状态 */
  setClientStatus: (status: SSEClientStatus) => void;

  /** 设置导出阶段 */
  setExportPhase: (phase: ExportPhase) => void;

  /** 设置当前任务文本 */
  setCurrentTaskText: (text: string) => void;

  /** 设置全局搜索关键词 */
  setGlobalSearchKeyword: (keyword: string) => void;

  /** 设置界面语言 */
  setLanguage: (language: 'zh' | 'en') => void;

  /** 重置所有状态 */
  reset: () => void;
}

type UIStateStore = UIState & UIActions;

// ============================================================================
// 初始状态
// ============================================================================

/** 界面语言的本地缓存键。后端配置是权威来源，这里只是一份同步缓存。 */
const LANGUAGE_KEY = 'graph-workbench.language';

/**
 * 同步读取本地缓存的界面语言。
 *
 * 语言在服务端（全局配置）持久化，但那是**异步**获取的：若初始值写死为
 * 'zh'，每次刷新都会先按中文绘制一帧、等 /v1/config 返回后再翻成英文，
 * 表现为「中文一闪而过」。在 store 初始化时同步读缓存即可消除该闪烁
 * （与 SessionStore 读取 activeSessionId 同一套路）。
 *
 * 缓存只是加速首屏，App 启动后仍会用服务端值纠正；读不到或存储不可用
 * （隐私模式 / 禁用存储）时回退 'zh'。
 */
function readCachedLanguage(): 'zh' | 'en' {
  try {
    return localStorage.getItem(LANGUAGE_KEY) === 'en' ? 'en' : 'zh';
  } catch {
    return 'zh';
  }
}

const initialState: UIState = {
  modals: {
    [ModalType.SubmitTask]: false,
    [ModalType.EditTask]: false,
    [ModalType.Export]: false,
    [ModalType.GlobalLogSearch]: false,
    [ModalType.RecoveryGuide]: false,
    [ModalType.StopConfirm]: false,
    [ModalType.NodeDetail]: false,
  },
  toasts: [],
  showDependencyEdges: true,
  performanceMode: false,
  colorBlindMode: false,
  highContrastMode: false,
  clientStatus: SSEClientStatus.Disconnected,
  exportPhase: ExportPhase.Idle,
  currentTaskText: '',
  globalSearchKeyword: '',
  language: readCachedLanguage(),
};

// ============================================================================
// Store 创建
// ============================================================================

export const useUIStateStore = create<UIStateStore>((set) => ({
  ...initialState,

  // ---- 弹窗操作 ----
  openModal: (type: ModalType) =>
    set((state) => ({
      modals: { ...state.modals, [type]: true },
    })),

  closeModal: (type: ModalType) =>
    set((state) => ({
      modals: { ...state.modals, [type]: false },
    })),

  toggleModal: (type: ModalType) =>
    set((state) => ({
      modals: { ...state.modals, [type]: !state.modals[type] },
    })),

  closeAllModals: () =>
    set((state) => {
      const closed = Object.fromEntries(
        Object.keys(state.modals).map((key) => [key, false]),
      ) as Record<ModalType, boolean>;
      return { modals: closed };
    }),

  // ---- Toast 操作 ----
  addToast: (toast: Omit<Toast, 'id'>) =>
    set((state) => ({
      toasts: [
        ...state.toasts,
        { ...toast, id: `toast-${Date.now()}-${Math.random().toString(36).slice(2, 8)}` },
      ],
    })),

  removeToast: (id: string) =>
    set((state) => ({
      toasts: state.toasts.filter((t) => t.id !== id),
    })),

  clearToasts: () => set({ toasts: [] }),

  // ---- 视图设置 ----
  setShowDependencyEdges: (show: boolean) => set({ showDependencyEdges: show }),

  toggleDependencyEdges: () =>
    set((state) => ({ showDependencyEdges: !state.showDependencyEdges })),

  // ---- 性能模式 ----
  setPerformanceMode: (mode: boolean) => set({ performanceMode: mode }),

  // ---- 无障碍模式 ----
  setColorBlindMode: (mode: boolean) => set({ colorBlindMode: mode }),
  setHighContrastMode: (mode: boolean) => set({ highContrastMode: mode }),

  // ---- 连接状态 ----
  setClientStatus: (status: SSEClientStatus) => set({ clientStatus: status }),

  // ---- 导出状态 ----
  setExportPhase: (phase: ExportPhase) => set({ exportPhase: phase }),

  // ---- 任务文本 ----
  setCurrentTaskText: (text: string) => set({ currentTaskText: text }),

  // ---- 搜索 ----
  setGlobalSearchKeyword: (keyword: string) => set({ globalSearchKeyword: keyword }),

  // ---- 语言 ----
  setLanguage: (language: 'zh' | 'en') => {
    // 同步写缓存，供下次刷新的首屏直接命中（避免中文闪烁）
    try {
      localStorage.setItem(LANGUAGE_KEY, language);
    } catch {
      /* 存储不可用时仅影响首屏语言，不影响本次切换 */
    }
    set({ language });
  },

  // ---- 重置 ----
  reset: () => set({ ...initialState }),
}));
