// Owns every app window (P20-06): creation through the one factory, the main window's reveal on
// first themed paint, saved geometry, zoom, crash reloads, and the list of web contents the IPC
// sender check trusts. Nothing here focuses a window on its own; `raise` is the explicit
// user-action path (R08.20).

import { BRAND_COLORS } from "@indus/ui";
import type { AppOrigin } from "../appOrigin.ts";
import { CrashReloadBudget, isIntentionalExit } from "./crashReload.ts";
import { KIND_TRAITS, type TitleBarColors, type WindowKind, windowOptionsFor } from "./factory.ts";
import {
  type DisplayLike,
  placeWindow,
  type Rect,
  readWindowState,
  type SavedWindowState,
  writeWindowState,
} from "./windowState.ts";
import { parseWindowQuery, type WindowUrlOptions, windowUrl } from "./windowUrl.ts";
import {
  readZoomLevel,
  stepZoom,
  type Timers,
  type TrackedZoom,
  writeZoomLevel,
  ZoomKeeper,
  zoomPercent,
} from "./zoom.ts";

/** The renderer never paints within this → the window is shown anyway. */
export const REVEAL_FALLBACK_MS = 4_000;
/** Geometry is written at most this often while the person drags or resizes. */
export const SAVE_STATE_DEBOUNCE_MS = 500;

export const DEFAULT_TITLE_BAR: TitleBarColors = { color: "#00000000", symbolColor: "#8a8f98" };
/** The window before the renderer paints: the brand black (the dark scheme's canvas). */
export const DEFAULT_BACKGROUND = BRAND_COLORS.black;

/** Why a window may be brought to the front: always something the person did. */
export type UserActionReason =
  | "app-activate"
  | "second-launch"
  | "notification-click"
  | "deep-link"
  | "menu"
  | "open-request";

export interface WebContentsLike {
  readonly id: number;
  on(event: string, listener: (...args: never[]) => void): unknown;
  reload(): void;
  getURL(): string;
  getZoomLevel(): number;
  setZoomLevel(level: number): void;
  isDestroyed(): boolean;
  send(channel: string, payload: unknown): void;
}

export interface BrowserWindowLike {
  readonly webContents: WebContentsLike;
  on(event: string, listener: (...args: never[]) => void): unknown;
  loadURL(url: string): Promise<unknown>;
  show(): void;
  focus(): void;
  restore(): void;
  minimize(): void;
  maximize(): void;
  unmaximize(): void;
  close(): void;
  isDestroyed(): boolean;
  isVisible(): boolean;
  isMinimized(): boolean;
  isMaximized(): boolean;
  isFullScreen(): boolean;
  isFocused(): boolean;
  getNormalBounds(): Rect;
  setTitleBarOverlay?(options: TitleBarColors & { height?: number }): void;
}

export interface WindowManagerDeps {
  createWindow(options: Record<string, unknown>): BrowserWindowLike;
  screen: { getAllDisplays(): DisplayLike[]; getPrimaryDisplay(): DisplayLike };
  appOrigin: AppOrigin;
  preloadPath: string;
  dev: boolean;
  platform: string;
  title: string;
  windowStatePath: string;
  zoomStatePath: string;
  timers: Timers;
  now: () => number;
  log: (line: string) => void;
  /** Navigation guard, window-open deny and input tracking for a new web contents. */
  prepareContents(contents: WebContentsLike, kind: WindowKind): void;
  /** Tell a window about its own state (`indus:window:state`). */
  emitWindowState(
    contents: WebContentsLike,
    state: { maximized: boolean; fullscreen: boolean; focused: boolean },
  ): void;
  /** Tell a window its zoom changed (`indus:zoom:changed`). */
  emitZoom(contents: WebContentsLike, state: { level: number; percent: number }): void;
}

interface Managed {
  win: BrowserWindowLike;
  kind: WindowKind;
  options: WindowUrlOptions;
  zoom: TrackedZoom;
  revealed: boolean;
}

export class WindowManager {
  private readonly deps: WindowManagerDeps;
  private readonly byContents = new Map<number, Managed>();
  private readonly sessions = new Map<string, Managed>();
  private readonly createdListeners = new Set<(win: BrowserWindowLike, kind: WindowKind) => void>();
  private main: Managed | null = null;
  private readonly crashBudget: CrashReloadBudget;
  readonly zoom: ZoomKeeper;
  private saveTimer: unknown = null;

  constructor(deps: WindowManagerDeps) {
    this.deps = deps;
    this.crashBudget = new CrashReloadBudget(deps.now);
    this.zoom = new ZoomKeeper({
      level: readZoomLevel(deps.zoomStatePath),
      timers: deps.timers,
      onApplied: (target, level) => {
        const managed = [...this.byContents.values()].find((m) => m.win.webContents === target);
        if (managed !== undefined)
          this.deps.emitZoom(managed.win.webContents, { level, percent: zoomPercent(level) });
      },
    });
  }

  /** True for web contents of a window this manager created (and has not lost). */
  isKnownWebContents = (id: number): boolean => {
    const managed = this.byContents.get(id);
    return managed !== undefined && !managed.win.isDestroyed();
  };

  /** The kind and options of the window showing `contentsId`. */
  describe(
    contentsId: number,
  ): { kind: WindowKind; watch: boolean; profile: string | null } | null {
    const managed = this.byContents.get(contentsId);
    if (managed === undefined) return null;
    const parsed = parseWindowQuery(managed.win.webContents.getURL());
    return {
      kind: managed.kind,
      watch: parsed.watch,
      profile: managed.options.profile ?? parsed.profile,
    };
  }

  windows(): BrowserWindowLike[] {
    return [...this.byContents.values()].map((m) => m.win).filter((w) => !w.isDestroyed());
  }

  mainWindow(): BrowserWindowLike | null {
    return this.main !== null && !this.main.win.isDestroyed() ? this.main.win : null;
  }

  openMain(): BrowserWindowLike {
    const existing = this.mainWindow();
    if (existing !== null) return existing;
    const saved = readWindowState(this.deps.windowStatePath);
    const placement = placeWindow(
      saved,
      this.deps.screen.getAllDisplays(),
      this.deps.screen.getPrimaryDisplay(),
    );
    const managed = this.create("main", {}, placement.bounds);
    this.main = managed;
    if (placement.maximized) managed.win.maximize();
    const persist = () => this.scheduleSave();
    for (const event of ["resize", "move", "maximize", "unmaximize"])
      managed.win.on(event, persist);
    managed.win.on("close", () => this.saveNow());
    return managed.win;
  }

  /** One window per session; opening it again brings the existing one forward. */
  openSecondary(options: {
    sessionId: string;
    watch?: boolean;
    profile?: string;
  }): BrowserWindowLike {
    const existing = this.sessions.get(options.sessionId);
    if (existing !== undefined && !existing.win.isDestroyed()) {
      this.raise(existing.win, "open-request");
      return existing.win;
    }
    const managed = this.create("secondary", options, this.cascade(900, 760));
    this.sessions.set(options.sessionId, managed);
    managed.win.on("closed", () => {
      if (this.sessions.get(options.sessionId) === managed) this.sessions.delete(options.sessionId);
    });
    return managed.win;
  }

  openInstance(options: { profile?: string } = {}): BrowserWindowLike {
    return this.create("instance", options, this.cascade(1280, 860)).win;
  }

  /** The floating HUD for one conversation (P20-35); bounds are the HUD controller's. */
  openHud(
    options: { sessionId?: string; profile?: string | null },
    bounds: Rect,
  ): BrowserWindowLike {
    return this.create("hud", options, bounds).win;
  }

  /** The quick-entry composer (P20-35). */
  openQuick(bounds: Rect): BrowserWindowLike {
    return this.create("quick", {}, bounds).win;
  }

  /** The pet overlay over a work area (P24-19); bounds are the pet controller's. */
  openPet(bounds: Rect): BrowserWindowLike {
    return this.create("pet", {}, bounds).win;
  }

  /** The wake-word indicator (P24-20); bounds are the indicator controller's. */
  openWakeIndicator(bounds: Rect): BrowserWindowLike {
    return this.create("wake", {}, bounds).win;
  }

  /** Called for every window this manager creates (tray, ambient wiring). */
  onWindowCreated(listener: (win: BrowserWindowLike, kind: WindowKind) => void): () => void {
    this.createdListeners.add(listener);
    return () => {
      this.createdListeners.delete(listener);
    };
  }

  /** The kind of the window showing `contentsId`, or null. */
  kindOf(contentsId: number): WindowKind | null {
    return this.byContents.get(contentsId)?.kind ?? null;
  }

  /** The live window showing `contentsId`, or null. */
  windowOf(contentsId: number): BrowserWindowLike | null {
    const managed = this.byContents.get(contentsId);
    return managed === undefined || managed.win.isDestroyed() ? null : managed.win;
  }

  /** The renderer of `contentsId` painted its first themed frame. */
  painted(contentsId: number): void {
    const managed = this.byContents.get(contentsId);
    if (managed !== undefined) this.reveal(managed);
  }

  control(contentsId: number, action: "minimize" | "toggle-maximize" | "close"): void {
    const managed = this.byContents.get(contentsId);
    if (managed === undefined || managed.win.isDestroyed()) return;
    if (action === "minimize") managed.win.minimize();
    else if (action === "close") managed.win.close();
    else if (managed.win.isMaximized()) managed.win.unmaximize();
    else managed.win.maximize();
  }

  setTitleBar(contentsId: number, colors: TitleBarColors): void {
    const managed = this.byContents.get(contentsId);
    if (managed === undefined || managed.win.isDestroyed() || this.deps.platform === "darwin")
      return;
    if (!KIND_TRAITS[managed.kind].frameless || KIND_TRAITS[managed.kind].transparent) return;
    try {
      managed.win.setTitleBarOverlay?.({ ...colors, height: 34 });
    } catch {
      // Older window managers without overlay support keep the default colours.
    }
  }

  /** Bring `win` forward because the person asked for it. The only focusing path. */
  raise(win: BrowserWindowLike, reason: UserActionReason): void {
    if (win.isDestroyed()) return;
    if (win.isMinimized()) win.restore();
    if (!win.isVisible()) win.show();
    win.focus();
    this.deps.log(`[windows] raised for ${reason}`);
  }

  setZoom(level: number): { level: number; percent: number } {
    const applied = this.zoom.setLevel(level);
    this.persistZoom(applied);
    return { level: applied, percent: zoomPercent(applied) };
  }

  stepZoomBy(direction: "in" | "out" | "reset"): { level: number; percent: number } {
    return this.setZoom(stepZoom(this.zoom.level, direction));
  }

  zoomState(): { level: number; percent: number } {
    return { level: this.zoom.level, percent: zoomPercent(this.zoom.level) };
  }

  private persistZoom(level: number): void {
    try {
      writeZoomLevel(this.deps.zoomStatePath, level);
    } catch {
      this.deps.log("[windows] could not save the zoom level");
    }
  }

  private cascade(width: number, height: number): Rect {
    const area = this.deps.screen.getPrimaryDisplay().workArea;
    const offset = 32 * (this.byContents.size % 6);
    const w = Math.min(width, area.width);
    const h = Math.min(height, area.height);
    return {
      width: w,
      height: h,
      x: Math.round(area.x + (area.width - w) / 2 + offset),
      y: Math.round(area.y + (area.height - h) / 2 + offset),
    };
  }

  private create(kind: WindowKind, options: WindowUrlOptions, bounds: Rect): Managed {
    const win = this.deps.createWindow(
      windowOptionsFor(kind, {
        preloadPath: this.deps.preloadPath,
        dev: this.deps.dev,
        platform: this.deps.platform,
        bounds,
        title: this.deps.title,
        backgroundColor: DEFAULT_BACKGROUND,
        titleBar: DEFAULT_TITLE_BAR,
      }),
    );
    const contents = win.webContents;
    const contentsId = contents.id;
    this.deps.prepareContents(contents, kind);
    const zoom = this.zoom.track(contents, { selfSizing: KIND_TRAITS[kind].selfSizing });
    const managed: Managed = { win, kind, options, zoom, revealed: false };
    this.byContents.set(contentsId, managed);

    for (const event of [
      "show",
      "restore",
      "focus",
      "resize",
      "move",
      "moved",
      "enter-full-screen",
      "leave-full-screen",
    ]) {
      win.on(event, () => zoom.nudge());
    }
    contents.on("did-finish-load", () => zoom.nudge());
    contents.on("did-navigate-in-page", () => zoom.nudge());

    const pushState = () => {
      if (win.isDestroyed()) return;
      this.deps.emitWindowState(contents, {
        maximized: win.isMaximized(),
        fullscreen: win.isFullScreen(),
        focused: win.isFocused(),
      });
    };
    for (const event of [
      "maximize",
      "unmaximize",
      "enter-full-screen",
      "leave-full-screen",
      "focus",
      "blur",
    ]) {
      win.on(event, pushState);
    }

    contents.on("render-process-gone", ((
      _event: unknown,
      details: { reason?: string; exitCode?: number },
    ) => {
      const reason = String(details?.reason ?? "unknown");
      if (isIntentionalExit(reason) || win.isDestroyed()) return;
      if (this.crashBudget.allow()) {
        this.deps.log(`[windows] renderer gone (${reason}); reloading`);
        contents.reload();
      } else {
        this.deps.log(
          `[windows] renderer gone (${reason}) again; not reloading (3 reloads in 60 s)`,
        );
      }
    }) as (...args: never[]) => void);

    win.on("closed", () => {
      zoom.dispose();
      this.byContents.delete(contentsId);
      if (this.main === managed) this.main = null;
    });

    const fallback = this.deps.timers.setTimeout(() => {
      if (!managed.revealed && !win.isDestroyed()) {
        this.deps.log(`[windows] ${kind} window shown without a paint signal`);
        this.reveal(managed);
      }
    }, REVEAL_FALLBACK_MS);
    win.on("closed", () => this.deps.timers.clearTimeout(fallback));

    for (const listener of this.createdListeners) {
      try {
        listener(win, kind);
      } catch {
        this.deps.log(`[windows] a window-created listener failed for a ${kind} window`);
      }
    }

    win.loadURL(windowUrl(this.deps.appOrigin, kind, options)).catch(() => {
      this.deps.log(`[windows] ${kind} window failed to load the app`);
    });
    return managed;
  }

  private reveal(managed: Managed): void {
    if (managed.revealed || managed.win.isDestroyed()) return;
    managed.revealed = true;
    managed.win.show();
  }

  private scheduleSave(): void {
    if (this.saveTimer !== null) this.deps.timers.clearTimeout(this.saveTimer);
    this.saveTimer = this.deps.timers.setTimeout(() => {
      this.saveTimer = null;
      this.saveNow();
    }, SAVE_STATE_DEBOUNCE_MS);
  }

  /** Write the main window's geometry now (also on close). */
  saveNow(): void {
    if (this.saveTimer !== null) {
      this.deps.timers.clearTimeout(this.saveTimer);
      this.saveTimer = null;
    }
    const main = this.main;
    if (main === null || main.win.isDestroyed()) return;
    const bounds = main.win.getNormalBounds();
    const state: SavedWindowState = { ...bounds, maximized: main.win.isMaximized() };
    try {
      writeWindowState(this.deps.windowStatePath, state);
    } catch {
      this.deps.log("[windows] could not save the window state");
    }
  }
}
