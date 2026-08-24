// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import React from 'react';
import { SearchBar } from '../SearchBar';

afterEach(cleanup);

describe('SearchBar', () => {
  it('should render input with search placeholder', () => {
    render(<SearchBar onSearch={() => {}} />);
    const input = screen.getByPlaceholderText('搜索文章标题...');
    expect(input).toBeDefined();
  });

  it('should call onSearch with input value when Enter is pressed', () => {
    const handleSearch = vi.fn();
    render(<SearchBar onSearch={handleSearch} />);
    const input = screen.getByPlaceholderText('搜索文章标题...');
    fireEvent.change(input, { target: { value: 'golang' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(handleSearch).toHaveBeenCalledWith('golang');
  });

  it('should call onSearch with empty string when clear button is clicked', () => {
    const handleSearch = vi.fn();
    render(<SearchBar onSearch={handleSearch} initialValue="react" />);
    const clearBtn = screen.getByLabelText('清除搜索');
    fireEvent.click(clearBtn);
    expect(handleSearch).toHaveBeenCalledWith('');
  });

  it('should show the initial value', () => {
    render(<SearchBar onSearch={() => {}} initialValue="typescript" />);
    const input = screen.getByPlaceholderText('搜索文章标题...') as HTMLInputElement;
    expect(input.value).toBe('typescript');
  });

  it('should not call onSearch for other key presses', () => {
    const handleSearch = vi.fn();
    render(<SearchBar onSearch={handleSearch} />);
    const input = screen.getByPlaceholderText('搜索文章标题...');
    fireEvent.change(input, { target: { value: 'test' } });
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(handleSearch).not.toHaveBeenCalled();
  });
});
