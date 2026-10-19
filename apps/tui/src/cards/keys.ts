// The slice of Ink's key flags the cards read, plus small helpers shared by their reducers.

import type { Key } from "ink";

export type KeyFlags = Partial<
  Pick<
    Key,
    | "upArrow"
    | "downArrow"
    | "leftArrow"
    | "rightArrow"
    | "pageUp"
    | "pageDown"
    | "return"
    | "escape"
    | "ctrl"
    | "shift"
    | "meta"
    | "tab"
    | "backspace"
    | "delete"
  >
>;

/** Ctrl plus a letter; Ink reports the letter as the input. */
export const isCtrl = (input: string, key: KeyFlags, letter: string): boolean =>
  key.ctrl === true && input.toLowerCase() === letter;

/** The 1-based number a single digit key stands for, else null. */
export function digitOf(input: string, key: KeyFlags): number | null {
  if (key.ctrl || key.meta || input.length !== 1) return null;
  const code = input.charCodeAt(0);
  return code >= 49 && code <= 57 ? code - 48 : null;
}

/** Text the key would type (letters, digits, pasted chunks), or "" for control keys. */
export function typedText(input: string, key: KeyFlags): string {
  if (key.ctrl || key.meta || key.escape || key.return || key.tab) return "";
  if (key.upArrow || key.downArrow || key.leftArrow || key.rightArrow) return "";
  if (key.backspace || key.delete || key.pageUp || key.pageDown) return "";
  // biome-ignore lint/suspicious/noControlCharactersInRegex: drop control bytes from typed text
  return input.replace(/[\u0000-\u001f\u007f]/g, "");
}

export const isErase = (key: KeyFlags): boolean => key.backspace === true || key.delete === true;
