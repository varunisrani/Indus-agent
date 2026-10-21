// GUI launches (Dock, Finder, a desktop launcher) inherit a minimal PATH because no shell profile
// ran, so CLIs the user installed (Homebrew, ~/.local/bin, version managers) are invisible to the
// backend and to MCP servers it starts (R08.16). On ready we ask the user's login shell for its
// PATH once and merge it in: login-shell entries first, entries only the inherited PATH had kept
// at the end, duplicates and empties dropped.
//
// The whole merge has one deadline (3 s). The shell runs with no stdin, in its own process group
// (killed at the deadline, grandchildren included), and its PATH is read between two random
// markers so profile banners cannot corrupt it. A shell that fails, prints nothing usable or hangs
// leaves PATH exactly as it was: a broken profile never blocks boot.

import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";

export const SHELL_PATH_DEADLINE_MS = 3_000;

export interface ShellRunResult {
  stdout: string;
}

/** Run `shell args...` and resolve its stdout, or null on error or when `timeoutMs` passes. */
export type ShellRunner = (
  shell: string,
  args: string[],
  timeoutMs: number,
) => Promise<ShellRunResult | null>;

export interface ShellPathOptions {
  env: Record<string, string | undefined>;
  platform: NodeJS.Platform | string;
  run?: ShellRunner;
  deadlineMs?: number;
  now?: () => number;
}

export type ShellPathOutcome =
  | { applied: true; path: string }
  | { applied: false; reason: "win32" | "no-answer" | "unchanged" };

export function defaultShell(env: Record<string, string | undefined>, platform: string): string {
  const shell = (env.SHELL ?? "").trim();
  if (shell !== "") return shell;
  return platform === "darwin" ? "/bin/zsh" : "/bin/sh";
}

/** Text between the last `start` marker and the `end` marker after it, or null. */
export function betweenMarkers(stdout: string, start: string, end: string): string | null {
  const from = stdout.lastIndexOf(start);
  if (from < 0) return null;
  const valueStart = from + start.length;
  const to = stdout.indexOf(end, valueStart);
  if (to < 0) return null;
  const value = stdout.slice(valueStart, to).trim();
  return value === "" ? null : value;
}

/** Join PATH lists in order, dropping empty and repeated entries. */
export function mergePathLists(
  lists: ReadonlyArray<string | undefined>,
  delimiter: string,
): string {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const list of lists) {
    for (const entry of String(list ?? "").split(delimiter)) {
      const item = entry.trim();
      if (item === "" || seen.has(item)) continue;
      seen.add(item);
      out.push(item);
    }
  }
  return out.join(delimiter);
}

export const spawnShell: ShellRunner = (shell, args, timeoutMs) =>
  new Promise((resolve) => {
    let stdout = "";
    let done = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = (value: ShellRunResult | null) => {
      if (done) return;
      done = true;
      if (timer !== undefined) clearTimeout(timer);
      resolve(value);
    };
    let child: ReturnType<typeof spawn>;
    try {
      child = spawn(shell, args, {
        stdio: ["ignore", "pipe", "ignore"],
        detached: true,
        windowsHide: true,
      });
    } catch {
      finish(null);
      return;
    }
    const killGroup = () => {
      try {
        if (child.pid !== undefined) process.kill(-child.pid, "SIGKILL");
      } catch {
        try {
          child.kill("SIGKILL");
        } catch {
          // already gone
        }
      }
    };
    timer = setTimeout(() => {
      killGroup();
      finish(null);
    }, timeoutMs);
    child.stdout?.setEncoding("utf8");
    child.stdout?.on("data", (chunk: string) => {
      if (stdout.length < 256 * 1024) stdout += chunk;
    });
    child.on("error", () => finish(null));
    // A profile may exit non-zero after printing the markers: the markers decide, not the code.
    child.on("close", () => finish({ stdout }));
  });

/** Ask the login shell for PATH and merge it into `env` (in place). Never rejects. */
export async function mergeLoginShellPath(options: ShellPathOptions): Promise<ShellPathOutcome> {
  const { env, platform } = options;
  if (platform === "win32") return { applied: false, reason: "win32" };
  const run = options.run ?? spawnShell;
  const now = options.now ?? Date.now;
  const deadline = now() + (options.deadlineMs ?? SHELL_PATH_DEADLINE_MS);
  const nonce = randomBytes(6).toString("hex");
  const start = `__INDUS_PATH_${nonce}_START__`;
  const end = `__INDUS_PATH_${nonce}_END__`;
  const probe = `printf '%s' "${start}\${PATH}${end}"`;
  const shell = defaultShell(env, platform);

  let found: string | null = null;
  // `-i` reads rc files (where version managers live), `-l` the login profile. Some shells reject
  // `-i` without a terminal, so a plain login shell is the second try.
  for (const flags of ["-ilc", "-lc"]) {
    const left = deadline - now();
    if (left <= 0) break;
    try {
      const result = await run(shell, [flags, probe], left);
      found = result === null ? null : betweenMarkers(result.stdout, start, end);
    } catch {
      found = null;
    }
    if (found !== null) break;
  }
  if (found === null) return { applied: false, reason: "no-answer" };
  const merged = mergePathLists([found, env.PATH], ":");
  if (merged === "" || merged === env.PATH) return { applied: false, reason: "unchanged" };
  env.PATH = merged;
  return { applied: true, path: merged };
}
