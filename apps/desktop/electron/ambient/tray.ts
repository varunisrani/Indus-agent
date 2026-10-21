// Minimise to tray (P20-36). Off by default, per device. When it is on *and* a tray is really
// shown, minimising an app window hides it and closing the main window hides it; the tray brings
// every hidden window back and its Quit goes through the normal quit (the quit guard still asks).
//
// "Really shown": on Linux a tray object can exist with nobody drawing it (no StatusNotifier host,
// as on a stock GNOME), and hiding a window there would strand it. So Linux asks D-Bus first and
// keeps asking while the option is on; if the host goes away every hidden window comes back.

import type { BrowserWindowLike } from "../windows/manager.ts";
import type { HostCheck } from "./dbus.ts";
import type { Timers } from "./hud.ts";

/** How often Linux re-asks whether a tray host is still registered. */
export const TRAY_HOST_POLL_MS = 10_000;

export interface TrayLike {
  setToolTip(text: string): void;
  setContextMenu(menu: unknown): void;
  on(event: "click" | "double-click", listener: () => void): unknown;
  destroy(): void;
  isDestroyed(): boolean;
}

export interface MenuItem {
  label?: string;
  type?: "separator";
  click?: () => void;
}

/** What tray hiding needs from a BrowserWindow beyond the manager's view. */
export interface TrayWindow extends BrowserWindowLike {
  hide(): void;
  showInactive?(): void;
  setSkipTaskbar?(skip: boolean): void;
  blur?(): void;
}

export interface TrayStatus {
  enabled: boolean;
  available: boolean;
  reason: string | null;
}

export interface TrayDeps {
  platform: string;
  /** Build the native tray (throws when the platform cannot show one). */
  createTray(): TrayLike;
  buildMenu(items: MenuItem[]): unknown;
  /** Linux only: is a StatusNotifier host registered right now? */
  hostCheck(): Promise<HostCheck>;
  /** `app.quit()` — goes through the quit guard. */
  quit(): void;
  /** Make sure there is a main window to come back to. */
  ensureMainWindow(): void;
  dock?: { hide(): void; show(): void } | undefined;
  timers: Timers;
  log(line: string): void;
  /** The status changed (broadcast to every window). */
  onChange(status: TrayStatus): void;
  labels: { show: string; quit: string; tooltip: string };
}

interface Registered {
  win: TrayWindow;
  closeToTray: boolean;
}

export class TrayController {
  private readonly deps: TrayDeps;
  private enabled = false;
  private tray: TrayLike | null = null;
  private reason: string | null = "turned off";
  private quitting = false;
  private hostTimer: unknown = null;
  private hostGeneration = 0;
  private queue: Promise<unknown> = Promise.resolve();
  private readonly windows = new Set<Registered>();
  private readonly hidden = new Set<TrayWindow>();
  private dockHidden = false;
  private restoring = false;

  constructor(deps: TrayDeps) {
    this.deps = deps;
  }

  status(): TrayStatus {
    return { enabled: this.enabled, available: this.available(), reason: this.reason };
  }

  /** Turn the option on or off; calls are applied in order (a stale one never wins). */
  setEnabled(on: boolean): Promise<TrayStatus> {
    const next = this.queue.then(() => this.apply(on));
    this.queue = next.catch(() => undefined);
    return next;
  }

  /** Follow an app window (main, secondary, instance). `closeToTray`: the main window. */
  register(win: TrayWindow, options: { closeToTray: boolean }): void {
    const entry: Registered = { win, closeToTray: options.closeToTray };
    this.windows.add(entry);
    win.on("minimize", () => {
      // Hide after the native minimise has finished; the person may have restored it meanwhile.
      this.deps.timers.setTimeout(() => {
        if (win.isDestroyed()) return;
        if (!win.isMinimized() && win.isVisible()) return;
        this.hide(win);
      }, 0);
    });
    if (options.closeToTray) {
      win.on("close", ((event: { preventDefault(): void }) => {
        if (this.hide(win)) event.preventDefault();
      }) as (...args: never[]) => void);
    }
    const released = () => {
      const wasHidden = this.hidden.has(win);
      this.release(win);
      // Shown by something else (a deep link, a notification, a second launch): on Windows a
      // restored-but-inactive window can ignore input, so finish the activation after the native
      // restore settles.
      if (wasHidden && this.deps.platform === "win32") {
        this.deps.timers.setTimeout(() => {
          if (!win.isDestroyed() && win.isVisible() && !win.isMinimized()) {
            win.show();
            win.focus();
          }
        }, 0);
      }
    };
    win.on("show", released);
    win.on("restore", released);
    win.on("query-session-end", () => {
      this.quitting = true;
    });
    win.on("closed", () => {
      this.windows.delete(entry);
      this.hidden.delete(win);
      this.syncDock();
    });
  }

  /** Every hidden window back (tray Show, option off, host lost). */
  restoreAll(): void {
    this.showDock();
    this.restoring = true;
    try {
      this.restoreEach();
    } finally {
      this.restoring = false;
    }
    this.syncDock();
  }

  private restoreEach(): void {
    for (const win of [...this.hidden]) {
      this.release(win);
      if (win.isDestroyed()) continue;
      if (win.isMinimized()) win.restore();
      if (this.deps.platform === "win32") {
        // A restored-but-inactive window on Windows can ignore input; activate it properly.
        win.show();
        win.focus();
      } else if (win.showInactive !== undefined) {
        win.showInactive();
      } else {
        win.show();
      }
    }
  }

  /** The person accepted the quit (the quit guard did not stop it): never hide from now on. */
  beginQuit(): void {
    this.quitting = true;
  }

  hiddenCount(): number {
    return this.hidden.size;
  }

  dispose(): void {
    this.quitting = true;
    this.stopWatching();
    if (this.tray !== null && !this.tray.isDestroyed()) this.tray.destroy();
    this.tray = null;
  }

  // -- internals ---------------------------------------------------------------------------------

  private available(): boolean {
    return this.enabled && this.tray !== null && !this.tray.isDestroyed() && this.reason === null;
  }

  private async apply(on: boolean): Promise<TrayStatus> {
    this.enabled = on;
    if (!on) {
      this.stopWatching();
      this.restoreAll();
      this.dropTray(false);
      this.reason = "turned off";
      return this.changed();
    }
    if (this.quitting) return this.changed();
    if (this.deps.platform === "linux") {
      const generation = ++this.hostGeneration;
      const check = await this.deps.hostCheck();
      if (generation !== this.hostGeneration || !this.enabled) return this.status();
      if (!check.ok) {
        this.reason = check.reason;
        this.deps.log(`[tray] not available: ${check.reason}`);
        this.dropTray(true);
        return this.changed();
      }
    }
    try {
      if (this.tray === null || this.tray.isDestroyed()) this.tray = this.makeTray();
      this.reason = null;
    } catch (error) {
      this.reason = "this desktop cannot show a tray icon";
      this.deps.log(
        `[tray] not available: ${error instanceof Error ? error.message : String(error)}`,
      );
      this.dropTray(true);
      return this.changed();
    }
    if (this.deps.platform === "linux") this.watchHost();
    return this.changed();
  }

  private makeTray(): TrayLike {
    const tray = this.deps.createTray();
    tray.setToolTip(this.deps.labels.tooltip);
    const show = () => this.show();
    tray.setContextMenu(
      this.deps.buildMenu([
        { label: this.deps.labels.show, click: show },
        { type: "separator" },
        // The normal quit path: the quit guard still asks about running work.
        { label: this.deps.labels.quit, click: () => this.deps.quit() },
      ]),
    );
    // On macOS a click opens the menu; elsewhere a click restores.
    if (this.deps.platform !== "darwin") tray.on("click", show);
    tray.on("double-click", show);
    return tray;
  }

  private show(): void {
    this.restoreAll();
    this.deps.ensureMainWindow();
  }

  /**
   * Linux keeps a parked tray object instead of destroying it when the option goes off (a
   * StatusNotifier item cannot be exported again in the same process); `force` destroys anyway
   * (the host went away, or nothing usable was made).
   */
  private dropTray(force: boolean): void {
    if (this.tray === null) return;
    if (this.deps.platform === "linux" && !force && !this.quitting) return;
    if (!this.tray.isDestroyed()) this.tray.destroy();
    this.tray = null;
  }

  private watchHost(): void {
    this.stopWatching();
    const generation = this.hostGeneration;
    const tick = () => {
      this.hostTimer = null;
      void this.deps.hostCheck().then((check) => {
        if (generation !== this.hostGeneration || !this.enabled) return;
        if (check.ok) {
          this.hostTimer = this.deps.timers.setTimeout(tick, TRAY_HOST_POLL_MS);
          return;
        }
        this.deps.log(`[tray] the tray host went away (${check.reason}); windows restored`);
        this.hostGeneration += 1;
        this.reason = check.reason;
        this.restoreAll();
        this.dropTray(true);
        this.changed();
      });
    };
    this.hostTimer = this.deps.timers.setTimeout(tick, TRAY_HOST_POLL_MS);
  }

  private stopWatching(): void {
    this.hostGeneration += 1;
    if (this.hostTimer !== null) this.deps.timers.clearTimeout(this.hostTimer);
    this.hostTimer = null;
  }

  private hide(win: TrayWindow): boolean {
    if (win.isDestroyed() || this.quitting || !this.available()) return false;
    this.hidden.add(win);
    if (this.deps.platform === "win32") {
      win.setSkipTaskbar?.(true);
      // A hidden window on Windows keeps the keyboard otherwise.
      win.blur?.();
    }
    win.hide();
    this.syncDock();
    return true;
  }

  private release(win: TrayWindow): void {
    if (!this.hidden.delete(win)) return;
    if (this.deps.platform === "win32" && !win.isDestroyed()) win.setSkipTaskbar?.(false);
    this.syncDock();
  }

  private syncDock(): void {
    if (this.restoring || this.deps.platform !== "darwin" || this.deps.dock === undefined) return;
    const anyVisible = [...this.windows].some(
      ({ win }) => !win.isDestroyed() && win.isVisible() && !win.isMinimized(),
    );
    if (this.available() && this.hidden.size > 0 && !anyVisible && !this.quitting) {
      if (!this.dockHidden) {
        this.dockHidden = true;
        this.deps.dock.hide();
      }
    } else {
      this.showDock();
    }
  }

  private showDock(): void {
    if (!this.dockHidden) return;
    this.dockHidden = false;
    this.deps.dock?.show();
  }

  private changed(): TrayStatus {
    const status = this.status();
    this.deps.onChange(status);
    return status;
  }
}
