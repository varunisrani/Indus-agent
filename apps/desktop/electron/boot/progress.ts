// The boot progress every window shows (P20-12): one value in main, pushed on
// `indus:boot:progress` to every window and readable with `indus:boot:get`. Progress never goes
// down, except on an error and on the first step after one (a new attempt starts low again).

export type BootPhase =
  | "resolve"
  | "runtime"
  | "remote-connect"
  | "update-wait"
  | "first-run"
  | "ready"
  | "error";

export interface BootProgress {
  phase: BootPhase;
  message: string;
  progress: number;
  /** False only on an error and while the first-run choice waits for the person. */
  running: boolean;
  error?: string;
  retryable?: boolean;
  statusCode?: number;
  /** A local failure latched in this process (Retry does not re-run the installer). */
  latched?: boolean;
}

/** Where each phase puts the bar. */
export const PHASE_PROGRESS = {
  resolve: 8,
  "remote-connect": 24,
  runtime: 28,
  ready: 94,
} as const;

export const INITIAL_PROGRESS: BootProgress = {
  phase: "resolve",
  message: "Starting",
  progress: 0,
  running: false,
};

export class ProgressBoard {
  private value: BootProgress = INITIAL_PROGRESS;
  private afterError = false;
  private readonly listeners = new Set<(progress: BootProgress) => void>();

  current(): BootProgress {
    return this.value;
  }

  onChange(listener: (progress: BootProgress) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Publish `next`; its progress is raised to the current one unless decreasing is allowed. */
  update(next: BootProgress): BootProgress {
    const isError = next.phase === "error";
    const mayDecrease = isError || this.afterError;
    const progress = Math.max(0, Math.min(100, next.progress));
    this.value = {
      ...next,
      progress: mayDecrease ? progress : Math.max(this.value.progress, progress),
    };
    this.afterError = isError;
    for (const listener of this.listeners) listener(this.value);
    return this.value;
  }
}
