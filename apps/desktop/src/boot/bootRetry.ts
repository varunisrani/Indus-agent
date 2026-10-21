// The renderer's own retry of a retryable boot failure (P20-16): full jitter in
// [0, base · 2^(n−1)), base 2,000 ms, at most 5 attempts; then the failure overlay stays.
// Latching (whether retrying can help at all) is main's verdict (P20-12), not decided here.

import { backoffDelay, type Clock, systemClock } from "@indus/client";

export const BOOT_RETRY_BASE_MS = 2_000;
export const BOOT_RETRY_MAX_ATTEMPTS = 5;
export const BOOT_RETRY_CAP_MS = 60_000;

export interface BootRetryOptions {
  clock?: Clock;
  random?: () => number;
  maxAttempts?: number;
  baseMs?: number;
}

export function bootRetryDelay(attempt: number, random: () => number = Math.random): number {
  return backoffDelay(attempt, {
    baseMs: BOOT_RETRY_BASE_MS,
    capMs: BOOT_RETRY_CAP_MS,
    jitter: "full",
    random,
  });
}

export class BootRetry {
  private readonly clock: Clock;
  private readonly random: () => number;
  private readonly maxAttempts: number;
  private readonly baseMs: number;
  private timer: unknown;
  private used = 0;
  /** When the pending attempt fires (ms on the clock), null when none is pending. */
  dueAt: number | null = null;

  constructor(options: BootRetryOptions = {}) {
    this.clock = options.clock ?? systemClock;
    this.random = options.random ?? Math.random;
    this.maxAttempts = options.maxAttempts ?? BOOT_RETRY_MAX_ATTEMPTS;
    this.baseMs = options.baseMs ?? BOOT_RETRY_BASE_MS;
  }

  get attempts(): number {
    return this.used;
  }

  get max(): number {
    return this.maxAttempts;
  }

  get pending(): boolean {
    return this.timer !== undefined;
  }

  get exhausted(): boolean {
    return this.used >= this.maxAttempts;
  }

  /** Schedule the next attempt; returns its delay, or null when none is left (or one waits). */
  schedule(run: () => void): number | null {
    if (this.timer !== undefined || this.exhausted) return null;
    this.used += 1;
    const delay = backoffDelay(this.used, {
      baseMs: this.baseMs,
      capMs: BOOT_RETRY_CAP_MS,
      jitter: "full",
      random: this.random,
    });
    this.dueAt = this.clock.now() + delay;
    this.timer = this.clock.setTimeout(() => {
      this.timer = undefined;
      this.dueAt = null;
      run();
    }, delay);
    return delay;
  }

  cancel(): void {
    if (this.timer !== undefined) this.clock.clearTimeout(this.timer);
    this.timer = undefined;
    this.dueAt = null;
  }

  /** A manual retry or a successful boot starts a fresh ladder. */
  reset(): void {
    this.cancel();
    this.used = 0;
  }
}
