// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import React from 'react';
import { Pagination } from '../Pagination';

afterEach(cleanup);

describe('Pagination', () => {
  it('should render page numbers', () => {
    render(<Pagination current={1} total={5} onChange={() => {}} />);
    expect(screen.getByText('1')).toBeDefined();
    expect(screen.getByText('5')).toBeDefined();
  });

  it('should render prev and next buttons', () => {
    render(<Pagination current={3} total={5} onChange={() => {}} />);
    expect(screen.getByLabelText('上一页')).toBeDefined();
    expect(screen.getByLabelText('下一页')).toBeDefined();
  });

  it('should call onChange with next page when next is clicked', () => {
    const handleChange = vi.fn();
    render(<Pagination current={2} total={5} onChange={handleChange} />);
    fireEvent.click(screen.getByLabelText('下一页'));
    expect(handleChange).toHaveBeenCalledWith(3);
  });

  it('should call onChange with prev page when prev is clicked', () => {
    const handleChange = vi.fn();
    render(<Pagination current={3} total={5} onChange={handleChange} />);
    fireEvent.click(screen.getByLabelText('上一页'));
    expect(handleChange).toHaveBeenCalledWith(2);
  });

  it('should disable prev button on first page', () => {
    render(<Pagination current={1} total={5} onChange={() => {}} />);
    const prevBtn = screen.getByLabelText('上一页') as HTMLButtonElement;
    expect(prevBtn.disabled).toBe(true);
  });

  it('should disable next button on last page', () => {
    render(<Pagination current={5} total={5} onChange={() => {}} />);
    const nextBtn = screen.getByLabelText('下一页') as HTMLButtonElement;
    expect(nextBtn.disabled).toBe(true);
  });

  it('should call onChange with page number when page is clicked', () => {
    const handleChange = vi.fn();
    render(<Pagination current={1} total={5} onChange={handleChange} />);
    fireEvent.click(screen.getByText('3'));
    expect(handleChange).toHaveBeenCalledWith(3);
  });

  it('should not render when total is 0', () => {
    const { container } = render(<Pagination current={1} total={0} onChange={() => {}} />);
    expect(container.innerHTML).toBe('');
  });

  it('should not render when total is 1', () => {
    const { container } = render(<Pagination current={1} total={1} onChange={() => {}} />);
    expect(container.innerHTML).toBe('');
  });
});
