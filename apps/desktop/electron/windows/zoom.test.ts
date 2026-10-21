import { describe, expect, it, vi } from "vitest";
import { FakeTimers } from "../../testing/fakeTimers.ts";
import {
  clampZoom,
  stepZoom,
  ZOOM_DEBOUNCE_MS,
  ZOOM_MAX,
  ZOOM_MIN,
  ZOOM_SETTLE_CHECKS,
  ZOOM_SETTLE_MS,
  ZoomKeeper,
  type ZoomTarget,
  zoomPercent,
} from "./zoom.ts";

/** A web contents whose zoom Chromium resets (to its per-URL memory, here 0) on navigation. */
function fakeContents(): ZoomTarget & { navigateInPage(): void; destroy(): void; sets: number[] } {
  let level = 0;
  let destroyed = false;
  const target = {
    sets: [] as number[],
    getZoomLevel: () => level,
    setZoomLevel: (next: number) => {
      level = next;
      target.sets.push(next);
    },
    isDestroyed: () => destroyed,
    navigateInPage: () => {
      level = 0;
    },
    destroy: () => {
      destroyed = true;
    },
  };
  return target;
}

describe("zoom levels", () => {
  it("clamps to the range on half steps", () => {
    expect(clampZoom(0.3)).toBe(0.5);
    expect(clampZoom(99)).toBe(ZOOM_MAX);
    expect(clampZoom(-99)).toBe(ZOOM_MIN);
    expect(clampZoom(Number.NaN)).toBe(0);
    expect(clampZoom("2")).toBe(0);
  });

  it("steps and resets", () => {
    expect(stepZoom(0, "in")).toBe(0.5);
    expect(stepZoom(0, "out")).toBe(-0.5);
    expect(stepZoom(ZOOM_MAX, "in")).toBe(ZOOM_MAX);
    expect(stepZoom(3, "reset")).toBe(0);
    expect(zoomPercent(0)).toBe(100);
    expect(zoomPercent(1)).toBe(120);
  });
});

describe("ZoomKeeper (R08.15)", () => {
  it("re-applies the saved level after a simulated hash navigation", () => {
    const timers = new FakeTimers();
    const onApplied = vi.fn();
    const keeper = new ZoomKeeper({ level: 2, timers, onApplied });
    const contents = fakeContents();
    const tracked = keeper.track(contents);
    expect(contents.getZoomLevel()).toBe(2);
    contents.navigateInPage();
    tracked.nudge();
    timers.advance(ZOOM_DEBOUNCE_MS - 1);
    expect(contents.getZoomLevel()).toBe(0);
    timers.advance(1);
    expect(contents.getZoomLevel()).toBe(2);
    expect(onApplied).toHaveBeenLastCalledWith(contents, 2);
  });

  it("settle checks catch a drop that lands after the first re-assert, then stop", () => {
    const timers = new FakeTimers();
    const keeper = new ZoomKeeper({ level: 1, timers });
    const contents = fakeContents();
    const tracked = keeper.track(contents);
    tracked.nudge();
    timers.advance(ZOOM_DEBOUNCE_MS);
    contents.navigateInPage(); // the compositor drops it again while settling
    timers.advance(ZOOM_SETTLE_MS);
    expect(contents.getZoomLevel()).toBe(1);
    const before = contents.sets.length;
    timers.advance(ZOOM_SETTLE_MS * ZOOM_SETTLE_CHECKS);
    expect(contents.sets.length).toBe(before); // no churn when nothing drifted
    expect(timers.pending()).toBe(0);
  });

  it("a burst of events re-asserts once (debounced)", () => {
    const timers = new FakeTimers();
    const keeper = new ZoomKeeper({ level: 1, timers });
    const contents = fakeContents();
    const tracked = keeper.track(contents);
    contents.navigateInPage();
    for (let i = 0; i < 10; i += 1) {
      tracked.nudge();
      timers.advance(10);
    }
    const before = contents.sets.length;
    timers.advance(ZOOM_DEBOUNCE_MS);
    expect(contents.sets.length).toBe(before + 1);
  });

  it("self-sizing windows are not tracked; destroyed windows are left alone", () => {
    const timers = new FakeTimers();
    const keeper = new ZoomKeeper({ level: 3, timers });
    const hud = fakeContents();
    keeper.track(hud, { selfSizing: true }).nudge();
    timers.advance(1_000);
    expect(hud.sets).toEqual([]);
    const gone = fakeContents();
    const tracked = keeper.track(gone);
    gone.destroy();
    tracked.nudge();
    timers.advance(1_000);
    expect(gone.sets).toEqual([3]);
  });

  it("setLevel applies to every tracked window; dispose stops tracking", () => {
    const timers = new FakeTimers();
    const keeper = new ZoomKeeper({ timers });
    const a = fakeContents();
    const b = fakeContents();
    keeper.track(a);
    keeper.track(b).dispose();
    expect(keeper.setLevel(1.2)).toBe(1);
    expect(a.getZoomLevel()).toBe(1);
    expect(b.getZoomLevel()).toBe(0);
  });
});
