// ============================================================================
// infra/api.ts — Shared HTTP infrastructure
// Low-level fetch utilities consumed by all business modules
// ============================================================================

import type { ApiResponse } from './types';

let _baseUrl = '/api/v1';

/** Override the base URL (for tests or environment config) */
export function setBaseUrl(url: string): void {
  _baseUrl = url;
}

/** Build query string from params, skipping undefined/null/empty values */
export function buildQuery(params: Record<string, string | number | undefined | null>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') {
      parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
    }
  }
  return parts.length > 0 ? `?${parts.join('&')}` : '';
}

/**
 * Low-level HTTP GET request returning typed ApiResponse.
 * Throws on non-OK HTTP status.
 */
export async function requestRaw<T>(path: string, query = ''): Promise<ApiResponse<T>> {
  const url = `${_baseUrl}${path}${query}`;
  const res = await fetch(url, {
    headers: { Accept: 'application/json' },
  });

  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }

  return res.json();
}

/**
 * HTTP request with JSON body (POST / PUT / DELETE).
 * Sends a JSON payload and expects a typed ApiResponse.
 */
export async function requestJSON<T>(
  path: string,
  method: 'POST' | 'PUT' | 'DELETE',
  body?: unknown,
): Promise<ApiResponse<T>> {
  const url = `${_baseUrl}${path}`;
  const res = await fetch(url, {
    method,
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }

  return res.json();
}
