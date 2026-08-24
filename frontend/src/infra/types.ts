// ============================================================================
// infra/types.ts — Shared infrastructure type definitions
// Generic types consumed by all business modules (blog, etc.)
// ============================================================================

/** Unified API response wrapper */
export interface ApiResponse<T> {
  code: number;
  message: string;
  data: T | null;
}

/** Paginated list response data */
export interface PaginatedData<T> {
  total: number;
  items: T[];
}
