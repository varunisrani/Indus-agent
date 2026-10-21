// File and folder watches for the panes (P20-28 live reload; P20-29 watched folders).
//
// A watch belongs to the window that asked for it: its changes go to that window only, and the
// watch is closed the moment that window's web contents is destroyed (not at the next change, which
// may never come). Changes are debounced: a burst of saves is reported once, 120 ms after the last
// event. A file is watched through its folder, filtered by name, so an editor that saves by writing a
// temp file and renaming it over the original still counts. A raw watcher flooded with events
// (more than 1,000 within a second) is closed and replaced by a slow poll of a cheap snapshot.

import nodeFs from "node:fs";
import path from "node:path";

export const WATCH_DEBOUNCE_MS = 120;
export const WATCH_STORM_EVENTS = 1_000;
export const WATCH_STORM_WINDOW_MS = 1_000;
export const WATCH_POLL_MS = 5_000;
export const WATCHES_PER_OWNER = 64;

export interface Closable {
  close(): void;
}

export type RawListener = (eventType: string, filename: string | null) => void;

export interface WatchTimers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
  setInterval(fn: () => void, ms: number): unknown;
  clearInterval(handle: unknown): void;
  now(): number;
}

export const REAL_TIMERS: WatchTimers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (handle) => clearInterval(handle as ReturnType<typeof setInterval>),
  now: () => Date.now(),
};

export interface WatchFs {
  /** Start a raw watcher on a folder (fs.watch). */
  watch(dir: string, listener: RawListener): Closable;
  /** A cheap fingerprint for the poll fallback; null when it cannot be read. */
  snapshot(target: string, kind: "file" | "dir"): string | null;
  exists(target: string): boolean;
}

export const NODE_WATCH_FS: WatchFs = {
  watch: (dir, listener) => {
    const watcher = nodeFs.watch(dir, { persistent: false }, (eventType, filename) =>
      listener(eventType, filename === null ? null : String(filename)),
    );
    watcher.on("error", () => watcher.close());
    return watcher;
  },
  snapshot: (target, kind) => {
    try {
      if (kind === "dir") return nodeFs.readdirSync(target).sort().join("\0");
      const info = nodeFs.statSync(target);
      return `${info.mtimeMs}:${info.size}`;
    } catch {
      return null;
    }
  },
  exists: (target) => nodeFs.existsSync(target),
};

/** The web contents a watch reports to. */
export interface WatchOwner {
  readonly id: number;
  isDestroyed(): boolean;
  on(event: "destroyed", listener: () => void): unknown;
  removeListener?(event: "destroyed", listener: () => void): unknown;
}

export interface Change {
  /** The child that changed (a folder watch), or the watched file. */
  path: string;
  kind: "add" | "change" | "unlink";
}

interface GuardedOptions {
  dir: string;
  /** Report only events for this name (a file watch); null for every child (a folder watch). */
  onlyName: string | null;
  fs: WatchFs;
  timers: WatchTimers;
  onChange: (change: Change) => void;
  onStorm?: () => void;
}

/** One debounced, storm-guarded raw watch on `dir`. */
class GuardedWatch implements Closable {
  private readonly options: GuardedOptions;
  private raw: Closable | null;
  private debounce: unknown = null;
  private poll: unknown = null;
  private lastSnapshot: string | null = null;
  private windowStart = 0;
  private windowEvents = 0;
  private pending: { name: string | null; eventType: string } | null = null;
  private closed = false;

  constructor(options: GuardedOptions) {
    this.options = options;
    this.windowStart = options.timers.now();
    this.raw = options.fs.watch(options.dir, (eventType, filename) =>
      this.onRaw(eventType, filename),
    );
  }

  mode(): "watch" | "poll" | "closed" {
    if (this.closed) return "closed";
    return this.raw === null ? "poll" : "watch";
  }

  private onRaw(eventType: string, filename: string | null): void {
    if (this.closed || this.raw === null) return;
    const { timers } = this.options;
    const now = timers.now();
    if (now - this.windowStart > WATCH_STORM_WINDOW_MS) {
      this.windowStart = now;
      this.windowEvents = 0;
    }
    this.windowEvents += 1;
    if (this.windowEvents > WATCH_STORM_EVENTS) {
      this.toPoll();
      return;
    }
    const name = filename === null ? null : path.basename(filename);
    if (this.options.onlyName !== null && name !== null && name !== this.options.onlyName) return;
    this.pending = { name, eventType };
    if (this.debounce !== null) timers.clearTimeout(this.debounce);
    this.debounce = timers.setTimeout(() => {
      this.debounce = null;
      this.flush();
    }, WATCH_DEBOUNCE_MS);
  }

  private flush(): void {
    const pending = this.pending;
    this.pending = null;
    if (pending === null || this.closed) return;
    const { dir, onlyName, fs } = this.options;
    const name = onlyName ?? pending.name;
    const target = name === null ? dir : path.join(dir, name);
    const present = fs.exists(target);
    if (onlyName !== null && !present) return; // a deleted preview file: nothing to show
    const kind: Change["kind"] =
      pending.eventType === "change" ? "change" : present ? "add" : "unlink";
    this.options.onChange({ path: target, kind: onlyName !== null ? "change" : kind });
  }

  private toPoll(): void {
    this.raw?.close();
    this.raw = null;
    if (this.debounce !== null) this.options.timers.clearTimeout(this.debounce);
    this.debounce = null;
    this.options.onStorm?.();
    const { dir, onlyName, fs, timers } = this.options;
    const target = onlyName === null ? dir : path.join(dir, onlyName);
    const kind = onlyName === null ? "dir" : "file";
    this.lastSnapshot = fs.snapshot(target, kind);
    this.poll = timers.setInterval(() => {
      const next = fs.snapshot(target, kind);
      if (next === this.lastSnapshot) return;
      this.lastSnapshot = next;
      if (next === null && onlyName !== null) return;
      this.options.onChange({ path: target, kind: "change" });
    }, WATCH_POLL_MS);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.raw?.close();
    this.raw = null;
    if (this.debounce !== null) this.options.timers.clearTimeout(this.debounce);
    if (this.poll !== null) this.options.timers.clearInterval(this.poll);
  }
}

export class WatchLimitError extends Error {}

interface Entry {
  ownerId: number;
  watch: GuardedWatch;
}

export interface WatchHubOptions {
  fs?: WatchFs;
  timers?: WatchTimers;
  log?: (line: string) => void;
  /** Ids are random in production; tests pass a counter. */
  newId?: () => string;
  /** Called when an owner's web contents is destroyed (after its watches closed). */
  onOwnerGone?: (ownerId: number) => void;
}

/** Every watch main holds for the windows, keyed by id and owner. */
export class WatchHub {
  private readonly fs: WatchFs;
  private readonly timers: WatchTimers;
  private readonly log: (line: string) => void;
  private readonly newId: () => string;
  private readonly onOwnerGone: ((ownerId: number) => void) | undefined;
  private readonly entries = new Map<string, Entry>();
  private readonly tiedOwners = new Set<number>();

  constructor(options: WatchHubOptions = {}) {
    this.fs = options.fs ?? NODE_WATCH_FS;
    this.timers = options.timers ?? REAL_TIMERS;
    this.log = options.log ?? (() => {});
    this.newId = options.newId ?? (() => crypto.randomUUID());
    this.onOwnerGone = options.onOwnerGone;
  }

  /** Watch one file (by its folder); `onChange` gets each debounced change. */
  watchFile(owner: WatchOwner, file: string, onChange: (change: Change) => void): string {
    return this.add(owner, path.dirname(file), path.basename(file), onChange);
  }

  /** Watch a folder's children. */
  watchDirectory(owner: WatchOwner, dir: string, onChange: (change: Change) => void): string {
    return this.add(owner, dir, null, onChange);
  }

  /** Close one watch (only its owner may); true when it existed. */
  unwatch(ownerId: number, id: string): boolean {
    const entry = this.entries.get(id);
    if (entry === undefined || entry.ownerId !== ownerId) return false;
    this.drop(id);
    return true;
  }

  /** Watches held by `ownerId`. */
  countFor(ownerId: number): number {
    let count = 0;
    for (const entry of this.entries.values()) if (entry.ownerId === ownerId) count += 1;
    return count;
  }

  size(): number {
    return this.entries.size;
  }

  closeAll(): void {
    for (const id of [...this.entries.keys()]) this.drop(id);
  }

  private add(
    owner: WatchOwner,
    dir: string,
    onlyName: string | null,
    onChange: (change: Change) => void,
  ): string {
    if (owner.isDestroyed()) throw new WatchLimitError("the window is gone");
    if (this.countFor(owner.id) >= WATCHES_PER_OWNER) {
      throw new WatchLimitError(`at most ${WATCHES_PER_OWNER} watches per window`);
    }
    const id = this.newId();
    const watch = new GuardedWatch({
      dir,
      onlyName,
      fs: this.fs,
      timers: this.timers,
      onChange: (change) => {
        if (owner.isDestroyed()) {
          this.drop(id);
          return;
        }
        onChange(change);
      },
      onStorm: () => this.log(`[watch] event storm on a watched folder; polling it instead`),
    });
    this.entries.set(id, { ownerId: owner.id, watch });
    this.tie(owner);
    return id;
  }

  /** Close every watch of `owner` once its web contents is destroyed. */
  private tie(owner: WatchOwner): void {
    if (this.tiedOwners.has(owner.id)) return;
    this.tiedOwners.add(owner.id);
    owner.on("destroyed", () => {
      this.tiedOwners.delete(owner.id);
      for (const [id, entry] of [...this.entries]) if (entry.ownerId === owner.id) this.drop(id);
      this.onOwnerGone?.(owner.id);
    });
  }

  private drop(id: string): void {
    const entry = this.entries.get(id);
    if (entry === undefined) return;
    this.entries.delete(id);
    entry.watch.close();
  }

  /** For tests: the mode of one watch. */
  modeOf(id: string): "watch" | "poll" | "closed" {
    return this.entries.get(id)?.watch.mode() ?? "closed";
  }
}
