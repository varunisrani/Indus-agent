// How the desktop pet wanders (P24-19). Pure: the overlay calls `stepRoam` once per animation
// frame with the time that passed, and draws the figure where the state says.
//
// The pet window covers the work area, so "inside the work area" means inside the window: the
// figure's whole box stays within `[0, area.width - sprite.width] × [0, area.height -
// sprite.height]` at every step, also after the area shrinks (a display change). A figure larger
// than the area sits at the top-left corner.
//
// Rhythm: rest for a while (most of the time), then walk at a steady pace to a new spot that is
// at least `MIN_TRIP_PX` away, mostly along the bottom band of the screen where it stays out of
// the way; after `SLEEP_AFTER_MS` of rests in a row it falls asleep until something happens.

export interface Size {
  readonly width: number;
  readonly height: number;
}

export interface Point {
  readonly x: number;
  readonly y: number;
}

export type RoamMood = "rest" | "walk" | "sleep";

export interface RoamState {
  readonly x: number;
  readonly y: number;
  readonly target: Point | null;
  /** Milliseconds of rest left before the next walk. */
  readonly restLeft: number;
  /** Rest time in a row (sleep after `SLEEP_AFTER_MS`). */
  readonly idleFor: number;
  readonly facing: 1 | -1;
  readonly mood: RoamMood;
}

export type Rng = () => number;

export const WALK_SPEED_PX_S = 70;
export const MIN_TRIP_PX = 120;
export const REST_MIN_MS = 3_000;
export const REST_MAX_MS = 14_000;
export const SLEEP_AFTER_MS = 90_000;
/** Share of trips that stay in the bottom band (the rest go anywhere). */
export const BOTTOM_BIAS = 0.8;
/** Height of the bottom band, as a share of the free height. */
export const BOTTOM_BAND = 0.25;

/** The largest x and y the figure's top-left corner may take (never below 0). */
export function roomFor(area: Size, sprite: Size): { maxX: number; maxY: number } {
  return {
    maxX: Math.max(0, Math.floor(area.width - sprite.width)),
    maxY: Math.max(0, Math.floor(area.height - sprite.height)),
  };
}

export function clampPoint(area: Size, sprite: Size, point: Point): Point {
  const { maxX, maxY } = roomFor(area, sprite);
  const fix = (v: number, max: number) => (Number.isFinite(v) ? Math.min(max, Math.max(0, v)) : 0);
  return { x: fix(point.x, maxX), y: fix(point.y, maxY) };
}

export function restDuration(rng: Rng): number {
  // squared: mostly short rests, now and then a long one
  const u = rng();
  return Math.round(REST_MIN_MS + (REST_MAX_MS - REST_MIN_MS) * u * u);
}

/** A destination at least `MIN_TRIP_PX` away when the area allows it. */
export function pickTarget(area: Size, sprite: Size, from: Point, rng: Rng): Point {
  const { maxX, maxY } = roomFor(area, sprite);
  const bottom = rng() < BOTTOM_BIAS;
  const bandTop = Math.floor(maxY * (1 - BOTTOM_BAND));
  let best: Point = from;
  let bestDistance = -1;
  for (let attempt = 0; attempt < 6; attempt++) {
    const x = Math.round(rng() * maxX);
    const y = bottom ? Math.round(bandTop + rng() * (maxY - bandTop)) : Math.round(rng() * maxY);
    const distance = Math.hypot(x - from.x, y - from.y);
    if (distance >= MIN_TRIP_PX) return { x, y };
    if (distance > bestDistance) {
      best = { x, y };
      bestDistance = distance;
    }
  }
  return best;
}

export function startRoam(area: Size, sprite: Size, rng: Rng): RoamState {
  const { maxX, maxY } = roomFor(area, sprite);
  return {
    x: Math.round(maxX * (0.6 + 0.3 * rng())),
    y: maxY,
    target: null,
    restLeft: restDuration(rng),
    idleFor: 0,
    facing: -1,
    mood: "rest",
  };
}

/** Wake a sleeping pet (a click, a status line): it rests briefly and goes on. */
export function nudge(state: RoamState): RoamState {
  return { ...state, idleFor: 0, mood: state.target === null ? "rest" : "walk" };
}

/** Advance the wander by `dtMs` inside `area` (see the module notes). */
export function stepRoam(
  state: RoamState,
  area: Size,
  sprite: Size,
  dtMs: number,
  rng: Rng,
): RoamState {
  const dt = Number.isFinite(dtMs) ? Math.max(0, Math.min(dtMs, 1_000)) : 0;
  const here = clampPoint(area, sprite, state);
  if (state.target === null) {
    const restLeft = state.restLeft - dt;
    const idleFor = state.idleFor + dt;
    if (idleFor >= SLEEP_AFTER_MS) {
      return { ...state, ...here, restLeft: Math.max(0, restLeft), idleFor, mood: "sleep" };
    }
    if (restLeft > 0) return { ...state, ...here, restLeft, idleFor, mood: "rest" };
    const target = pickTarget(area, sprite, here, rng);
    const facing = target.x < here.x ? -1 : target.x > here.x ? 1 : state.facing;
    return { ...state, ...here, target, restLeft: 0, facing, mood: "walk" };
  }
  const goal = clampPoint(area, sprite, state.target);
  const dx = goal.x - here.x;
  const dy = goal.y - here.y;
  const distance = Math.hypot(dx, dy);
  const travel = (WALK_SPEED_PX_S * dt) / 1_000;
  if (distance <= travel || distance === 0) {
    return {
      ...state,
      ...goal,
      target: null,
      restLeft: restDuration(rng),
      idleFor: 0,
      mood: "rest",
    };
  }
  const next = clampPoint(area, sprite, {
    x: here.x + (dx / distance) * travel,
    y: here.y + (dy / distance) * travel,
  });
  return {
    ...state,
    ...next,
    target: goal,
    idleFor: 0,
    facing: dx < 0 ? -1 : dx > 0 ? 1 : state.facing,
    mood: "walk",
  };
}

/** A small seeded generator (tests, and a stable first position per session). */
export function seededRng(seed: number): Rng {
  let s = seed >>> 0 || 1;
  return () => {
    s ^= s << 13;
    s >>>= 0;
    s ^= s >>> 17;
    s ^= s << 5;
    s >>>= 0;
    return s / 4_294_967_296;
  };
}
