// Our key events from raw terminal bytes. Understands the kitty CSI-u form (`ESC [ code ; mods u`),
// xterm modifyOtherKeys (`ESC [ 27 ; mods ; code ~`) and the classic single-byte and ESC-prefixed
// forms, so Shift+Enter, Alt+Enter, Ctrl+W and Ctrl+Backspace are told apart where the terminal can.

export type KeyName =
  | "enter"
  | "backspace"
  | "tab"
  | "escape"
  | "space"
  | "delete"
  | "up"
  | "down"
  | "left"
  | "right"
  | "home"
  | "end"
  | "pageup"
  | "pagedown"
  | "char";

export interface KeyEvent {
  name: KeyName;
  /** The character for `char` keys (lower case for letters when Ctrl is held). */
  char?: string;
  shift: boolean;
  alt: boolean;
  ctrl: boolean;
  super: boolean;
}

const NAMED_CODES: Readonly<Record<number, KeyName>> = {
  13: "enter",
  9: "tab",
  27: "escape",
  127: "backspace",
  8: "backspace",
  32: "space",
};

function key(name: KeyName, mods: Partial<KeyEvent> = {}, char?: string): KeyEvent {
  return {
    name,
    shift: mods.shift ?? false,
    alt: mods.alt ?? false,
    ctrl: mods.ctrl ?? false,
    super: mods.super ?? false,
    ...(char === undefined ? {} : { char }),
  };
}

/** The modifier parameter is 1 + a bit set: 1 shift, 2 alt, 4 ctrl, 8 super. */
export function decodeModifiers(param: number): Pick<KeyEvent, "shift" | "alt" | "ctrl" | "super"> {
  const bits = Math.max(0, param - 1);
  return {
    shift: (bits & 1) !== 0,
    alt: (bits & 2) !== 0,
    ctrl: (bits & 4) !== 0,
    super: (bits & 8) !== 0,
  };
}

function fromCode(code: number, modParam: number): KeyEvent {
  const mods = decodeModifiers(modParam);
  const named = NAMED_CODES[code];
  if (named !== undefined) return key(named, mods);
  return key("char", mods, String.fromCodePoint(code));
}

// biome-ignore lint/suspicious/noControlCharactersInRegex: key input is escape sequences
const CSI_U = /^\u001b\[(\d+)(?::\d+)*(?:;(\d+)(?::\d+)?)?u$/;
// biome-ignore lint/suspicious/noControlCharactersInRegex: key input is escape sequences
const OTHER_KEYS = /^\u001b\[27;(\d+);(\d+)~$/;
// biome-ignore lint/suspicious/noControlCharactersInRegex: key input is escape sequences
const CURSOR = /^\u001b\[(?:1;(\d+))?([ABCDHF])$/;
// biome-ignore lint/suspicious/noControlCharactersInRegex: key input is escape sequences
const TILDE = /^\u001b\[(\d+)(?:;(\d+))?~$/;

const CURSOR_NAMES: Readonly<Record<string, KeyName>> = {
  A: "up",
  B: "down",
  C: "right",
  D: "left",
  H: "home",
  F: "end",
};
const TILDE_NAMES: Readonly<Record<number, KeyName>> = {
  1: "home",
  3: "delete",
  4: "end",
  5: "pageup",
  6: "pagedown",
  7: "home",
  8: "end",
};

/** Decode one key sequence; null when it is not one we know. */
export function decodeKey(sequence: string): KeyEvent | null {
  if (sequence === "") return null;
  let match = CSI_U.exec(sequence);
  if (match !== null) return fromCode(Number(match[1]), Number(match[2] ?? "1"));
  match = OTHER_KEYS.exec(sequence);
  if (match !== null) return fromCode(Number(match[2]), Number(match[1]));
  match = CURSOR.exec(sequence);
  if (match !== null) {
    const name = CURSOR_NAMES[match[2] ?? ""];
    return name === undefined ? null : key(name, decodeModifiers(Number(match[1] ?? "1")));
  }
  match = TILDE.exec(sequence);
  if (match !== null && match[1] !== "27") {
    const name = TILDE_NAMES[Number(match[1])];
    return name === undefined ? null : key(name, decodeModifiers(Number(match[2] ?? "1")));
  }
  // ESC + one key: Alt held (Alt+Enter arrives as ESC CR in the classic encoding).
  if (sequence.length === 2 && sequence[0] === "\u001b") {
    const inner = decodeKey(sequence.slice(1));
    return inner === null ? null : { ...inner, alt: true };
  }
  if (sequence.length !== 1 && [...sequence].length !== 1) return null;
  const code = sequence.codePointAt(0) ?? 0;
  if (code === 13 || code === 10) return key("enter");
  if (code === 9) return key("tab");
  if (code === 27) return key("escape");
  if (code === 127) return key("backspace");
  // Many terminals send ^H for Ctrl+Backspace once plain Backspace is DEL.
  if (code === 8) return key("backspace", { ctrl: true });
  if (code === 32) return key("space");
  if (code >= 1 && code <= 26) return key("char", { ctrl: true }, String.fromCharCode(code + 96));
  return key("char", {}, sequence);
}

/** A short stable label (`ctrl+w`, `shift+enter`) for key tables and tests. */
export function keyLabel(event: KeyEvent): string {
  const parts: string[] = [];
  if (event.ctrl) parts.push("ctrl");
  if (event.alt) parts.push("alt");
  if (event.shift) parts.push("shift");
  if (event.super) parts.push("super");
  parts.push(event.name === "char" ? (event.char ?? "").toLowerCase() : event.name);
  return parts.join("+");
}
