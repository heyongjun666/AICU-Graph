// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { formatReadTime, formatViews, formatRate, formatCount } from '../utils';

describe('stats utils — formatting functions', () => {
  // --- formatReadTime ---
  describe('formatReadTime', () => {
    it('formats sub-minute values as seconds', () => {
      expect(formatReadTime(0)).toBe('0s');
      expect(formatReadTime(45)).toBe('45s');
      expect(formatReadTime(59)).toBe('59s');
    });

    it('formats minute values as m s', () => {
      expect(formatReadTime(60)).toBe('1m 0s');
      expect(formatReadTime(185)).toBe('3m 5s');
      expect(formatReadTime(3600)).toBe('60m 0s');
    });

    it('rounds fractional seconds', () => {
      expect(formatReadTime(90.7)).toBe('1m 31s');
    });
  });

  // --- formatViews ---
  describe('formatViews', () => {
    it('returns the number as string for values under 1000', () => {
      expect(formatViews(0)).toBe('0');
      expect(formatViews(42)).toBe('42');
      expect(formatViews(999)).toBe('999');
    });

    it('uses K suffix for thousands', () => {
      expect(formatViews(1000)).toBe('1.0K');
      expect(formatViews(3200)).toBe('3.2K');
      expect(formatViews(15800)).toBe('15.8K');
      expect(formatViews(999999)).toBe('1000.0K');
    });

    it('uses M suffix for millions', () => {
      expect(formatViews(1_000_000)).toBe('1.0M');
      expect(formatViews(1_500_000)).toBe('1.5M');
      expect(formatViews(2_500_000)).toBe('2.5M');
    });
  });

  // --- formatRate ---
  describe('formatRate', () => {
    it('formats 0..1 as percentage', () => {
      expect(formatRate(0)).toBe('0.0%');
      expect(formatRate(0.85)).toBe('85.0%');
      expect(formatRate(0.42)).toBe('42.0%');
      expect(formatRate(1)).toBe('100.0%');
    });

    it('handles values above 1', () => {
      expect(formatRate(1.5)).toBe('150.0%');
    });
  });

  // --- formatCount (chart axis) ---
  describe('formatCount', () => {
    it('returns the number as string for small values', () => {
      expect(formatCount(0)).toBe('0');
      expect(formatCount(42)).toBe('42');
    });

    it('uses K suffix (no M for chart axis labels)', () => {
      expect(formatCount(1000)).toBe('1.0K');
      expect(formatCount(3200)).toBe('3.2K');
      expect(formatCount(2_500_000)).toBe('2500.0K'); // no M suffix
    });
  });

  // --- Consistency: All view-related formatters agree ---
  describe('consistency', () => {
    it('formatViews and formatCount agree on < 1M values', () => {
      expect(formatViews(3200)).toBe('3.2K');
      expect(formatCount(3200)).toBe('3.2K');
    });

    it('all four functions produce stable output for known inputs', () => {
      // Regression check — these exact strings are asserted in component tests
      expect(formatViews(15800)).toBe('15.8K');
      expect(formatReadTime(185)).toBe('3m 5s');
      expect(formatRate(0.85)).toBe('85.0%');
      expect(formatRate(0.42)).toBe('42.0%');
    });
  });
});
