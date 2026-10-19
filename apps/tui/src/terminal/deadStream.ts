// A terminal that went away (tab closed, SSH dropped without SIGHUP) makes every write fail with
// EIO or EPIPE. Five such failures in a row end the TUI with exit 1 instead of a loop that logs
// forever while the bridge keeps running (R05.8).

export const DEAD_STREAM_LIMIT = 5;

export function isDeadStreamError(error: unknown): boolean {
  const code = (error as { code?: unknown } | null)?.code;
  return code === "EIO" || code === "EPIPE";
}

export interface DeadStreamOptions {
  limit?: number;
  /** Called once when the limit is reached. */
  onDead: (code: string) => void;
}

export class DeadStreamGuard {
  private readonly limit: number;
  private readonly onDead: (code: string) => void;
  private streak = 0;
  private fired = false;

  constructor(options: DeadStreamOptions) {
    this.limit = options.limit ?? DEAD_STREAM_LIMIT;
    this.onDead = options.onDead;
  }

  get count(): number {
    return this.streak;
  }

  /** Count an error; true when it was a dead-stream error. Any other error breaks the streak. */
  noteError(error: unknown): boolean {
    if (!isDeadStreamError(error)) {
      this.streak = 0;
      return false;
    }
    this.streak += 1;
    if (this.streak >= this.limit && !this.fired) {
      this.fired = true;
      this.onDead(String((error as { code: string }).code));
    }
    return true;
  }

  /** A write went through: the terminal is alive. */
  noteWrite(): void {
    this.streak = 0;
  }

  /** Count the stream's own `error` events. Returns a detach function. */
  watch(stream: {
    on(event: "error", listener: (error: unknown) => void): unknown;
    off(event: "error", listener: (error: unknown) => void): unknown;
  }): () => void {
    const listener = (error: unknown): void => {
      this.noteError(error);
    };
    stream.on("error", listener);
    return () => stream.off("error", listener);
  }
}
