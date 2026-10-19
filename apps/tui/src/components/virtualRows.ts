/**
 * Virtual transcript geometry and scrolling.
 *
 * Every transcript row has an exact height (our own layout makes its lines), so offsets are
 * prefix sums and the visible window is found by binary search. The scroll position is kept as
 * an anchor — the row at the top of the view and how many of its lines are above the edge — not
 * as an absolute offset: when the width changes and every height changes, the same row stays on
 * top, and output appended below never moves a view the user scrolled up. While the view
 * follows the bottom (the default), new output keeps it pinned there.
 */
import { createStore, type StoreApi } from "zustand/vanilla";

export class Offsets {
  private readonly sums: Float64Array;

  constructor(heights: readonly number[]) {
    this.sums = new Float64Array(heights.length + 1);
    for (let i = 0; i < heights.length; i++) {
      this.sums[i + 1] = (this.sums[i] as number) + Math.max(0, heights[i] ?? 0);
    }
  }

  get count(): number {
    return this.sums.length - 1;
  }

  get total(): number {
    return this.sums[this.sums.length - 1] as number;
  }

  /** First line of row `i`. */
  offsetOf(i: number): number {
    return this.sums[Math.max(0, Math.min(i, this.count))] as number;
  }

  heightOf(i: number): number {
    return this.offsetOf(i + 1) - this.offsetOf(i);
  }

  /** The row holding line `y` (clamped to the last row). */
  indexAt(y: number): number {
    if (this.count === 0) return 0;
    let lo = 0;
    let hi = this.count - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if ((this.sums[mid] as number) <= y) lo = mid;
      else hi = mid - 1;
    }
    return lo;
  }
}

export interface ViewWindow {
  /** First row with a visible line. */
  start: number;
  /** One past the last row with a visible line. */
  end: number;
  /** Lines of row `start` above the top edge. */
  skip: number;
  /** Lines above the window (the top spacer). */
  topSpacer: number;
  /** Lines below the window (the bottom spacer). */
  bottomSpacer: number;
  offset: number;
}

export function maxOffset(total: number, viewport: number): number {
  return Math.max(0, total - Math.max(0, viewport));
}

export function windowAt(offsets: Offsets, offset: number, viewport: number): ViewWindow {
  const top = Math.max(0, Math.min(offset, maxOffset(offsets.total, viewport)));
  if (offsets.count === 0 || viewport <= 0) {
    return { start: 0, end: 0, skip: 0, topSpacer: top, bottomSpacer: 0, offset: top };
  }
  const start = offsets.indexAt(top);
  const bottom = Math.min(offsets.total, top + viewport);
  let end = start;
  while (end < offsets.count && offsets.offsetOf(end) < bottom) end++;
  return {
    start,
    end,
    skip: top - offsets.offsetOf(start),
    topSpacer: offsets.offsetOf(start),
    bottomSpacer: offsets.total - offsets.offsetOf(end),
    offset: top,
  };
}

// ---------------------------------------------------------------- anchors

export interface Anchor {
  key: string;
  within: number;
}

export interface Geometry {
  keys: readonly string[];
  offsets: Offsets;
  viewport: number;
}

export interface ScrollModel {
  follow: boolean;
  anchor: Anchor | null;
}

function indexOfKey(geo: Geometry, key: string): number {
  // Rows are mostly appended, so the anchor is usually near where it was; a plain scan is fine
  // for thousands of rows and avoids building a map per frame.
  return geo.keys.indexOf(key);
}

export function anchorAt(geo: Geometry, offset: number): Anchor | null {
  if (geo.keys.length === 0) return null;
  const index = geo.offsets.indexAt(offset);
  return { key: geo.keys[index] as string, within: offset - geo.offsets.offsetOf(index) };
}

/** The offset a scroll model stands for in the current geometry. */
export function resolveOffset(model: ScrollModel, geo: Geometry): number {
  const bottom = maxOffset(geo.offsets.total, geo.viewport);
  if (model.follow || !model.anchor) return bottom;
  const index = indexOfKey(geo, model.anchor.key);
  if (index < 0) return bottom;
  const within = Math.min(model.anchor.within, Math.max(0, geo.offsets.heightOf(index) - 1));
  return Math.min(bottom, geo.offsets.offsetOf(index) + within);
}

/** Moves to `offset`; reaching the bottom turns following back on. */
export function scrollModelAt(geo: Geometry, offset: number): ScrollModel {
  const bottom = maxOffset(geo.offsets.total, geo.viewport);
  const clamped = Math.max(0, Math.min(offset, bottom));
  if (clamped >= bottom) return { follow: true, anchor: null };
  return { follow: false, anchor: anchorAt(geo, clamped) };
}

/** PgUp / PgDn step: half the viewport, at least 4 rows. */
export function pageSize(viewport: number): number {
  return Math.max(4, Math.floor(viewport / 2));
}

// ---------------------------------------------------------------- wheel

export interface WheelState {
  dir: 1 | -1 | 0;
  at: number;
  streak: number;
}

export const WHEEL_FAST_MS = 50;
export const WHEEL_FLIP_MS = 120;
export const WHEEL_MAX_ROWS = 6;

export const idleWheel: WheelState = { dir: 0, at: Number.NEGATIVE_INFINITY, streak: 0 };

/**
 * Rows to move for one wheel event. Quick events in one direction speed up; a single reversed
 * event right after a fast run is taken as the wheel bouncing and dropped; with `precise` (a
 * modifier held) every event is one row and the speed-up resets.
 */
export function wheelStep(
  state: WheelState,
  dir: 1 | -1,
  now: number,
  precise = false,
): { rows: number; state: WheelState } {
  if (precise) return { rows: dir, state: { dir, at: now, streak: 0 } };
  const gap = now - state.at;
  if (state.dir !== 0 && dir !== state.dir && gap <= WHEEL_FLIP_MS && state.streak >= 3) {
    return { rows: 0, state: { dir: state.dir, at: now, streak: 0 } };
  }
  const streak = dir === state.dir && gap <= WHEEL_FAST_MS ? state.streak + 1 : 0;
  const rows = Math.min(WHEEL_MAX_ROWS, 1 + Math.floor(streak / 2));
  return { rows: rows * dir, state: { dir, at: now, streak } };
}

// ---------------------------------------------------------------- completion window

export const COMPLETION_ROWS = 16;

/** The slice of a completion list to show: at most `max` rows, centred on the selection. */
export function completionWindow(
  count: number,
  selected: number,
  max = COMPLETION_ROWS,
): { start: number; end: number } {
  if (count <= max) return { start: 0, end: count };
  const sel = Math.max(0, Math.min(selected, count - 1));
  const start = Math.max(0, Math.min(sel - Math.floor(max / 2), count - max));
  return { start, end: start + max };
}

// ---------------------------------------------------------------- store

export interface ScrollState extends ScrollModel {
  wheel: WheelState;
}

export const scrollStore: StoreApi<ScrollState> = createStore<ScrollState>(() => ({
  follow: true,
  anchor: null,
  wheel: idleWheel,
}));

let geometry: Geometry | null = null;

/** The transcript reports its geometry on every render so key handlers can scroll. */
export function setScrollGeometry(geo: Geometry): void {
  geometry = geo;
}

export function scrollBy(rows: number): void {
  if (!geometry || rows === 0) return;
  const now = resolveOffset(scrollStore.getState(), geometry);
  scrollStore.setState(scrollModelAt(geometry, now + rows));
}

export function scrollPage(dir: 1 | -1): void {
  if (geometry) scrollBy(dir * pageSize(geometry.viewport));
}

export function scrollWheel(dir: 1 | -1, now: number, precise = false): void {
  const step = wheelStep(scrollStore.getState().wheel, dir, now, precise);
  scrollStore.setState({ wheel: step.state });
  scrollBy(step.rows);
}

export function scrollToBottom(): void {
  scrollStore.setState({ follow: true, anchor: null });
}

export function scrollToTop(): void {
  if (geometry) scrollStore.setState(scrollModelAt(geometry, 0));
}
