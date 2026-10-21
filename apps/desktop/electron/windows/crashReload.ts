// A crashed renderer is reloaded, but never in a loop: at most 3 reloads in any 60,000 ms. The
// fourth crash inside the window leaves the window on the crash (and logs it) instead of
// spinning; once the oldest reload leaves the window, a crash reloads again.

export const CRASH_RELOAD_MAX = 3;
export const CRASH_RELOAD_WINDOW_MS = 60_000;

export class CrashReloadBudget {
  private readonly times: number[] = [];
  private readonly now: () => number;
  private readonly max: number;
  private readonly windowMs: number;

  constructor(
    now: () => number = Date.now,
    max = CRASH_RELOAD_MAX,
    windowMs = CRASH_RELOAD_WINDOW_MS,
  ) {
    this.now = now;
    this.max = max;
    this.windowMs = windowMs;
  }

  /** True (and the reload is counted) when another reload fits in the window. */
  allow(): boolean {
    const at = this.now();
    while (this.times.length > 0 && at - (this.times[0] ?? at) >= this.windowMs) this.times.shift();
    if (this.times.length >= this.max) return false;
    this.times.push(at);
    return true;
  }
}

/** `render-process-gone` reasons that are not crashes: the renderer ended on purpose. */
export function isIntentionalExit(reason: string): boolean {
  return reason === "clean-exit";
}
