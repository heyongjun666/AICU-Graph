// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { setBaseUrl, buildQuery, requestRaw, requestJSON } from '../api';

describe('infra API utilities', () => {
  beforeEach(() => {
    setBaseUrl('http://test.local/api/v1');
    vi.restoreAllMocks();
  });

  describe('buildQuery', () => {
    it('should build query string from params', () => {
      const result = buildQuery({ page: 1, page_size: 20, status: 'published' });
      expect(result).toBe('?page=1&page_size=20&status=published');
    });

    it('should skip undefined and null values', () => {
      const result = buildQuery({ page: 1, tag_id: undefined, keyword: null as string | null });
      expect(result).toBe('?page=1');
    });

    it('should skip empty string values', () => {
      const result = buildQuery({ page: 1, keyword: '' });
      expect(result).toBe('?page=1');
    });

    it('should encode special characters', () => {
      const result = buildQuery({ q: 'hello world', lang: 'C++' });
      expect(result).toContain(encodeURIComponent('hello world'));
      expect(result).toContain(encodeURIComponent('C++'));
    });

    it('should return empty string for empty params', () => {
      const result = buildQuery({});
      expect(result).toBe('');
    });
  });

  describe('requestRaw', () => {
    it('should make GET request and return parsed JSON', async () => {
      const mockData = { code: 0, message: 'success', data: { id: 1 } };
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockData),
      });

      const result = await requestRaw('/test', '?page=1');

      expect(globalThis.fetch).toHaveBeenCalledWith(
        'http://test.local/api/v1/test?page=1',
        expect.objectContaining({ headers: { Accept: 'application/json' } }),
      );
      expect(result).toEqual(mockData);
    });

    it('should throw on HTTP error', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
      });

      await expect(requestRaw('/test')).rejects.toThrow('HTTP 500');
    });

    it('should use default base URL', async () => {
      setBaseUrl('/api/v1');
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ code: 0, message: 'success', data: null }),
      });

      await requestRaw('/articles');

      expect(globalThis.fetch).toHaveBeenCalledWith(
        '/api/v1/articles',
        expect.any(Object),
      );
    });
  });

  describe('requestJSON', () => {
    it('should make POST request with JSON body', async () => {
      const mockResponse = { code: 0, message: 'success', data: { id: 1, name: 'New Tag' } };
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve(mockResponse),
      });

      const result = await requestJSON('/tags', 'POST', { name: 'New Tag' });

      expect(globalThis.fetch).toHaveBeenCalledWith(
        'http://test.local/api/v1/tags',
        expect.objectContaining({
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ name: 'New Tag' }),
        }),
      );
      expect(result).toEqual(mockResponse);
    });

    it('should make PUT request with JSON body', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ code: 0, message: 'success', data: null }),
      });

      await requestJSON('/tags/1', 'PUT', { name: 'Renamed' });

      expect(globalThis.fetch).toHaveBeenCalledWith(
        'http://test.local/api/v1/tags/1',
        expect.objectContaining({ method: 'PUT' }),
      );
    });

    it('should make DELETE request without body', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ code: 0, message: 'success', data: null }),
      });

      await requestJSON('/tags/1', 'DELETE');

      expect(globalThis.fetch).toHaveBeenCalledWith(
        'http://test.local/api/v1/tags/1',
        expect.objectContaining({
          method: 'DELETE',
          body: undefined,
        }),
      );
    });

    it('should throw on HTTP error', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: false,
        status: 400,
      });

      await expect(requestJSON('/tags', 'POST', { name: '' })).rejects.toThrow('HTTP 400');
    });
  });
});
