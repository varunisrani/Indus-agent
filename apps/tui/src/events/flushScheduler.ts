// Batches screen updates for streamed text: one flush per window, the window chosen by what the
// user is doing — 16 ms normally, 80 ms while typing, 96 ms while scrolling.

import { type Clock, systemClock } from "@indus/client";

type TimerHandle = ReturnType<Clock["setTimeout"]>;

import {
  FLUSH_IDLE_MS,
  FLUSH_SCROLL_MS,
  FLUSH_TYPING_MS,
  SCROLL_IDLE_MS,
  TYPING_IDLE_MS,
} from "./timing.ts";

export interface FlushSchedulerOptions {
  flush: () => void;
  clock?: Clock;
}

export class FlushScheduler {
  private readonly run: () => void;
  private readonly clock: Clock;
  private timer: TimerHandle | null = null;
  private lastTypingAt = Number.NEGATIVE_INFINITY;
  private lastScrollAt = Number.NEGATIVE_INFINITY;
  /** Flushes run so far (diagnostics and tests). */
  flushes = 0;

  constructor(options: FlushSchedulerOptions) {
    this.run = options.flush;
    this.clock = options.clock ?? systemClock;
  }

  /** The window a flush requested now would wait. */
  currentDelay(): number {
    const now = this.clock.now();
    if (now - this.lastScrollAt < SCROLL_IDLE_MS) return FLUSH_SCROLL_MS;
    if (now - this.lastTypingAt < TYPING_IDLE_MS) return FLUSH_TYPING_MS;
    return FLUSH_IDLE_MS;
  }

  /** A key was typed in the composer. */
  noteTyping(): void {
    this.lastTypingAt = this.clock.now();
  }

  /** The transcript scrolled. */
  noteScrolling(): void {
    this.lastScrollAt = this.clock.now();
  }

  get pending(): boolean {
    return this.timer !== null;
  }

  /** Ask for a flush; requests inside an open window share it. */
  request(): void {
    if (this.timer !== null) return;
    this.timer = this.clock.setTimeout(() => {
      this.timer = null;
      this.flushes += 1;
      this.run();
    }, this.currentDelay());
  }

  /** Flush at once (a seal, a completion) and drop the open window. */
  flushNow(): void {
    this.cancel();
    this.flushes += 1;
    this.run();
  }

  cancel(): void {
    if (this.timer !== null) this.clock.clearTimeout(this.timer);
    this.timer = null;
  }
}
