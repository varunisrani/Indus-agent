// Prompt history shared with the classic CLI (P12-17): `<profile home>/input_history`.
// The classic CLI (P06, `indus.cli.repl.history`) writes one JSON string per line and rewrites
// the file atomically with mode 0600; we write the same shape so both front ends share one file.
// Reading also accepts the older block shape (blank line, `# <time>`, one `+` line per text line).
// Loaded lazily once; the newest 1,000 entries kept; blank input and an exact repeat of the newest
// entry are skipped; a file that cannot be read or written never reaches the user.

import { chmodSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

export const HISTORY_LIMIT = 1000;
export const HISTORY_FILE = "input_history";

/** `$INDUS_HOME/input_history` (the active profile's home), else `~/.indus/input_history`. */
export function historyPath(env: NodeJS.ProcessEnv = process.env): string {
  const home = env.INDUS_HOME?.trim();
  return join(home ? home : join(homedir(), ".indus"), HISTORY_FILE);
}

/** Entries, oldest first, from either the JSON-lines shape or the `+`-block shape. */
export function parseHistory(raw: string): string[] {
  const entries: string[] = [];
  let block: string[] | null = null;
  const flush = (): void => {
    if (block !== null) entries.push(block.join("\n"));
    block = null;
  };
  for (const line of raw.split("\n")) {
    if (line.startsWith("+")) {
      block ??= [];
      block.push(line.slice(1));
      continue;
    }
    flush();
    if (!line.startsWith('"')) continue;
    try {
      const value: unknown = JSON.parse(line);
      if (typeof value === "string") entries.push(value);
    } catch {
      // a damaged line is skipped, the rest still loads
    }
  }
  flush();
  return entries;
}

/** The classic CLI's shape: one JSON string per line, newline-terminated. */
export function formatHistory(entries: readonly string[]): string {
  return entries.map((entry) => `${JSON.stringify(entry)}\n`).join("");
}

export class InputHistory {
  private loaded: string[] | null = null;
  readonly path: string;
  readonly limit: number;

  constructor(path: string = historyPath(), limit: number = HISTORY_LIMIT) {
    this.path = path;
    this.limit = limit;
  }

  /** Oldest first; the file is read on the first call only. */
  entries(): readonly string[] {
    if (this.loaded === null) {
      try {
        this.loaded = parseHistory(readFileSync(this.path, "utf8")).slice(-this.limit);
      } catch {
        this.loaded = [];
      }
    }
    return this.loaded;
  }

  /** Remember a submitted prompt; false when it was blank or repeats the newest entry. */
  push(text: string): boolean {
    const entry = text.trim();
    if (entry === "") return false;
    const list = [...this.entries()];
    if (list.at(-1) === entry) return false;
    list.push(entry);
    this.loaded = list.slice(-this.limit);
    this.save();
    return true;
  }

  private save(): void {
    const temp = `${this.path}.${process.pid}.tmp`;
    try {
      mkdirSync(dirname(this.path), { recursive: true, mode: 0o700 });
      writeFileSync(temp, formatHistory(this.entries()), { encoding: "utf8", mode: 0o600 });
      chmodSync(temp, 0o600);
      renameSync(temp, this.path);
    } catch {
      // history is a convenience: a read-only home must not interrupt typing
    }
  }
}
