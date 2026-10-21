// The main window's saved geometry (`<userData>/window-state.json`). Pure placement rules plus a
// small read/write pair: a saved position is trusted only when at least 48 px of the window lie
// over some display's work area on both axes (so the title bar stays reachable after a monitor
// goes away); otherwise the window is centred on the primary display. Sizes never go below the
// minimum (400×620) nor above the work area they land on.

import { readJsonFile, writeFileAtomic } from "../fsSecure.ts";
import { WINDOW_MIN_HEIGHT, WINDOW_MIN_WIDTH } from "./factory.ts";

export const MIN_VISIBLE_PX = 48;
export const DEFAULT_WINDOW_SIZE = { width: 1280, height: 860 } as const;

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface DisplayLike {
  workArea: Rect;
}

export interface SavedWindowState {
  width: number;
  height: number;
  x?: number;
  y?: number;
  maximized: boolean;
}

export interface Placement {
  bounds: Rect;
  maximized: boolean;
}

const finite = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

/** A clean state from whatever the file held, or null for garbage. */
export function sanitizeWindowState(raw: unknown): SavedWindowState | null {
  if (raw === null || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  if (!finite(record.width) || !finite(record.height) || record.width <= 0 || record.height <= 0)
    return null;
  const state: SavedWindowState = {
    width: Math.max(WINDOW_MIN_WIDTH, Math.round(record.width)),
    height: Math.max(WINDOW_MIN_HEIGHT, Math.round(record.height)),
    maximized: record.maximized === true,
  };
  if (finite(record.x) && finite(record.y)) {
    state.x = Math.round(record.x);
    state.y = Math.round(record.y);
  }
  return state;
}

function visibleSpan(a: Rect, b: Rect): { width: number; height: number } {
  const width = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
  const height = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
  return { width: Math.max(0, width), height: Math.max(0, height) };
}

/** The display whose work area shows the most of `rect` with ≥ 48 px on both axes, or null. */
export function displayShowing(rect: Rect, displays: readonly DisplayLike[]): DisplayLike | null {
  let best: DisplayLike | null = null;
  let bestArea = 0;
  for (const display of displays) {
    const span = visibleSpan(rect, display.workArea);
    if (span.width < MIN_VISIBLE_PX || span.height < MIN_VISIBLE_PX) continue;
    const area = span.width * span.height;
    if (area > bestArea) {
      best = display;
      bestArea = area;
    }
  }
  return best;
}

function fitSize(width: number, height: number, area: Rect): { width: number; height: number } {
  return {
    width: Math.max(WINDOW_MIN_WIDTH, Math.min(width, area.width)),
    height: Math.max(WINDOW_MIN_HEIGHT, Math.min(height, area.height)),
  };
}

function centred(size: { width: number; height: number }, area: Rect): Rect {
  return {
    ...size,
    x: Math.round(area.x + (area.width - size.width) / 2),
    y: Math.round(area.y + (area.height - size.height) / 2),
  };
}

/** Where to open the main window given what was saved and the displays there are now. */
export function placeWindow(
  saved: SavedWindowState | null,
  displays: readonly DisplayLike[],
  primary: DisplayLike,
): Placement {
  const wanted = saved ?? { ...DEFAULT_WINDOW_SIZE, maximized: false };
  if (saved !== null && saved.x !== undefined && saved.y !== undefined) {
    const rect = { x: saved.x, y: saved.y, width: saved.width, height: saved.height };
    const display = displayShowing(rect, displays);
    if (display !== null) {
      return {
        bounds: { ...rect, ...fitSize(rect.width, rect.height, display.workArea) },
        maximized: saved.maximized,
      };
    }
  }
  const size = fitSize(wanted.width, wanted.height, primary.workArea);
  return { bounds: centred(size, primary.workArea), maximized: wanted.maximized };
}

export function readWindowState(file: string): SavedWindowState | null {
  return sanitizeWindowState(readJsonFile(file));
}

export function writeWindowState(file: string, state: SavedWindowState): void {
  writeFileAtomic(file, `${JSON.stringify(state)}\n`);
}
