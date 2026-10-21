// Supervision of a local daemon this app started (P20-13). The daemon is detached — not our
// child — so its liveness is an `identify` answer on its control socket, asked every 2 s. When it
// stops answering after ready, it is ensured again (which starts a fresh one), readiness is waited
// for (≤ 180 s, a probe every 500 ms, each ≤ 5 s) and the windows hear "stopped working in the
// background". After 3 restarts within 2 minutes nothing is respawned and the windows hear "keeps
// crashing" with the exit code (when the daemon recorded one) and its last output lines. A daemon
// this app only attached to is never restarted by it.

export const WATCH_INTERVAL_MS = 2_000;
export const RESTART_LIMIT = 3;
export const RESTART_WINDOW_MS = 120_000;
export const READY_TIMEOUT_MS = 180_000;
export const READY_POLL_MS = 500;
export const READY_PROBE_TIMEOUT_MS = 5_000;

export type BackendNotice =
  | { kind: "restarted"; message: string }
  | { kind: "crashing"; message: string; exitCode: number | null; recentOutput: string[] };

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface SupervisorDeps {
  /** One liveness probe (the caller bounds it by `READY_PROBE_TIMEOUT_MS`). */
  alive(): Promise<boolean>;
  /** Start the daemon again (`gateway ensure`). */
  respawn(): Promise<void>;
  /** What the dead daemon left: its exit code if recorded, and the tail of its output (redacted). */
  describeExit(): Promise<{ exitCode: number | null; recentOutput: string[] }>;
  notify(notice: BackendNotice): void;
  timers: Timers;
  now: () => number;
  log(line: string): void;
}

export class DaemonSupervisor {
  private readonly deps: SupervisorDeps;
  private timer: unknown = null;
  private watching = false;
  private restarts: number[] = [];
  private gaveUp = false;

  constructor(deps: SupervisorDeps) {
    this.deps = deps;
  }

  active(): boolean {
    return this.watching;
  }

  /** Begin watching after ready; an attached-only daemon is left alone. */
  watch(startedByApp: boolean): void {
    this.stop();
    if (!startedByApp) {
      this.deps.log("[supervisor] attached to a daemon this app did not start; not supervising it");
      return;
    }
    this.gaveUp = false;
    this.watching = true;
    this.schedule();
  }

  stop(): void {
    this.watching = false;
    if (this.timer !== null) this.deps.timers.clearTimeout(this.timer);
    this.timer = null;
  }

  private schedule(): void {
    if (!this.watching) return;
    this.timer = this.deps.timers.setTimeout(() => {
      this.timer = null;
      void this.tick();
    }, WATCH_INTERVAL_MS);
  }

  private async tick(): Promise<void> {
    if (!this.watching) return;
    let alive = false;
    try {
      alive = await this.deps.alive();
    } catch {
      alive = false;
    }
    if (!this.watching) return;
    if (alive) {
      this.schedule();
      return;
    }
    await this.exited();
  }

  private async exited(): Promise<void> {
    const now = this.deps.now();
    this.restarts = this.restarts.filter((at) => now - at < RESTART_WINDOW_MS);
    const exit = await this.deps.describeExit().catch(() => ({ exitCode: null, recentOutput: [] }));
    if (this.restarts.length >= RESTART_LIMIT) {
      this.giveUp(exit, `it stopped ${RESTART_LIMIT + 1} times within two minutes`);
      return;
    }
    this.restarts.push(now);
    this.deps.log(
      `[supervisor] the daemon stopped (exit ${exit.exitCode ?? "unknown"}); restart ${this.restarts.length}/${RESTART_LIMIT}`,
    );
    try {
      await this.deps.respawn();
      if (!(await this.waitReady())) {
        this.giveUp(exit, "it did not become ready again within three minutes");
        return;
      }
    } catch (error) {
      this.giveUp(exit, error instanceof Error ? error.message : String(error));
      return;
    }
    if (!this.watching) return;
    this.deps.notify({
      kind: "restarted",
      message: "Indus stopped working in the background and was restarted.",
    });
    this.schedule();
  }

  private giveUp(exit: { exitCode: number | null; recentOutput: string[] }, why: string): void {
    this.gaveUp = true;
    this.stop();
    this.deps.log(`[supervisor] not restarting the daemon again: ${why}`);
    this.deps.notify({
      kind: "crashing",
      message: "Indus keeps crashing in the background, so it was not restarted again.",
      exitCode: exit.exitCode,
      recentOutput: exit.recentOutput,
    });
  }

  stoppedRespawning(): boolean {
    return this.gaveUp;
  }

  /** Poll until a probe answers, every 500 ms, for at most 180 s. */
  private waitReady(): Promise<boolean> {
    const deadline = this.deps.now() + READY_TIMEOUT_MS;
    return new Promise((resolve) => {
      const probe = async () => {
        let alive = false;
        try {
          alive = await this.deps.alive();
        } catch {
          alive = false;
        }
        if (alive) resolve(true);
        else if (this.deps.now() + READY_POLL_MS > deadline || !this.watching) resolve(false);
        else this.deps.timers.setTimeout(() => void probe(), READY_POLL_MS);
      };
      void probe();
    });
  }
}

/** `promise`, or `fallback` after `ms` (a probe never waits longer than its budget). */
export function withTimeout<T>(
  promise: Promise<T>,
  ms: number,
  fallback: T,
  timers: Timers,
): Promise<T> {
  return new Promise((resolve) => {
    const handle = timers.setTimeout(() => resolve(fallback), ms);
    promise.then(
      (value) => {
        timers.clearTimeout(handle);
        resolve(value);
      },
      () => {
        timers.clearTimeout(handle);
        resolve(fallback);
      },
    );
  });
}
