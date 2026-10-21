// Tab and window shortcuts of the pane shell (P20-27). The platform modifier is ⌘ on macOS and
// Ctrl elsewhere. Holding it alone for 400 ms shows slot numbers on the tabs; any other key or
// letting go hides them. The mapping is pure (`actionForKey`) so it is tested without a DOM.

import { useEffect, useRef, useState } from "react";

export const SLOT_HINT_DELAY_MS = 400;

export type PaneAction =
  | { readonly kind: "new-chat" }
  | { readonly kind: "cycle"; readonly step: 1 | -1 }
  | { readonly kind: "jump"; readonly slot: number }
  | { readonly kind: "close" }
  | { readonly kind: "reopen" }
  | { readonly kind: "new-window" }
  | { readonly kind: "toggle-left" }
  | { readonly kind: "toggle-right" };

export interface KeyLike {
  readonly key: string;
  readonly code?: string;
  readonly metaKey: boolean;
  readonly ctrlKey: boolean;
  readonly shiftKey: boolean;
  readonly altKey: boolean;
}

export function isMac(platform: string): boolean {
  return platform === "darwin" || /^mac/i.test(platform);
}

/** The platform modifier is down (and the other one is not). */
export function modifierDown(event: KeyLike, platform: string): boolean {
  return isMac(platform) ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
}

/** The key is the platform modifier itself. */
export function isModifierKey(event: KeyLike, platform: string): boolean {
  return event.key === (isMac(platform) ? "Meta" : "Control");
}

export function actionForKey(event: KeyLike, platform: string): PaneAction | null {
  // Ctrl+Tab cycles on every platform (as browsers do).
  if (event.key === "Tab" && event.ctrlKey && !event.metaKey && !event.altKey) {
    return { kind: "cycle", step: event.shiftKey ? -1 : 1 };
  }
  if (!modifierDown(event, platform)) return null;
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  const digit = /^Digit([1-9])$/.exec(event.code ?? "")?.[1] ?? (/^[1-9]$/.test(key) ? key : null);
  if (digit !== null && !event.shiftKey && !event.altKey) {
    return { kind: "jump", slot: Number(digit) };
  }
  if (event.altKey) return key === "b" || event.code === "KeyB" ? { kind: "toggle-right" } : null;
  if (event.shiftKey) {
    if (key === "t") return { kind: "reopen" };
    if (key === "n") return { kind: "new-window" };
    return null;
  }
  if (key === "t") return { kind: "new-chat" };
  if (key === "w") return { kind: "close" };
  if (key === "b") return { kind: "toggle-left" };
  return null;
}

export interface ShortcutHandlers {
  onAction(action: PaneAction, event: KeyboardEvent): boolean;
}

export interface ShortcutTimers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

const browserTimers: ShortcutTimers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/**
 * Listen on the window: run actions, and report whether slot numbers should show (the modifier
 * held alone for 400 ms). `onAction` returns true when it handled the key (default prevented).
 */
export function usePaneShortcuts(
  platform: string,
  handlers: ShortcutHandlers,
  timers: ShortcutTimers = browserTimers,
): boolean {
  const [showSlots, setShowSlots] = useState(false);
  const latest = useRef(handlers);
  latest.current = handlers;
  useEffect(() => {
    let timer: unknown;
    const stop = () => {
      if (timer !== undefined) timers.clearTimeout(timer);
      timer = undefined;
      setShowSlots(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (isModifierKey(event, platform)) {
        if (timer === undefined && !event.repeat) {
          timer = timers.setTimeout(() => setShowSlots(true), SLOT_HINT_DELAY_MS);
        }
        return;
      }
      stop();
      const action = actionForKey(event, platform);
      if (action !== null && latest.current.onAction(action, event)) event.preventDefault();
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (isModifierKey(event, platform)) stop();
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", stop);
    return () => {
      stop();
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", stop);
    };
  }, [platform, timers]);
  return showSlots;
}
