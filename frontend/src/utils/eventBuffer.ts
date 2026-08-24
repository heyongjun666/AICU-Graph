// ============================================================================
// eventBuffer — SSE 事件缓冲区
// 在 SSE 断开期间缓存事件，重连后回放
// ============================================================================

import { SSEEvent } from '../types';

const MAX_BUFFER_SIZE = 500;

export class EventBuffer {
  private buffer: SSEEvent[] = [];
  private maxSize: number;

  constructor(maxSize: number = MAX_BUFFER_SIZE) {
    this.maxSize = maxSize;
  }

  /**
   * 推入单个事件
   */
  push(event: SSEEvent): void {
    if (this.buffer.length >= this.maxSize) {
      this.buffer.shift(); // 移除最早的事件
    }
    this.buffer.push(event);
  }

  /**
   * 批量推入事件
   */
  pushBatch(events: SSEEvent[]): void {
    for (const event of events) {
      this.push(event);
    }
  }

  /**
   * 回放所有缓存事件并清空
   * @param handler 事件处理函数
   */
  replay(handler: (event: SSEEvent) => void): void {
    const events = [...this.buffer];
    for (const event of events) {
      try {
        handler(event);
      } catch (e) {
        console.warn('[EventBuffer] replay error:', e);
      }
    }
    this.buffer = [];
  }

  /**
   * 清空缓冲区
   */
  clear(): void {
    this.buffer = [];
  }

  /**
   * 停止并清空
   */
  stop(): void {
    this.clear();
  }

  /**
   * 获取缓冲区大小
   */
  get size(): number {
    return this.buffer.length;
  }

  /**
   * 获取所有缓存事件
   */
  get events(): SSEEvent[] {
    return [...this.buffer];
  }
}
