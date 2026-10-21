// Liveness of the saved remote (P20-11). A probe gets 10,000 ms; 3 failures within 60,000 ms mark
// the route dead (the orchestrator then reconnects); a success clears the count. After a power
// resume, revalidation is held for 15,000 ms (networks come back slowly) and every window's
// requests during the hold coalesce into one run per window.

export const REMOTE_PROBE_TIMEOUT_MS = 10_000;
export const REMOTE_FAILURE_LIMIT = 3;
export const REMOTE_FAILURE_WINDOW_MS = 60_000;
export const RESUME_REVALIDATION_HOLD_MS = 15_000;

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export class LivenessTracker {
  private readonly failures = new Map<string, { count: number; last: number }>();
  private readonly now: () => number;

  constructor(now: () => number = Date.now) {
    this.now = now;
  }

  recordSuccess(key: string): void {
    this.failures.delete(key);
  }

  /** Count one failed probe; `dead` once the 3rd lands within 60 s of the previous one. */
  recordFailure(key: string): { failures: number; dead: boolean } {
    const now = this.now();
    const previous = this.failures.get(key);
    const count =
      previous !== undefined && now - previous.last <= REMOTE_FAILURE_WINDOW_MS
        ? previous.count + 1
        : 1;
    const dead = count >= REMOTE_FAILURE_LIMIT;
    if (dead) this.failures.delete(key);
    else this.failures.set(key, { count, last: now });
    return { failures: count, dead };
  }
}

/** Holds revalidation after a resume and coalesces requests per window. */
export class ResumeGate {
  private readonly timers: Timers;
  private readonly now: () => number;
  private holdUntil = 0;
  private holdTimer: unknown = null;
  private readonly waiting = new Map<
    string,
    { run: () => Promise<void>; done: Array<() => void> }
  >();
  private readonly inFlight = new Map<string, Promise<void>>();

  constructor(timers: Timers, now: () => number = Date.now) {
    this.timers = timers;
    this.now = now;
  }

  holding(): boolean {
    return this.now() < this.holdUntil;
  }

  /** The machine woke up: hold revalidation for 15 s from now. */
  noteResume(): void {
    this.holdUntil = this.now() + RESUME_REVALIDATION_HOLD_MS;
    if (this.holdTimer !== null) this.timers.clearTimeout(this.holdTimer);
    this.holdTimer = this.timers.setTimeout(() => this.release(), RESUME_REVALIDATION_HOLD_MS);
  }

  /**
   * Revalidate for `key` (a window): during a hold the request waits for the hold to end and
   * joins any earlier request of the same window; otherwise it joins a run already in flight.
   */
  request(key: string, run: () => Promise<void>): Promise<void> {
    if (this.holding()) {
      return new Promise((resolve) => {
        const entry = this.waiting.get(key);
        if (entry !== undefined) {
          entry.run = run;
          entry.done.push(resolve);
        } else this.waiting.set(key, { run, done: [resolve] });
      });
    }
    return this.start(key, run);
  }

  private start(key: string, run: () => Promise<void>): Promise<void> {
    const existing = this.inFlight.get(key);
    if (existing !== undefined) return existing;
    const pending = Promise.resolve()
      .then(run)
      .catch(() => {})
      .finally(() => this.inFlight.delete(key));
    this.inFlight.set(key, pending);
    return pending;
  }

  private release(): void {
    this.holdTimer = null;
    const entries = [...this.waiting.entries()];
    this.waiting.clear();
    for (const [key, entry] of entries) {
      void this.start(key, entry.run).then(() => {
        for (const done of entry.done) done();
      });
    }
  }
}
