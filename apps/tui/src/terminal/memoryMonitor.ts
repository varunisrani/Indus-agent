// Watches the renderer's heap: one warning line when it grows fast, and the memory-critical exit
// (137, after the mode reset) when it nears the limit. Readings and time are injectable.

import { getHeapStatistics } from "node:v8";

export interface HeapReading {
  used: number;
  limit: number;
}

export interface MemoryMonitorOptions {
  read?: () => HeapReading;
  now?: () => number;
  /** Growth of this share of the limit inside `windowMs` counts as fast. */
  fastGrowthShare?: number;
  windowMs?: number;
  /** At or above this share of the limit the TUI exits. */
  criticalShare?: number;
  onWarn: (reading: HeapReading) => void;
  onCritical: (reading: HeapReading) => void;
}

export const CHECK_INTERVAL_MS = 5_000;

function processHeap(): HeapReading {
  return { used: process.memoryUsage().heapUsed, limit: getHeapStatistics().heap_size_limit };
}

export class MemoryMonitor {
  private readonly options: Required<Omit<MemoryMonitorOptions, "onWarn" | "onCritical">> &
    Pick<MemoryMonitorOptions, "onWarn" | "onCritical">;
  private samples: { at: number; used: number }[] = [];
  private lastWarnAt = Number.NEGATIVE_INFINITY;
  private critical = false;

  constructor(options: MemoryMonitorOptions) {
    this.options = {
      read: processHeap,
      now: Date.now,
      fastGrowthShare: 0.25,
      windowMs: 60_000,
      criticalShare: 0.92,
      ...options,
    };
  }

  /** Take one reading and act on it. */
  check(): "ok" | "warn" | "critical" {
    const { read, now, windowMs, fastGrowthShare, criticalShare } = this.options;
    const reading = read();
    const at = now();
    if (reading.used >= reading.limit * criticalShare) {
      if (!this.critical) {
        this.critical = true;
        this.options.onCritical(reading);
      }
      return "critical";
    }
    this.samples = [
      ...this.samples.filter((s) => at - s.at <= windowMs),
      { at, used: reading.used },
    ];
    const oldest = this.samples[0];
    const grew = oldest === undefined ? 0 : reading.used - oldest.used;
    if (grew >= reading.limit * fastGrowthShare && at - this.lastWarnAt >= windowMs) {
      this.lastWarnAt = at;
      this.options.onWarn(reading);
      return "warn";
    }
    return "ok";
  }

  /** Check every `intervalMs` until the returned stop function is called. */
  start(intervalMs = CHECK_INTERVAL_MS): () => void {
    const timer = setInterval(() => this.check(), intervalMs);
    timer.unref?.();
    return () => clearInterval(timer);
  }
}
