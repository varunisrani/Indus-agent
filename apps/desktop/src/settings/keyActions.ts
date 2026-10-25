// The in-app key actions the person can rebind and that this module runs (W59-K1-2). One window
// listener (installed by `PrefsEffects`) turns a keydown into an action id through the person's
// effective bindings and announces it as a window event; the part of the app that owns the action
// (composer, model picker, voice buttons, shell) listens with `useKeyAction`. A key the pane
// shortcuts own, a repeat, or a keydown already handled (a key being recorded, the composer's own
// Enter) never runs an action.

import { useEffect, useRef } from "react";
import { actionForKey } from "../panes/shortcuts.ts";
import { acceleratorFromEvent, effectiveBindings, normalizeAccelerator } from "./keybindings.ts";

export const KEY_ACTION_EVENT = "indus-key-action";

/** The rebindable actions this dispatcher runs (the other rows are handled where they live). */
export const DISPATCHED_ACTIONS = [
  "chat.new",
  "settings.open",
  "composer.focus",
  "model.picker",
  "effort.up",
  "effort.down",
  "voice.dictation",
  "voice.conversation",
  "profile.next",
  "profile.previous",
  "profile.default",
] as const;

export type DispatchedAction = (typeof DISPATCHED_ACTIONS)[number];

export interface KeyEventLike {
  key: string;
  code?: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  repeat?: boolean;
  defaultPrevented?: boolean;
}

const fold = (accelerator: string) =>
  accelerator.replace(/\b(Command|Control)\b/g, "CommandOrControl");

/** The action `event` runs under `overrides`, or null. */
export function keyActionFor(
  event: KeyEventLike,
  overrides: Readonly<Record<string, string>>,
  platform: string,
): DispatchedAction | null {
  if (event.defaultPrevented === true || event.repeat === true) return null;
  const pressed = acceleratorFromEvent(event);
  if (pressed === "") return null;
  if (actionForKey(event, platform) !== null) return null;
  const wanted = fold(pressed);
  const bindings = effectiveBindings(overrides);
  for (const id of DISPATCHED_ACTIONS) {
    const bound = normalizeAccelerator(bindings[id] ?? "");
    if (bound !== null && fold(bound) === wanted) return id;
  }
  return null;
}

/** Listen on this window and announce each bound action (`PrefsEffects`). */
export function useKeyActionDispatch(
  overrides: Readonly<Record<string, string>> | null,
  platform: string,
): void {
  const latest = useRef(overrides);
  latest.current = overrides;
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const id = keyActionFor(event, latest.current ?? {}, platform);
      if (id === null) return;
      const announced = new CustomEvent<{ id: DispatchedAction; handled: boolean }>(
        KEY_ACTION_EVENT,
        { detail: { id, handled: false } },
      );
      window.dispatchEvent(announced);
      if (announced.detail.handled) event.preventDefault();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [platform]);
}

/** Run `handler` when one of `ids` is pressed; it returns false when it could not act. */
export function useKeyAction(
  ids: readonly DispatchedAction[],
  handler: (id: DispatchedAction) => boolean | undefined,
): void {
  const latest = useRef(handler);
  latest.current = handler;
  const key = ids.join(" ");
  useEffect(() => {
    const wanted = new Set(key.split(" "));
    const onAction = (event: Event) => {
      const detail = (event as CustomEvent<{ id: DispatchedAction; handled: boolean }>).detail;
      if (detail === undefined || !wanted.has(detail.id)) return;
      if (latest.current(detail.id) !== false) detail.handled = true;
    };
    window.addEventListener(KEY_ACTION_EVENT, onAction);
    return () => window.removeEventListener(KEY_ACTION_EVENT, onAction);
  }, [key]);
}
