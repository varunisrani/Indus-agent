import { describe, expect, it } from "vitest";
import {
  COMPLETION_ROWS,
  completionWindow,
  type Geometry,
  idleWheel,
  Offsets,
  pageSize,
  resolveOffset,
  scrollModelAt,
  type WheelState,
  wheelStep,
  windowAt,
} from "./virtualRows.ts";

const geometry = (heights: number[], viewport: number, prefix = "r"): Geometry => ({
  keys: heights.map((_, i) => `${prefix}${i}`),
  offsets: new Offsets(heights),
  viewport,
});

describe("offsets and windows", () => {
  it("finds rows by line with prefix sums", () => {
    const offsets = new Offsets([3, 1, 4, 2]);
    expect(offsets.total).toBe(10);
    expect(offsets.offsetOf(2)).toBe(4);
    expect(offsets.indexAt(0)).toBe(0);
    expect(offsets.indexAt(3)).toBe(1);
    expect(offsets.indexAt(4)).toBe(2);
    expect(offsets.indexAt(9)).toBe(3);
    expect(offsets.indexAt(99)).toBe(3);
  });

  it("covers only the rows inside a viewport of a 5,000-row list", () => {
    const offsets = new Offsets(Array.from({ length: 5000 }, () => 2));
    const win = windowAt(offsets, 101, 10);
    expect(win).toMatchObject({ start: 50, end: 56, skip: 1, topSpacer: 100 });
    expect(win.bottomSpacer).toBe(10_000 - 112);
    expect(win.end - win.start).toBeLessThanOrEqual(6);
  });

  it("clamps the offset to the last full screen", () => {
    const offsets = new Offsets([5, 5]);
    expect(windowAt(offsets, 50, 4).offset).toBe(6);
    expect(windowAt(offsets, -3, 4).offset).toBe(0);
    expect(windowAt(new Offsets([]), 0, 4)).toMatchObject({ start: 0, end: 0 });
  });
});

describe("scroll model", () => {
  it("follows the bottom by default", () => {
    const geo = geometry([4, 4, 4], 5);
    expect(resolveOffset({ follow: true, anchor: null }, geo)).toBe(7);
  });

  it("stops following when scrolled up, and resumes at the bottom", () => {
    const geo = geometry([4, 4, 4], 5);
    const up = scrollModelAt(geo, 5);
    expect(up).toEqual({ follow: false, anchor: { key: "r1", within: 1 } });
    expect(scrollModelAt(geo, 7)).toEqual({ follow: true, anchor: null });
    expect(scrollModelAt(geo, 999).follow).toBe(true);
  });

  it("does not move a scrolled-up view when output is appended", () => {
    const before = geometry([4, 4, 4], 5);
    const model = scrollModelAt(before, 2);
    const after = geometry([4, 4, 4, 9, 9], 5);
    expect(resolveOffset(model, after)).toBe(2);
  });

  it("keeps the anchor row on top when the width changes every height", () => {
    const narrow = geometry([6, 6, 6, 6, 6], 4);
    const model = scrollModelAt(narrow, 13); // row 2, one line in
    const wide = geometry([3, 3, 3, 3, 3], 4);
    const offset = resolveOffset(model, wide);
    expect(wide.offsets.indexAt(offset)).toBe(2);
    expect(offset - wide.offsets.offsetOf(2)).toBe(1);
  });

  it("falls back to the bottom when the anchor row is gone", () => {
    const model = scrollModelAt(geometry([4, 4, 4], 2), 1);
    expect(resolveOffset(model, geometry([4, 4], 2, "other"))).toBe(6);
  });

  it("pages by half the viewport, at least four rows", () => {
    expect(pageSize(6)).toBe(4);
    expect(pageSize(30)).toBe(15);
    expect(pageSize(0)).toBe(4);
  });
});

describe("wheel", () => {
  const run = (events: [1 | -1, number, boolean?][]) => {
    let state: WheelState = idleWheel;
    return events.map(([dir, at, precise]) => {
      const step = wheelStep(state, dir, at, precise);
      state = step.state;
      return step.rows;
    });
  };

  it("speeds up on a quick run in one direction", () => {
    const rows = run([
      [1, 0],
      [1, 20],
      [1, 40],
      [1, 60],
      [1, 80],
      [1, 100],
    ]);
    expect(rows).toEqual([1, 1, 2, 2, 3, 3]);
    expect(
      run([
        [-1, 0],
        [-1, 500],
      ]),
    ).toEqual([-1, -1]);
  });

  it("drops one reversed tick right after a fast run", () => {
    const rows = run([
      [1, 0],
      [1, 20],
      [1, 40],
      [1, 60],
      [-1, 80],
      [-1, 100],
    ]);
    expect(rows.slice(4)).toEqual([0, -1]);
  });

  it("moves one row per event with a modifier held", () => {
    const rows = run([
      [1, 0, true],
      [1, 10, true],
      [1, 20, true],
      [1, 30, true],
      [-1, 40, true],
    ]);
    expect(rows).toEqual([1, 1, 1, 1, -1]);
  });

  it("caps the speed", () => {
    const events = Array.from({ length: 40 }, (_, i) => [1, i * 10] as [1, number]);
    expect(Math.max(...run(events))).toBe(6);
  });
});

describe("completion window", () => {
  it("shows everything when the list is short", () => {
    expect(completionWindow(10, 3)).toEqual({ start: 0, end: 10 });
  });

  it("centres a long list on the selection, pinned at the ends", () => {
    expect(completionWindow(100, 0)).toEqual({ start: 0, end: COMPLETION_ROWS });
    expect(completionWindow(100, 50)).toEqual({ start: 42, end: 58 });
    expect(completionWindow(100, 99)).toEqual({ start: 84, end: 100 });
  });
});
