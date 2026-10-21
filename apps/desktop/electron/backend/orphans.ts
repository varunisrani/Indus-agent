// The ledger of processes this app owns (P20-13): `<userData>/backend-ownership.json`. In 1.0 the
// app owns only the installer's stage hosts while they run (daemons and serves are meant to
// outlive the app and are never recorded). An app that crashed mid-install leaves a host behind;
// the next start reaps it — on the lock holder only (R08.3, `machinery.guard`) and only when the
// recorded pid is alive with the same process start time. A pid the OS reused for another process
// has another start time and is never killed (R03.20-style identity). A corrupt ledger is moved
// aside and nothing is killed.

import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { writeFileAtomic } from "../fsSecure.ts";

export const LEDGER_FILE = "backend-ownership.json";
export const REAP_KILL_GRACE_MS = 2_000;

export interface OwnedProcess {
  pid: number;
  startTime: string;
  nonce: string;
  kind: string;
}

export interface ProcessProbe {
  /** An OS-reported start time for `pid`, or null when it is not running. */
  startTime(pid: number): string | null;
  kill(pid: number, signal: NodeJS.Signals): void;
}

/** Linux: field 22 of /proc/<pid>/stat; macOS: `ps -o lstart=`; elsewhere unknown (null). */
export function processStartTime(pid: number, platform: string = process.platform): string | null {
  if (!Number.isInteger(pid) || pid <= 0) return null;
  try {
    if (platform === "linux") {
      const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
      const fields = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
      return fields[19] ?? null; // field 22 overall; fields after the name start at field 3
    }
    if (platform === "darwin") {
      const out = execFileSync("ps", ["-o", "lstart=", "-p", String(pid)], {
        encoding: "utf8",
        timeout: 2_000,
      }).trim();
      return out === "" ? null : out;
    }
  } catch {
    return null;
  }
  return null;
}

export const nodeProbe: ProcessProbe = {
  startTime: (pid) => processStartTime(pid),
  kill: (pid, signal) => process.kill(pid, signal),
};

export interface LedgerDeps {
  userData: string;
  probe?: ProcessProbe;
  /** `machinery.guard` from the single-instance lock. */
  guard<A extends unknown[], R>(what: string, fn: (...args: A) => R): (...args: A) => R;
  log(line: string): void;
  sleep?: (ms: number) => Promise<void>;
}

function parse(text: string): OwnedProcess[] | null {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return null;
  }
  const list = (raw as { processes?: unknown } | null)?.processes;
  if (!Array.isArray(list)) return null;
  return list.filter(
    (entry): entry is OwnedProcess =>
      entry !== null &&
      typeof entry === "object" &&
      Number.isInteger((entry as OwnedProcess).pid) &&
      (entry as OwnedProcess).pid > 0 &&
      typeof (entry as OwnedProcess).startTime === "string" &&
      (entry as OwnedProcess).startTime !== "" &&
      typeof (entry as OwnedProcess).nonce === "string" &&
      typeof (entry as OwnedProcess).kind === "string",
  );
}

export class OrphanLedger {
  private readonly deps: LedgerDeps;
  private readonly probe: ProcessProbe;
  readonly claim: (pid: number, kind: string) => OwnedProcess | null;
  readonly reapOrphans: () => Promise<{ killed: number[]; spared: number[] }>;

  constructor(deps: LedgerDeps) {
    this.deps = deps;
    this.probe = deps.probe ?? nodeProbe;
    this.claim = deps.guard("claiming a process", (pid: number, kind: string) =>
      this.doClaim(pid, kind),
    );
    this.reapOrphans = deps.guard("reaping orphans", () => this.doReap());
  }

  private file(): string {
    return path.join(this.deps.userData, LEDGER_FILE);
  }

  /** The entries, or null when the ledger exists but cannot be read. */
  private read(): OwnedProcess[] | null {
    let text: string;
    try {
      text = fs.readFileSync(this.file(), "utf8");
    } catch {
      return [];
    }
    return text.trim() === "" ? [] : parse(text);
  }

  private write(entries: OwnedProcess[]): void {
    fs.mkdirSync(this.deps.userData, { recursive: true });
    writeFileAtomic(this.file(), `${JSON.stringify({ v: 1, processes: entries }, null, 2)}\n`, {
      mode: 0o600,
    });
  }

  entries(): OwnedProcess[] {
    return this.read() ?? [];
  }

  private doClaim(pid: number, kind: string): OwnedProcess | null {
    const startTime = this.probe.startTime(pid);
    if (startTime === null) return null; // no identity, nothing safe to record
    const entry: OwnedProcess = { pid, startTime, nonce: randomBytes(8).toString("hex"), kind };
    const entries = (this.read() ?? []).filter((e) => e.pid !== pid);
    this.write([...entries, entry]);
    return entry;
  }

  release(pid: number): void {
    const entries = this.read();
    if (entries === null) return;
    const next = entries.filter((e) => e.pid !== pid);
    if (next.length !== entries.length) this.write(next);
  }

  private async doReap(): Promise<{ killed: number[]; spared: number[] }> {
    const entries = this.read();
    if (entries === null) {
      const aside = `${this.file()}.corrupt-${Date.now()}`;
      try {
        fs.renameSync(this.file(), aside);
      } catch {
        // gone already
      }
      this.deps.log(
        `[orphans] the ownership ledger was unreadable; moved it to ${path.basename(aside)}`,
      );
      return { killed: [], spared: [] };
    }
    const killed: number[] = [];
    const spared: number[] = [];
    for (const entry of entries) {
      const now = this.probe.startTime(entry.pid);
      if (now === null || now !== entry.startTime) {
        spared.push(entry.pid);
        continue;
      }
      try {
        this.probe.kill(entry.pid, "SIGTERM");
        killed.push(entry.pid);
        this.deps.log(`[orphans] stopped a leftover ${entry.kind} (pid ${entry.pid})`);
      } catch {
        spared.push(entry.pid);
      }
    }
    if (killed.length > 0) {
      await (this.deps.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms))))(
        REAP_KILL_GRACE_MS,
      );
      for (const entry of entries) {
        if (!killed.includes(entry.pid)) continue;
        if (this.probe.startTime(entry.pid) === entry.startTime) {
          try {
            this.probe.kill(entry.pid, "SIGKILL");
          } catch {
            // exited meanwhile
          }
        }
      }
    }
    this.write([]);
    return { killed, spared };
  }
}
