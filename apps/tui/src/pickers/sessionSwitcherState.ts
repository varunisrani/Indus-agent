// The session switcher's state (J13, research 05 §3.19), kept pure so every key can be tested
// without Ink. Rows: a "+ new" row first, then the sessions live in this process, then stored
// sessions. The selection follows a row id, so a refresh that reorders rows keeps it in place.

import type { SessionRow } from "@indus/protocol";

export const VISIBLE_ROWS = 12;
export const MIN_WIDTH = 64;
export const MAX_WIDTH = 128;
export const TITLE_MAX = 64;
export const REFRESH_MS = 1_500;
export const NEW_ROW_ID = "+new";
export const BUSY_REFUSAL = "A reply is still running: stop the turn first, then switch.";
export const DELETE_HINT = "Press d again to delete this stored session; any other key keeps it.";

export type LiveStatus = "idle" | "working" | "waiting" | "starting";

export type SwitcherRow =
  | { readonly kind: "new"; readonly id: typeof NEW_ROW_ID }
  | {
      readonly kind: "live";
      readonly id: string;
      readonly title: string;
      readonly model: string;
      readonly source: string;
      readonly status: LiveStatus;
      readonly current: boolean;
    }
  | {
      readonly kind: "stored";
      readonly id: string;
      readonly title: string;
      readonly preview: string;
      /** Epoch seconds of the newest activity. */
      readonly when: number;
      readonly model: string;
      readonly source: string;
    };

export interface LiveInput {
  readonly id: string;
  readonly title: string | null;
  readonly model: string | null;
  readonly status: LiveStatus;
}

export interface SwitcherState {
  readonly rows: readonly SwitcherRow[];
  /** Id of the selected row. */
  readonly selected: string;
  /** First visible row. */
  readonly offset: number;
  /** Text typed on the "+ new" row: the first prompt of the new session. */
  readonly draft: string;
  /** A stored row waiting for the second `d`. */
  readonly pendingDelete: string | null;
  readonly notice: string | null;
}

export type SwitcherEffect =
  | { readonly type: "none" }
  | { readonly type: "close" }
  | { readonly type: "refresh" }
  | { readonly type: "new"; readonly text: string }
  | { readonly type: "open"; readonly id: string; readonly live: boolean }
  | { readonly type: "close-live"; readonly id: string }
  | { readonly type: "delete"; readonly id: string };

/** The keys the switcher reads, already normalised. */
export interface SwitcherKey {
  readonly input: string;
  readonly name: "up" | "down" | "return" | "escape" | "backspace" | "pageup" | "pagedown" | null;
  readonly ctrl: boolean;
  readonly meta: boolean;
}

export function clip(text: string, max: number): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= max ? flat : `${flat.slice(0, Math.max(0, max - 1))}…`;
}

/** The model name without its provider path. */
export function shortModel(model: string | null): string {
  if (!model) return "";
  return clip(model.split("/").pop() ?? model, 24);
}

/** "now", "5m", "3h", "2d", "6w" since `when` (epoch seconds). */
export function relativeAge(when: number, nowMs: number): string {
  const seconds = Math.max(0, Math.floor(nowMs / 1000 - when));
  if (seconds < 60) return "now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 14) return `${days}d`;
  return `${Math.floor(days / 7)}w`;
}

/**
 * Rows in display order; a stored row that is also live is shown once, as live. A live row
 * takes what `session.info` said, else its stored row's title, model and source: attached to the
 * daemon no `session.info` arrives, so the listing is what names it (W48-b).
 */
export function buildRows(
  live: readonly LiveInput[],
  stored: readonly SessionRow[],
  currentId: string | null,
): SwitcherRow[] {
  const rows: SwitcherRow[] = [{ kind: "new", id: NEW_ROW_ID }];
  const listed = new Map(stored.map((row) => [row.session_id, row]));
  const liveIds = new Set<string>();
  for (const session of live) {
    liveIds.add(session.id);
    const row = listed.get(session.id);
    rows.push({
      kind: "live",
      id: session.id,
      title: clip(session.title || row?.title || row?.preview || session.id, TITLE_MAX),
      model: shortModel(session.model || row?.model || null),
      source: row?.source ?? "",
      status: session.status,
      current: session.id === currentId,
    });
  }
  for (const row of stored) {
    if (liveIds.has(row.session_id)) continue;
    rows.push({
      kind: "stored",
      id: row.session_id,
      title: clip(row.title || row.preview || row.session_id, TITLE_MAX),
      preview: row.title ? clip(row.preview ?? "", TITLE_MAX) : "",
      when: row.last_active ?? row.started_at,
      model: shortModel(row.model ?? null),
      source: row.source ?? "",
    });
  }
  return rows;
}

/** Where the window starts so `index` is visible. */
export function windowFor(index: number, offset: number, count: number): number {
  const size = VISIBLE_ROWS;
  let start = offset;
  if (index < start) start = index;
  if (index >= start + size) start = index - size + 1;
  return Math.max(0, Math.min(start, Math.max(0, count - size)));
}

/** Width of the card for a terminal `columns` wide. */
export function switcherWidth(columns: number): number {
  return Math.max(Math.min(MIN_WIDTH, columns), Math.min(MAX_WIDTH, columns - 4));
}

export function initialSwitcher(
  rows: readonly SwitcherRow[] = buildRows([], [], null),
): SwitcherState {
  const current = rows.find((row) => row.kind === "live" && row.current);
  return {
    rows,
    selected: current?.id ?? NEW_ROW_ID,
    offset: 0,
    draft: "",
    pendingDelete: null,
    notice: null,
  };
}

function indexOf(state: SwitcherState): number {
  const at = state.rows.findIndex((row) => row.id === state.selected);
  return at < 0 ? 0 : at;
}

/** New rows from a refresh: the selection stays on the same id, else on the same position. */
export function withRows(state: SwitcherState, rows: readonly SwitcherRow[]): SwitcherState {
  if (rows.length === 0) return { ...state, rows, selected: NEW_ROW_ID, offset: 0 };
  let selected = state.selected;
  if (!rows.some((row) => row.id === selected)) {
    const old = indexOf(state);
    selected = (rows[Math.min(old, rows.length - 1)] as SwitcherRow).id;
  }
  const at = rows.findIndex((row) => row.id === selected);
  const pendingDelete = rows.some((row) => row.id === state.pendingDelete)
    ? state.pendingDelete
    : null;
  return {
    ...state,
    rows,
    selected,
    pendingDelete,
    offset: windowFor(at, state.offset, rows.length),
  };
}

function move(state: SwitcherState, step: number): SwitcherState {
  const count = state.rows.length;
  if (count === 0) return state;
  const at = Math.max(0, Math.min(count - 1, indexOf(state) + step));
  return {
    ...state,
    selected: (state.rows[at] as SwitcherRow).id,
    offset: windowFor(at, state.offset, count),
  };
}

const done = (state: SwitcherState, effect: SwitcherEffect = { type: "none" }) => ({
  state,
  effect,
});

/** One key. `busy`: a turn runs, so anything that changes the open session is refused. */
export function switcherStep(
  state: SwitcherState,
  key: SwitcherKey,
  busy: boolean,
): { state: SwitcherState; effect: SwitcherEffect } {
  const row = state.rows[indexOf(state)];
  const isDelete = !key.ctrl && !key.meta && key.name === null && key.input === "d";
  const onStored = row?.kind === "stored";

  // Any key but the second `d` on the same stored row cancels a pending delete.
  if (state.pendingDelete !== null) {
    const confirmed = isDelete && onStored && row.id === state.pendingDelete;
    const cleared: SwitcherState = { ...state, pendingDelete: null, notice: null };
    if (confirmed) return done(cleared, { type: "delete", id: row.id });
    state = cleared;
  }

  const refuse = (): { state: SwitcherState; effect: SwitcherEffect } =>
    done({ ...state, notice: BUSY_REFUSAL });

  if (key.name === "escape") return done(state, { type: "close" });
  if (key.name === "up") return done(move(state, -1));
  if (key.name === "down") return done(move(state, 1));
  if (key.name === "pageup") return done(move(state, -VISIBLE_ROWS));
  if (key.name === "pagedown") return done(move(state, VISIBLE_ROWS));
  if (key.ctrl && key.input === "r") return done({ ...state, notice: null }, { type: "refresh" });
  if (key.ctrl && key.input === "n") {
    return busy ? refuse() : done(state, { type: "new", text: "" });
  }
  if (key.ctrl && key.input === "d") {
    if (row?.kind !== "live") return done({ ...state, notice: "Ctrl+D closes a live session." });
    if (row.current && busy) return refuse();
    return done(state, { type: "close-live", id: row.id });
  }
  if (key.name === "return") {
    if (row === undefined) return done(state);
    if (busy) return refuse();
    if (row.kind === "new") return done(state, { type: "new", text: state.draft.trim() });
    return done(state, { type: "open", id: row.id, live: row.kind === "live" });
  }
  if (row?.kind === "new") {
    if (key.name === "backspace") return done({ ...state, draft: state.draft.slice(0, -1) });
    if (key.name === null && !key.ctrl && !key.meta && key.input !== "") {
      return done({ ...state, draft: state.draft + key.input.replace(/[\r\n]+/g, " ") });
    }
    return done(state);
  }
  if (isDelete && onStored) return done({ ...state, pendingDelete: row.id, notice: DELETE_HINT });
  return done(state);
}
