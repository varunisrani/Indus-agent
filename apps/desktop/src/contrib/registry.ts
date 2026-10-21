// The contribution registry (P24-32): the one place the shell's extensible areas read from.
//
// A contribution is a small record `{ id, area, source, order?, … }`; the shell lists an area
// (`list("status-bar")`) and re-renders when it changes (`subscribe`). Bundled code registers
// directly; plugins never do — they get a `PluginContext` (`sdk.ts`) that stamps the source
// (`plugin:<id>`), namespaces the id (`<plugin>:<local>`) and stages what `register(ctx)` adds.
//
// Loading a plugin is a transaction (`stage`/`commit`/`abort`): everything a plugin's `register`
// adds is checked and held back; only when `register` returned without throwing — and stayed under
// `MAX_PER_SOURCE` — does all of it enter the registry, in one change. A throwing `register`
// leaves nothing behind. Every entry carries a disposer; removing a source drops all of its
// entries at once.

export const AREAS = [
  "pane",
  "route",
  "sidebar-nav",
  "status-bar",
  "title-bar",
  "palette-command",
  "keybind",
  "theme",
  "composer-extension",
  "session-row-decoration",
  "model-menu-decoration",
  "appearance-setting",
  "plugin-settings-page",
  "transcript-directive",
] as const;

export type Area = (typeof AREAS)[number];

/** At most this many live contributions per source (a plugin cannot flood an area). */
export const MAX_PER_SOURCE = 200;

const ID = /^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$/;

export interface Contribution {
  readonly id: string;
  readonly area: Area;
  /** `app` for bundled code, `plugin:<id>` for a plugin. */
  readonly source: string;
  /** Lower first; ties by id. */
  readonly order?: number;
  readonly [field: string]: unknown;
}

export class ContributionError extends Error {}

type Fn = (...args: never[]) => unknown;

function isFn(value: unknown): value is Fn {
  return typeof value === "function";
}

function isText(value: unknown, max = 256): value is string {
  return typeof value === "string" && value.trim() !== "" && value.length <= max;
}

/** The fields each area needs; anything else a contribution carries is the area's business. */
const REQUIRED: Readonly<Record<Area, (c: Contribution) => string | null>> = {
  pane: (c) => (isText(c.title) && isFn(c.render) ? null : "a pane needs title and render"),
  route: (c) =>
    typeof c.path === "string" && /^\/[a-z0-9][a-z0-9/_-]*$/.test(c.path) && isFn(c.render)
      ? null
      : "a route needs a path like /name and render",
  "sidebar-nav": (c) =>
    isText(c.label) && typeof c.path === "string" ? null : "a nav item needs label and path",
  "status-bar": (c) => (isFn(c.render) ? null : "a status item needs render"),
  "title-bar": (c) => (isFn(c.render) ? null : "a title bar item needs render"),
  "palette-command": (c) =>
    isText(c.title) && isFn(c.run) ? null : "a palette command needs title and run",
  keybind: (c) => (isText(c.keys, 64) && isFn(c.run) ? null : "a keybind needs keys and run"),
  theme: (c) =>
    isText(c.label) && typeof c.tokens === "object" && c.tokens !== null
      ? null
      : "a theme needs label and tokens",
  "composer-extension": (c) => (isFn(c.render) ? null : "a composer extension needs render"),
  "session-row-decoration": (c) => (isFn(c.render) ? null : "a decoration needs render"),
  "model-menu-decoration": (c) => (isFn(c.render) ? null : "a decoration needs render"),
  "appearance-setting": (c) =>
    isText(c.label) && isFn(c.render) ? null : "a setting needs label and render",
  "plugin-settings-page": (c) =>
    isText(c.title) && isFn(c.render) ? null : "a settings page needs title and render",
  "transcript-directive": (c) =>
    isText(c.name, 64) && isFn(c.render) ? null : "a directive needs name and render",
};

/** Throws `ContributionError` when `c` cannot be registered. */
export function checkContribution(c: Contribution): void {
  if (typeof c !== "object" || c === null) throw new ContributionError("not a contribution");
  if (!ID.test(c.id)) throw new ContributionError(`bad contribution id ${JSON.stringify(c.id)}`);
  if (!(AREAS as readonly string[]).includes(c.area)) {
    throw new ContributionError(`unknown area ${JSON.stringify(c.area)}`);
  }
  if (!isText(c.source, 128)) throw new ContributionError("a contribution needs its source");
  if (c.order !== undefined && !Number.isFinite(c.order)) {
    throw new ContributionError("order must be a number");
  }
  const problem = REQUIRED[c.area](c);
  if (problem !== null) throw new ContributionError(`${c.id}: ${problem}`);
}

type Listener = () => void;

export interface Staging {
  readonly source: string;
  add(c: Contribution): () => void;
}

export class ContributionRegistry {
  private readonly entries = new Map<string, Contribution>();
  private readonly listeners = new Set<Listener>();
  private version = 0;

  /** Register one contribution now; returns its disposer (idempotent). */
  register(c: Contribution): () => void {
    return this.registerMany([c]);
  }

  /** Register several at once: all of them or none. */
  registerMany(list: readonly Contribution[]): () => void {
    const fresh = new Map<string, Contribution>();
    const perSource = new Map<string, number>();
    for (const c of list) {
      checkContribution(c);
      if (this.entries.has(c.id) || fresh.has(c.id)) {
        throw new ContributionError(`contribution ${c.id} is already registered`);
      }
      fresh.set(c.id, c);
      perSource.set(c.source, (perSource.get(c.source) ?? 0) + 1);
    }
    for (const [source, adding] of perSource) {
      if (this.countOf(source) + adding > MAX_PER_SOURCE) {
        throw new ContributionError(`${source} would pass ${MAX_PER_SOURCE} contributions`);
      }
    }
    if (fresh.size === 0) return () => undefined;
    for (const [id, c] of fresh) this.entries.set(id, c);
    this.changed();
    let disposed = false;
    return () => {
      if (disposed) return;
      disposed = true;
      let removed = false;
      for (const [id, c] of fresh) {
        if (this.entries.get(id) === c) {
          this.entries.delete(id);
          removed = true;
        }
      }
      if (removed) this.changed();
    };
  }

  /**
   * Run `fill` with a staging area for `source`; commit what it added only when it returns
   * normally. Returns the disposer of the committed set; rethrows (with nothing registered) when
   * `fill` throws or the staged set is refused.
   */
  transaction(source: string, fill: (stage: Staging) => void): () => void {
    const staged: Contribution[] = [];
    const removed = new Set<Contribution>();
    let open = true;
    const stage: Staging = {
      source,
      add: (c) => {
        if (!open) throw new ContributionError("the load of this plugin already finished");
        checkContribution(c);
        if (c.source !== source) throw new ContributionError("a contribution of another source");
        staged.push(c);
        if (staged.length > MAX_PER_SOURCE) {
          throw new ContributionError(`${source} would pass ${MAX_PER_SOURCE} contributions`);
        }
        return () => {
          removed.add(c);
        };
      },
    };
    try {
      fill(stage);
    } finally {
      open = false;
    }
    return this.registerMany(staged.filter((c) => !removed.has(c)));
  }

  list(area: Area): Contribution[] {
    return [...this.entries.values()]
      .filter((c) => c.area === area)
      .sort((a, b) => (a.order ?? 0) - (b.order ?? 0) || a.id.localeCompare(b.id));
  }

  bySource(source: string): Contribution[] {
    return [...this.entries.values()].filter((c) => c.source === source);
  }

  countOf(source: string): number {
    let count = 0;
    for (const c of this.entries.values()) if (c.source === source) count += 1;
    return count;
  }

  /** Drop every entry of `source` (a plugin unloaded, whatever disposers it lost). */
  removeSource(source: string): void {
    let removed = false;
    for (const [id, c] of this.entries) {
      if (c.source === source) {
        this.entries.delete(id);
        removed = true;
      }
    }
    if (removed) this.changed();
  }

  snapshot = (): number => this.version;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private changed(): void {
    this.version += 1;
    for (const listener of [...this.listeners]) {
      try {
        listener();
      } catch {
        // a subscriber's bug never stops the others
      }
    }
  }
}

/** The app's registry. */
export const contributions = new ContributionRegistry();
