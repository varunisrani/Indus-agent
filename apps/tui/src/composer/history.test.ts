import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { HISTORY_LIMIT, historyPath, InputHistory, parseHistory } from "./history.ts";

const GOLDEN = resolve(import.meta.dirname, "../../../../tests/fixtures/tui/input_history.golden");
const ENTRIES = [
  "first prompt",
  "explain this\nacross two lines",
  "unicode: café 漢字 👍🏽",
  'quote "inside" and back\\slash',
  "last one",
];

let folder = "";

beforeEach(() => {
  folder = mkdtempSync(join(tmpdir(), "indus-history-test-"));
});

afterEach(() => {
  rmSync(folder, { recursive: true, force: true });
});

describe("history file shared with the classic CLI", () => {
  it("reads the golden file the classic CLI tests also read", () => {
    expect(new InputHistory(GOLDEN).entries()).toEqual(ENTRIES);
  });

  it("writes the same bytes as the classic CLI, owner-only", () => {
    const path = join(folder, "input_history");
    const history = new InputHistory(path);
    for (const entry of ENTRIES) history.push(entry);
    expect(readFileSync(path, "utf8")).toBe(readFileSync(GOLDEN, "utf8"));
    expect(statSync(path).mode & 0o777).toBe(0o600);
    expect(new InputHistory(path).entries()).toEqual(ENTRIES);
  });

  it("also reads the block shape (blank line, # stamp, + lines)", () => {
    const raw = "\n# 2026-10-01 10:00:00\n+one\n\n# 2026-10-01 10:01:00\n+two\n+lines\n";
    expect(parseHistory(raw)).toEqual(["one", "two\nlines"]);
  });

  it("skips damaged lines", () => {
    expect(parseHistory('"ok"\n"broken\nnot json\n"fine"\n')).toEqual(["ok", "fine"]);
  });
});

describe("history rules", () => {
  it(`keeps the newest ${HISTORY_LIMIT} of ${HISTORY_LIMIT + 1} entries`, () => {
    const path = join(folder, "input_history");
    const history = new InputHistory(path);
    for (let i = 0; i <= HISTORY_LIMIT; i += 1) history.push(`entry ${i}`);
    const kept = new InputHistory(path).entries();
    expect(kept).toHaveLength(HISTORY_LIMIT);
    expect(kept[0]).toBe("entry 1");
    expect(kept.at(-1)).toBe(`entry ${HISTORY_LIMIT}`);
  });

  it("skips blank input and an exact repeat of the newest entry", () => {
    const history = new InputHistory(join(folder, "input_history"));
    expect(history.push("same")).toBe(true);
    expect(history.push("same")).toBe(false);
    expect(history.push("   ")).toBe(false);
    expect(history.push("other")).toBe(true);
    expect(history.push("same")).toBe(true);
    expect(history.entries()).toEqual(["same", "other", "same"]);
  });

  it("loads lazily, once", () => {
    const path = join(folder, "input_history");
    writeFileSync(path, '"before"\n');
    const history = new InputHistory(path);
    expect(history.entries()).toEqual(["before"]);
    writeFileSync(path, '"changed behind our back"\n');
    expect(history.entries()).toEqual(["before"]);
  });

  it("stays silent when the file cannot be written", () => {
    const blocker = join(folder, "not-a-folder");
    writeFileSync(blocker, "a file where the home folder should be");
    const history = new InputHistory(join(blocker, "input_history"));
    expect(() => history.push("kept in memory")).not.toThrow();
    expect(history.entries()).toEqual(["kept in memory"]);
  });

  it("lives in the profile home the launcher exports", () => {
    expect(historyPath({ INDUS_HOME: "/x/profiles/work" })).toBe("/x/profiles/work/input_history");
    expect(historyPath({})).toMatch(/\.indus[\\/]input_history$/);
  });
});
