// Whole-window UI scale (R08.15). One level for the app, in Chromium zoom levels (factor
// 1.2^level): 0 by default, half-level steps, clamped to −4…9, saved in
// `<userData>/zoom-state.json`. Chromium drops or per-URL-remembers zoom after show/restore,
// display moves, reloads and in-page navigation, so every app window is "nudged" on those
// events: after a 100 ms debounce the saved level is re-applied when the window drifted, then
// checked again three times 300 ms apart (a compositor can drop the value while it settles).
// Self-sizing transparent windows (HUD, quick entry) are not tracked.

import { readJsonFile, writeFileAtomic } from "../fsSecure.ts";

export const ZOOM_DEFAULT = 0;
export const ZOOM_MIN = -4;
export const ZOOM_MAX = 9;
export const ZOOM_STEP = 0.5;
export const ZOOM_DEBOUNCE_MS = 100;
export const ZOOM_SETTLE_MS = 300;
export const ZOOM_SETTLE_CHECKS = 3;

/** A finite level on the half-step grid inside the range; anything else is the default. */
export function clampZoom(level: unknown): number {
  if (typeof level !== "number" || !Number.isFinite(level)) return ZOOM_DEFAULT;
  const stepped = Math.round(level / ZOOM_STEP) * ZOOM_STEP;
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, stepped));
}

export function stepZoom(level: number, direction: "in" | "out" | "reset"): number {
  if (direction === "reset") return ZOOM_DEFAULT;
  return clampZoom(level + (direction === "in" ? ZOOM_STEP : -ZOOM_STEP));
}

export function zoomPercent(level: number): number {
  return Math.round(1.2 ** clampZoom(level) * 100);
}

export interface ZoomTarget {
  getZoomLevel(): number;
  setZoomLevel(level: number): void;
  isDestroyed(): boolean;
}

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface TrackedZoom {
  /** A window event or navigation happened: re-assert after the debounce. */
  nudge(): void;
  dispose(): void;
}

export interface ZoomKeeperOptions {
  level?: number;
  timers: Timers;
  /** Called after a target's zoom was (re)applied, e.g. to tell its renderer. */
  onApplied?: (target: ZoomTarget, level: number) => void;
}

export class ZoomKeeper {
  private current: number;
  private readonly timers: Timers;
  private readonly onApplied: ((target: ZoomTarget, level: number) => void) | undefined;
  private readonly tracked = new Set<ZoomTarget>();

  constructor(options: ZoomKeeperOptions) {
    this.current = clampZoom(options.level ?? ZOOM_DEFAULT);
    this.timers = options.timers;
    this.onApplied = options.onApplied;
  }

  get level(): number {
    return this.current;
  }

  /** Change the app's level and apply it to every tracked window now. Returns the clamped level. */
  setLevel(level: number): number {
    this.current = clampZoom(level);
    for (const target of this.tracked) this.apply(target, true);
    return this.current;
  }

  /** Apply the level to `target` when it drifted (or always with `force`). */
  apply(target: ZoomTarget, force = false): boolean {
    if (target.isDestroyed()) return false;
    let drifted = true;
    try {
      drifted = Math.abs(target.getZoomLevel() - this.current) > 1e-6;
    } catch {
      drifted = true;
    }
    if (!drifted && !force) return false;
    target.setZoomLevel(this.current);
    this.onApplied?.(target, this.current);
    return true;
  }

  track(target: ZoomTarget, options: { selfSizing?: boolean } = {}): TrackedZoom {
    if (options.selfSizing === true) return { nudge: () => {}, dispose: () => {} };
    this.tracked.add(target);
    let debounce: unknown = null;
    let settle: unknown = null;
    let checksLeft = 0;
    const clear = () => {
      if (debounce !== null) this.timers.clearTimeout(debounce);
      if (settle !== null) this.timers.clearTimeout(settle);
      debounce = null;
      settle = null;
    };
    const settleCheck = () => {
      settle = null;
      if (target.isDestroyed() || checksLeft <= 0) return;
      checksLeft -= 1;
      this.apply(target);
      if (checksLeft > 0) settle = this.timers.setTimeout(settleCheck, ZOOM_SETTLE_MS);
    };
    this.apply(target, true);
    return {
      nudge: () => {
        if (target.isDestroyed()) return;
        clear();
        debounce = this.timers.setTimeout(() => {
          debounce = null;
          this.apply(target);
          checksLeft = ZOOM_SETTLE_CHECKS;
          settle = this.timers.setTimeout(settleCheck, ZOOM_SETTLE_MS);
        }, ZOOM_DEBOUNCE_MS);
      },
      dispose: () => {
        clear();
        this.tracked.delete(target);
      },
    };
  }
}

export function readZoomLevel(file: string): number {
  const raw = readJsonFile(file);
  return clampZoom(
    raw !== null && typeof raw === "object" ? (raw as { level?: unknown }).level : undefined,
  );
}

export function writeZoomLevel(file: string, level: number): void {
  writeFileAtomic(file, `${JSON.stringify({ level: clampZoom(level) })}\n`);
}
