// One store for the device settings the ambient side applies (integration of P20-32 with P20-35/36).
// The Settings overlay edits the desktop preferences (`indus:prefs:*`, `<userData>/desktop-prefs.json`);
// the tray, keep awake, notifications and the global shortcuts are applied by the ambient step. When
// main hands the ambient step the preferences store, those values are read from it and every
// `indus:ambient:set` / `indus:shortcuts:set` is written back into it, so the two views never
// disagree. Only the one-time keep-awake offer flag stays in the ambient file (it is not a setting).

import type { DesktopPrefsPatch, DesktopPrefsValue } from "../prefs.ts";
import type { AmbientSettings } from "./settings.ts";
import {
  parseAccelerator,
  SHORTCUT_ACTIONS,
  type ShortcutAction,
  type ShortcutSettings,
} from "./shortcuts.ts";

/** The part of `DesktopPrefs` the ambient step uses. */
export interface PrefsPort {
  get(): DesktopPrefsValue;
  set(patch: DesktopPrefsPatch): DesktopPrefsValue;
  onChange(listener: (prefs: DesktopPrefsValue) => void): () => void;
}

const PREF_KEY: Readonly<
  Record<ShortcutAction, "hudToggleShortcut" | "hudSnapShortcut" | "quickEntryShortcut">
> = {
  "hud-toggle": "hudToggleShortcut",
  "hud-snap": "hudSnapShortcut",
  "quick-entry": "quickEntryShortcut",
};

/** The ambient settings the preferences describe (`offered` comes from the ambient file). */
export function ambientFromPrefs(prefs: DesktopPrefsValue, offered: boolean): AmbientSettings {
  return {
    minimizeToTray: prefs.minimizeToTray,
    keepAwake: prefs.keepAwake,
    keepAwakeOffered: offered,
    // the master switch: off only when both kinds of notice are off (the renderer gates each kind)
    notifications: prefs.notifications.turnComplete || prefs.notifications.approvals,
    sounds: prefs.notifications.sounds,
  };
}

/** `indus:ambient:set` fields as a preferences patch. */
export function prefsPatchFromAmbient(patch: {
  minimizeToTray?: boolean | undefined;
  keepAwake?: DesktopPrefsValue["keepAwake"] | undefined;
  notifications?: boolean | undefined;
  sounds?: boolean | undefined;
}): DesktopPrefsPatch {
  const out: DesktopPrefsPatch = {};
  if (patch.minimizeToTray !== undefined) out.minimizeToTray = patch.minimizeToTray;
  if (patch.keepAwake !== undefined) out.keepAwake = patch.keepAwake;
  const notifications: NonNullable<DesktopPrefsPatch["notifications"]> = {};
  if (patch.notifications !== undefined) {
    notifications.turnComplete = patch.notifications;
    notifications.approvals = patch.notifications;
  }
  if (patch.sounds !== undefined) notifications.sounds = patch.sounds;
  if (Object.keys(notifications).length > 0) out.notifications = notifications;
  return out;
}

/**
 * The global shortcuts the preferences describe. An empty accelerator turns the action off and
 * keeps its last chord; a chord the grammar refuses keeps the previous setting (and is reported).
 */
export function shortcutsFromPrefs(
  prefs: DesktopPrefsValue,
  previous: ShortcutSettings,
  refused?: (action: ShortcutAction) => void,
): ShortcutSettings {
  const next = { ...previous };
  for (const action of SHORTCUT_ACTIONS) {
    const raw = prefs[PREF_KEY[action]].trim();
    if (raw === "") {
      next[action] = { ...previous[action], enabled: false };
      continue;
    }
    const parsed = parseAccelerator(raw);
    if (!parsed.ok) {
      refused?.(action);
      next[action] = { ...previous[action] };
      continue;
    }
    next[action] = { enabled: true, accelerator: parsed.accelerator };
  }
  return next;
}

/** The shortcut settings as a preferences patch (a disabled action is saved as ""). */
export function prefsPatchFromShortcuts(settings: ShortcutSettings): DesktopPrefsPatch {
  const out: DesktopPrefsPatch = {};
  for (const action of SHORTCUT_ACTIONS) {
    out[PREF_KEY[action]] = settings[action].enabled ? settings[action].accelerator : "";
  }
  return out;
}

export function sameShortcuts(a: ShortcutSettings, b: ShortcutSettings): boolean {
  return SHORTCUT_ACTIONS.every(
    (action) =>
      a[action].enabled === b[action].enabled && a[action].accelerator === b[action].accelerator,
  );
}
