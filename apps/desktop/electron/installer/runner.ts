// The in-app installer runner (P20-09): drives the platform installer script stage by stage and
// streams what it does to every window (`indus:installer:event`). See stageProtocol.ts for the
// wire and docs/notes/P20/installer-runner.md for the rules:
//
// - the script is fetched fresh for this app's release on every run (or a dev override is used);
// - a stage host that exits without its result frame is retried (3 attempts, 500 ms apart, the
//   wait cancellable); a frame that says ok:false is final;
// - the exit status comes from the process exiting; afterwards its pipes get 20 s to drain (a
//   grandchild may hold them) and the rest is abandoned, said out loud in the log;
// - success writes `<home>/.indus-bootstrap-complete` (schema 1).

import { type ChildProcess, type SpawnOptions, spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import type { Readable } from "node:stream";
import { writeFileAtomic } from "../fsSecure.ts";
import { redactSecrets } from "../redact.ts";
import { RELEASE_DOWNLOAD_BASE } from "../releaseBase.ts";
import {
  cleanLine,
  LineSplitter,
  type ManifestStage,
  manifestArgs,
  parseManifest,
  parseStageFrame,
  stageArgs,
} from "./stageProtocol.ts";

export const STAGE_ATTEMPTS = 3;
export const STAGE_RETRY_BACKOFF_MS = 500;
export const PIPE_DRAIN_GRACE_MS = 20_000;
export const LOG_RING_LINES = 500;
export const MARKER_FILE = ".indus-bootstrap-complete";
export const MARKER_SCHEMA = 1;
export const DEFAULT_RELEASE_BASE = RELEASE_DOWNLOAD_BASE;
const MAX_LINE = 4_000;
const MAX_STDOUT_KEPT = 1024 * 1024;

export type InstallerEvent =
  | { type: "manifest"; stages: ManifestStage[] }
  | { type: "log"; stage?: string; line: string; stream: "stdout" | "stderr" }
  | {
      type: "stage";
      name: string;
      state: "running" | "succeeded" | "skipped" | "failed";
      error?: string;
      durationMs?: number;
    }
  | { type: "complete"; marker: string }
  | { type: "failed"; stage?: string; error: string };

export type InstallOutcome =
  | { ok: true; marker: string }
  | { ok: false; stage?: string; error: string; cancelled: boolean };

export interface InstallerScript {
  path: string;
  /** How to run it: `bash`, PowerShell, or Node (`.mjs`: tests and dev only). */
  kind: "posix" | "powershell" | "node";
  source: "local" | "download";
}

export interface ChildLike {
  readonly pid?: number | undefined;
  readonly stdout: Readable | null;
  readonly stderr: Readable | null;
  on(
    event: "exit",
    listener: (code: number | null, signal: NodeJS.Signals | null) => void,
  ): unknown;
  on(event: "error", listener: (error: Error) => void): unknown;
  kill(signal?: NodeJS.Signals): boolean;
}

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface RunnerDeps {
  home: string;
  env: Record<string, string | undefined>;
  /** Resolve the script for this run (fresh every time). */
  fetchScript(): Promise<InstallerScript>;
  emit(event: InstallerEvent): void;
  log(line: string): void;
  spawn?: (
    script: InstallerScript,
    args: string[],
    env: Record<string, string | undefined>,
  ) => ChildLike;
  timers?: Timers;
  now?: () => number;
  /** Record / forget a running stage host in the orphan ledger (P20-13). */
  claim?(pid: number): void;
  release?(pid: number): void;
  appVersion: string;
}

interface HostResult {
  code: number | null;
  signal: string | null;
  killed: boolean;
  stdout: string;
  abandoned: boolean;
  spawnError: string | null;
}

const realTimers: Timers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export function scriptKind(file: string): InstallerScript["kind"] {
  if (file.endsWith(".ps1")) return "powershell";
  if (file.endsWith(".mjs")) return "node";
  return "posix";
}

export function defaultSpawn(
  script: InstallerScript,
  args: string[],
  env: Record<string, string | undefined>,
): ChildLike {
  const options: SpawnOptions = { env, stdio: ["ignore", "pipe", "pipe"], windowsHide: true };
  if (script.kind === "powershell") {
    return spawn(
      "powershell.exe",
      ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script.path, ...args],
      options,
    ) as ChildProcess;
  }
  if (script.kind === "node") return spawn(process.execPath, [script.path, ...args], options);
  return spawn("bash", [script.path, ...args], options);
}

/** `install.sh` / `install.ps1` for this platform. */
export function scriptName(platform: string): string {
  return platform === "win32" ? "install.ps1" : "install.sh";
}

export interface ScriptSourceOptions {
  env: Readonly<Record<string, string | undefined>>;
  home: string;
  appVersion: string;
  platform: string;
  /** Download `url` to `dest` (the default uses fetch; tests inject). */
  download?: (url: string, dest: string) => Promise<void>;
}

async function fetchToFile(url: string, dest: string): Promise<void> {
  const response = await fetch(url, { redirect: "follow" });
  if (response.status !== 200) throw new Error(`HTTP ${response.status} for ${url}`);
  writeFileAtomic(dest, new Uint8Array(await response.arrayBuffer()), { mode: 0o700 });
}

/**
 * Where this run's script comes from: `INDUS_DESKTOP_INSTALL_SCRIPT` (an absolute local file, for
 * a dev checkout), else this release's asset downloaded now into `<home>/bootstrap-cache/`.
 */
export async function resolveInstallerScript(
  options: ScriptSourceOptions,
): Promise<InstallerScript> {
  const local = (options.env.INDUS_DESKTOP_INSTALL_SCRIPT ?? "").trim();
  if (local !== "") {
    if (!path.isAbsolute(local) || !fs.statSync(local, { throwIfNoEntry: false })?.isFile()) {
      throw new Error("INDUS_DESKTOP_INSTALL_SCRIPT does not name a file");
    }
    return { path: local, kind: scriptKind(local), source: "local" };
  }
  if (!/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(options.appVersion)) {
    throw new Error(`this build (${options.appVersion}) has no release to install from`);
  }
  const base = (options.env.INDUS_DESKTOP_RELEASE_BASE ?? "").trim() || DEFAULT_RELEASE_BASE;
  if (!base.startsWith("https://")) throw new Error("the release origin must be https");
  const name = scriptName(options.platform);
  const url = `${base.replace(/\/+$/, "")}/v${options.appVersion}/${name}`;
  const dir = path.join(options.home, "bootstrap-cache");
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const dest = path.join(dir, name);
  fs.rmSync(dest, { force: true }); // never run an older copy
  await (options.download ?? fetchToFile)(url, dest);
  return { path: dest, kind: scriptKind(dest), source: "download" };
}

export class InstallCancelled extends Error {}

export class InstallerRunner {
  private readonly deps: RunnerDeps;
  private readonly timers: Timers;
  private readonly now: () => number;
  private readonly ring: string[] = [];
  private abort: AbortController | null = null;
  private live = new Set<ChildLike>();

  constructor(deps: RunnerDeps) {
    this.deps = deps;
    this.timers = deps.timers ?? realTimers;
    this.now = deps.now ?? Date.now;
  }

  running(): boolean {
    return this.abort !== null;
  }

  /** The last 500 log lines of the current or last run. */
  recentLog(): string[] {
    return [...this.ring];
  }

  /** Stop the run: the running host is killed and the run fails with "cancelled". */
  cancel(): boolean {
    if (this.abort === null) return false;
    this.abort.abort();
    for (const child of this.live) child.kill("SIGTERM");
    return true;
  }

  async run(): Promise<InstallOutcome> {
    if (this.abort !== null) throw new Error("the installer is already running");
    const abort = new AbortController();
    this.abort = abort;
    this.ring.length = 0;
    let current: string | undefined;
    // an empty manifest first, so the windows switch to the install view while the script downloads
    this.deps.emit({ type: "manifest", stages: [] });
    try {
      const script = await this.deps.fetchScript();
      this.logLine(`[installer] using ${script.source} script ${script.path}`, undefined, "stdout");
      if (abort.signal.aborted) throw new InstallCancelled();

      const manifestRun = await this.host(
        script,
        manifestArgs({ home: this.deps.home }),
        undefined,
      );
      if (manifestRun.killed || abort.signal.aborted) throw new InstallCancelled();
      if (manifestRun.code !== 0) {
        throw new Error(
          `the installer manifest failed (${describeExit(manifestRun)})${tail(manifestRun.stdout)}`,
        );
      }
      const manifest = parseManifest(manifestRun.stdout);
      if ("error" in manifest) throw new Error(manifest.error);
      this.deps.emit({ type: "manifest", stages: manifest.stages });

      for (const stage of manifest.stages) {
        if (abort.signal.aborted) throw new InstallCancelled();
        current = stage.name;
        const started = this.now();
        this.deps.emit({ type: "stage", name: stage.name, state: "running" });
        let attempt = 1;
        let result: HostResult;
        for (;;) {
          result = await this.host(
            script,
            stageArgs(stage.name, { home: this.deps.home }),
            stage.name,
          );
          if (result.killed || abort.signal.aborted) {
            this.deps.emit({
              type: "stage",
              name: stage.name,
              state: "failed",
              error: "cancelled",
              durationMs: this.now() - started,
            });
            throw new InstallCancelled();
          }
          if (parseStageFrame(result.stdout) !== null || attempt >= STAGE_ATTEMPTS) break;
          attempt += 1;
          this.logLine(
            `[installer] the ${stage.name} stage host exited without a result (${describeExit(result)}); retrying (${attempt}/${STAGE_ATTEMPTS})`,
            stage.name,
            "stderr",
          );
          if (!(await this.wait(STAGE_RETRY_BACKOFF_MS, abort.signal))) {
            this.deps.emit({
              type: "stage",
              name: stage.name,
              state: "failed",
              error: "cancelled",
            });
            throw new InstallCancelled();
          }
        }
        const durationMs = this.now() - started;
        const frame = parseStageFrame(result.stdout);
        if (frame === null) {
          const error = `the ${stage.name} stage ended without a result (${describeExit(result)})`;
          this.deps.emit({ type: "stage", name: stage.name, state: "failed", error, durationMs });
          return this.fail(stage.name, error);
        }
        if (!frame.ok) {
          const error = frame.reason ?? `the ${stage.name} stage failed (${describeExit(result)})`;
          this.deps.emit({ type: "stage", name: stage.name, state: "failed", error, durationMs });
          return this.fail(stage.name, error);
        }
        this.deps.emit({
          type: "stage",
          name: stage.name,
          state: frame.skipped ? "skipped" : "succeeded",
          durationMs,
        });
      }
      current = undefined;
      const marker = this.writeMarker(script);
      this.deps.emit({ type: "complete", marker });
      return { ok: true, marker };
    } catch (error) {
      if (error instanceof InstallCancelled || abort.signal.aborted) {
        this.deps.emit({
          type: "failed",
          ...(current === undefined ? {} : { stage: current }),
          error: "cancelled",
        });
        return {
          ok: false,
          ...(current === undefined ? {} : { stage: current }),
          error: "cancelled",
          cancelled: true,
        };
      }
      const message = redactSecrets(error instanceof Error ? error.message : String(error));
      return this.fail(current, message);
    } finally {
      this.abort = null;
    }
  }

  private fail(stage: string | undefined, error: string): InstallOutcome {
    this.deps.emit({ type: "failed", ...(stage === undefined ? {} : { stage }), error });
    this.deps.log(`[installer] failed${stage === undefined ? "" : ` at ${stage}`}: ${error}`);
    return { ok: false, ...(stage === undefined ? {} : { stage }), error, cancelled: false };
  }

  private writeMarker(script: InstallerScript): string {
    const file = path.join(this.deps.home, MARKER_FILE);
    fs.mkdirSync(this.deps.home, { recursive: true });
    const body = {
      schema: MARKER_SCHEMA,
      completed_at: new Date(this.now()).toISOString(),
      app_version: this.deps.appVersion,
      script: script.source,
    };
    writeFileAtomic(file, `${JSON.stringify(body)}\n`, { mode: 0o600 });
    return file;
  }

  private logLine(text: string, stage: string | undefined, stream: "stdout" | "stderr"): void {
    const line = redactSecrets(text.length > MAX_LINE ? `${text.slice(0, MAX_LINE)}…` : text);
    this.ring.push(line);
    if (this.ring.length > LOG_RING_LINES) this.ring.splice(0, this.ring.length - LOG_RING_LINES);
    this.deps.emit({ type: "log", ...(stage === undefined ? {} : { stage }), line, stream });
    this.deps.log(`[installer]${stage === undefined ? "" : ` ${stage}:`} ${line}`);
  }

  /** Resolves true after `ms`, false as soon as `signal` aborts. */
  private wait(ms: number, signal: AbortSignal): Promise<boolean> {
    return new Promise((resolve) => {
      if (signal.aborted) {
        resolve(false);
        return;
      }
      const onAbort = () => {
        this.timers.clearTimeout(handle);
        resolve(false);
      };
      const handle = this.timers.setTimeout(() => {
        signal.removeEventListener("abort", onAbort);
        resolve(true);
      }, ms);
      signal.addEventListener("abort", onAbort, { once: true });
    });
  }

  /** Run the script once; resolves when the process exited and its pipes drained (or the grace ran out). */
  private host(
    script: InstallerScript,
    args: string[],
    stage: string | undefined,
  ): Promise<HostResult> {
    return new Promise((resolve) => {
      const env = { ...this.deps.env, INDUS_HOME: this.deps.home };
      let child: ChildLike;
      try {
        child = (this.deps.spawn ?? defaultSpawn)(script, args, env);
      } catch (error) {
        resolve({
          code: null,
          signal: null,
          killed: false,
          stdout: "",
          abandoned: false,
          spawnError: error instanceof Error ? error.message : String(error),
        });
        return;
      }
      this.live.add(child);
      const pid = child.pid;
      if (pid !== undefined) this.deps.claim?.(pid);
      if (this.abort?.signal.aborted === true) child.kill("SIGTERM");
      let stdout = "";
      let open = 0;
      let exit: { code: number | null; signal: string | null } | null = null;
      let grace: unknown = null;
      let settled = false;
      const finish = (abandoned: boolean, spawnError: string | null = null) => {
        if (settled) return;
        settled = true;
        if (grace !== null) this.timers.clearTimeout(grace);
        this.live.delete(child);
        if (pid !== undefined) this.deps.release?.(pid);
        for (const stream of [child.stdout, child.stderr]) stream?.destroy();
        if (abandoned) {
          this.logLine(
            "[installer] the stage host exited but something it started still holds its output; the rest of the output was dropped",
            stage,
            "stderr",
          );
        }
        resolve({
          code: exit?.code ?? null,
          signal: exit?.signal ?? null,
          killed: this.abort?.signal.aborted === true,
          stdout,
          abandoned,
          spawnError,
        });
      };
      const pipe = (stream: Readable | null, name: "stdout" | "stderr") => {
        if (stream === null) return;
        open += 1;
        const splitter = new LineSplitter();
        const take = (raw: string) => {
          const line = cleanLine(raw);
          if (line === "") return;
          if (name === "stdout" && stdout.length < MAX_STDOUT_KEPT) stdout += `${line}\n`;
          this.logLine(line, stage, name);
        };
        stream.on("data", (chunk: Buffer) => {
          for (const raw of splitter.push(chunk)) take(raw);
        });
        stream.on("end", () => {
          for (const raw of splitter.flush()) take(raw);
          open -= 1;
          if (open === 0 && exit !== null) finish(false);
        });
        stream.on("error", () => {});
      };
      pipe(child.stdout, "stdout");
      pipe(child.stderr, "stderr");
      child.on("error", (error) => {
        if (exit === null) finish(false, error.message);
      });
      child.on("exit", (code, signal) => {
        exit = { code, signal };
        if (open === 0) finish(false);
        else grace = this.timers.setTimeout(() => finish(true), PIPE_DRAIN_GRACE_MS);
      });
    });
  }
}

function describeExit(result: HostResult): string {
  if (result.spawnError !== null) return `could not start: ${result.spawnError}`;
  if (result.signal !== null) return `signal ${result.signal}`;
  return `exit ${result.code ?? "?"}`;
}

function tail(stdout: string): string {
  const last = stdout.trim().split("\n").slice(-3).join(" / ");
  return last === "" ? "" : `: ${last}`;
}
