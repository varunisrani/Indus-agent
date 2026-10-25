// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SessionRow } from "../sidebar/sessionList.ts";
import {
  IDLE,
  REVEAL_MS,
  SessionSwitcher,
  stepSwitcher,
  switcherRowsFrom,
} from "./SessionSwitcher.tsx";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const ROWS = [
  { id: "a", title: "Alpha", state: "idle" as const },
  { id: "b", title: "Beta", state: "unread" as const },
  { id: "c", title: "Gamma", state: "running" as const },
];

const down = (init: KeyboardEventInit) =>
  window.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, ...init }));
const up = (init: KeyboardEventInit) =>
  window.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, ...init }));

describe("session switcher (W52-h-2)", () => {
  it("steps from the open chat and wraps both ways", () => {
    const first = stepSwitcher(IDLE, 1, 3, 2);
    expect(first).toEqual({ cycling: true, shown: false, index: 0 });
    expect(stepSwitcher(first, -1, 3, 2).index).toBe(2);
    expect(stepSwitcher(IDLE, 1, 1, 0)).toBe(IDLE);
  });

  it("a quick tap opens the next chat without showing the card", () => {
    const onOpen = vi.fn();
    render(<SessionSwitcher rows={ROWS} activeId="a" onOpen={onOpen} />);
    act(() => {
      down({ key: "Tab", ctrlKey: true });
      up({ key: "Control" });
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onOpen).toHaveBeenCalledWith("b");
  });

  it("held, the card shows the chats; letting go of Ctrl opens the pick", () => {
    vi.useFakeTimers();
    const onOpen = vi.fn();
    render(<SessionSwitcher rows={ROWS} activeId="a" onOpen={onOpen} />);
    act(() => {
      down({ key: "Tab", ctrlKey: true });
      down({ key: "Tab", ctrlKey: true });
      vi.advanceTimersByTime(REVEAL_MS + 10);
    });
    const options = screen.getAllByRole("option");
    // the running row adds a screen-reader note after its title
    expect(options.map((o) => o.textContent)).toEqual(["Alpha", "Beta", "Gamma(working)"]);
    expect(
      options.map((o) => o.querySelector(".session-switcher-dot")?.getAttribute("data-state")),
    ).toEqual(["idle", "unread", "running"]);
    expect(options[2]?.getAttribute("aria-selected")).toBe("true");
    act(() => up({ key: "Control" }));
    expect(onOpen).toHaveBeenCalledWith("c");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Escape cancels; fewer than two chats do nothing", () => {
    const onOpen = vi.fn();
    const view = render(<SessionSwitcher rows={ROWS} activeId="a" onOpen={onOpen} />);
    act(() => {
      down({ key: "Tab", ctrlKey: true });
      down({ key: "Escape" });
      up({ key: "Control" });
    });
    expect(onOpen).not.toHaveBeenCalled();
    view.rerender(<SessionSwitcher rows={ROWS.slice(0, 1)} activeId="a" onOpen={onOpen} />);
    act(() => {
      down({ key: "Tab", ctrlKey: true });
      up({ key: "Control" });
    });
    expect(onOpen).not.toHaveBeenCalled();
  });
});

describe("switcher rows (W55-F1-2)", () => {
  const row = (id: string, extra: Partial<SessionRow> = {}): SessionRow => ({
    id,
    durableId: id,
    title: id,
    preview: null,
    source: null,
    lastActive: 0,
    startedAt: 0,
    active: false,
    pinned: false,
    archived: false,
    branch: false,
    cwd: null,
    gitBranch: null,
    messageCount: 0,
    ids: [id],
    ...extra,
  });

  it("reads `is_active` as live, not running, and uses the sidebar's dots", () => {
    const rows = [
      row("quiet"),
      row("recent", { active: true }),
      row("answering", { active: true, ids: ["answering", "older"] }),
      row("asking", { pinned: true }),
      row("news"),
    ];
    const out = switcherRowsFrom(rows, {
      running: new Set(["older"]),
      attention: new Set(["asking"]),
      isUnread: (r) => r.id === "news",
      title: (r) => `T ${r.id}`,
    });
    expect(out.map((r) => [r.id, r.state])).toEqual([
      ["asking", "attention"],
      ["quiet", "idle"],
      ["recent", "live"],
      ["answering", "running"],
      ["news", "unread"],
    ]);
    expect(out[0]?.title).toBe("T asking");
  });
});
