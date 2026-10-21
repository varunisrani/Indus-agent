// The prepared-submission journal (P20-20): before the renderer hands an input to the daemon, main
// records it in a private file, so a crash between "sent" and "receipt" cannot lose it.
//
// One file per input under `<userData>/prepared-submissions/` (0700), named by the SHA-256 of the
// client's request id and written 0600 through a temp file, fsync and rename. The request id is the
// `request_id` the renderer puts on `prompt.submit`; the daemon answers a repeated id with the
// first admission, so retrying an entry can never run the input twice. An entry is retired by an
// accepted receipt (or a definite rejection, whose text the renderer puts back in the composer);
// an unknown outcome keeps it, marked `uncertain`, for an explicit Retry or Dismiss. Nothing here
// resends anything.

import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { ensurePrivateDir, readRegularFile, writePrivateFile } from "../fsSecure.ts";
import { IpcError } from "../ipc/envelope.ts";
import type { IpcRegistry } from "../ipc/registry.ts";

/** Largest serialized payload one entry may hold. */
export const MAX_PAYLOAD_BYTES = 256 * 1024;
/** Live entries at most; a renderer that leaks entries hits this instead of filling the disk. */
export const MAX_ENTRIES = 200;

export type EntryState = "prepared" | "uncertain";
export type ReceiptOutcome = "accepted" | "rejected" | "unknown";

export interface JournalEntry {
  requestId: string;
  sessionId: string;
  createdAt: number;
  updatedAt: number;
  state: EntryState;
  /** The daemon's admission id once an answer named one. */
  admissionId?: string;
  payload: unknown;
}

export interface JournalOptions {
  dir: string;
  now?: () => number;
  platform?: string;
  log?: (line: string) => void;
}

const FILE_SUFFIX = ".json";

function isEntry(value: unknown): value is JournalEntry {
  if (typeof value !== "object" || value === null) return false;
  const entry = value as Record<string, unknown>;
  return (
    typeof entry.requestId === "string" &&
    typeof entry.sessionId === "string" &&
    typeof entry.createdAt === "number" &&
    typeof entry.updatedAt === "number" &&
    (entry.state === "prepared" || entry.state === "uncertain") &&
    "payload" in entry
  );
}

export class PreparedSubmissions {
  private readonly dir: string;
  private readonly now: () => number;
  private readonly platform: string;
  private readonly log: (line: string) => void;

  constructor(options: JournalOptions) {
    this.dir = options.dir;
    this.now = options.now ?? Date.now;
    this.platform = options.platform ?? process.platform;
    this.log = options.log ?? (() => {});
  }

  private fileFor(requestId: string): string {
    const digest = createHash("sha256").update(requestId, "utf8").digest("hex");
    return path.join(this.dir, `${digest}${FILE_SUFFIX}`);
  }

  private files(): string[] {
    let names: string[];
    try {
      names = fs.readdirSync(this.dir);
    } catch {
      return [];
    }
    return names
      .filter((name) => name.endsWith(FILE_SUFFIX) && !name.startsWith("."))
      .map((name) => path.join(this.dir, name));
  }

  private read(file: string): JournalEntry | null {
    let text: string | null;
    try {
      text = readRegularFile(file);
    } catch {
      return null;
    }
    if (text === null) return null;
    try {
      const parsed: unknown = JSON.parse(text);
      if (isEntry(parsed)) return parsed;
    } catch {
      // quarantined below
    }
    // A torn or garbled entry cannot be trusted; keep its bytes for diagnosis, out of the way.
    const aside = `${file}.corrupt-${this.now()}`;
    try {
      fs.renameSync(file, aside);
      this.log(`prepared-submissions: moved unreadable entry aside (${path.basename(aside)})`);
    } catch {
      // another window moved it first
    }
    return null;
  }

  private write(entry: JournalEntry): void {
    ensurePrivateDir(this.dir, this.platform);
    writePrivateFile(this.fileFor(entry.requestId), JSON.stringify(entry), this.platform);
  }

  /** Every live entry, oldest first. */
  list(): JournalEntry[] {
    const entries: JournalEntry[] = [];
    for (const file of this.files()) {
      const entry = this.read(file);
      if (entry !== null) entries.push(entry);
    }
    return entries.sort((a, b) => a.createdAt - b.createdAt);
  }

  /** Record an input before it is sent; preparing the same request id again refreshes it. */
  prepare(requestId: string, sessionId: string, payload: unknown): JournalEntry {
    const size = Buffer.byteLength(JSON.stringify(payload ?? null), "utf8");
    if (size > MAX_PAYLOAD_BYTES) {
      throw new IpcError(
        "too-large",
        `the input is ${size} bytes; the journal takes at most ${MAX_PAYLOAD_BYTES}`,
      );
    }
    const existing = this.read(this.fileFor(requestId));
    if (existing === null && this.files().length >= MAX_ENTRIES) {
      throw new IpcError("journal-full", `${MAX_ENTRIES} inputs are still waiting for a receipt`);
    }
    const at = this.now();
    const entry: JournalEntry = {
      requestId,
      sessionId,
      createdAt: existing?.createdAt ?? at,
      updatedAt: at,
      state: "prepared",
      payload,
    };
    this.write(entry);
    return entry;
  }

  /**
   * Settle an entry with the receipt the daemon gave (or did not give). Accepted and rejected
   * remove it; unknown keeps it as `uncertain`. False when no entry has this request id.
   */
  retire(requestId: string, outcome: ReceiptOutcome, admissionId?: string): boolean {
    const file = this.fileFor(requestId);
    const entry = this.read(file);
    if (entry === null) return false;
    if (outcome === "unknown") {
      this.write({
        ...entry,
        state: "uncertain",
        updatedAt: this.now(),
        ...(admissionId === undefined ? {} : { admissionId }),
      });
      return false;
    }
    fs.rmSync(file, { force: true });
    return true;
  }
}

/** The `indus:journal:*` handlers (P20-20). */
export function installJournalIpc(registry: IpcRegistry, journal: PreparedSubmissions): void {
  registry.handle("indus:journal:list", () => ({ entries: journal.list() }));
  registry.handle("indus:journal:prepare", ({ requestId, sessionId, payload }) => {
    journal.prepare(requestId, sessionId, payload);
    return { ok: true as const };
  });
  registry.handle("indus:journal:retire", ({ requestId, receipt }) => ({
    retired: journal.retire(requestId, receipt.outcome, receipt.admissionId),
  }));
}
