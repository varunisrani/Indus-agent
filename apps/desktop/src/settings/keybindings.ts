// Key bindings (P20-32 Keyboard): the actions a person can rebind, their defaults, accelerator
// checks and conflict detection. Overrides live in the desktop preferences (`keybindings`), an
// empty string unbinds an action. Global shortcuts (quick entry, HUD) are separate preferences
// but take part in conflict detection, so one key never does two things.

import type { MessageKey } from "../i18n/index.tsx";

export interface KeyAction {
  readonly id: string;
  readonly label: MessageKey;
  readonly defaultKey: string;
  /** Registered with the OS (works when the app is in the background). */
  readonly global?: boolean;
}

export const KEY_ACTIONS: readonly KeyAction[] = [
  { id: "chat.new", label: "settings.key.chatNew", defaultKey: "CommandOrControl+N" },
  { id: "settings.open", label: "settings.key.settings", defaultKey: "CommandOrControl+," },
  { id: "sidebar.toggle", label: "settings.key.sidebar", defaultKey: "CommandOrControl+B" },
  { id: "composer.focus", label: "settings.key.focusComposer", defaultKey: "CommandOrControl+L" },
  { id: "tab.close", label: "settings.key.closeTab", defaultKey: "CommandOrControl+W" },
  { id: "tab.reopen", label: "settings.key.reopenTab", defaultKey: "CommandOrControl+Shift+T" },
  { id: "tab.next", label: "settings.key.nextTab", defaultKey: "Control+Tab" },
  { id: "find", label: "settings.key.find", defaultKey: "CommandOrControl+F" },
];

/** The global shortcuts held as their own preferences (P20-35). */
export const GLOBAL_ACTIONS = [
  { id: "quickEntryShortcut", label: "settings.key.quickEntry" },
  { id: "hudSnapShortcut", label: "settings.key.hudSnap" },
  { id: "hudToggleShortcut", label: "settings.key.hudToggle" },
] as const satisfies ReadonlyArray<{ id: string; label: MessageKey }>;

const MODIFIERS = new Map<string, string>([
  ["commandorcontrol", "CommandOrControl"],
  ["cmdorctrl", "CommandOrControl"],
  ["command", "Command"],
  ["cmd", "Command"],
  ["control", "Control"],
  ["ctrl", "Control"],
  ["alt", "Alt"],
  ["option", "Alt"],
  ["shift", "Shift"],
  ["super", "Super"],
  ["meta", "Super"],
]);
const MODIFIER_ORDER = ["CommandOrControl", "Command", "Control", "Alt", "Super", "Shift"];
const NAMED_KEYS = new Set([
  "Space",
  "Tab",
  "Enter",
  "Escape",
  "Backspace",
  "Delete",
  "Up",
  "Down",
  "Left",
  "Right",
  "Home",
  "End",
  "PageUp",
  "PageDown",
  "Plus",
]);

/**
 * `accelerator` in one canonical spelling (modifier order fixed, key upper-cased), or null when it
 * is not one: at least one modifier, exactly one key (a letter, a digit, F1–F24, punctuation or a
 * named key).
 */
export function normalizeAccelerator(accelerator: string): string | null {
  const parts = accelerator
    .split("+")
    .map((p) => p.trim())
    .filter((p) => p !== "");
  if (accelerator.trim().endsWith("++")) parts.push("Plus");
  const modifiers = new Set<string>();
  let key: string | null = null;
  for (const part of parts) {
    const modifier = MODIFIERS.get(part.toLowerCase());
    if (modifier !== undefined) {
      modifiers.add(modifier);
      continue;
    }
    if (key !== null) return null;
    if (/^[a-z0-9]$/i.test(part)) key = part.toUpperCase();
    else if (/^f([1-9]|1[0-9]|2[0-4])$/i.test(part)) key = part.toUpperCase();
    else if (/^[,./;'[\]\\`=-]$/.test(part)) key = part;
    else {
      const named = [...NAMED_KEYS].find((n) => n.toLowerCase() === part.toLowerCase());
      if (named === undefined) return null;
      key = named;
    }
  }
  if (key === null || modifiers.size === 0) return null;
  const ordered = MODIFIER_ORDER.filter((m) => modifiers.has(m));
  return [...ordered, key].join("+");
}

/** The accelerator a keydown makes ("" when only modifiers are down). */
export function acceleratorFromEvent(event: {
  key: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
}): string {
  if (["Control", "Meta", "Alt", "Shift"].includes(event.key)) return "";
  const parts: string[] = [];
  if (event.ctrlKey || event.metaKey) parts.push("CommandOrControl");
  if (event.altKey) parts.push("Alt");
  if (event.shiftKey) parts.push("Shift");
  const named: Record<string, string> = {
    " ": "Space",
    ArrowUp: "Up",
    ArrowDown: "Down",
    ArrowLeft: "Left",
    ArrowRight: "Right",
    "+": "Plus",
  };
  parts.push(named[event.key] ?? (event.key.length === 1 ? event.key.toUpperCase() : event.key));
  return normalizeAccelerator(parts.join("+")) ?? "";
}

/** Each action's effective accelerator ("" = unbound). */
export function effectiveBindings(
  overrides: Readonly<Record<string, string>>,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const action of KEY_ACTIONS) {
    const override = overrides[action.id];
    out[action.id] = override === undefined ? action.defaultKey : override;
  }
  return out;
}

/**
 * The actions (in-app ids or global preference names) already using `accelerator`, other than
 * `self`. `CommandOrControl` and `Control`/`Command` count as the same key on the matching
 * platform, so the comparison folds them together.
 */
export function conflictsFor(
  accelerator: string,
  self: string,
  bindings: Readonly<Record<string, string>>,
): string[] {
  const wanted = normalizeAccelerator(accelerator);
  if (wanted === null) return [];
  const fold = (value: string) => value.replace(/\b(Command|Control)\b/g, "CommandOrControl");
  const target = fold(wanted);
  return Object.entries(bindings)
    .filter(([id, value]) => id !== self && value !== "")
    .filter(([, value]) => {
      const normal = normalizeAccelerator(value);
      return normal !== null && fold(normal) === target;
    })
    .map(([id]) => id);
}
