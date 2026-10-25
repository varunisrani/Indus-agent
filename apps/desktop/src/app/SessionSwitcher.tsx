// The session switcher (W52-h-2): Ctrl+Tab steps to the next chat (Ctrl+Shift+Tab the previous)
// in the sidebar's order — pinned first, then by last activity. A quick tap switches at once; held
// past `REVEAL_MS` a small card at the top of the window shows the chats with the pick marked, and
// letting go of Ctrl opens it. While the card shows: Tab / ↓ next, Shift+Tab / ↑ previous, Enter
// opens, Escape cancels, a click opens that row. Needs two chats or more.

import { useCallback, useEffect, useRef, useState } from "react";
import { useT } from "../i18n/index.tsx";
import { switcherStep } from "../panes/shortcuts.ts";
import { FloatingHud } from "../ui/components.tsx";

export const REVEAL_MS = 220;

export interface SwitcherRow {
  readonly id: string;
  readonly title: string;
  readonly state: "running" | "unread" | "idle";
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
          </div>
        ))}
      </div>
    </FloatingHud>
  );
}
