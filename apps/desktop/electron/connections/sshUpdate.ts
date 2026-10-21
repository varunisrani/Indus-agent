// Install and update the Indus runtime on an SSH host (P24-30), with a durable recovery record.
//
// A run goes through phases, and before each one main writes the record
// `<userData>/ssh-update/<connection id>.json` = `{v, connectionId, correlationId, phase,
// attempts, startedAt, updatedAt, preVersion?}`:
//   draining  — the app's own work on that host is stopped (its serve, its tunnels);
//   updating  — the remote command runs detached (`nohup`), writing its exit code to
//               `~/.indus/desktop-update/<correlation id>.exit` and its output beside it;
//   restoring — the serve is started again and the tunnels reopened;
//   done      — the record is removed.
// When the app quits or crashes mid-run, the record stays. The next launch calls `recover()`:
// a run caught while `updating` is never launched a second time — main watches the same
// correlation's exit file; `restoring` restores; `draining` restores (nothing was launched). Each
// recovery counts an attempt; after 3 the record is given up (kept as `<id>.json.given-up`) and the
// connection is no longer fenced. A new run is refused while a record exists.
//
// The remote command is `indus update --yes` when Indus is there; a host without Indus needs an
// install command (the P23 install script's published one-liner, passed in by main once the
// release base is settled, D-16.690) and is refused without one. The version before and after
// comes from `indus --version` under the remote watchdog.

import { randomUUID } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { readJsonFile, writeFileAtomic } from "../fsSecure.ts";
import { type RunResult, shellQuote } from "./ssh.ts";

export const RECORD_DIR = "ssh-update";
export const MAX_RECOVERY_ATTEMPTS = 3;
export const UPDATE_POLL_MS = 1_000;
export const UPDATE_TIMEOUT_MS = 60 * 60_000;
export const MARKER_DIR = "~/.indus/desktop-update";
export const DEFAULT_UPDATE_COMMAND = "indus update --yes";

export type UpdatePhase = "draining" | "updating" | "restoring" | "done";
export type UpdateOutcome =
  | "updated"
  | "update-failed"
  | "restore-failed"
  | "cancelled"
  | "given-up"
  | "nothing-to-recover";

export interface RecoveryRecord {
  v: 1;
  connectionId: string;
  correlationId: string;
  phase: UpdatePhase;
  attempts: number;
  startedAt: number;
  updatedAt: number;
  preVersion?: string;
}

export interface UpdateResult {
  outcome: UpdateOutcome;
  correlationId: string | null;
  exitCode: number | null;
  preVersion?: string;
  postVersion?: string;
  message?: string;
}

/** What the update needs from the SSH connection (an `SshConnection` fits). */
export interface RemoteExec {
  exec(
    command: string,
    options?: { stdin?: string | Buffer; timeoutMs?: number },
  ): Promise<RunResult>;
  probe(command: string): Promise<RunResult>;
}

export interface SshUpdateDeps {
  userData: string;
  remote: RemoteExec;
  /** Stop this app's serve and tunnels on the host. */
  drain: () => Promise<void>;
  /** Start them again. */
  restore: () => Promise<void>;
  log: (line: string) => void;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
  newId?: () => string;
  pollMs?: number;
  timeoutMs?: number;
  updateCommand?: string;
  installCommand?: string;
}

const CORRELATION_RE = /^[A-Za-z0-9-]{8,64}$/;

export class UpdateRefused extends Error {}

export class SshUpdate {
  private readonly deps: SshUpdateDeps;
  private readonly connectionId: string;

  constructor(connectionId: string, deps: SshUpdateDeps) {
    if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(connectionId))
      throw new UpdateRefused("bad connection id");
    this.connectionId = connectionId;
    this.deps = deps;
  }

  private now(): number {
    return (this.deps.now ?? Date.now)();
  }

  recordPath(): string {
    return path.join(this.deps.userData, RECORD_DIR, `${this.connectionId}.json`);
  }

  readRecord(): RecoveryRecord | null {
    const file = this.recordPath();
    let raw: unknown;
    try {
      raw = readJsonFile(file);
    } catch {
      raw = null;
    }
    if (raw === null) {
      if (fs.existsSync(file)) {
        fs.renameSync(file, `${file}.bad`);
        this.deps.log("[ssh-update] an unreadable recovery record was set aside");
      }
      return null;
    }
    const record = raw as Partial<RecoveryRecord>;
    const phases: UpdatePhase[] = ["draining", "updating", "restoring", "done"];
    if (
      record.v !== 1 ||
      record.connectionId !== this.connectionId ||
      typeof record.correlationId !== "string" ||
      !CORRELATION_RE.test(record.correlationId) ||
      !phases.includes(record.phase as UpdatePhase)
    ) {
      fs.renameSync(file, `${file}.bad`);
      this.deps.log("[ssh-update] a malformed recovery record was set aside");
      return null;
    }
    return {
      v: 1,
      connectionId: this.connectionId,
      correlationId: record.correlationId,
      phase: record.phase as UpdatePhase,
      attempts: typeof record.attempts === "number" ? record.attempts : 0,
      startedAt: typeof record.startedAt === "number" ? record.startedAt : 0,
      updatedAt: typeof record.updatedAt === "number" ? record.updatedAt : 0,
      ...(typeof record.preVersion === "string" ? { preVersion: record.preVersion } : {}),
    };
  }

  private write(record: RecoveryRecord): void {
    fs.mkdirSync(path.dirname(this.recordPath()), { recursive: true, mode: 0o700 });
    writeFileAtomic(
      this.recordPath(),
      `${JSON.stringify({ ...record, updatedAt: this.now() })}\n`,
      {
        mode: 0o600,
      },
    );
  }

  private phase(record: RecoveryRecord, phase: UpdatePhase): RecoveryRecord {
    const next = { ...record, phase };
    this.write(next);
    return next;
  }

  private clear(): void {
    fs.rmSync(this.recordPath(), { force: true });
  }

  private async version(): Promise<string | undefined> {
    try {
      const result = await this.deps.remote.probe("indus --version");
      const text = result.stdout.trim().split(/\r?\n/)[0] ?? "";
      return result.code === 0 && text !== "" ? text.slice(0, 120) : undefined;
    } catch {
      return undefined;
    }
  }

  private markerFile(correlationId: string, suffix: "exit" | "log"): string {
    return `${MARKER_DIR}/${correlationId}.${suffix}`;
  }

  private async launch(correlationId: string, installed: boolean): Promise<void> {
    const command = installed
      ? (this.deps.updateCommand ?? DEFAULT_UPDATE_COMMAND)
      : this.deps.installCommand;
    if (command === undefined) {
      throw new UpdateRefused("Indus is not installed on this host; install it there first");
    }
    const exit = this.markerFile(correlationId, "exit");
    const log = this.markerFile(correlationId, "log");
    const job = `${command} > ${log} 2>&1; echo $? > ${exit}.tmp && mv ${exit}.tmp ${exit}`;
    // `&` binds to the nohup command alone, whose stdio is detached, so the SSH session can end
    const script = `mkdir -p ${MARKER_DIR} || exit 1; nohup sh -c ${shellQuote(job)} < /dev/null > /dev/null 2>&1 &`;
    const result = await this.deps.remote.exec(script);
    if (result.code !== 0) {
      throw new UpdateRefused(
        `the update could not be started on the host: ${result.stderr.trim().slice(-500)}`,
      );
    }
  }

  private async readExit(correlationId: string): Promise<number | null> {
    const result = await this.deps.remote.exec(
      `cat ${this.markerFile(correlationId, "exit")} 2>/dev/null || true`,
    );
    const text = result.stdout.trim();
    return text !== "" && /^\d+$/.test(text) ? Number(text) : null;
  }

  /** The exit code the detached run wrote, waiting up to the timeout. */
  private async awaitExit(correlationId: string): Promise<number | null> {
    const sleep =
      this.deps.sleep ?? ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));
    const deadline = this.now() + (this.deps.timeoutMs ?? UPDATE_TIMEOUT_MS);
    while (this.now() < deadline) {
      const code = await this.readExit(correlationId);
      if (code !== null) return code;
      await sleep(this.deps.pollMs ?? UPDATE_POLL_MS);
    }
    return null;
  }

  private async finish(record: RecoveryRecord, exitCode: number | null): Promise<UpdateResult> {
    const restoring = this.phase(record, "restoring");
    try {
      await this.deps.restore();
    } catch (error) {
      return {
        outcome: "restore-failed",
        correlationId: record.correlationId,
        exitCode,
        message: error instanceof Error ? error.message : "the server did not start again",
      };
    }
    this.phase(restoring, "done");
    this.clear();
    const postVersion = await this.version();
    const result: UpdateResult = {
      outcome: exitCode === 0 ? "updated" : "update-failed",
      correlationId: record.correlationId,
      exitCode,
    };
    if (record.preVersion !== undefined) result.preVersion = record.preVersion;
    if (postVersion !== undefined) result.postVersion = postVersion;
    if (exitCode === null) result.message = "the update did not finish in time";
    return result;
  }

  /** Install or update the runtime on the host (refused while a recovery record exists). */
  async run(): Promise<UpdateResult> {
    if (this.readRecord() !== null) {
      throw new UpdateRefused("an earlier update on this host has to be recovered first");
    }
    const correlationId = (this.deps.newId ?? randomUUID)();
    const preVersion = await this.version();
    let record: RecoveryRecord = {
      v: 1,
      connectionId: this.connectionId,
      correlationId,
      phase: "draining",
      attempts: 0,
      startedAt: this.now(),
      updatedAt: this.now(),
      ...(preVersion === undefined ? {} : { preVersion }),
    };
    this.write(record);
    await this.deps.drain();
    record = this.phase(record, "updating");
    await this.launch(correlationId, preVersion !== undefined);
    this.deps.log("[ssh-update] the update is running on the host");
    const exitCode = await this.awaitExit(correlationId);
    return this.finish(record, exitCode);
  }

  /** Resume an interrupted run from its record (launch never repeated). */
  async recover(): Promise<UpdateResult> {
    const record = this.readRecord();
    if (record === null)
      return { outcome: "nothing-to-recover", correlationId: null, exitCode: null };
    if (record.attempts >= MAX_RECOVERY_ATTEMPTS) {
      fs.renameSync(this.recordPath(), `${this.recordPath()}.given-up`);
      this.deps.log("[ssh-update] gave up recovering an interrupted update after 3 attempts");
      return {
        outcome: "given-up",
        correlationId: record.correlationId,
        exitCode: null,
        message: "the interrupted update could not be recovered; check the host by hand",
      };
    }
    const counted = { ...record, attempts: record.attempts + 1 };
    this.write(counted);
    this.deps.log(`[ssh-update] recovering an interrupted update (${record.phase})`);
    if (record.phase === "draining") {
      const result = await this.finish(counted, null);
      return result.outcome === "restore-failed"
        ? result
        : { ...result, outcome: "cancelled", message: "the update had not started" };
    }
    if (record.phase === "updating") {
      const exitCode = await this.awaitExit(record.correlationId);
      return this.finish(counted, exitCode);
    }
    return this.finish(counted, await this.readExit(record.correlationId));
  }
}
