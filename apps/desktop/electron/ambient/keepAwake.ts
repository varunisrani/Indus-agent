// Keep awake (P20-36). Main owns one power-save blocker (`prevent-app-suspension`: the computer
// does not go to sleep, the display may still dim). Modes: never, while working (the default) and
// always. "While working" holds the blocker exactly while some window reports a running turn and
// lets go when the last one ends — one start per working period.
//
// The one-time offer: when the machine went to sleep after at least 55 s without input (a timed
// sleep, the kind keep awake prevents — a closed lid is not) while a turn ran and keep awake was
// off, the next wake offers "keep awake while working", once per device.

import type { KeepAwakeMode } from "./settings.ts";

/** A sleep after at least this much idle time is a timed sleep. */
export const TIMED_SLEEP_IDLE_SECONDS = 55;

export interface PowerSaveBlockerLike {
  start(type: "prevent-app-suspension" | "prevent-display-sleep"): number;
  stop(id: number): void;
  isStarted(id: number): boolean;
}

export interface KeepAwakeDeps {
  blocker: PowerSaveBlockerLike;
  mode(): KeepAwakeMode;
  offered(): boolean;
  markOffered(): void;
  /** Show the offer in a main window; false when none can show it now. */
  offer(): boolean;
  log(line: string): void;
}

export function blockerWanted(mode: KeepAwakeMode, working: boolean): boolean {
  return mode === "always" || (mode === "while-working" && working);
}

export class KeepAwake {
  private readonly deps: KeepAwakeDeps;
  private id: number | null = null;
  private working = false;
  private sleptMidTurn = false;

  constructor(deps: KeepAwakeDeps) {
    this.deps = deps;
  }

  /** The blocker is held right now. */
  active(): boolean {
    return this.id !== null && this.deps.blocker.isStarted(this.id);
  }

  /** The windows' merged work picture changed. */
  setWorking(working: boolean): void {
    this.working = working;
    this.reconcile();
  }

  /** The mode changed (settings) or the app just started. */
  reconcile(): void {
    const wanted = blockerWanted(this.deps.mode(), this.working);
    if (wanted && !this.active()) {
      try {
        this.id = this.deps.blocker.start("prevent-app-suspension");
        this.deps.log("[keep-awake] holding the computer awake");
      } catch {
        this.id = null;
        this.deps.log("[keep-awake] the power-save blocker could not start");
      }
    } else if (!wanted && this.id !== null) {
      const id = this.id;
      this.id = null;
      try {
        if (this.deps.blocker.isStarted(id)) this.deps.blocker.stop(id);
      } catch {
        // Already gone.
      }
      this.deps.log("[keep-awake] released");
    }
  }

  /** The system is about to sleep after `idleSeconds` without input. */
  noteSuspend(idleSeconds: number): void {
    this.sleptMidTurn =
      this.deps.mode() === "never" && this.working && idleSeconds >= TIMED_SLEEP_IDLE_SECONDS;
  }

  /** The system woke up: make the one-time offer when a timed sleep interrupted a turn. */
  noteResume(): void {
    if (this.sleptMidTurn && !this.deps.offered() && this.deps.mode() === "never") {
      if (this.deps.offer()) {
        this.deps.markOffered();
        this.sleptMidTurn = false;
        this.deps.log("[keep-awake] offered keeping the computer awake while working");
      }
      // No window could show it: keep the flag for the next wake.
      return;
    }
    this.sleptMidTurn = false;
  }

  dispose(): void {
    this.working = false;
    if (this.id !== null) {
      try {
        if (this.deps.blocker.isStarted(this.id)) this.deps.blocker.stop(this.id);
      } catch {
        // Already gone.
      }
      this.id = null;
    }
  }
}
