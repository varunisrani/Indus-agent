import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { FakeContents } from "../../testing/paneIpc.ts";
import {
  type Change,
  type RawListener,
  WATCH_DEBOUNCE_MS,
  WATCH_POLL_MS,
  WATCH_STORM_EVENTS,
  WATCHES_PER_OWNER,
  WatchHub,
  WatchLimitError,
  type WatchTimers,
} from "./fileWatch.ts";

class Clock implements WatchTimers {
  now_ = 0;
  private items: Array<{ at: number; fn: () => void; every: number | null; id: number }> = [];
  private ids = 0;
  setTimeout = (fn: () => void, ms: number) => this.add(fn, ms, null);
  setInterval = (fn: () => void, ms: number) => this.add(fn, ms, ms);
  clearTimeout = (handle: unknown) => this.remove(handle);
  clearInterval = (handle: unknown) => this.remove(handle);
  now = () => this.now_;
  pending = () => this.items.length;
  private add(fn: () => void, ms: number, every: number | null) {
    const id = ++this.ids;
    this.items.push({ at: this.now_ + ms, fn, every, id });
    return id;
  }
  private remove(handle: unknown) {
    this.items = this.items.filter((t) => t.id !== handle);
  }
  advance(ms: number) {
    const until = this.now_ + ms;
    for (;;) {
      this.items.sort((a, b) => a.at - b.at);
      const next = this.items[0];
      if (next === undefined || next.at > until) break;
      this.now_ = next.at;
      if (next.every === null) this.items.shift();
      else next.at += next.every;
      next.fn();
    }
    this.now_ = until;
  }
}

let raws: Array<{ dir: string; listener: RawListener; closed: boolean }> = [];
let present = new Set<string>();
let snapshot = "a";
let clock: Clock;
let hub: WatchHub;
let gone: number[] = [];

beforeEach(() => {
  raws = [];
  present = new Set();
  snapshot = "a";
  gone = [];
  clock = new Clock();
  let n = 0;
  hub = new WatchHub({
    timers: clock,
    newId: () => `w${++n}`,
    onOwnerGone: (id) => gone.push(id),
    fs: {
      watch: (dir, listener) => {
        const raw = { dir, listener, closed: false };
        raws.push(raw);
        return { close: () => (raw.closed = true) };
      },
      snapshot: () => snapshot,
      exists: (p) => present.has(p),
    },
  });
});

function raw(index = 0) {
  const found = raws[index];
  if (found === undefined) throw new Error("no raw watcher");
  return found;
}

describe("file watch (preview live reload)", () => {
  it("watches the file's folder and reports one change per 120 ms burst", () => {
    const owner = new FakeContents(5);
    const changes: Change[] = [];
    present.add("/w/page.html");
    hub.watchFile(owner, "/w/page.html", (c) => changes.push(c));
    expect(raw().dir).toBe("/w");
    raw().listener("change", "page.html");
    clock.advance(60);
    raw().listener("rename", "page.html"); // an editor's save-by-rename
    clock.advance(60);
    raw().listener("change", "page.html");
    clock.advance(WATCH_DEBOUNCE_MS - 1);
    expect(changes).toEqual([]);
    clock.advance(1);
    expect(changes).toEqual([{ path: "/w/page.html", kind: "change" }]);
  });

  it("ignores other names in the folder and a deleted file", () => {
    const owner = new FakeContents(5);
    const changes: Change[] = [];
    hub.watchFile(owner, "/w/page.html", (c) => changes.push(c));
    present.add("/w/other.css");
    raw().listener("change", "other.css");
    clock.advance(500);
    raw().listener("rename", "page.html"); // gone
    clock.advance(500);
    expect(changes).toEqual([]);
    present.add("/w/page.html"); // written again
    raw().listener("rename", "page.html");
    clock.advance(WATCH_DEBOUNCE_MS);
    expect(changes).toHaveLength(1);
  });

  it("a folder watch reports add / change / unlink of children", () => {
    const owner = new FakeContents(5);
    const changes: Change[] = [];
    hub.watchDirectory(owner, "/w", (c) => changes.push(c));
    present.add("/w/new.txt");
    raw().listener("rename", "new.txt");
    clock.advance(WATCH_DEBOUNCE_MS);
    raw().listener("rename", "old.txt");
    clock.advance(WATCH_DEBOUNCE_MS);
    raw().listener("change", "new.txt");
    clock.advance(WATCH_DEBOUNCE_MS);
    expect(changes).toEqual([
      { path: "/w/new.txt", kind: "add" },
      { path: "/w/old.txt", kind: "unlink" },
      { path: "/w/new.txt", kind: "change" },
    ]);
  });

  it("dies with the window that asked, and only the owner may stop it", () => {
    const owner = new FakeContents(5);
    const id = hub.watchFile(owner, "/w/a.html", () => {});
    expect(hub.unwatch(6, id)).toBe(false);
    expect(raw().closed).toBe(false);
    owner.destroy();
    expect(raw().closed).toBe(true);
    expect(hub.size()).toBe(0);
    expect(gone).toEqual([5]);
    expect(clock.pending()).toBe(0);
  });

  it(`at most ${WATCHES_PER_OWNER} per window`, () => {
    const owner = new FakeContents(5);
    for (let i = 0; i < WATCHES_PER_OWNER; i += 1) hub.watchFile(owner, `/w/${i}`, () => {});
    expect(() => hub.watchFile(owner, "/w/x", () => {})).toThrow(WatchLimitError);
    expect(() => hub.watchFile(new FakeContents(6), "/w/x", () => {})).not.toThrow();
  });

  it("an event storm switches the watch to a slow poll", () => {
    const owner = new FakeContents(5);
    const changes: Change[] = [];
    const id = hub.watchDirectory(owner, "/w", (c) => changes.push(c));
    for (let i = 0; i <= WATCH_STORM_EVENTS; i += 1) raw().listener("rename", `f${i}`);
    expect(hub.modeOf(id)).toBe("poll");
    expect(raw().closed).toBe(true);
    clock.advance(WATCH_POLL_MS);
    expect(changes).toEqual([]);
    snapshot = "b";
    clock.advance(WATCH_POLL_MS);
    expect(changes).toEqual([{ path: "/w", kind: "change" }]);
    hub.closeAll();
    expect(clock.pending()).toBe(0);
  });
});

describe("file watch on the real file system", () => {
  let dir = "";
  beforeEach(() => {
    dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "indus-watch-")));
  });
  afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

  it("sees a rewrite of the watched file", async () => {
    const file = path.join(dir, "page.html");
    fs.writeFileSync(file, "one");
    const real = new WatchHub();
    const owner = new FakeContents(9);
    const seen = new Promise<Change>((resolve) => real.watchFile(owner, file, resolve));
    await new Promise((r) => setTimeout(r, 50));
    fs.writeFileSync(file, "two");
    const change = await seen;
    expect(change.path).toBe(file);
    real.closeAll();
  });
});
