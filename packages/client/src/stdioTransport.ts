// Node-only: run the bridge as a child process and speak NDJSON JSON-RPC over its pipes.

import { type ChildProcess, spawn } from "node:child_process";
import type { CloseInfo, OutboundFrame, Transport, Unsubscribe } from "./channel.ts";
import { type Clock, systemClock, type TimerHandle } from "./clock.ts";
import {
  CLOSE_GRACE_MS,
  START_TIMEOUT_TAIL_LINES,
  STARTUP_TIMEOUT_ENV,
  startupTimeoutFromEnv,
} from "./limits.ts";
import { READY_METHOD, type TransportEvent } from "./localEvents.ts";
import { LogRing } from "./logRing.ts";
import { encodeFrame, LineSplitter, parseFrameLine, preview } from "./ndjson.ts";

export interface StdioTransportOptions {
  /** Program to run; default `indus`. */
  command?: string;
  /** Arguments; default `tui-bridge --stdio`. */
  args?: readonly string[];
  cwd?: string;
  env?: NodeJS.ProcessEnv;
  /** Ready deadline; default from `INDUS_TUI_STARTUP_TIMEOUT_MS` (floor 5,000 ms) or 15,000. */
  startupTimeoutMs?: number;
  /** How long `close()` waits after ending stdin before it kills the process group. */
  closeGraceMs?: number;
  clock?: Clock;
  /** Shared log ring (stderr and lifecycle lines); one is made when absent. */
  logs?: LogRing;
  /** Synthetic `transport.*` events. */
  onEvent?: (event: TransportEvent) => void;
}

export const DEFAULT_COMMAND = "indus";
/** After `exit`, how long to wait for stdout to drain before reporting the exit anyway. */
const EXIT_DRAIN_MS = 1_000;
export const DEFAULT_ARGS: readonly string[] = ["tui-bridge", "--stdio"];

type Listener<T> = (value: T) => void;

export class StdioTransport implements Transport {
  readonly logs: LogRing;
  private readonly child: ChildProcess;
  private readonly options: StdioTransportOptions;
  private readonly clock: Clock;
  private readonly frameListeners = new Set<Listener<unknown>>();
  private readonly closeListeners = new Set<Listener<CloseInfo>>();
  private readonly stdout: LineSplitter;
  private readonly stderr: LineSplitter;
  private readonly commandLine: string;
  private readonly cwd: string;
  private startupTimer: TimerHandle | undefined;
  private lastStderr: string | null = null;
  private exited = false;
  private closing: Promise<void> | null = null;
  private readonly exitWaiters: Array<() => void> = [];

  constructor(options: StdioTransportOptions = {}) {
    this.options = options;
    this.clock = options.clock ?? systemClock;
    this.logs = options.logs ?? new LogRing();
    const command = options.command ?? DEFAULT_COMMAND;
    const args = [...(options.args ?? DEFAULT_ARGS)];
    this.commandLine = [command, ...args].join(" ");
    this.cwd = options.cwd ?? process.cwd();
    this.stdout = new LineSplitter((line) => this.onStdoutLine(line), {
      onOversize: (bytes, start) => this.protocolError(`line of ${bytes} bytes dropped: ${start}`),
    });
    this.stderr = new LineSplitter((line) => this.onStderrLine(line));

    this.child = spawn(command, args, {
      cwd: this.cwd,
      env: options.env ?? process.env,
      stdio: ["pipe", "pipe", "pipe"],
      // Own process group on POSIX so close() can take grandchildren down too.
      detached: process.platform !== "win32",
      windowsHide: true,
    });
    this.log(`[lifecycle] started ${this.commandLine} (pid ${this.child.pid ?? "none"})`);
    this.child.stdout?.on("data", (chunk: Buffer) => this.stdout.push(chunk));
    this.child.stdout?.on("end", () => this.stdout.end());
    this.child.stderr?.on("data", (chunk: Buffer) => this.stderr.push(chunk));
    this.child.stderr?.on("end", () => this.stderr.end());
    // A write after the child died raises EPIPE here; send() reports it to the caller.
    this.child.stdin?.on("error", (error) => this.log(`[lifecycle] stdin: ${error.message}`));
    this.child.on("error", (error) => {
      this.log(`[lifecycle] could not run ${this.commandLine}: ${error.message}`);
      if (this.child.pid === undefined) this.onExit(null, null);
    });
    // `close` comes after stdout is drained; a grandchild holding the pipe open would delay it
    // forever, so `exit` plus a short wait settles it too.
    this.child.on("close", (code, signal) => this.onExit(code, signal));
    this.child.on("exit", (code, signal) => {
      this.clock.setTimeout(() => this.onExit(code, signal), EXIT_DRAIN_MS);
    });

    const timeout =
      options.startupTimeoutMs ?? startupTimeoutFromEnv(process.env[STARTUP_TIMEOUT_ENV]);
    this.startupTimer = this.clock.setTimeout(() => this.onStartupTimeout(), timeout);
  }

  get pid(): number | undefined {
    return this.child.pid;
  }

  get alive(): boolean {
    return !this.exited;
  }

  send(frame: OutboundFrame): void {
    const stdin = this.child.stdin;
    if (this.exited || stdin === null || stdin.destroyed || !stdin.writable) {
      throw new Error("the bridge process is not running");
    }
    stdin.write(encodeFrame(frame));
  }

  onFrame(listener: Listener<unknown>): Unsubscribe {
    this.frameListeners.add(listener);
    return () => this.frameListeners.delete(listener);
  }

  onClose(listener: Listener<CloseInfo>): Unsubscribe {
    this.closeListeners.add(listener);
    return () => this.closeListeners.delete(listener);
  }

  /** End stdin, wait the grace period, then kill the whole process group. Idempotent. */
  close(): Promise<void> {
    if (this.closing) return this.closing;
    this.clearStartupTimer();
    this.closing = new Promise<void>((resolve) => {
      if (this.exited) {
        resolve();
        return;
      }
      this.exitWaiters.push(resolve);
      this.log("[lifecycle] closing: stdin ended");
      this.child.stdin?.end();
      const grace = this.options.closeGraceMs ?? CLOSE_GRACE_MS;
      this.clock.setTimeout(() => {
        if (this.exited) return;
        this.log(`[lifecycle] still running after ${grace} ms; killing the process tree`);
        this.killTree();
      }, grace);
    });
    return this.closing;
  }

  private killTree(): void {
    const pid = this.child.pid;
    if (pid === undefined) return;
    if (process.platform === "win32") {
      spawn("taskkill", ["/pid", String(pid), "/T", "/F"], { stdio: "ignore", windowsHide: true });
      return;
    }
    try {
      process.kill(-pid, "SIGKILL");
    } catch {
      try {
        this.child.kill("SIGKILL");
      } catch {
        // already gone
      }
    }
  }

  private onStdoutLine(line: string): void {
    const parsed = parseFrameLine(line);
    if (!parsed.ok) {
      this.protocolError(parsed.preview);
      return;
    }
    if (parsed.frame.method === READY_METHOD) this.clearStartupTimer();
    for (const listener of [...this.frameListeners]) listener(parsed.frame);
  }

  private onStderrLine(line: string): void {
    this.lastStderr = line;
    this.log(line);
    this.emit({ type: "transport.stderr", line });
  }

  private protocolError(text: string): void {
    const shown = preview(text);
    this.log(`[protocol] not a frame: ${shown}`);
    this.emit({ type: "transport.protocol_error", preview: shown });
  }

  private onStartupTimeout(): void {
    this.startupTimer = undefined;
    if (this.exited) return;
    const tail = this.logs.tail(START_TIMEOUT_TAIL_LINES);
    this.log(`[lifecycle] no ${READY_METHOD} yet; still waiting`);
    this.emit({
      type: "transport.start_timeout",
      python: this.commandLine,
      cwd: this.cwd,
      stderr_tail: tail,
    });
  }

  private onExit(code: number | null, signal: NodeJS.Signals | null): void {
    if (this.exited) return;
    this.exited = true;
    this.clearStartupTimer();
    this.log(`[lifecycle] exited code=${code ?? "none"} signal=${signal ?? "none"}`);
    this.emit({ type: "transport.exit", code, signal, stderr_last: this.lastStderr });
    for (const listener of [...this.closeListeners]) listener({ reason: "exit" });
    for (const resolve of this.exitWaiters.splice(0)) resolve();
  }

  private clearStartupTimer(): void {
    if (this.startupTimer !== undefined) this.clock.clearTimeout(this.startupTimer);
    this.startupTimer = undefined;
  }

  private emit(event: TransportEvent): void {
    this.options.onEvent?.(event);
  }

  private log(line: string): void {
    this.logs.push(line);
  }
}

/** Spawn the bridge child (or any NDJSON peer) and return its transport. */
export function spawnStdioTransport(options: StdioTransportOptions = {}): StdioTransport {
  return new StdioTransport(options);
}
