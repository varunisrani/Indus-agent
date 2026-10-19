// Reconnect timing: doubling waits with a cap; optional full jitter for fleets of clients.

import { type Clock, systemClock, type TimerHandle } from "./clock.ts";
import { BACKOFF_BASE_MS, BACKOFF_CAP_MS } from "./limits.ts";

export interface BackoffOptions {
  baseMs?: number;
  capMs?: number;
  /** `none` (TUI: one process collides with nobody) or `full` (desktop: random in [0, step)). */
  jitter?: "none" | "full";
  random?: () => number;
}

/** Wait before attempt `attempt` (1-based): base · 2^(attempt−1), capped; jittered if asked. */
export function backoffDelay(attempt: number, options: BackoffOptions = {}): number {
  const base = options.baseMs ?? BACKOFF_BASE_MS;
  const cap = options.capMs ?? BACKOFF_CAP_MS;
  const exponent = Math.min(Math.max(0, Math.floor(attempt) - 1), 30);
  const step = Math.min(cap, base * 2 ** exponent);
  if (options.jitter !== "full") return step;
  return Math.floor((options.random ?? Math.random)() * step);
}

export interface ReconnectingEvent {
  type: "transport.reconnecting";
  attempt: number;
  delay_ms: number;
}

export interface ReconnectorOptions extends BackoffOptions {
  clock?: Clock;
  /** Start the next connection attempt. */
  reconnect: () => void;
  /** Announce each scheduled attempt. */
  onReconnecting?: (event: ReconnectingEvent) => void;
}

/** Owns the one pending reconnect timer and the attempt counter. */
export class Reconnector {
  private readonly options: ReconnectorOptions;
  private readonly clock: Clock;
  private timer: TimerHandle | undefined;
  private attempts = 0;

  constructor(options: ReconnectorOptions) {
    this.options = options;
    this.clock = options.clock ?? systemClock;
  }

  get attempt(): number {
    return this.attempts;
  }

  get scheduled(): boolean {
    return this.timer !== undefined;
  }

  /** Schedule the next attempt. No-op (returns null) while one is already pending. */
  schedule(): ReconnectingEvent | null {
    if (this.timer !== undefined) return null;
    this.attempts += 1;
    const event: ReconnectingEvent = {
      type: "transport.reconnecting",
      attempt: this.attempts,
      delay_ms: backoffDelay(this.attempts, this.options),
    };
    this.timer = this.clock.setTimeout(() => {
      this.timer = undefined;
      this.options.reconnect();
    }, event.delay_ms);
    this.options.onReconnecting?.(event);
    return event;
  }

  /** The link reached ready: the next outage starts the ladder again. */
  markReady(): void {
    this.attempts = 0;
  }

  cancel(): void {
    if (this.timer !== undefined) this.clock.clearTimeout(this.timer);
    this.timer = undefined;
  }
}
