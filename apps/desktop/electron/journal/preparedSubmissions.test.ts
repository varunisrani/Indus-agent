import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { IpcError } from "../ipc/envelope.ts";
import { MAX_ENTRIES, MAX_PAYLOAD_BYTES, PreparedSubmissions } from "./preparedSubmissions.ts";

const dirs: string[] = [];

function tempDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-journal-"));
  dirs.push(dir);
  return path.join(dir, "prepared-submissions");
}

afterEach(() => {
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

const posix = process.platform !== "win32";

describe("prepared-submission journal", () => {
  it("writes private files (0600 in a 0700 directory)", () => {
    const dir = tempDir();
    const journal = new PreparedSubmissions({ dir });
    journal.prepare("req-1", "s1", { text: "hello" });
    const [name] = fs.readdirSync(dir);
    expect(name).toMatch(/^[0-9a-f]{64}\.json$/);
    if (posix) {
      expect(fs.statSync(dir).mode & 0o777).toBe(0o700);
      expect(fs.statSync(path.join(dir, name as string)).mode & 0o777).toBe(0o600);
    }
    // no temp file left behind
    expect(fs.readdirSync(dir)).toHaveLength(1);
  });

  it("survives a restart between the write and the receipt", () => {
    const dir = tempDir();
    new PreparedSubmissions({ dir, now: () => 10 }).prepare("req-1", "s1", { text: "kept" });
    // a new process: nothing in memory, only the files
    const reopened = new PreparedSubmissions({ dir });
    expect(reopened.list()).toEqual([
      {
        requestId: "req-1",
        sessionId: "s1",
        createdAt: 10,
        updatedAt: 10,
        state: "prepared",
        payload: { text: "kept" },
      },
    ]);
  });

  it("an accepted receipt retires the entry", () => {
    const journal = new PreparedSubmissions({ dir: tempDir() });
    journal.prepare("req-1", "s1", { text: "a" });
    expect(journal.retire("req-1", "accepted", "adm-1")).toBe(true);
    expect(journal.list()).toEqual([]);
    expect(journal.retire("req-1", "accepted")).toBe(false);
  });

  it("an unknown receipt keeps the entry, marked uncertain", () => {
    let now = 1;
    const journal = new PreparedSubmissions({ dir: tempDir(), now: () => now });
    journal.prepare("req-1", "s1", { text: "a" });
    now = 5;
    expect(journal.retire("req-1", "unknown")).toBe(false);
    const [entry] = journal.list();
    expect(entry).toMatchObject({
      requestId: "req-1",
      state: "uncertain",
      createdAt: 1,
      updatedAt: 5,
    });
    // a later retry that is accepted retires it
    journal.prepare("req-1", "s1", { text: "a" });
    expect(journal.retire("req-1", "accepted")).toBe(true);
    expect(journal.list()).toEqual([]);
  });

  it("a rejected receipt removes the entry (its text goes back to the composer)", () => {
    const journal = new PreparedSubmissions({ dir: tempDir() });
    journal.prepare("req-1", "s1", { text: "a" });
    expect(journal.retire("req-1", "rejected")).toBe(true);
    expect(journal.list()).toEqual([]);
  });

  it("a corrupt entry is moved aside and does not block later sends", () => {
    const dir = tempDir();
    const lines: string[] = [];
    const journal = new PreparedSubmissions({ dir, now: () => 42, log: (l) => lines.push(l) });
    journal.prepare("req-1", "s1", { text: "a" });
    const [name] = fs.readdirSync(dir);
    fs.writeFileSync(path.join(dir, name as string), "{ torn");
    expect(journal.list()).toEqual([]);
    expect(fs.readdirSync(dir)).toEqual([`${name}.corrupt-42`]);
    expect(lines.join("\n")).toContain("moved unreadable entry aside");
    journal.prepare("req-2", "s1", { text: "b" });
    expect(journal.list().map((e) => e.requestId)).toEqual(["req-2"]);
  });

  it("refuses an oversized payload and a full journal", () => {
    const journal = new PreparedSubmissions({ dir: tempDir() });
    const big = "x".repeat(MAX_PAYLOAD_BYTES + 1);
    expect(() => journal.prepare("req-big", "s1", big)).toThrow(IpcError);
    for (let i = 0; i < MAX_ENTRIES; i += 1) journal.prepare(`req-${i}`, "s1", i);
    expect(() => journal.prepare("one-more", "s1", 0)).toThrow(/still waiting/);
    // re-preparing an existing entry is still allowed
    expect(() => journal.prepare("req-0", "s1", 0)).not.toThrow();
  });

  it("lists entries oldest first", () => {
    let now = 3;
    const journal = new PreparedSubmissions({ dir: tempDir(), now: () => now });
    journal.prepare("late", "s1", 1);
    now = 1;
    journal.prepare("early", "s1", 2);
    expect(journal.list().map((e) => e.requestId)).toEqual(["early", "late"]);
  });
});
