import { describe, expect, it } from "vitest";
import {
  clampPoint,
  MIN_TRIP_PX,
  nudge,
  pickTarget,
  REST_MAX_MS,
  REST_MIN_MS,
  type RoamState,
  restDuration,
  roomFor,
  type Size,
  SLEEP_AFTER_MS,
  seededRng,
  startRoam,
  stepRoam,
} from "./roam.ts";

function inside(state: RoamState, area: Size, sprite: Size): boolean {
  const { maxX, maxY } = roomFor(area, sprite);
  return state.x >= 0 && state.y >= 0 && state.x <= maxX && state.y <= maxY;
}

describe("pet roam", () => {
  it("stays inside the work area for random geometries", () => {
    const rng = seededRng(20261021);
    for (let geometry = 0; geometry < 200; geometry++) {
      const area = { width: 40 + Math.floor(rng() * 3000), height: 40 + Math.floor(rng() * 2000) };
      const sprite = { width: 16 + Math.floor(rng() * 300), height: 16 + Math.floor(rng() * 300) };
      let state = startRoam(area, sprite, rng);
      expect(inside(state, area, sprite)).toBe(true);
      let current = area;
      for (let frame = 0; frame < 600; frame++) {
        // now and then a display change shrinks or grows the area mid-walk
        if (frame === 300) {
          current = {
            width: Math.max(20, Math.floor(area.width * (0.3 + rng()))),
            height: Math.max(20, Math.floor(area.height * (0.3 + rng()))),
          };
        }
        state = stepRoam(state, current, sprite, 16 + rng() * 400, rng);
        expect(inside(state, current, sprite)).toBe(true);
        if (state.target !== null) {
          expect(inside({ ...state, ...state.target }, current, sprite)).toBe(true);
        }
      }
    }
  });

  it("a figure larger than the area sits at the corner", () => {
    const area = { width: 100, height: 80 };
    const sprite = { width: 200, height: 200 };
    expect(clampPoint(area, sprite, { x: 50, y: 50 })).toEqual({ x: 0, y: 0 });
    let state = startRoam(area, sprite, seededRng(3));
    for (let i = 0; i < 100; i++) state = stepRoam(state, area, sprite, 500, seededRng(i + 1));
    expect([state.x, state.y]).toEqual([0, 0]);
  });

  it("non-finite inputs never escape", () => {
    const area = { width: 800, height: 600 };
    const sprite = { width: 100, height: 100 };
    expect(clampPoint(area, sprite, { x: Number.NaN, y: Number.POSITIVE_INFINITY })).toEqual({
      x: 0,
      y: 0,
    });
    const state = stepRoam(
      startRoam(area, sprite, seededRng(1)),
      area,
      sprite,
      Number.NaN,
      seededRng(2),
    );
    expect(inside(state, area, sprite)).toBe(true);
  });

  it("rests, then walks at least a trip's length, then rests again", () => {
    const area = { width: 1600, height: 900 };
    const sprite = { width: 96, height: 96 };
    const rng = seededRng(7);
    let state = startRoam(area, sprite, rng);
    expect(state.mood).toBe("rest");
    let waited = 0;
    while (state.mood === "rest" && waited < REST_MAX_MS + 2_000) {
      state = stepRoam(state, area, sprite, 1_000, rng); // one step covers at most a second
      waited += 1_000;
    }
    expect(state.mood).toBe("walk");
    const start = { x: state.x, y: state.y };
    const target = state.target as { x: number; y: number };
    expect(Math.hypot(target.x - start.x, target.y - start.y)).toBeGreaterThanOrEqual(MIN_TRIP_PX);
    let frames = 0;
    while (state.mood === "walk" && frames < 10_000) {
      state = stepRoam(state, area, sprite, 50, rng);
      frames += 1;
    }
    expect(state.mood).toBe("rest");
    expect([state.x, state.y]).toEqual([target.x, target.y]);
    expect(state.facing).toBe(target.x < start.x ? -1 : 1);
  });

  it("falls asleep after a long idle and wakes on a nudge", () => {
    const area = { width: 800, height: 600 };
    const sprite = { width: 64, height: 64 };
    let state: RoamState = { ...startRoam(area, sprite, seededRng(1)), restLeft: 10 ** 9 };
    for (let t = 0; t < SLEEP_AFTER_MS; t += 1_000)
      state = stepRoam(state, area, sprite, 1_000, seededRng(t));
    expect(state.mood).toBe("sleep");
    expect(nudge(state).mood).toBe("rest");
  });

  it("rest lengths and targets stay in their ranges", () => {
    const rng = seededRng(99);
    for (let i = 0; i < 500; i++) {
      const rest = restDuration(rng);
      expect(rest).toBeGreaterThanOrEqual(REST_MIN_MS);
      expect(rest).toBeLessThanOrEqual(REST_MAX_MS);
      const t = pickTarget(
        { width: 500, height: 400 },
        { width: 50, height: 50 },
        { x: 0, y: 0 },
        rng,
      );
      expect(t.x).toBeGreaterThanOrEqual(0);
      expect(t.x).toBeLessThanOrEqual(450);
      expect(t.y).toBeLessThanOrEqual(350);
    }
  });
});
