// Bridge child restarts (P12-27, research 05 §3.24). When the child dies the session it served is
// parked as the recovery target and a new child is started; the next ready frame reopens that same
// stored session. Restarts are rationed: a crash-looping child must not spawn a storm, so only
// RESTART_LIMIT restarts happen at once inside any RESTART_WINDOW_MS; past that the user is told
// once, and further attempts wait on a doubling backoff until a child stays up.

import { type BackoffOptions, backoffDelay, type Clock, systemClock } from "@indus/client";

export const RESTART_LIMIT = 3;
export const RESTART_WINDOW_MS = 60_000;

export const RESTART_LINE = "The agent process stopped; restarting and reopening your chat…";
/** The same moment when the link is the daemon attach (P17-19): the daemon may still run. */
export const ATTACH_RESTART_LINE =
  "Lost the connection to the gateway daemon; reconnecting and reopening your chat…";
export const RESTART_STATUS = "restarting…";
export const GAVE_UP_STATUS = "agent stopped";

/** How the child ended, as far as we know (the fake server knows nothing). */
export interface ExitInfo {
  readonly code: number | null;
  readonly signal: string | null;
  /** The last line the child wrote to stderr. */
  readonly stderrLast: string | null;
}

export interface BudgetPlan {
  /** Restart times still inside the window, plus `now` when this restart is granted. */
  readonly attempts: number[];
  readonly granted: boolean;
}

/** Whether one more immediate restart fits the sliding window. Pure. */
export function planRestart(
  attempts: readonly number[],
  now: number,
  limit: number = RESTART_LIMIT,
  windowMs: number = RESTART_WINDOW_MS,
): BudgetPlan {
  const recent = attempts.filter((at) => now - at < windowMs);
  const granted = recent.length < limit;
  return { attempts: granted ? [...recent, now] : recent, granted };
}

/** The one error line said when the budget is spent. */
export function gaveUpLine(exit: ExitInfo | null): string {
  const how =
    exit === null
      ? "it closed the connection"
      : exit.signal !== null
        ? `killed by ${exit.signal}`
        : `exit code ${exit.code ?? "unknown"}`;
  const tail = exit?.stderrLast?.trim();
  return (
    `error: the agent process keeps stopping (${how}) and could not be restarted; ` +
    `still retrying in the background and your chat will reopen when it is back.` +
    (tail ? `\nlast message: ${tail.slice(0, 300)}` : "")
  );
}

export interface RecoveryOptions {
  /** Start a new child (the client's `start()`). */
  restart(): void;
  /** A transcript line. */
  say(text: string): void;
  /** What a restart says (default `RESTART_LINE`; the attach link says `ATTACH_RESTART_LINE`). */
  restartLine?: string;
  /** The status text. */
  status(text: string): void;
  clock?: Clock;
  limit?: number;
  windowMs?: number;
  backoff?: BackoffOptions;
}

export type LossOutcome = "restarted" | "waiting" | "stopped";

export class Recovery {
  private readonly options: RecoveryOptions;
  private readonly clock: Clock;
  private attempts: number[] = [];
  /** The stored session to reopen on the next ready; kept until a resume succeeds. */
  private pending: string | null = null;
  /** The budget was spent in this outage: say so once, then retry quietly on the backoff. */
  private gaveUp = false;
  private quietTries = 0;
  private timer: unknown = null;
  private stopped = false;

  constructor(options: RecoveryOptions) {
    this.options = options;
    this.clock = options.clock ?? systemClock;
  }

  /** The session the next ready must reopen, if any. */
  get target(): string | null {
    return this.pending;
  }

  /** True between the budget running out and the next ready frame. */
  get outage(): boolean {
    return this.gaveUp;
  }

  /** A background retry is scheduled. */
  get waiting(): boolean {
    return this.timer !== null;
  }

  /**
   * The link to the child is gone. `live` is the session that was open (null when none was);
   * a target left by an earlier loss is kept when nothing newer was open.
   */
  lost(live: string | null, exit: ExitInfo | null): LossOutcome {
    if (this.stopped) return "stopped";
    this.pending = live ?? this.pending;
    if (this.timer !== null) return "waiting";
    if (!this.gaveUp) {
      const plan = planRestart(
        this.attempts,
        this.clock.now(),
        this.options.limit ?? RESTART_LIMIT,
        this.options.windowMs ?? RESTART_WINDOW_MS,
      );
      this.attempts = plan.attempts;
      if (plan.granted) {
        this.options.status(RESTART_STATUS);
        this.options.say(this.options.restartLine ?? RESTART_LINE);
        this.options.restart();
        return "restarted";
      }
      this.gaveUp = true;
      this.options.status(GAVE_UP_STATUS);
      this.options.say(gaveUpLine(exit));
    }
    this.quietTries += 1;
    this.timer = this.clock.setTimeout(
      () => {
        this.timer = null;
        if (!this.stopped) this.options.restart();
      },
      backoffDelay(this.quietTries, this.options.backoff),
    );
    return "waiting";
  }

  /** A child said ready: the outage is over. Returns the session to reopen, if any. */
  ready(): string | null {
    this.gaveUp = false;
    this.quietTries = 0;
    this.cancelTimer();
    return this.pending;
  }

  /** A resume of `id` succeeded (or another session was opened on purpose): drop the target. */
  settled(id: string | null = null): void {
    if (id === null || this.pending === id) this.pending = null;
  }

  /** No more restarts (the app is leaving). */
  stop(): void {
    this.stopped = true;
    this.cancelTimer();
  }

  private cancelTimer(): void {
    if (this.timer !== null) this.clock.clearTimeout(this.timer);
    this.timer = null;
  }
}
