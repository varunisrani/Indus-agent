// The HUD (P20-35): one conversation in a small frameless window that floats above other apps.
//
// - It opens for a session (and the profile that session lives on) and the main window steps
//   aside while it is up; when it closes the main window comes back without taking focus.
// - Asked again for another session it switches in place (every window hears `hud:changed`); for
//   another profile it is replaced, because a renderer adopts its backend once at boot.
// - Moving and resizing are driven by main: the renderer says "the person pressed and holds the
//   grip / an edge" and "let go", and main follows the pointer in between. No coordinates cross
//   the bridge.
// - Where it sits is saved in `hud-state.json` and reused while ≥ 48 px of it are on a display.

import { readJsonFile, writeFileAtomic } from "../fsSecure.ts";
import type { BrowserWindowLike } from "../windows/manager.ts";
import { type DisplayLike, displayShowing, type Rect } from "../windows/windowState.ts";

export const HUD_DEFAULT_SIZE = { width: 620, height: 320 } as const;
export const HUD_MIN_SIZE = { width: 360, height: 160 } as const;
/** Gap between the default HUD and the bottom of the work area. */
export const HUD_BOTTOM_MARGIN = 72;
/** Pointer poll while a move or resize is held. */
export const HUD_DRAG_POLL_MS = 16;
/** A press that never reports its release stops being followed after this. */
export const HUD_DRAG_MAX_MS = 30_000;
/** Geometry is saved this long after the last change. */
export const HUD_SAVE_DEBOUNCE_MS = 300;
/** Snap-to-pointer puts the pointer this far below the HUD's top edge, centred. */
export const HUD_SNAP_ANCHOR_Y = 40;

export type DragMode = "move" | "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

export interface Point {
  x: number;
  y: number;
}

/** What the HUD needs from a BrowserWindow beyond the manager's view. */
export interface FloatingWindow extends BrowserWindowLike {
  getBounds(): Rect;
  setBounds(bounds: Rect): void;
  hide(): void;
  setResizable?(resizable: boolean): void;
  setAlwaysOnTop?(flag: boolean, level?: string): void;
  setVisibleOnAllWorkspaces?(visible: boolean, options?: Record<string, boolean>): void;
}

/**
 * Keep a floating window above full-screen apps and on every workspace (best effort: not every
 * window manager supports it).
 */
export function floatAbove(win: FloatingWindow, platform: string): void {
  try {
    win.setAlwaysOnTop?.(true, platform === "darwin" ? "floating" : "screen-saver");
    win.setVisibleOnAllWorkspaces?.(
      true,
      platform === "darwin" ? { visibleOnFullScreen: true, skipTransformProcessType: true } : {},
    );
  } catch {
    // Unsupported here: the window is still always on top from its options.
  }
}

export interface ScreenLike {
  getAllDisplays(): DisplayLike[];
  getPrimaryDisplay(): DisplayLike;
  getCursorScreenPoint(): Point;
  getDisplayNearestPoint(point: Point): DisplayLike;
}

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface HudState {
  open: boolean;
  sessionId: string | null;
  profile: string | null;
}

export interface HudDeps {
  /** `windows.openHud` — the window manager's factory, URL and checks. */
  create(options: { sessionId?: string; profile?: string | null }, bounds: Rect): FloatingWindow;
  /** The main window (to step aside), or null. */
  mainWindow(): (BrowserWindowLike & { hide(): void }) | null;
  screen: ScreenLike;
  statePath: string;
  timers: Timers;
  now(): number;
  /** Tell every window where the HUD stands (`indus:hud:changed`). */
  broadcast(state: HudState): void;
  /** The HUD came up / went away (the snap chord follows it). */
  onOpenChanged(open: boolean): void;
  platform?: string;
  log(line: string): void;
}

// -- geometry ------------------------------------------------------------------------------------

const finite = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

/** Default HUD bounds on a work area: centred, near the bottom. */
export function defaultHudBounds(area: Rect): Rect {
  const size = {
    width: Math.min(HUD_DEFAULT_SIZE.width, area.width),
    height: Math.min(HUD_DEFAULT_SIZE.height, area.height),
  };
  const spareX = area.width - size.width;
  const bottomGap = Math.min(HUD_BOTTOM_MARGIN, area.height - size.height);
  return {
    ...size,
    x: area.x + Math.round(spareX / 2),
    y: area.y + Math.max(0, area.height - size.height - bottomGap),
  };
}

/** Saved bounds that are still usable on the displays there are now, else null. */
export function usableSavedBounds(raw: unknown, displays: readonly DisplayLike[]): Rect | null {
  if (raw === null || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  if (![record.x, record.y, record.width, record.height].every(finite)) return null;
  const rect: Rect = {
    x: Math.round(record.x as number),
    y: Math.round(record.y as number),
    width: Math.max(HUD_MIN_SIZE.width, Math.round(record.width as number)),
    height: Math.max(HUD_MIN_SIZE.height, Math.round(record.height as number)),
  };
  return displayShowing(rect, displays) === null ? null : rect;
}

/** The HUD placed so the pointer sits at its top centre, kept inside the work area. */
export function snapBounds(
  pointer: Point,
  size: { width: number; height: number },
  area: Rect,
): Rect {
  const width = Math.min(size.width, area.width);
  const height = Math.min(size.height, area.height);
  const x = Math.round(pointer.x - width / 2);
  const y = Math.round(pointer.y - HUD_SNAP_ANCHOR_Y);
  return {
    width,
    height,
    x: Math.min(Math.max(x, area.x), area.x + area.width - width),
    y: Math.min(Math.max(y, area.y), area.y + area.height - height),
  };
}

/** Bounds after the pointer moved by (dx, dy) from where a drag in `mode` started. */
export function dragBounds(start: Rect, mode: DragMode, dx: number, dy: number): Rect {
  if (mode === "move") return { ...start, x: start.x + dx, y: start.y + dy };
  const next = { ...start };
  if (mode.includes("e")) next.width = Math.max(HUD_MIN_SIZE.width, start.width + dx);
  if (mode.includes("w")) {
    next.width = Math.max(HUD_MIN_SIZE.width, start.width - dx);
    next.x = start.x + start.width - next.width;
  }
  if (mode.includes("s")) next.height = Math.max(HUD_MIN_SIZE.height, start.height + dy);
  if (mode.includes("n")) {
    next.height = Math.max(HUD_MIN_SIZE.height, start.height - dy);
    next.y = start.y + start.height - next.height;
  }
  return next;
}

const sameRect = (a: Rect, b: Rect) =>
  a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height;

const profileKey = (profile: string | null | undefined): string | null => {
  const trimmed = (profile ?? "").trim();
  return trimmed === "" ? null : trimmed;
};

// -- the controller ------------------------------------------------------------------------------

interface Drag {
  mode: DragMode;
  pointer: Point;
  bounds: Rect;
  startedAt: number;
  timer: unknown;
}

export class HudController {
  private readonly deps: HudDeps;
  private win: FloatingWindow | null = null;
  private sessionId: string | null = null;
  private profile: string | null = null;
  private mainStepAside = false;
  private drag: Drag | null = null;
  private saveTimer: unknown = null;
  /** The HUD's last known bounds (saved even when the window is already gone). */
  private lastBounds: Rect | null = null;

  constructor(deps: HudDeps) {
    this.deps = deps;
  }

  state(): HudState {
    return { open: this.live() !== null, sessionId: this.sessionId, profile: this.profile };
  }

  /** True for the HUD's own web contents (its drag messages are the only ones followed). */
  isHud(contentsId: number): boolean {
    return this.live()?.webContents.id === contentsId;
  }

  open(sessionId: string | null, profile: string | null): HudState {
    const wantedProfile = profileKey(profile);
    const current = this.live();
    if (current !== null) {
      if (wantedProfile !== this.profile) {
        // Another backend: replace the window; the old one's close is not a teardown.
        this.sessionId = sessionId;
        this.profile = wantedProfile;
        const previous = current;
        this.win = this.spawn(previous.getBounds());
        previous.close();
        this.deps.broadcast(this.state());
        return this.state();
      }
      if (sessionId !== null && sessionId !== this.sessionId) {
        this.sessionId = sessionId;
        this.deps.broadcast(this.state());
      }
      current.focus();
      return this.state();
    }
    this.sessionId = sessionId;
    this.profile = wantedProfile;
    this.win = this.spawn(this.initialBounds());
    this.deps.onOpenChanged(true);
    this.deps.broadcast(this.state());
    return this.state();
  }

  close(): HudState {
    const win = this.live();
    if (win !== null) win.close();
    else this.teardown();
    return { open: false, sessionId: this.sessionId, profile: this.profile };
  }

  /** The global toggle: close an open HUD, else open it for `fallback` (the last-active session). */
  toggle(fallback: () => { sessionId: string | null; profile: string | null }): HudState {
    if (this.live() !== null) return this.close();
    const target = fallback();
    return this.open(target.sessionId, target.profile);
  }

  resetLayout(): boolean {
    const win = this.live();
    if (win === null) return false;
    const pointer = this.deps.screen.getCursorScreenPoint();
    const bounds = defaultHudBounds(this.deps.screen.getDisplayNearestPoint(pointer).workArea);
    this.apply(win, bounds);
    this.saveNow();
    return true;
  }

  snapToPointer(): void {
    const win = this.live();
    if (win === null) return;
    const pointer = this.deps.screen.getCursorScreenPoint();
    const area = this.deps.screen.getDisplayNearestPoint(pointer).workArea;
    this.apply(win, snapBounds(pointer, win.getBounds(), area));
    this.scheduleSave();
  }

  /** The renderer's press-and-hold on the grip (`move`) or an edge started / ended. */
  dragFrom(contentsId: number, phase: "start" | "end", mode: DragMode): void {
    if (!this.isHud(contentsId)) return;
    if (phase === "end") {
      this.endDrag();
      return;
    }
    const win = this.live();
    if (win === null) return;
    this.endDrag();
    this.drag = {
      mode,
      pointer: this.deps.screen.getCursorScreenPoint(),
      bounds: win.getBounds(),
      startedAt: this.deps.now(),
      timer: null,
    };
    this.followPointer();
  }

  dispose(): void {
    this.endDrag();
    const win = this.live();
    if (win !== null) win.close();
  }

  // -- internals ---------------------------------------------------------------------------------

  private live(): FloatingWindow | null {
    return this.win !== null && !this.win.isDestroyed() ? this.win : null;
  }

  private initialBounds(): Rect {
    const saved = usableSavedBounds(
      readJsonFile(this.deps.statePath),
      this.deps.screen.getAllDisplays(),
    );
    if (saved !== null) return saved;
    const pointer = this.deps.screen.getCursorScreenPoint();
    return defaultHudBounds(this.deps.screen.getDisplayNearestPoint(pointer).workArea);
  }

  private spawn(bounds: Rect): FloatingWindow {
    const win = this.deps.create(
      {
        ...(this.sessionId === null ? {} : { sessionId: this.sessionId }),
        profile: this.profile,
      },
      bounds,
    );
    floatAbove(win, this.deps.platform ?? process.platform);
    const persist = () => {
      if (this.win !== win || win.isDestroyed()) return;
      this.lastBounds = win.getBounds();
      this.scheduleSave();
    };
    win.on("move", persist);
    win.on("resize", persist);
    win.on("close", () => {
      if (this.win === win && !win.isDestroyed()) this.lastBounds = win.getBounds();
    });
    win.on("show", () => {
      if (this.win !== win) return;
      const main = this.deps.mainWindow();
      if (main !== null && !main.isDestroyed() && main.isVisible() && !this.mainStepAside) {
        this.mainStepAside = true;
        main.hide();
      }
    });
    win.on("closed", () => {
      if (this.win !== win) return; // replaced by a respawn: nothing to hand back
      this.teardown();
    });
    return win;
  }

  private teardown(): void {
    this.endDrag();
    this.flushSave();
    this.win = null;
    this.deps.onOpenChanged(false);
    if (this.mainStepAside) {
      this.mainStepAside = false;
      const main = this.deps.mainWindow();
      if (main !== null && !main.isDestroyed()) {
        if (main.isMinimized()) main.restore();
        main.show();
      }
    }
    this.deps.broadcast({ open: false, sessionId: this.sessionId, profile: this.profile });
  }

  private apply(win: FloatingWindow, bounds: Rect): void {
    const now = win.getBounds();
    if (sameRect(now, bounds)) return;
    const resizing = now.width !== bounds.width || now.height !== bounds.height;
    // A non-resizable window refuses a new size on some platforms: open the gate for the call.
    if (resizing) win.setResizable?.(true);
    try {
      win.setBounds(bounds);
    } finally {
      if (resizing) win.setResizable?.(false);
    }
    this.lastBounds = win.getBounds();
  }

  private followPointer(): void {
    const drag = this.drag;
    const win = this.live();
    if (drag === null) return;
    if (win === null || this.deps.now() - drag.startedAt > HUD_DRAG_MAX_MS) {
      if (win !== null) this.deps.log("[hud] a drag never reported its release; stopped following");
      this.endDrag();
      return;
    }
    const pointer = this.deps.screen.getCursorScreenPoint();
    this.apply(
      win,
      dragBounds(drag.bounds, drag.mode, pointer.x - drag.pointer.x, pointer.y - drag.pointer.y),
    );
    drag.timer = this.deps.timers.setTimeout(() => this.followPointer(), HUD_DRAG_POLL_MS);
  }

  private endDrag(): void {
    const drag = this.drag;
    if (drag === null) return;
    this.drag = null;
    if (drag.timer !== null) this.deps.timers.clearTimeout(drag.timer);
    this.scheduleSave();
  }

  private scheduleSave(): void {
    if (this.saveTimer !== null) this.deps.timers.clearTimeout(this.saveTimer);
    this.saveTimer = this.deps.timers.setTimeout(() => {
      this.saveTimer = null;
      this.saveNow();
    }, HUD_SAVE_DEBOUNCE_MS);
  }

  private flushSave(): void {
    if (this.saveTimer === null) return;
    this.deps.timers.clearTimeout(this.saveTimer);
    this.saveTimer = null;
    this.saveNow();
  }

  private saveNow(): void {
    const bounds = this.live()?.getBounds() ?? this.lastBounds;
    if (bounds === null) return;
    const { x, y, width, height } = bounds;
    try {
      writeFileAtomic(this.deps.statePath, `${JSON.stringify({ x, y, width, height })}\n`);
    } catch {
      this.deps.log("[hud] could not save where the HUD is");
    }
  }
}
