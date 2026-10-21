// desktop.log: the main process's own log, next to the agent's logs in `<home>/logs/`.
//
// Lines are stamped, redacted and buffered; the buffer is written every 120 ms, or at once when
// it passes 64 KiB, and synchronously at exit. The file rotates at 10 MiB into three numbered
// backups; a file far past the limit (a boot loop's transcript) is dropped instead of kept. A
// write failure is swallowed: the log must never take the main process down.

import fs from "node:fs";
import path from "node:path";
import { redactSecrets } from "./redact.ts";

export const LOG_FLUSH_MS = 120;
export const LOG_BUFFER_MAX_BYTES = 64 * 1024;
export const LOG_ROTATE_BYTES = 10 * 1024 * 1024;
export const LOG_BACKUPS = 3;
/** Past this size the live file and its backups are deleted rather than rotated. */
export const LOG_DISCARD_BYTES = 4 * LOG_ROTATE_BYTES;
/** Lines kept in memory for the logs viewer (P20-39). */
export const LOG_RECENT_LINES = 500;

export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR";

export interface LogFs {
  appendFileSync(file: string, data: string): void;
  statSize(file: string): number | null;
  renameSync(from: string, to: string): void;
  rmSync(file: string): void;
  mkdirSync(dir: string): void;
}

export interface LogTimers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export const nodeLogFs: LogFs = {
  appendFileSync: (file, data) => fs.appendFileSync(file, data, { encoding: "utf8", mode: 0o600 }),
  statSize: (file) => {
    try {
      return fs.statSync(file).size;
    } catch {
      return null;
    }
  },
  renameSync: (from, to) => fs.renameSync(from, to),
  rmSync: (file) => fs.rmSync(file, { force: true }),
  mkdirSync: (dir) => fs.mkdirSync(dir, { recursive: true, mode: 0o700 }),
};

const nodeTimers: LogTimers = {
  setTimeout: (fn, ms) => {
    const handle = setTimeout(fn, ms);
    handle.unref?.();
    return handle;
  },
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** Ordered file steps that bring the log at `file` back under the limit; empty when it is. */
export function rotationSteps(
  size: number,
  file: string,
): Array<["rm", string] | ["mv", string, string]> {
  if (size < LOG_ROTATE_BYTES) return [];
  const backup = (n: number) => `${file}.${n}`;
  if (size > LOG_DISCARD_BYTES) {
    return [file, ...Array.from({ length: LOG_BACKUPS }, (_, i) => backup(i + 1))].map(
      (p) => ["rm", p] as ["rm", string],
    );
  }
  const steps: Array<["rm", string] | ["mv", string, string]> = [["rm", backup(LOG_BACKUPS)]];
  for (let n = LOG_BACKUPS - 1; n >= 1; n -= 1) steps.push(["mv", backup(n), backup(n + 1)]);
  steps.push(["mv", file, backup(1)]);
  return steps;
}

const two = (n: number) => String(n).padStart(2, "0");

/** `2026-10-21 09:15:03.042` in local time, the shape the Python logs use (with a dot). */
export function logStamp(date: Date): string {
  const day = `${date.getFullYear()}-${two(date.getMonth() + 1)}-${two(date.getDate())}`;
  const time = `${two(date.getHours())}:${two(date.getMinutes())}:${two(date.getSeconds())}`;
  return `${day} ${time}.${String(date.getMilliseconds()).padStart(3, "0")}`;
}

export interface DesktopLogOptions {
  file: string;
  fs?: LogFs;
  timers?: LogTimers;
  now?: () => Date;
  /** Mirror lines to stderr (dev builds). */
  echo?: (line: string) => void;
}

export class DesktopLog {
  readonly file: string;
  private readonly fs: LogFs;
  private readonly timers: LogTimers;
  private readonly now: () => Date;
  private readonly echo: ((line: string) => void) | undefined;
  private buffer: string[] = [];
  private bufferBytes = 0;
  private timer: unknown = null;
  private readonly recentLines: string[] = [];
  private dirReady = false;
  private closed = false;

  constructor(options: DesktopLogOptions) {
    this.file = options.file;
    this.fs = options.fs ?? nodeLogFs;
    this.timers = options.timers ?? nodeTimers;
    this.now = options.now ?? (() => new Date());
    this.echo = options.echo;
  }

  /**
   * Log one message (multi-line text keeps one stamp per line). Never throws. Lines read
   * `<stamp> <LEVEL> desktop: <text>`, the shape `indus logs` parses for the Python logs.
   */
  write(message: string, level: LogLevel = "INFO"): void {
    if (this.closed) return;
    const stamp = logStamp(this.now());
    for (const part of redactSecrets(String(message)).split(/\r?\n/)) {
      if (part === "") continue;
      const line = `${stamp} ${level} desktop: ${part}`;
      this.recentLines.push(line);
      if (this.recentLines.length > LOG_RECENT_LINES) this.recentLines.shift();
      this.echo?.(line);
      this.buffer.push(`${line}\n`);
      this.bufferBytes += Buffer.byteLength(line) + 1;
    }
    if (this.bufferBytes >= LOG_BUFFER_MAX_BYTES) {
      this.flush();
    } else if (this.timer === null && this.buffer.length > 0) {
      this.timer = this.timers.setTimeout(() => {
        this.timer = null;
        this.flush();
      }, LOG_FLUSH_MS);
    }
  }

  /** The last lines written in this process, oldest first (already redacted). */
  recent(limit = LOG_RECENT_LINES): string[] {
    return this.recentLines.slice(-limit);
  }

  /** Write the buffer now. Called by the timer, by a full buffer and at exit. */
  flush(): void {
    if (this.timer !== null) {
      this.timers.clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.buffer.length === 0) return;
    const data = this.buffer.join("");
    this.buffer = [];
    this.bufferBytes = 0;
    try {
      if (!this.dirReady) {
        this.fs.mkdirSync(path.dirname(this.file));
        this.dirReady = true;
      }
      this.rotateIfNeeded();
      this.fs.appendFileSync(this.file, data);
    } catch {
      // A full disk or a read-only home must not crash the app; the lines are dropped.
    }
  }

  /** Flush and stop accepting lines (before-quit). */
  close(): void {
    this.flush();
    this.closed = true;
  }

  private rotateIfNeeded(): void {
    const size = this.fs.statSize(this.file);
    if (size === null) return;
    for (const step of rotationSteps(size, this.file)) {
      try {
        if (step[0] === "rm") this.fs.rmSync(step[1]);
        else if (this.fs.statSize(step[1]) !== null) this.fs.renameSync(step[1], step[2]);
      } catch {
        // A missing link in the chain does not stop the rest.
      }
    }
  }
}
