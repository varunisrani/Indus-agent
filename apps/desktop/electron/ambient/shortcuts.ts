// Global shortcuts (P20-35): one grammar for every chord the app registers with the OS, and one
// board that owns the registrations so a chord is never held twice and a failure is reported
// instead of thrown.
//
// Grammar: modifiers first, then exactly one key. At least one modifier (a bare global key would be
// taken away from every other app) and never Escape (our floating windows use it to go away).
// Spelling is normalised (`ctrl+shift+g` → `Control+Shift+G`) so a saved value round-trips.

import fs from "node:fs";
import path from "node:path";
import { writeFileAtomic } from "../fsSecure.ts";

export type ShortcutAction = "hud-toggle" | "hud-snap" | "quick-entry";
export const SHORTCUT_ACTIONS: readonly ShortcutAction[] = [
  "hud-toggle",
  "hud-snap",
  "quick-entry",
];

export const DEFAULT_ACCELERATORS: Readonly<Record<ShortcutAction, string>> = {
  "hud-toggle": "CommandOrControl+Shift+H",
  "hud-snap": "CommandOrControl+Shift+G",
  "quick-entry": "CommandOrControl+Shift+Space",
};

export type AcceleratorRefusal =
  | "empty"
  | "no-modifier"
  | "no-key"
  | "invalid-key"
  | "invalid-modifier"
  | "reserved";

export type ParsedAccelerator =
  | { ok: true; accelerator: string }
  | { ok: false; reason: AcceleratorRefusal };

/** Lower-case spelling → the form we save; the order is the display order. */
const MODIFIERS: ReadonlyArray<readonly [string[], string]> = [
  [["commandorcontrol", "cmdorctrl"], "CommandOrControl"],
  [["command", "cmd"], "Command"],
  [["control", "ctrl"], "Control"],
  [["super", "meta"], "Super"],
  [["alt"], "Alt"],
  [["option"], "Option"],
  [["altgr"], "AltGr"],
  [["shift"], "Shift"],
];
const MODIFIER_FORM = new Map<string, string>(
  MODIFIERS.flatMap(([spellings, form]) => spellings.map((s) => [s, form] as const)),
);
const MODIFIER_RANK = new Map<string, number>(MODIFIERS.map(([, form], index) => [form, index]));

const NAMED_KEYS: Readonly<Record<string, string>> = {
  space: "Space",
  tab: "Tab",
  enter: "Enter",
  return: "Return",
  backspace: "Backspace",
  delete: "Delete",
  insert: "Insert",
  home: "Home",
  end: "End",
  pageup: "PageUp",
  pagedown: "PageDown",
  up: "Up",
  down: "Down",
  left: "Left",
  right: "Right",
  escape: "Escape",
  esc: "Escape",
  plus: "Plus",
  printscreen: "PrintScreen",
  volumeup: "VolumeUp",
  volumedown: "VolumeDown",
  volumemute: "VolumeMute",
  medianexttrack: "MediaNextTrack",
  mediaprevioustrack: "MediaPreviousTrack",
  mediastop: "MediaStop",
  mediaplaypause: "MediaPlayPause",
};

const PUNCTUATION = new Set([..."`~!@#$%^&*()-_=[]{}\\|;:'\",.<>/?"]);

function keyForm(token: string): string | null {
  if (token === "+") return "Plus";
  const lower = token.toLowerCase();
  const named = NAMED_KEYS[lower];
  if (named !== undefined) return named;
  if (/^[a-z0-9]$/.test(lower)) return lower.toUpperCase();
  const fn = /^f(\d{1,2})$/.exec(lower);
  if (fn !== null && Number(fn[1]) >= 1 && Number(fn[1]) <= 24) return `F${Number(fn[1])}`;
  const pad = /^num(\d|lock|dec|add|sub|mult|div)$/.exec(lower);
  if (pad !== null) return `num${pad[1]}`;
  if (token.length === 1 && PUNCTUATION.has(token)) return token;
  return null;
}

/** Split on `+`, keeping a trailing `+` as the key (`Ctrl+Shift++` → the plus key). */
function tokens(text: string): string[] {
  const parts = text.split("+").map((part) => part.trim());
  const out: string[] = [];
  for (let i = 0; i < parts.length; i += 1) {
    const part = parts[i] ?? "";
    if (part === "" && i === parts.length - 1 && i > 0 && parts[i - 1] === "") {
      out.push("+");
      continue;
    }
    if (part !== "") out.push(part);
  }
  return out;
}

export function parseAccelerator(raw: unknown): ParsedAccelerator {
  if (typeof raw !== "string" || raw.trim() === "") return { ok: false, reason: "empty" };
  const parts = tokens(raw.trim());
  if (parts.length === 0) return { ok: false, reason: "empty" };
  const modifiers = new Set<string>();
  let key: string | null = null;
  for (const part of parts) {
    const modifier = MODIFIER_FORM.get(part.toLowerCase());
    if (modifier !== undefined) {
      if (key !== null) return { ok: false, reason: "invalid-modifier" };
      modifiers.add(modifier);
      continue;
    }
    if (key !== null) return { ok: false, reason: "invalid-key" };
    const form = keyForm(part);
    if (form === null) return { ok: false, reason: "invalid-key" };
    key = form;
  }
  if (key === null) return { ok: false, reason: "no-key" };
  if (modifiers.size === 0) return { ok: false, reason: "no-modifier" };
  if (key === "Escape") return { ok: false, reason: "reserved" };
  const ordered = [...modifiers].sort(
    (a, b) => (MODIFIER_RANK.get(a) ?? 0) - (MODIFIER_RANK.get(b) ?? 0),
  );
  return { ok: true, accelerator: [...ordered, key].join("+") };
}

// -- saved settings ------------------------------------------------------------------------------

export interface ShortcutSetting {
  enabled: boolean;
  accelerator: string;
}

export type ShortcutSettings = Record<ShortcutAction, ShortcutSetting>;

export function defaultShortcutSettings(): ShortcutSettings {
  return {
    "hud-toggle": { enabled: true, accelerator: DEFAULT_ACCELERATORS["hud-toggle"] },
    "hud-snap": { enabled: true, accelerator: DEFAULT_ACCELERATORS["hud-snap"] },
    "quick-entry": { enabled: true, accelerator: DEFAULT_ACCELERATORS["quick-entry"] },
  };
}

/** Whatever was on disk → usable settings; a bad chord falls back to its default. */
export function sanitizeShortcutSettings(raw: unknown): ShortcutSettings {
  const settings = defaultShortcutSettings();
  const record = raw !== null && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  for (const action of SHORTCUT_ACTIONS) {
    const entry = record[action];
    if (entry === null || typeof entry !== "object") continue;
    const { enabled, accelerator } = entry as Record<string, unknown>;
    if (typeof enabled === "boolean") settings[action].enabled = enabled;
    const parsed = parseAccelerator(accelerator);
    if (parsed.ok) settings[action].accelerator = parsed.accelerator;
  }
  return settings;
}

export function readShortcutSettings(file: string): ShortcutSettings {
  try {
    return sanitizeShortcutSettings(JSON.parse(fs.readFileSync(file, "utf8")));
  } catch {
    return defaultShortcutSettings();
  }
}

export function writeShortcutSettings(file: string, settings: ShortcutSettings): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  writeFileAtomic(file, `${JSON.stringify(settings, null, 2)}\n`);
}

// -- the board -----------------------------------------------------------------------------------

/** The slice of Electron's `globalShortcut` the board uses. */
export interface GlobalShortcutLike {
  register(accelerator: string, callback: () => void): boolean;
  unregister(accelerator: string): void;
  isRegistered(accelerator: string): boolean;
}

export type ShortcutError = "invalid" | "taken" | "conflict" | "failed";

export interface ShortcutState {
  action: ShortcutAction;
  accelerator: string;
  enabled: boolean;
  registered: boolean;
  error: ShortcutError | null;
  conflictWith?: ShortcutAction;
}

export interface ShortcutBoardDeps {
  globalShortcut: GlobalShortcutLike;
  handlers: Readonly<Record<ShortcutAction, () => void>>;
  log(line: string): void;
}

/**
 * Owns every global chord the app holds. `apply(settings, active)` reconciles the OS with the
 * settings: `active` says which actions want their chord right now (the snap chord only while a
 * HUD is open). Nothing here throws.
 */
export class ShortcutBoard {
  private readonly deps: ShortcutBoardDeps;
  private readonly held = new Map<ShortcutAction, string>();
  private states: ShortcutState[] = [];

  constructor(deps: ShortcutBoardDeps) {
    this.deps = deps;
  }

  current(): ShortcutState[] {
    return this.states.map((state) => ({ ...state }));
  }

  apply(
    settings: ShortcutSettings,
    active: (action: ShortcutAction) => boolean = () => true,
  ): ShortcutState[] {
    // Release first, so moving a chord from one action to another works in one apply.
    for (const action of SHORTCUT_ACTIONS) this.release(action);
    const claimed = new Map<string, ShortcutAction>();
    const next: ShortcutState[] = [];
    for (const action of SHORTCUT_ACTIONS) {
      const setting = settings[action];
      const parsed = parseAccelerator(setting.accelerator);
      const accelerator = parsed.ok ? parsed.accelerator : setting.accelerator;
      const state: ShortcutState = {
        action,
        accelerator,
        enabled: setting.enabled,
        registered: false,
        error: null,
      };
      next.push(state);
      if (!setting.enabled) continue;
      if (!parsed.ok) {
        state.error = "invalid";
        continue;
      }
      const owner = claimed.get(accelerator.toLowerCase());
      if (owner !== undefined) {
        state.error = "conflict";
        state.conflictWith = owner;
        this.deps.log(`[shortcuts] ${action} uses the same chord as ${owner}; not registered`);
        continue;
      }
      claimed.set(accelerator.toLowerCase(), action);
      if (!active(action)) continue;
      const outcome = this.register(action, accelerator);
      state.registered = outcome === null;
      state.error = outcome;
    }
    this.states = next;
    return this.current();
  }

  /** Release everything (quit). */
  dispose(): void {
    for (const action of SHORTCUT_ACTIONS) this.release(action);
    this.states = this.states.map((state) => ({ ...state, registered: false }));
  }

  private register(action: ShortcutAction, accelerator: string): ShortcutError | null {
    const { globalShortcut, handlers, log } = this.deps;
    try {
      if (globalShortcut.isRegistered(accelerator)) {
        log(`[shortcuts] ${accelerator} for ${action} is already taken`);
        return "taken";
      }
      if (!globalShortcut.register(accelerator, handlers[action])) {
        log(`[shortcuts] ${accelerator} for ${action} is held by another application`);
        return "taken";
      }
    } catch (error) {
      log(
        `[shortcuts] registering ${accelerator} for ${action} failed: ${error instanceof Error ? error.message : String(error)}`,
      );
      return "failed";
    }
    this.held.set(action, accelerator);
    return null;
  }

  private release(action: ShortcutAction): void {
    const accelerator = this.held.get(action);
    if (accelerator === undefined) return;
    this.held.delete(action);
    try {
      this.deps.globalShortcut.unregister(accelerator);
    } catch {
      // A chord that cannot be released must not block the next registration.
    }
  }
}
