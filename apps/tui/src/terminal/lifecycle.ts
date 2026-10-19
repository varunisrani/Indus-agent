// One owner for every way the TUI ends: normal exit, SIGTERM, SIGHUP, an uncaught error, an
// unhandled rejection, /quit, Ctrl+D, Ctrl+C, an update request, a dead terminal and the
// memory-critical exit. Each path unmounts Ink first (else it repaints its last frame onto the
// main screen), writes the mode reset synchronously, stops the bridge child, then exits.

import { type ResetTarget, writeModeReset } from "./modes.ts";

export const EXIT_OK = 0;
export const EXIT_DEAD_TERMINAL = 1;
export const EXIT_UPDATE = 42;
export const EXIT_CTRL_C = 130;
export const EXIT_MEMORY = 137;

const SIGNAL_CODES: Readonly<Record<string, number>> = { SIGHUP: 129, SIGINT: 130, SIGTERM: 143 };

export type ExitReason =
  | "quit"
  | "ctrl-d"
  | "ctrl-c"
  | "update"
  | "dead-terminal"
  | "memory"
  | "signal"
  | "uncaught"
  | "rejection"
  | "exit";

interface ProcessLike {
  on(event: string, listener: (...args: unknown[]) => void): unknown;
  exit(code?: number): never | undefined;
}

export interface LifecycleOptions {
  out?: ResetTarget;
  proc?: ProcessLike;
  /** One diagnostic line (stderr by default; it may be the dead stream, so failures are ignored). */
  log?: (line: string) => void;
  /** Signals to ignore (e.g. SIGINT when embedded). */
  ignoredSignals?: readonly string[];
}

export class Lifecycle {
  private readonly out: ResetTarget;
  private readonly proc: ProcessLike;
  private readonly log: (line: string) => void;
  private readonly ignored: ReadonlySet<string>;
  private unmount: (() => void) | null = null;
  private stopBridge: (() => void) | null = null;
  private resetDone = false;
  private leaving = false;
  /** Why the process is ending (for the exit hook and tests). */
  reason: ExitReason | null = null;

  constructor(options: LifecycleOptions = {}) {
    this.out = options.out ?? process.stdout;
    this.proc = options.proc ?? (process as unknown as ProcessLike);
    this.log =
      options.log ??
      ((line) => {
        try {
          process.stderr.write(`${line}\n`);
        } catch {
          // stderr may be the dead terminal itself
        }
      });
    this.ignored = new Set(options.ignoredSignals ?? []);
  }

  /** Ink's unmount, set once the app is rendered. */
  setUnmount(unmount: (() => void) | null): void {
    this.unmount = unmount;
  }

  /** Stops the bridge child (synchronous signal; the child also sees stdin close). */
  setBridgeStopper(stop: (() => void) | null): void {
    this.stopBridge = stop;
  }

  /** Reset at most once per process (start-up reset excluded). */
  resetModes(): boolean {
    if (this.resetDone) return false;
    this.resetDone = true;
    return writeModeReset(this.out);
  }

  /** Hook the process-level paths. Call once, after the start-up reset. */
  install(): void {
    this.proc.on("exit", () => {
      this.reason ??= "exit";
      this.teardown();
    });
    for (const signal of ["SIGTERM", "SIGHUP", "SIGINT"]) {
      this.proc.on(signal, () => {
        if (this.ignored.has(signal)) return;
        // Raw mode turns Ctrl+C into a key; a real SIGINT means raw mode dropped: leave as Ctrl+C.
        if (signal === "SIGINT") {
          this.quit(EXIT_CTRL_C, "signal");
          return;
        }
        this.log(`indus tui: received ${signal}`);
        this.quit(SIGNAL_CODES[signal] ?? 1, "signal");
      });
    }
    this.proc.on("uncaughtException", (error) => {
      this.log(`indus tui: uncaught error: ${describe(error)}`);
      this.quit(1, "uncaught");
    });
    this.proc.on("unhandledRejection", (error) => {
      this.log(`indus tui: unhandled rejection: ${describe(error)}`);
      this.quit(1, "rejection");
    });
  }

  /** Leave now with `code`. A second call while leaving is ignored. */
  quit(code: number, reason: ExitReason = "quit"): void {
    if (this.leaving) return;
    this.leaving = true;
    this.reason = reason;
    this.teardown();
    this.proc.exit(code);
  }

  private teardown(): void {
    const unmount = this.unmount;
    this.unmount = null;
    try {
      unmount?.();
    } catch {
      // the terminal may be gone; the reset below still runs
    }
    this.resetModes();
    const stop = this.stopBridge;
    this.stopBridge = null;
    try {
      stop?.();
    } catch {
      // best effort: the child also sees its stdin close when we exit
    }
  }
}

function describe(error: unknown): string {
  if (error instanceof Error) return `${error.name}: ${error.message}`.slice(0, 400);
  return String(error).slice(0, 400);
}

export const NOT_A_TERMINAL_LINE =
  "indus --tui needs an interactive terminal; run `indus` for the classic chat or pipe into `indus -q`.";

/** Without a TTY on stdin: one line on stdout and exit 0 (the caller exits). */
export function refuseWithoutTty(
  stdin: { isTTY?: boolean },
  write: (line: string) => void,
): boolean {
  if (stdin.isTTY === true) return false;
  write(`${NOT_A_TERMINAL_LINE}\n`);
  return true;
}
