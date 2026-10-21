// The shared update marker and the update gate (P20-37, R08.13).
//
// `<home>/.indus-update-in-progress` says "an update owns the runtime right now". Whoever runs the
// update (`indus update`, P23) creates it with its pid and that process's start time and removes it
// when done. This app only ever reads it:
//
//   - live:  the pid runs, is not a zombie, and started when the marker says (± 2 s) — or, when no
//            comparable start time exists, the marker is younger than 20 minutes; an empty file
//            younger than 5 s is a claim being written;
//   - stale: anything else (dead pid, zombie, a pid the OS gave to another process, garbage).
//
// A stale marker is ignored and left exactly as it is: never deleted, never rewritten (another
// tool reclaims it under its own lock). While a marker is live the app starts no backend from the
// runtime under replacement and starts no update of its own. The gate also honours an in-process
// flag, because an apply stops things before any marker would say so.

import { execFile } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import type { Timers } from "../ambient/hud.ts";

export const MARKER_FILE = ".indus-update-in-progress";
export const MARKER_FORMAT = 1;
export const START_TIME_TOLERANCE_S = 2;
export const EMPTY_MARKER_GRACE_MS = 5_000;
export const UNVERIFIED_MARKER_CEILING_MS = 20 * 60_000;
export const GATE_POLL_MS = 1_000;
/** The in-process flag is waited out at most this long (a live marker has no deadline). */
export const IN_FLIGHT_WAIT_MAX_MS = 20 * 60_000;
/** Background refresh of the cached judgement (the boot's synchronous question). */
export const GATE_WATCH_MS = 10_000;

export interface MarkerBody {
  format: number;
  owner: string;
  pid: number;
  /** Unix seconds when `pid` started, or null when the writer could not tell. */
  processStart: number | null;
  /** Unix seconds when the marker was written. */
  createdAt: number;
}

export type MarkerJudgement =
  | { state: "absent" }
  | { state: "live"; owner: string; pid: number; reason: string }
  | { state: "ours"; pid: number }
  | { state: "stale"; reason: string };

/** What the judge needs to know about processes (fakes in tests). */
export interface ProcessFacts {
  selfPid: number;
  alive(pid: number): boolean;
  /** True for a zombie (exited, not reaped): it answers signal 0 like a live one. */
  zombie(pid: number): boolean;
  /** Unix seconds when `pid` started, or null when it cannot be read. */
  startTime(pid: number): Promise<number | null>;
}

export function markerPath(home: string): string {
  return path.join(home, MARKER_FILE);
}

/** The marker's body, or null when it is not one. */
export function parseMarker(text: string): MarkerBody | null {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return null;
  }
  if (raw === null || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const pid = record.pid;
  const created = record.created_at;
  if (typeof pid !== "number" || !Number.isInteger(pid) || pid <= 0) return null;
  if (typeof created !== "number" || !Number.isFinite(created)) return null;
  const start = record.process_start;
  return {
    format: typeof record.format === "number" ? record.format : 0,
    owner: typeof record.owner === "string" ? record.owner.slice(0, 64) : "unknown",
    pid,
    processStart: typeof start === "number" && Number.isFinite(start) ? start : null,
    createdAt: created,
  };
}

/**
 * Judge a marker's text. `text` null: no file. `ageMs`: how old the file is (its mtime), used only
 * for an empty claim being written.
 */
export async function judgeMarker(
  text: string | null,
  ageMs: number,
  nowMs: number,
  facts: ProcessFacts,
): Promise<MarkerJudgement> {
  if (text === null) return { state: "absent" };
  if (text.trim() === "") {
    return ageMs < EMPTY_MARKER_GRACE_MS
      ? { state: "live", owner: "unknown", pid: 0, reason: "a claim is being written" }
      : { state: "stale", reason: "an empty marker" };
  }
  const body = parseMarker(text);
  if (body === null) return { state: "stale", reason: "not a marker this app can read" };
  if (!facts.alive(body.pid)) return { state: "stale", reason: "its process has ended" };
  if (facts.zombie(body.pid)) return { state: "stale", reason: "its process has exited" };
  const started = body.processStart === null ? null : await facts.startTime(body.pid);
  if (body.processStart !== null && started !== null) {
    if (Math.abs(started - body.processStart) > START_TIME_TOLERANCE_S) {
      return { state: "stale", reason: "its process id now belongs to another process" };
    }
    if (body.pid === facts.selfPid) return { state: "ours", pid: body.pid };
    return { state: "live", owner: body.owner, pid: body.pid, reason: "its process is running" };
  }
  // No comparable start time: trust the claim only for a bounded time.
  if (nowMs - body.createdAt * 1000 < UNVERIFIED_MARKER_CEILING_MS) {
    return {
      state: "live",
      owner: body.owner,
      pid: body.pid,
      reason: "its process is running (start time not verifiable)",
    };
  }
  return { state: "stale", reason: "older than 20 minutes and not verifiable" };
}

/** Read the marker (never writes, never follows a symlink) and judge it. */
export async function inspectMarker(
  home: string,
  facts: ProcessFacts,
  nowMs: number = Date.now(),
): Promise<MarkerJudgement> {
  const file = markerPath(home);
  let info: fs.Stats;
  try {
    info = fs.lstatSync(file);
  } catch {
    return { state: "absent" };
  }
  if (!info.isFile()) return { state: "stale", reason: "not a regular file" };
  let text: string;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    return { state: "stale", reason: "unreadable" };
  }
  return judgeMarker(text, Math.max(0, nowMs - info.mtimeMs), nowMs, facts);
}

// -- process facts on this machine ---------------------------------------------------------------

function run(file: string, args: string[], env?: NodeJS.ProcessEnv): Promise<string | null> {
  return new Promise((resolve) => {
    try {
      execFile(
        file,
        args,
        {
          timeout: 2_000,
          windowsHide: true,
          encoding: "utf8",
          ...(env === undefined ? {} : { env }),
        },
        (error, stdout) => resolve(error === null ? String(stdout) : null),
      );
    } catch {
      resolve(null);
    }
  });
}

const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

/** `ps -o lstart=` printed in UTC (`Mon Oct 21 03:45:00 2026`) → unix seconds. */
export function parseLstartUtc(text: string): number | null {
  const match = /^\s*\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{4})\s*$/.exec(
    text,
  );
  if (match === null) return null;
  const month = MONTHS.indexOf((match[1] ?? "").toLowerCase());
  if (month < 0) return null;
  const ms = Date.UTC(
    Number(match[6]),
    month,
    Number(match[2]),
    Number(match[3]),
    Number(match[4]),
    Number(match[5]),
  );
  return Number.isFinite(ms) ? ms / 1000 : null;
}

/** Linux `/proc/<pid>/stat` start ticks + boot time → unix seconds. */
export function linuxStartSeconds(
  stat: string,
  procStat: string,
  ticksPerSecond: number,
): number | null {
  const fields = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
  const ticks = Number(fields[19]);
  const btime = Number(/^btime\s+(\d+)/m.exec(procStat)?.[1]);
  if (!Number.isFinite(ticks) || !Number.isFinite(btime)) return null;
  return btime + ticks / ticksPerSecond;
}

export function nodeProcessFacts(platform: string = process.platform): ProcessFacts {
  let ticks: number | null = null;
  const linuxState = (pid: number): string | null => {
    try {
      const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
      return stat.slice(stat.lastIndexOf(")") + 2, stat.lastIndexOf(")") + 3) || null;
    } catch {
      return null;
    }
  };
  return {
    selfPid: process.pid,
    alive: (pid) => {
      try {
        process.kill(pid, 0);
        return true;
      } catch (error) {
        return (error as NodeJS.ErrnoException)?.code === "EPERM";
      }
    },
    zombie: (pid) => platform === "linux" && linuxState(pid) === "Z",
    startTime: async (pid) => {
      if (platform === "linux") {
        if (ticks === null) ticks = Number((await run("getconf", ["CLK_TCK"]))?.trim()) || 100;
        try {
          return linuxStartSeconds(
            fs.readFileSync(`/proc/${pid}/stat`, "utf8"),
            fs.readFileSync("/proc/stat", "utf8"),
            ticks,
          );
        } catch {
          return null;
        }
      }
      if (platform === "darwin") {
        const out = await run("ps", ["-o", "lstart=", "-p", String(pid)], {
          ...process.env,
          TZ: "UTC",
          LC_ALL: "C",
        });
        return out === null ? null : parseLstartUtc(out);
      }
      return null; // Windows: no cheap reader; the 20-minute ceiling applies
    },
  };
}

// -- the gate ------------------------------------------------------------------------------------

export interface UpdateGateDeps {
  home: string;
  facts: ProcessFacts;
  timers: Timers;
  now(): number;
  log(line: string): void;
}

export type ClearOutcome = "clear" | "finished" | "timeout";

export class UpdateGate {
  private readonly deps: UpdateGateDeps;
  private inFlight = false;
  private lastLive = false;
  private readonly clearedListeners = new Set<() => void>();
  private watchTimer: unknown = null;
  private watching = false;
  private readonly loggedStale = new Set<string>();

  constructor(deps: UpdateGateDeps) {
    this.deps = deps;
  }

  /** This process is inside an update apply. */
  setInFlight(on: boolean): void {
    this.inFlight = on;
  }

  inFlightNow(): boolean {
    return this.inFlight;
  }

  /** Synchronous answer (the cached marker judgement + the in-process flag). */
  isRunning(): boolean {
    return this.inFlight || this.lastLive;
  }

  /** Judge the marker now; note a live → gone transition (a backend update finished). */
  async refresh(): Promise<MarkerJudgement> {
    const judgement = await inspectMarker(this.deps.home, this.deps.facts, this.deps.now());
    const live = judgement.state === "live";
    if (judgement.state === "stale" && !this.loggedStale.has(judgement.reason)) {
      this.loggedStale.add(judgement.reason);
      this.deps.log(
        `[updates] ignoring a stale update marker (${judgement.reason}); left as it is`,
      );
    }
    if (this.lastLive && !live) {
      this.lastLive = false;
      for (const listener of this.clearedListeners) listener();
    }
    this.lastLive = live;
    return judgement;
  }

  /** A live marker went away (a runtime update ended). */
  onCleared(listener: () => void): () => void {
    this.clearedListeners.add(listener);
    return () => {
      this.clearedListeners.delete(listener);
    };
  }

  /**
   * Wait until no update owns the runtime. A live marker is waited out with no deadline (its
   * owner's liveness decides); the in-process flag at most 20 minutes.
   */
  async waitUntilClear(onWait?: (reason: string) => void): Promise<ClearOutcome> {
    let judgement = await this.refresh();
    if (judgement.state !== "live" && !this.inFlight) return "clear";
    const started = this.deps.now();
    for (;;) {
      if (judgement.state === "live") onWait?.(judgement.reason);
      else if (this.inFlight) {
        if (this.deps.now() - started >= IN_FLIGHT_WAIT_MAX_MS) return "timeout";
        onWait?.("an update in this app is running");
      } else {
        return "finished";
      }
      await new Promise<void>((resolve) => this.deps.timers.setTimeout(resolve, GATE_POLL_MS));
      judgement = await this.refresh();
    }
  }

  /** Keep the cached judgement fresh (and notice transitions) in the background. */
  watch(): void {
    if (this.watching) return;
    this.watching = true;
    const tick = () => {
      this.watchTimer = null;
      void this.refresh().finally(() => {
        if (this.watching) this.watchTimer = this.deps.timers.setTimeout(tick, GATE_WATCH_MS);
      });
    };
    tick();
  }

  stop(): void {
    this.watching = false;
    if (this.watchTimer !== null) this.deps.timers.clearTimeout(this.watchTimer);
    this.watchTimer = null;
  }
}
