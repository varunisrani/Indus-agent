import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FlushScheduler } from "./flushScheduler.ts";
import { FLUSH_IDLE_MS, FLUSH_SCROLL_MS, FLUSH_TYPING_MS, TYPING_IDLE_MS } from "./timing.ts";

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

function scheduler() {
  const flush = vi.fn();
  return { flush, s: new FlushScheduler({ flush }) };
}

describe("flush scheduler", () => {
  it("flushes 16 ms after a request when the user is idle", () => {
    const { flush, s } = scheduler();
    s.request();
    vi.advanceTimersByTime(FLUSH_IDLE_MS - 1);
    expect(flush).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(flush).toHaveBeenCalledTimes(1);
  });

  it("shares one window between many requests", () => {
    const { flush, s } = scheduler();
    for (let i = 0; i < 50; i++) s.request();
    vi.advanceTimersByTime(FLUSH_IDLE_MS);
    expect(flush).toHaveBeenCalledTimes(1);
  });

  it("waits 80 ms while typing and goes back to 16 ms once typing is idle", () => {
    const { flush, s } = scheduler();
    s.noteTyping();
    s.request();
    vi.advanceTimersByTime(FLUSH_TYPING_MS - 1);
    expect(flush).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(flush).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(TYPING_IDLE_MS);
    expect(s.currentDelay()).toBe(FLUSH_IDLE_MS);
  });

  it("waits 96 ms while scrolling, even while typing", () => {
    const { flush, s } = scheduler();
    s.noteTyping();
    s.noteScrolling();
    expect(s.currentDelay()).toBe(FLUSH_SCROLL_MS);
    s.request();
    vi.advanceTimersByTime(FLUSH_SCROLL_MS - 1);
    expect(flush).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(flush).toHaveBeenCalledTimes(1);
  });

  it("flushes at once on demand and drops the open window", () => {
    const { flush, s } = scheduler();
    s.request();
    s.flushNow();
    expect(flush).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(200);
    expect(flush).toHaveBeenCalledTimes(1);
    expect(s.pending).toBe(false);
  });
});
