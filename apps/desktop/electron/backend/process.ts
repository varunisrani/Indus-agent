// One bounded child runner for the short `indus` commands main runs (`--version`, `gateway ensure`,
// `serve ensure`, `gateway stop`). The answer comes when the process exits, not when its pipes
// close: a daemon the command started may inherit nothing from us (it is detached), but a broken
// launcher could leave a grandchild on our pipes, so the pipes get a short grace after the exit.
// A command past its deadline is sent SIGTERM, then SIGKILL a second later.

import { spawn } from "node:child_process";

export const KILL_GRACE_MS = 1_000;
export const PIPE_GRACE_MS = 1_000;
const MAX_STDOUT = 256 * 1024;
const MAX_STDERR_TAIL = 8 * 1024;

/** How to start `indus`: the program and the arguments that come before every subcommand. */
export interface CommandSpec {
  readonly command: string;
  readonly args: readonly string[];
}

export interface RunOptions {
  env?: Record<string, string | undefined>;
  cwd?: string;
  timeoutMs: number;
}

export interface RunResult {
  /** Exit code; null when the process was killed by a signal or never started. */
  code: number | null;
  signal: string | null;
  stdout: string;
  /** The last few KiB of stderr. */
  stderr: string;
  timedOut: boolean;
  /** The program could not be started at all (ENOENT, EACCES). */
  spawnError: string | null;
}

export type CommandRunner = (
  spec: CommandSpec,
  args: readonly string[],
  options: RunOptions,
) => Promise<RunResult>;

export const runCommand: CommandRunner = (spec, args, options) =>
  new Promise((resolve) => {
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    let settled = false;
    let exit: { code: number | null; signal: string | null } | null = null;
    let open = 2;
    const timers: ReturnType<typeof setTimeout>[] = [];
    const finish = (spawnError: string | null = null) => {
      if (settled) return;
      settled = true;
      for (const timer of timers) clearTimeout(timer);
      child.stdout?.destroy();
      child.stderr?.destroy();
      resolve({
        code: exit?.code ?? null,
        signal: exit?.signal ?? null,
        stdout,
        stderr,
        timedOut,
        spawnError,
      });
    };
    const child = spawn(spec.command, [...spec.args, ...args], {
      env: options.env ?? process.env,
      ...(options.cwd === undefined ? {} : { cwd: options.cwd }),
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    child.stdout?.setEncoding("utf8");
    child.stderr?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => {
      if (stdout.length < MAX_STDOUT) stdout += chunk.slice(0, MAX_STDOUT - stdout.length);
    });
    child.stderr?.on("data", (chunk: string) => {
      stderr = (stderr + chunk).slice(-MAX_STDERR_TAIL);
    });
    const ended = () => {
      open -= 1;
      if (open === 0 && exit !== null) finish();
    };
    child.stdout?.on("end", ended);
    child.stderr?.on("end", ended);
    child.on("error", (error: NodeJS.ErrnoException) => {
      if (exit === null) finish(error.code ?? error.message);
    });
    child.on("exit", (code, signal) => {
      exit = { code, signal };
      if (open <= 0) finish();
      else timers.push(setTimeout(() => finish(), PIPE_GRACE_MS));
    });
    timers.push(
      setTimeout(() => {
        timedOut = true;
        child.kill("SIGTERM");
        timers.push(
          setTimeout(() => {
            child.kill("SIGKILL");
            timers.push(setTimeout(() => finish(), PIPE_GRACE_MS));
          }, KILL_GRACE_MS),
        );
      }, options.timeoutMs),
    );
  });

/** The last non-empty line of `text`, for error messages. */
export function lastLine(text: string): string {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== "");
  return lines.at(-1) ?? "";
}
