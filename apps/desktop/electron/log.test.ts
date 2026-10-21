import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  DesktopLog,
  LOG_BUFFER_MAX_BYTES,
  LOG_FLUSH_MS,
  LOG_ROTATE_BYTES,
  nodeLogFs,
  rotationSteps,
} from "./log.ts";
import { redactKnown, redactSecrets } from "./redact.ts";

class FakeTimers {
  now = 0;
  private queue: Array<{ at: number; fn: () => void; id: number }> = [];
  private ids = 0;
  setTimeout = (fn: () => void, ms: number): unknown => {
    const id = ++this.ids;
    this.queue.push({ at: this.now + ms, fn, id });
    return id;
  };
  clearTimeout = (handle: unknown): void => {
    this.queue = this.queue.filter((t) => t.id !== handle);
  };
  advance(ms: number): void {
    this.now += ms;
    const due = this.queue.filter((t) => t.at <= this.now);
    this.queue = this.queue.filter((t) => t.at > this.now);
    for (const t of due) t.fn();
  }
}

const dirs: string[] = [];
function tempLog(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "indus-desktop-log-"));
  dirs.push(dir);
  return path.join(dir, "logs", "desktop.log");
}
afterEach(() => {
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

const fixedNow = () => new Date(2026, 9, 21, 9, 15, 3, 42);
// Built at run time: never a credential-shaped literal in the source.
const fakeKey = () => ["sk", "-", "abcdefghijklmnopqrstuvwxyz012345"].join("");

describe("DesktopLog", () => {
  it("buffers and writes after 120 ms, creating the logs folder", () => {
    const file = tempLog();
    const timers = new FakeTimers();
    const log = new DesktopLog({ file, timers, now: fixedNow });
    log.write("hello");
    expect(fs.existsSync(file)).toBe(false);
    timers.advance(LOG_FLUSH_MS - 1);
    expect(fs.existsSync(file)).toBe(false);
    timers.advance(1);
    expect(fs.readFileSync(file, "utf8")).toBe("2026-10-21 09:15:03.042 INFO desktop: hello\n");
  });

  it("flushes at once past 64 KiB", () => {
    const file = tempLog();
    const log = new DesktopLog({ file, timers: new FakeTimers(), now: fixedNow });
    log.write("x".repeat(LOG_BUFFER_MAX_BYTES));
    expect(fs.statSync(file).size).toBeGreaterThan(LOG_BUFFER_MAX_BYTES);
  });

  it("redacts a token pattern before it reaches the file or the recent view", () => {
    const file = tempLog();
    const log = new DesktopLog({ file, timers: new FakeTimers(), now: fixedNow });
    const key = fakeKey();
    log.write(`calling provider with key ${key}`);
    log.write("Authorization: Bearer abcdefghijklmnopqrstuvwx");
    log.close();
    const text = fs.readFileSync(file, "utf8");
    expect(text).not.toContain(key);
    expect(text).not.toContain("abcdefghijklmnopqrstuvwx");
    expect(text).toContain("[redacted]");
    expect(log.recent().join("\n")).not.toContain(key);
  });

  it("writes the level when given", () => {
    const file = tempLog();
    const log = new DesktopLog({ file, timers: new FakeTimers(), now: fixedNow });
    log.write("renderer gone", "WARNING");
    log.flush();
    expect(fs.readFileSync(file, "utf8")).toBe(
      "2026-10-21 09:15:03.042 WARNING desktop: renderer gone\n",
    );
  });

  it("stamps each line of a multi-line message and drops after close", () => {
    const file = tempLog();
    const log = new DesktopLog({ file, timers: new FakeTimers(), now: fixedNow });
    log.write("one\ntwo\n");
    log.close();
    log.write("late");
    log.flush();
    expect(fs.readFileSync(file, "utf8").trim().split("\n")).toHaveLength(2);
  });

  it("swallows write failures", () => {
    const log = new DesktopLog({
      file: "/nonexistent/x/desktop.log",
      timers: new FakeTimers(),
      fs: {
        ...nodeLogFs,
        mkdirSync: () => {},
        appendFileSync: () => {
          throw new Error("EROFS");
        },
      },
    });
    log.write("x");
    expect(() => log.flush()).not.toThrow();
  });

  it("rotates a full file into .1 before appending", () => {
    const file = tempLog();
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, Buffer.alloc(LOG_ROTATE_BYTES, 97));
    fs.writeFileSync(`${file}.1`, "old one");
    const log = new DesktopLog({ file, timers: new FakeTimers(), now: fixedNow });
    log.write("fresh");
    log.flush();
    expect(fs.readFileSync(file, "utf8")).toContain("fresh");
    expect(fs.statSync(`${file}.1`).size).toBe(LOG_ROTATE_BYTES);
    expect(fs.readFileSync(`${file}.2`, "utf8")).toBe("old one");
  });
});

describe("rotationSteps", () => {
  it("nothing under the limit, a cascade at it, deletion far past it", () => {
    expect(rotationSteps(10, "d.log")).toEqual([]);
    expect(rotationSteps(LOG_ROTATE_BYTES, "d.log")).toEqual([
      ["rm", "d.log.3"],
      ["mv", "d.log.2", "d.log.3"],
      ["mv", "d.log.1", "d.log.2"],
      ["mv", "d.log", "d.log.1"],
    ]);
    expect(rotationSteps(LOG_ROTATE_BYTES * 5, "d.log").map((s) => s[0])).toEqual([
      "rm",
      "rm",
      "rm",
      "rm",
    ]);
  });
});

describe("redactSecrets", () => {
  const ticket = () =>
    ["indus-gateway-ticket", "QWERTYUIOPasdfghjkl_zxcvbnm-1234567890abcd"].join(".");
  it.each([
    ["vendor key", () => `key=${fakeKey()}`],
    ["ticket subprotocol", ticket],
    ["session token header", () => "X-Indus-Session-Token: 0123456789abcdef0123"],
    ["query token", () => "GET http://127.0.0.1:9/api/x?token=0123456789abcdef"],
    ["assignment", () => "OPENAI_API_KEY=whatever-it-is"],
    ["json pair", () => '{"api_key": "the-value-here"}'],
    ["url user-info", () => "https://me:hunter2hunter2@example.com/"],
  ])("masks a %s", (_name, make) => {
    const text = make();
    const out = redactSecrets(text);
    expect(out).toContain("[redacted]");
    expect(out).not.toBe(text);
  });

  it("keeps ordinary text", () => {
    expect(redactSecrets("window shown in 120 ms")).toBe("window shown in 120 ms");
  });

  it("masks known literal values", () => {
    expect(redactKnown("token is abcdefgh1234 here", ["abcdefgh1234"])).toBe(
      "token is [redacted] here",
    );
  });
});
