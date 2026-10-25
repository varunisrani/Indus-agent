// The session switcher (W52-h-2): Ctrl+Tab steps to the next chat (Ctrl+Shift+Tab the previous)
// in the sidebar's order — pinned first, then by last activity. A quick tap switches at once; held
// past `REVEAL_MS` a small card at the top of the window shows the chats with the pick marked, and
// letting go of Ctrl opens it. While the card shows: Tab / ↓ next, Shift+Tab / ↑ previous, Enter
// opens, Escape cancels, a click opens that row. Needs two chats or more. Each row carries the
// sidebar's dot (W55-F1-2): waiting for the person, running (live turn events), unread, live (the
// list's `is_active`: recently active, not answering) or idle.

import { useCallback, useEffect, useRef, useState } from "react";
import { useT } from "../i18n/index.tsx";
import { switcherStep } from "../panes/shortcuts.ts";
import { type SessionDot, sessionDot } from "../sidebar/Sidebar.tsx";
import type { SessionRow } from "../sidebar/sessionList.ts";
import { FloatingHud } from "../ui/components.tsx";

export const REVEAL_MS = 220;

export interface SwitcherRow {
  readonly id: string;
  readonly title: string;
  /** The sidebar row's dot for this chat (`sessionDot`). */
  readonly state: SessionDot;
}

/**
 * The switcher's rows in the sidebar's order (pinned first) with the sidebar's dots. `running`
 * holds session ids with a live turn (turn events), `attention` ids waiting for the person; a
 * row's `active` (REST `is_active`) only ever makes it "live", never "running".
 */
export function switcherRowsFrom(
  rows: readonly SessionRow[],
  facts: {
    running: ReadonlySet<string>;
    attention: ReadonlySet<string>;
    isUnread: (row: SessionRow) => boolean;
    title: (row: SessionRow) => string;
  },
): SwitcherRow[] {
  const ordered = [...rows.filter((row) => row.pinned), ...rows.filter((row) => !row.pinned)];
  return ordered.map((row) => ({
    id: row.id,
    title: facts.title(row),
    state: sessionDot({
      attention: row.ids.some((id) => facts.attention.has(id)),
      running: row.ids.some((id) => facts.running.has(id)),
      unread: facts.isUnread(row),
      live: row.active,
    }),
  }));
}

export interface SwitcherState {
  /** A Ctrl+Tab cycle is in progress (Ctrl still held). */
  readonly cycling: boolean;
  /** The card is shown. */
  readonly shown: boolean;
  readonly index: number;
}

export const IDLE: SwitcherState = { cycling: false, shown: false, index: 0 };

/** The state after one step (pure; `current` is the open chat's index or -1). */
export function stepSwitcher(
  state: SwitcherState,
  step: 1 | -1,
  count: number,
  current: number,
): SwitcherState {
  if (count < 2) return state;
  const from = state.cycling ? state.index : current;
  const index = (((from + step) % count) + count) % count;
  return { cycling: true, shown: state.shown, index };
}

export function SessionSwitcher(props: {
  rows: readonly SwitcherRow[];
  activeId: string | null;
  onOpen: (id: string) => void;
}) {
  const t = useT();
  const [state, setShown] = useState<SwitcherState>(IDLE);
  // the latest state, readable at once by the next key event (before React renders)
  const current = useRef<SwitcherState>(IDLE);
  const setState = useCallback((next: (before: SwitcherState) => SwitcherState) => {
    current.current = next(current.current);
    setShown(current.current);
  }, []);
  const live = useRef({ rows: props.rows, activeId: props.activeId, onOpen: props.onOpen });
  live.current = { rows: props.rows, activeId: props.activeId, onOpen: props.onOpen };
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const reset = useCallback(() => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
    setState(() => IDLE);
  }, [setState]);

  const commit = useCallback(() => {
    const { rows, activeId, onOpen } = live.current;
    const row = rows[current.current.index];
    reset();
    if (row !== undefined && row.id !== activeId) onOpen(row.id);
  }, [reset]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const { rows, activeId } = live.current;
      const now = current.current;
      const step = switcherStep(event);
      if (step !== null) {
        // not stopped: the pane shortcuts see the key too (it ends their slot-number hint)
        event.preventDefault();
        if (rows.length < 2) return;
        const at = rows.findIndex((row) => row.id === activeId);
        setState((before) => stepSwitcher(before, step, rows.length, at));
        if (!now.cycling && timer.current === null) {
          timer.current = setTimeout(() => {
            timer.current = null;
            setState((before) => (before.cycling ? { ...before, shown: true } : before));
          }, REVEAL_MS);
        }
        return;
      }
      if (!now.cycling) return;
      if (event.key === "Escape") {
        event.preventDefault();
        reset();
      } else if (event.key === "Enter") {
        event.preventDefault();
        commit();
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const delta = event.key === "ArrowDown" ? 1 : -1;
        setState((before) => stepSwitcher(before, delta, rows.length, before.index));
      }
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.key === "Control" && current.current.cycling) commit();
    };
    const onBlur = () => reset();
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("keyup", onKeyUp, true);
      window.removeEventListener("blur", onBlur);
      if (timer.current !== null) clearTimeout(timer.current);
    };
  }, [commit, reset, setState]);

  if (!state.shown) return null;
  return (
    <FloatingHud
      label={t("switcher.label")}
      width={304}
      onClose={reset}
      className="session-switcher"
    >
      <div role="listbox" aria-label={t("switcher.label")} className="session-switcher-list">
        {props.rows.map((row, index) => (
          <div
            key={row.id}
            role="option"
            tabIndex={-1}
            aria-selected={index === state.index}
            className="desk-hud-row session-switcher-row"
            onMouseDown={(event) => {
              event.preventDefault();
              reset();
              if (row.id !== props.activeId) props.onOpen(row.id);
            }}
          >
            <span className="session-switcher-dot" data-state={row.state} aria-hidden="true" />
            <span className="session-switcher-title">{row.title}</span>
            {row.state === "running" && <span className="sr-only">{t("sidebar.running")}</span>}
            {row.state === "attention" && <span className="sr-only">{t("sidebar.attention")}</span>}
          </div>
        ))}
      </div>
    </FloatingHud>
  );
}
