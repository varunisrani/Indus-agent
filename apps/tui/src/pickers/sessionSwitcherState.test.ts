import type { SessionRow } from "@indus/protocol";
import { describe, expect, it } from "vitest";
import {
  BUSY_REFUSAL,
  buildRows,
  DELETE_HINT,
  initialSwitcher,
  NEW_ROW_ID,
  relativeAge,
  type SwitcherKey,
  type SwitcherState,
  switcherStep,
  switcherWidth,
  VISIBLE_ROWS,
  withRows,
} from "./sessionSwitcherState.ts";

const key = (spec: string): SwitcherKey => {
  const named: Record<string, SwitcherKey["name"]> = {
    up: "up",
    down: "down",
    enter: "return",
    esc: "escape",
    backspace: "backspace",
    pagedown: "pagedown",
  };
  if (spec.startsWith("ctrl+"))
    return { input: spec.slice(5), name: null, ctrl: true, meta: false };
  const name = named[spec] ?? null;
  return { input: name === null ? spec : "", name, ctrl: false, meta: false };
};

const stored = (id: string, extra: Partial<SessionRow> = {}): SessionRow => ({
  session_id: id,
  title: `title ${id}`,
  preview: `preview ${id}`,
  source: "tui",
  started_at: 1_000,
  last_active: 2_000,
  ...extra,
});

const live = (id: string) => ({
  id,
  title: `live ${id}`,
  model: "vendor/m-1",
  status: "idle" as const,
});

function press(state: SwitcherState, specs: string[], busy = false) {
  let current = state;
  const effects = [];
  for (const spec of specs) {
    const step = switcherStep(current, key(spec), busy);
    current = step.state;
    effects.push(step.effect);
  }
  return { state: current, effects };
}

describe("rows", () => {
  it("puts + new first, then live, then stored, a live one shown once", () => {
    const rows = buildRows([live("a")], [stored("a"), stored("b")], "a");
    expect(rows.map((row) => `${row.kind}:${row.id}`)).toEqual(["new:+new", "live:a", "stored:b"]);
    expect(rows[1]).toMatchObject({ current: true, model: "m-1" });
  });

  it("clips titles to 64 characters and starts on the open session", () => {
    const rows = buildRows([], [stored("x", { title: "t".repeat(100) })], null);
    expect((rows[1] as { title: string }).title).toHaveLength(64);
    expect(initialSwitcher(buildRows([live("a")], [], "a")).selected).toBe("a");
  });

  it("formats relative ages and fits width between 64 and 128", () => {
    expect(relativeAge(1_000, 1_000_000 + 30_000)).toBe("now");
    expect(relativeAge(0, 5 * 60_000)).toBe("5m");
    expect(relativeAge(0, 3 * 3_600_000)).toBe("3h");
    expect(relativeAge(0, 3 * 86_400_000)).toBe("3d");
    expect(switcherWidth(80)).toBe(76);
    expect(switcherWidth(300)).toBe(128);
    expect(switcherWidth(70)).toBe(66);
    expect(switcherWidth(40)).toBe(40);
  });
});

describe("keys", () => {
  it("a refresh keeps the selection on the same id when rows move", () => {
    let state = initialSwitcher(buildRows([], [stored("a"), stored("b"), stored("c")], null));
    state = press(state, ["down", "down"]).state;
    expect(state.selected).toBe("b");
    state = withRows(
      state,
      buildRows([], [stored("z"), stored("c"), stored("b"), stored("a")], null),
    );
    expect(state.selected).toBe("b");
  });

  it("a refresh that removes the selected row keeps its position", () => {
    let state = initialSwitcher(buildRows([], [stored("a"), stored("b"), stored("c")], null));
    state = press(state, ["down", "down"]).state;
    state = withRows(state, buildRows([], [stored("a"), stored("c")], null));
    expect(state.selected).toBe("c");
  });

  it("d then another key cancels the delete; d d deletes", () => {
    let state = initialSwitcher(buildRows([], [stored("a"), stored("b")], null));
    state = press(state, ["down"]).state;
    let run = press(state, ["d"]);
    expect(run.state.pendingDelete).toBe("a");
    expect(run.state.notice).toBe(DELETE_HINT);
    run = press(run.state, ["x", "d"]);
    expect(run.effects.map((e) => e.type)).toEqual(["none", "none"]);
    expect(run.state.pendingDelete).toBe("a");
    run = press(run.state, ["down"]);
    expect(run.state.pendingDelete).toBeNull();
    expect(run.state.selected).toBe("b");
    run = press(run.state, ["d", "d"]);
    expect(run.effects.at(-1)).toEqual({ type: "delete", id: "b" });
    expect(run.state.pendingDelete).toBeNull();
  });

  it("Enter opens, types on + new become the first prompt, Esc closes", () => {
    let state = initialSwitcher(buildRows([live("a")], [stored("b")], null));
    expect(state.selected).toBe(NEW_ROW_ID);
    let run = press(state, ["h", "i", "!", "backspace", "enter"]);
    expect(run.effects.at(-1)).toEqual({ type: "new", text: "hi" });
    state = press(state, ["down"]).state;
    run = press(state, ["enter"]);
    expect(run.effects[0]).toEqual({ type: "open", id: "a", live: true });
    run = press(state, ["down", "enter"]);
    expect(run.effects.at(-1)).toEqual({ type: "open", id: "b", live: false });
    expect(press(state, ["esc"]).effects[0]).toEqual({ type: "close" });
  });

  it("refuses switching, new and closing the open session while busy", () => {
    const state = initialSwitcher(buildRows([live("a")], [stored("b")], "a"));
    for (const spec of ["enter", "ctrl+n", "ctrl+d"]) {
      const run = press(state, [spec], true);
      expect(run.effects[0]).toEqual({ type: "none" });
      expect(run.state.notice).toBe(BUSY_REFUSAL);
    }
    expect(press(state, ["ctrl+r"], true).effects[0]).toEqual({ type: "refresh" });
  });

  it("Ctrl+D closes a live row, Ctrl+N starts a blank session", () => {
    const state = initialSwitcher(buildRows([live("a"), live("c")], [], "a"));
    expect(press(state, ["down", "ctrl+d"]).effects.at(-1)).toEqual({
      type: "close-live",
      id: "c",
    });
    expect(press(state, ["ctrl+n"]).effects[0]).toEqual({ type: "new", text: "" });
  });

  it("keeps the window on the selection past 12 rows", () => {
    const rows = buildRows(
      [],
      Array.from({ length: 30 }, (_, i) => stored(`s${i}`)),
      null,
    );
    const run = press(
      initialSwitcher(rows),
      Array.from({ length: 15 }, () => "down"),
    );
    expect(run.state.selected).toBe("s14");
    expect(run.state.offset).toBe(15 - VISIBLE_ROWS + 1);
    expect(press(run.state, ["pagedown"]).state.selected).toBe("s26");
  });
});
