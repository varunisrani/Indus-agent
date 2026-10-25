// Desktop preferences held by main (P20-32): settings that belong to this device and to the app,
// not to a profile — minimise to tray, keep awake, global shortcuts, notifications, in-app key
// bindings, chat text size. One JSON file in userData, written atomically; the renderer reads and
// patches it over `indus:prefs:get|set` and every window hears `indus:prefs:changed`. Main-side
// owners (tray, keep awake, quick entry, HUD) subscribe with `onChange`.

import path from "node:path";
import type { z } from "zod";
import type { MainContext } from "./context.ts";
import { readJsonFile, writeFileAtomic } from "./fsSecure.ts";
import { IpcError } from "./ipc/envelope.ts";
import type { EventTarget } from "./ipc/registry.ts";
import { type prefsPatch, prefsSnapshot } from "./ipc/schemas.ts";

export type DesktopPrefsValue = z.output<typeof prefsSnapshot>;
export type DesktopPrefsPatch = z.output<typeof prefsPatch>;

export const PREFS_FILE = "desktop-prefs.json";

export const DEFAULT_PREFS: DesktopPrefsValue = {
  minimizeToTray: false,
  keepAwake: "while-working",
  quickEntryShortcut: "CommandOrControl+Shift+Space",
  hudSnapShortcut: "CommandOrControl+Shift+G",
  hudToggleShortcut: "CommandOrControl+Shift+H",
  notifications: {
    turnComplete: true,
    approvals: true,
    sounds: true,
    input: true,
    turnFailed: true,
    plugins: true,
  },
  keybindings: {},
  chatTextScale: 110,
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

function clone(value: DesktopPrefsValue): DesktopPrefsValue {
  return {
    ...value,
    notifications: { ...value.notifications },
    keybindings: { ...value.keybindings },
  };
}

/** Each field of `raw` that is valid on its own, over the defaults (a bad field never wipes the rest). */
export function readPrefs(raw: unknown): DesktopPrefsValue {
  const out = clone(DEFAULT_PREFS);
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return out;
  const record = raw as Record<string, unknown>;
  const shape = prefsSnapshot.shape;
  for (const key of Object.keys(shape) as Array<keyof DesktopPrefsValue>) {
    if (!(key in record)) continue;
    if (key === "notifications") {
      const parsed = shape.notifications.partial().safeParse(record.notifications);
      if (parsed.success) {
        for (const [name, value] of Object.entries(parsed.data)) {
          if (typeof value === "boolean") out.notifications[name as "sounds"] = value;
        }
        // W59-K1-1: the kinds split out of an older file follow the switch that covered them
        // (questions were "approvals", failed turns were "turnComplete").
        const stored = parsed.data;
        if (stored.input === undefined && stored.approvals !== undefined)
          out.notifications.input = stored.approvals;
        if (stored.turnFailed === undefined && stored.turnComplete !== undefined)
          out.notifications.turnFailed = stored.turnComplete;
      }
      continue;
    }
    const parsed = shape[key].safeParse(record[key]);
    if (parsed.success) (out as Record<string, unknown>)[key] = parsed.data;
  }
  return out;
}

export function applyPatch(
  current: DesktopPrefsValue,
  patch: DesktopPrefsPatch,
): DesktopPrefsValue {
  const next = clone(current);
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) continue;
    if (key === "notifications") {
      next.notifications = {
        ...next.notifications,
        ...(value as Partial<typeof next.notifications>),
      };
    } else if (key === "keybindings") {
      next.keybindings = { ...(value as Record<string, string>) };
    } else {
      (next as Record<string, unknown>)[key] = value;
    }
  }
  return prefsSnapshot.parse(next);
}

export interface PrefsStoreOptions {
  file: string;
  log?(line: string): void;
  write?(file: string, text: string): void;
  read?(file: string): unknown;
}

export class DesktopPrefs {
  private value: DesktopPrefsValue;
  private readonly options: PrefsStoreOptions;
  private readonly listeners = new Set<(prefs: DesktopPrefsValue) => void>();

  constructor(options: PrefsStoreOptions) {
    this.options = options;
    this.value = readPrefs((options.read ?? readJsonFile)(options.file));
  }

  get(): DesktopPrefsValue {
    return clone(this.value);
  }

  /** Merge `patch`, write the file, tell listeners; the stored value when the write fails. */
  set(patch: DesktopPrefsPatch): DesktopPrefsValue {
    const next = applyPatch(this.value, patch);
    try {
      (this.options.write ?? ((file, text) => writeFileAtomic(file, text)))(
        this.options.file,
        `${JSON.stringify(next, null, 2)}\n`,
      );
    } catch {
      this.options.log?.("[prefs] could not save the desktop preferences");
      throw new IpcError("save-failed", "the preferences could not be saved");
    }
    this.value = next;
    for (const listener of this.listeners) {
      try {
        listener(clone(next));
      } catch {
        this.options.log?.("[prefs] a preference listener failed");
      }
    }
    return clone(next);
  }

  onChange(listener: (prefs: DesktopPrefsValue) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}

/** `indus:prefs:get|set` and the change broadcast; returns the store for main-side owners. */
export function installPrefsIpc(context: MainContext, prefs?: DesktopPrefs): DesktopPrefs {
  const store =
    prefs ??
    new DesktopPrefs({
      file: path.join(context.paths.userData, PREFS_FILE),
      log: (line) => context.log.write(line),
    });
  context.registry.handle("indus:prefs:get", () => store.get());
  context.registry.handle("indus:prefs:set", (patch) => store.set(patch));
  store.onChange((value) => {
    for (const win of context.windows.windows()) {
      context.registry.emit(win.webContents as EventTarget, "indus:prefs:changed", value);
    }
  });
  return store;
}
