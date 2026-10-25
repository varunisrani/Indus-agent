// The display preferences this window draws with (W55-F3, W56-G1): the renderer-only rows of
// Settings › Appearance (General, Window & layout, Chat display). Main keeps them in the desktop prefs file;
// `PrefsEffects` reads that once per window and publishes it here, so every transcript row, the
// sidebar and the shell read one snapshot instead of each asking main.

import { useSyncExternalStore } from "react";
import type { IndusPrefsSnapshot } from "../global.d.ts";

export type DisplayPrefs = Pick<
  IndusPrefsSnapshot,
  | "introSplash"
  | "reopenLastChat"
  | "sessionDensity"
  | "tabStrip"
  | "chatBackdrop"
  | "bubbleTransparency"
  | "textDirection"
  | "hideTimeline"
  | "toolCallDisplay"
  | "collapseThinking"
  | "appActionsSide"
  | "fileBrowser"
  | "floatingComposer"
>;

/** What a window draws before main answers (and without a bridge): the same as main's defaults. */
export const DISPLAY_DEFAULTS: DisplayPrefs = {
  introSplash: true,
  reopenLastChat: false,
  sessionDensity: "compact",
  tabStrip: "always",
  chatBackdrop: false,
  bubbleTransparency: 0,
  textDirection: "auto",
  hideTimeline: false,
  toolCallDisplay: "technical",
  collapseThinking: false,
  appActionsSide: "right",
  fileBrowser: false,
  floatingComposer: false,
};

let current: DisplayPrefs = DISPLAY_DEFAULTS;
let known = false;
const listeners = new Set<() => void>();

export function displayFrom(prefs: Partial<DisplayPrefs> | null | undefined): DisplayPrefs {
  if (prefs === null || prefs === undefined) return DISPLAY_DEFAULTS;
  const out = { ...DISPLAY_DEFAULTS };
  for (const key of Object.keys(DISPLAY_DEFAULTS) as Array<keyof DisplayPrefs>) {
    const value = prefs[key];
    if (value !== undefined) (out as Record<string, unknown>)[key] = value;
  }
  return out;
}

/** Publish main's snapshot (PrefsEffects). */
export function publishDisplayPrefs(prefs: Partial<DisplayPrefs> | null): void {
  const next = displayFrom(prefs);
  const wasKnown = known;
  known = prefs !== null;
  const same = (Object.keys(next) as Array<keyof DisplayPrefs>).every(
    (key) => next[key] === current[key],
  );
  // the first answer is news even when it holds the defaults (a cold-start choice waits for it)
  if (same && wasKnown === known) return;
  current = next;
  for (const listener of listeners) listener();
}

/** Whether main has answered in this window (a cold-start choice waits for it). */
export function displayPrefsKnown(): boolean {
  return known;
}

export function getDisplayPrefs(): DisplayPrefs {
  return current;
}

export function subscribeDisplayPrefs(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Back to the defaults (tests). */
export function resetDisplayPrefs(): void {
  known = false;
  current = DISPLAY_DEFAULTS;
  for (const listener of listeners) listener();
}

export function useDisplayPrefs(): DisplayPrefs {
  return useSyncExternalStore(subscribeDisplayPrefs, getDisplayPrefs, getDisplayPrefs);
}
