// Key normalisation and the master key table of the main view (P12-20).
// Ink hands us `(input, key)`; everything downstream works on one flat `KeyPress` so the router,
// the editor model and the tests share a single shape.

import type { Key } from "ink";

export type KeyName =
  | "up"
  | "down"
  | "left"
  | "right"
  | "pageup"
  | "pagedown"
  | "home"
  | "end"
  | "return"
  | "escape"
  | "tab"
  | "backspace"
  | "delete"
  | "wheelup"
  | "wheeldown";

export interface KeyPress {
  /** Printable text of the press ("" for named keys). Ctrl+letter carries the letter. */
  readonly input: string;
  readonly name: KeyName | null;
  readonly ctrl: boolean;
  readonly meta: boolean;
  readonly shift: boolean;
  /** Cmd on macOS / Win elsewhere; only reported through kitty keyboard reports. */
  readonly super: boolean;
}

const NAMED: ReadonlyArray<[keyof Key, KeyName]> = [
  ["upArrow", "up"],
  ["downArrow", "down"],
  ["leftArrow", "left"],
  ["rightArrow", "right"],
  ["pageUp", "pageup"],
  ["pageDown", "pagedown"],
  ["home", "home"],
  ["end", "end"],
  ["return", "return"],
  ["escape", "escape"],
  ["tab", "tab"],
  ["backspace", "backspace"],
  ["delete", "delete"],
];

/** Builds a `KeyPress` from Ink's `useInput` arguments. */
export function fromInk(input: string, key: Partial<Key>): KeyPress {
  let name: KeyName | null = null;
  for (const [flag, named] of NAMED) {
    if (key[flag] === true) {
      name = named;
      break;
    }
  }
  return {
    input: name === null ? input : "",
    name,
    ctrl: key.ctrl === true,
    meta: key.meta === true,
    shift: key.shift === true,
    super: key.super === true,
  };
}

/** Test and wiring helper: a press from a short spec such as `ctrl+c`, `shift+tab`, `x`. */
export function press(spec: string): KeyPress {
  const parts = spec.split("+");
  const last = parts.pop() ?? "";
  const mods = new Set(parts);
  const names: Record<string, KeyName> = {
    up: "up",
    down: "down",
    left: "left",
    right: "right",
    pageup: "pageup",
    pagedown: "pagedown",
    home: "home",
    end: "end",
    enter: "return",
    return: "return",
    esc: "escape",
    escape: "escape",
    tab: "tab",
    backspace: "backspace",
    delete: "delete",
    wheelup: "wheelup",
    wheeldown: "wheeldown",
  };
  const name = names[last] ?? null;
  return {
    input: name === null ? last : "",
    name,
    ctrl: mods.has("ctrl"),
    meta: mods.has("alt") || mods.has("meta"),
    shift: mods.has("shift"),
    super: mods.has("cmd") || mods.has("super"),
  };
}

/** A press of plain text (one or more characters), as typing or an unbracketed paste. */
export function textPress(text: string): KeyPress {
  return { input: text, name: null, ctrl: false, meta: false, shift: false, super: false };
}

export const isMac = process.platform === "darwin";

/**
 * The "action" modifier: Ctrl everywhere; on macOS also Cmd when the terminal forwards it as the
 * Super bit (kitty keys). Plain Option (meta) never counts, so Option+B stays word motion.
 */
export function actionMod(k: KeyPress, mac: boolean = isMac): boolean {
  return k.ctrl || (mac && k.super);
}

/** Action modifier plus one letter, case-insensitive. */
export function isAction(k: KeyPress, letter: string, mac: boolean = isMac): boolean {
  return actionMod(k, mac) && k.name === null && k.input.toLowerCase() === letter;
}

/** Ctrl plus one letter (never Cmd): Ctrl+C keeps its interrupt meaning on macOS. */
export function isCtrl(k: KeyPress, letter: string): boolean {
  return k.ctrl && k.name === null && k.input.toLowerCase() === letter;
}

/** Alt (meta) plus one letter, the fallback chord for editors that eat the Ctrl one. */
export function isAlt(k: KeyPress, letter: string): boolean {
  return k.meta && !k.ctrl && k.name === null && k.input.toLowerCase() === letter;
}

export function isScrollKey(k: KeyPress): boolean {
  if (k.name === "wheelup" || k.name === "wheeldown") return true;
  if (k.name === "pageup" || k.name === "pagedown") return true;
  return k.shift && (k.name === "up" || k.name === "down");
}

/** One row of the master table: what the key is and what it does, for help screens and tests. */
export interface KeyBinding {
  readonly keys: string;
  readonly does: string;
}

export const MAIN_KEYS: readonly KeyBinding[] = [
  { keys: "Enter", does: "send the prompt, or take the highlighted completion" },
  { keys: "Shift+Enter, Alt+Enter, \\ then Enter", does: "new line" },
  { keys: "Ctrl+C", does: "clear the draft, else stop the turn, else quit" },
  { keys: "Ctrl+D", does: "quit when the composer is empty" },
  { keys: "Esc Esc", does: "discard the draft, else stop the turn (not under a card)" },
  { keys: "Esc", does: "leave queue editing, clear the selection" },
  { keys: "Ctrl+L", does: "repaint the screen" },
  { keys: "Ctrl+O", does: "choose a model" },
  { keys: "Ctrl+X", does: "cut the selection, drop the edited queued prompt, or switch session" },
  { keys: "Shift+Tab", does: "toggle auto-approve for this session" },
  { keys: "Tab", does: "take the highlighted completion" },
  { keys: "Ctrl+G, Alt+G", does: "write the prompt in your editor" },
  { keys: "Ctrl+V, Alt+V", does: "paste from the clipboard" },
  { keys: "Ctrl+K", does: "send the next queued prompt now, else cut to the line end" },
  { keys: "Up, Down", does: "completions, then queued prompts, then history" },
  { keys: "Shift+Up, Shift+Down", does: "scroll one row" },
  { keys: "PgUp, PgDn", does: "scroll half a screen" },
  { keys: "Ctrl+Z, Ctrl+Y", does: "undo, redo" },
];
