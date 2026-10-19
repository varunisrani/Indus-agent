import { EventEmitter } from "node:events";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { gateStdout } from "./outputGate.ts";
import { createResizeCoalescer, RESIZE_INTERVAL_MS } from "./resize.ts";

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("resize coalescer", () => {
  it("200 events in 1 s → the first at once, one per 32 ms, and a final one at the last size", () => {
    let width = 80;
    const seen: { at: number; width: number }[] = [];
    const start = Date.now();
    const coalescer = createResizeCoalescer(() => seen.push({ at: Date.now() - start, width }));
    for (let i = 0; i < 200; i++) {
      width = 80 + i;
      coalescer.schedule();
      if (i === 0) expect(seen).toEqual([{ at: 0, width: 80 }]);
      vi.advanceTimersByTime(5);
    }
    vi.advanceTimersByTime(RESIZE_INTERVAL_MS * 2);
    expect(seen.length).toBeLessThanOrEqual(Math.floor(1000 / RESIZE_INTERVAL_MS) + 2);
    expect(seen.length).toBeGreaterThanOrEqual(Math.floor(1000 / RESIZE_INTERVAL_MS));
    expect(seen.at(-1)?.width).toBe(279);
    for (let i = 1; i < seen.length; i++) {
      expect((seen[i]?.at ?? 0) - (seen[i - 1]?.at ?? 0)).toBeGreaterThanOrEqual(
        RESIZE_INTERVAL_MS,
      );
    }
  });

  it("a single event reflows once and nothing trails it", () => {
    const reflow = vi.fn();
    const coalescer = createResizeCoalescer(reflow);
    coalescer.schedule();
    vi.advanceTimersByTime(500);
    expect(reflow).toHaveBeenCalledTimes(1);
  });

  it("cancel drops the owed trailing reflow", () => {
    const reflow = vi.fn();
    const coalescer = createResizeCoalescer(reflow);
    coalescer.schedule();
    coalescer.schedule();
    coalescer.cancel();
    vi.advanceTimersByTime(500);
    expect(reflow).toHaveBeenCalledTimes(1);
  });

  it("the output gate hands Ink coalesced resize events and detaches cleanly", () => {
    const out = Object.assign(new EventEmitter(), {
      columns: 80,
      rows: 24,
      isTTY: true,
      write: () => true,
    });
    const { stream, stats } = gateStdout(out as unknown as NodeJS.WriteStream, {
      syncOutput: false,
    });
    const inkListener = vi.fn();
    stream.on("resize", inkListener);
    for (let i = 0; i < 50; i++) {
      out.emit("resize");
      vi.advanceTimersByTime(2);
    }
    vi.advanceTimersByTime(100);
    expect(stats.resizeIn).toBe(50);
    expect(inkListener.mock.calls.length).toBeLessThanOrEqual(
      Math.ceil(100 / RESIZE_INTERVAL_MS) + 2,
    );
    stream.off("resize", inkListener);
    expect(out.listenerCount("resize")).toBe(0);
  });
});
