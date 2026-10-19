// Terminal mode sequences and the full reset written on every way out (R05.7). The reset is safe
// to send twice and goes out with a synchronous write so it lands before the process is gone.

import { writeSync } from "node:fs";

const CSI = "\u001b[";
const dec = (mode: number, on: boolean): string => `${CSI}?${mode}${on ? "h" : "l"}`;

export const seq = {
  altScreenOn: dec(1049, true),
  altScreenOff: dec(1049, false),
  syncBegin: dec(2026, true),
  syncEnd: dec(2026, false),
  pasteOn: dec(2004, true),
  pasteOff: dec(2004, false),
  focusOn: dec(1004, true),
  focusOff: dec(1004, false),
  cursorShow: dec(25, true),
  cursorHide: dec(25, false),
  /** Kitty keyboard is a stack: every push needs exactly one pop. */
  kittyPush: (flags = 1): string => `${CSI}>${flags}u`,
  kittyPop: `${CSI}<u`,
  otherKeysOn: `${CSI}>4;2m`,
  otherKeysOff: `${CSI}>4m`,
  sgrReset: `${CSI}0m`,
  /** DEC locator reporting off, and its event selection cleared. */
  locatorOff: `${CSI}0'z${CSI}0'{`,
} as const;

/** Every mouse reporting mode a terminal might have on, newest encodings first. */
export const MOUSE_MODES: readonly number[] = [
  2029, 1016, 1015, 1006, 1005, 1003, 1002, 1001, 1000, 9,
];

/**
 * Everything we (or a killed earlier run) may have switched on, switched off: locator, all mouse
 * modes, focus events, bracketed paste, alternate screen, kitty keyboard, modifyOtherKeys, SGR,
 * cursor visibility.
 */
export const MODE_RESET: string = [
  seq.locatorOff,
  ...MOUSE_MODES.map((mode) => dec(mode, false)),
  seq.focusOff,
  seq.pasteOff,
  seq.altScreenOff,
  seq.kittyPop,
  seq.otherKeysOff,
  seq.sgrReset,
  seq.cursorShow,
].join("");

export interface ResetTarget {
  isTTY?: boolean;
  fd?: number;
  write?: (text: string) => unknown;
}

/**
 * Write the reset synchronously (fd write, falling back to the stream). Nothing is written when
 * the target is not a terminal. Never throws: the terminal may already be gone.
 */
export function writeModeReset(target: ResetTarget = process.stdout): boolean {
  if (target.isTTY !== true) return false;
  const fd = typeof target.fd === "number" ? target.fd : target === process.stdout ? 1 : undefined;
  if (fd !== undefined) {
    try {
      writeSync(fd, MODE_RESET);
      return true;
    } catch {
      // fall through to the stream (mocked or unusual streams)
    }
  }
  try {
    target.write?.(MODE_RESET);
    return target.write !== undefined;
  } catch {
    return false;
  }
}
