// ============================================================================
// infra/index.ts — Barrel exports for the shared infrastructure module
// ============================================================================

export type { ApiResponse, PaginatedData } from './types';
export { setBaseUrl, buildQuery, requestRaw, requestJSON } from './api';
export { ArticleListSkeleton } from './components/Skeleton';
