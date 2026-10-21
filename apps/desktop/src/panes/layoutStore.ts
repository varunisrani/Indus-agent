// The window's pane layout state (P20-27): one layout per interface mode, persisted in local
// storage, plus the zone that has focus (not persisted). Every edit goes through the pure tree
// operations in `paneTree.ts`; the store enforces the two rules the tree does not know: the chat
// pane is always there, and Simple mode never holds a pane it hides.

import {
  activate,
  allPanes,
  CHAT_PANE,
  closePane,
  cycleTab,
  type DropTarget,
  insertPane,
  jumpToSlot,
  movePane,
  openPane,
  type PaneId,
  type PaneLayout,
  parseLayout,
  removePane,
  reopenLastClosed,
  resizeSplit,
  retainPanes,
  setHidden,
  setMinimized,
  zoneOf,
  zones,
} from "./paneTree.ts";
import {
  applyPreset,
  DEFAULT_MODE,
  INTERFACE_MODES,
  type InterfaceMode,
  initialLayout,
  PRESET_IDS,
  type PresetId,
  paneAllowed,
} from "./presets.ts";

export const LAYOUT_KEY = "indus.desktop.panes.v1";

export type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

interface ModeState {
  layout: PaneLayout | null;
  preset: PresetId | null;
}

interface Snapshot {
  readonly mode: InterfaceMode;
  readonly layout: PaneLayout;
  readonly preset: PresetId | null;
  readonly focusedZone: string | null;
}

export interface LayoutStoreOptions {
  /** Default: `window.localStorage` when there is one. */
  storage?: StorageLike | null;
  /** The panes this window can show (registered). */
  available: () => readonly PaneId[];
}

function defaultStorage(): StorageLike | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function isMode(value: unknown): value is InterfaceMode {
  return typeof value === "string" && (INTERFACE_MODES as readonly string[]).includes(value);
}

function isPreset(value: unknown): value is PresetId {
  return typeof value === "string" && (PRESET_IDS as readonly string[]).includes(value);
}

export class LayoutStore {
  private readonly storage: StorageLike | null;
  private readonly available: () => readonly PaneId[];
  private readonly listeners = new Set<() => void>();
  private readonly modes: Record<InterfaceMode, ModeState> = {
    advanced: { layout: null, preset: null },
    simple: { layout: null, preset: null },
  };
  private currentMode: InterfaceMode = DEFAULT_MODE;
  private focused: string | null = null;
  private snapshot: Snapshot | null = null;

  constructor(options: LayoutStoreOptions) {
    this.storage = options.storage === undefined ? defaultStorage() : options.storage;
    this.available = options.available;
    this.load();
  }

  // -- reading -----------------------------------------------------------------------------------

  /** A stable object until the next change (for `useSyncExternalStore`). */
  getSnapshot(): Snapshot {
    if (this.snapshot === null) {
      this.snapshot = {
        mode: this.currentMode,
        layout: this.layout(),
        preset: this.modes[this.currentMode].preset,
        focusedZone: this.focusedZone(),
      };
    }
    return this.snapshot;
  }

  mode(): InterfaceMode {
    return this.currentMode;
  }

  /** The current mode's layout (built from its initial preset the first time). */
  layout(): PaneLayout {
    const state = this.modes[this.currentMode];
    if (state.layout === null) {
      state.layout = this.enforce(initialLayout(this.currentMode, this.available()));
    }
    return state.layout;
  }

  /** The focused zone, else the chat's zone. */
  focusedZone(): string | null {
    const layout = this.layout();
    if (this.focused !== null && zones(layout.root).some((z) => z.id === this.focused)) {
      return this.focused;
    }
    return zoneOf(layout.root, CHAT_PANE)?.id ?? null;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  // -- editing -----------------------------------------------------------------------------------

  setMode(mode: InterfaceMode): void {
    if (mode === this.currentMode) return;
    this.currentMode = mode;
    this.focused = null;
    this.persist();
    this.changed();
  }

  setFocusedZone(zoneId: string | null): void {
    if (zoneId === this.focused) return;
    this.focused = zoneId;
    this.changed();
  }

  /** Open a pane (from the closed stack if it is there) and bring it to front. */
  open(pane: PaneId): boolean {
    if (!paneAllowed(this.currentMode, pane)) return false;
    this.edit((layout) => openPane(layout, pane));
    return true;
  }

  close(pane: PaneId): void {
    this.edit((layout) => closePane(layout, pane));
  }

  move(pane: PaneId, target: DropTarget): void {
    this.edit((layout) => movePane(layout, pane, target));
  }

  activate(pane: PaneId): void {
    this.edit((layout) => activate(layout, pane));
    const host = zoneOf(this.layout().root, pane);
    if (host !== null) this.setFocusedZone(host.id);
  }

  setHidden(pane: PaneId, hidden: boolean): void {
    this.edit((layout) => setHidden(layout, pane, hidden));
  }

  setMinimized(zoneId: string, minimized: boolean): void {
    this.edit((layout) => setMinimized(layout, zoneId, minimized));
  }

  resize(splitId: string, sizes: readonly number[]): void {
    this.edit((layout) => resizeSplit(layout, splitId, sizes));
  }

  /** Reopen the most recently closed pane this mode may show; returns it. */
  reopenLast(): PaneId | null {
    let reopened: PaneId | null = null;
    this.edit((layout) => {
      const result = reopenLastClosed(layout, (pane) => this.mayShow(pane));
      reopened = result.pane;
      return result.layout;
    });
    return reopened;
  }

  cycle(step: 1 | -1): void {
    const zoneId = this.focusedZone();
    if (zoneId !== null) this.edit((layout) => cycleTab(layout, zoneId, step));
  }

  jump(slot: number): void {
    const zoneId = this.focusedZone();
    if (zoneId !== null) this.edit((layout) => jumpToSlot(layout, zoneId, slot));
  }

  applyPreset(id: PresetId): void {
    const panes = this.available().filter((pane) => this.mayShow(pane));
    this.edit((layout) => applyPreset(id, layout, panes));
    this.modes[this.currentMode].preset = id;
    this.persist();
    this.changed();
  }

  /** Throw away the current mode's arrangement (it is rebuilt from its initial preset). */
  reset(): void {
    this.modes[this.currentMode] = { layout: null, preset: null };
    this.persist();
    this.changed();
  }

  // -- internals ---------------------------------------------------------------------------------

  private mayShow(pane: PaneId): boolean {
    return paneAllowed(this.currentMode, pane) && this.available().includes(pane);
  }

  /** The store's own rules on top of the tree's: chat present, mode-hidden panes out. */
  private enforce(layout: PaneLayout): PaneLayout {
    let next = layout;
    if (this.currentMode === "simple") {
      next = retainPanes(next, (pane) => paneAllowed("simple", pane));
    }
    if (!allPanes(next.root).includes(CHAT_PANE)) {
      const first = zones(next.root)[0];
      next = insertPane(
        next,
        CHAT_PANE,
        first === undefined ? undefined : { zoneId: first.id, edge: "left" },
      );
    }
    return next;
  }

  private edit(fn: (layout: PaneLayout) => PaneLayout): void {
    const before = this.layout();
    const after = this.enforce(fn(before));
    if (after === before) return;
    const state = this.modes[this.currentMode];
    state.layout = after;
    state.preset = null; // a hand edit leaves the preset
    this.persist();
    this.changed();
  }

  private changed(): void {
    this.snapshot = null;
    for (const listener of [...this.listeners]) listener();
  }

  private load(): void {
    let raw: string | null = null;
    try {
      raw = this.storage?.getItem(LAYOUT_KEY) ?? null;
    } catch {
      raw = null;
    }
    if (raw === null) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return;
    }
    if (typeof parsed !== "object" || parsed === null) return;
    const value = parsed as Record<string, unknown>;
    if (isMode(value.mode)) this.currentMode = value.mode;
    const layouts = (value.layouts ?? {}) as Record<string, unknown>;
    const presets = (value.presets ?? {}) as Record<string, unknown>;
    for (const mode of INTERFACE_MODES) {
      const layout = parseLayout(layouts[mode]);
      const preset = presets[mode];
      this.modes[mode] = { layout, preset: isPreset(preset) ? preset : null };
    }
    // a stored layout from an older build may hold a pane the mode hides
    const keep = this.currentMode;
    for (const mode of INTERFACE_MODES) {
      const layout = this.modes[mode].layout;
      if (layout === null) continue;
      this.currentMode = mode;
      this.modes[mode].layout = this.enforce(layout);
    }
    this.currentMode = keep;
  }

  private persist(): void {
    if (this.storage === null) return;
    const value = {
      version: 1,
      mode: this.currentMode,
      layouts: { advanced: this.modes.advanced.layout, simple: this.modes.simple.layout },
      presets: { advanced: this.modes.advanced.preset, simple: this.modes.simple.preset },
    };
    try {
      this.storage.setItem(LAYOUT_KEY, JSON.stringify(value));
    } catch {
      // storage full or unavailable: the layout still works for this window
    }
  }
}

/** Panes the tree holds that nothing registered (left out of the rendered view). */
export function withoutUnknown(layout: PaneLayout, known: ReadonlySet<PaneId>): PaneLayout {
  let next = layout;
  for (const pane of allPanes(layout.root)) {
    if (!known.has(pane)) next = removePane(next, pane);
  }
  return next;
}
