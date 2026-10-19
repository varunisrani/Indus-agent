// Spike (P12-01, throwaway): our filter in front of Ink's stdin.
// Ink decodes kitty CSI-u keys itself but not xterm modifyOtherKeys, and it has no mouse support.
// The filter rewrites `ESC [ 27 ; mods ; code ~` into the CSI-u form Ink understands and takes
// SGR mouse reports out of the stream so they never reach key handlers. Bracketed paste bodies
// pass through untouched.

import { PassThrough } from "node:stream";

export interface MouseEvent {
  kind: "press" | "release" | "drag" | "wheel-up" | "wheel-down";
  button: number;
  col: number;
  row: number;
  shift: boolean;
  alt: boolean;
  ctrl: boolean;
}

export interface FilterStats {
  otherKeysRewritten: number;
  mouseReports: number;
  chunksIn: number;
}

const ESC = "\u001b";
const PASTE_OPEN = `${ESC}[200~`;
const PASTE_CLOSE = `${ESC}[201~`;
// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal input is escape sequences
const OTHER_KEYS = /\u001b\[27;(\d+);(\d+)~/g;
// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal input is escape sequences
const SGR_MOUSE = /\u001b\[<(\d+);(\d+);(\d+)([Mm])/g;
// A tail that might be the start of a sequence we rewrite; held until the next chunk.
// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal input is escape sequences
const PARTIAL_TAIL = /\u001b(?:\[(?:<[\d;]*|27(?:;[\d;]*)?|2?|20[01]?)?)?$/;

export function decodeMouse(code: number, col: number, row: number, final: string): MouseEvent {
  const mods = { shift: (code & 4) !== 0, alt: (code & 8) !== 0, ctrl: (code & 16) !== 0 };
  const button = code & 3;
  if (code & 64)
    return { kind: button === 0 ? "wheel-up" : "wheel-down", button, col, row, ...mods };
  if (code & 32) return { kind: "drag", button, col, row, ...mods };
  return { kind: final === "m" ? "release" : "press", button, col, row, ...mods };
}

/** Pure part: rewrite one complete text piece outside paste brackets. */
export function rewriteKeys(
  text: string,
  onMouse: (event: MouseEvent) => void,
  stats: FilterStats,
) {
  return text
    .replace(SGR_MOUSE, (_all, code: string, col: string, row: string, final: string) => {
      stats.mouseReports += 1;
      onMouse(decodeMouse(Number(code), Number(col), Number(row), final));
      return "";
    })
    .replace(OTHER_KEYS, (_all, mods: string, code: string) => {
      stats.otherKeysRewritten += 1;
      return `${ESC}[${code};${mods}u`;
    });
}

export class InputFilter {
  readonly stream: PassThrough & { isTTY: boolean; setRawMode: (on: boolean) => unknown };
  readonly stats: FilterStats = { otherKeysRewritten: 0, mouseReports: 0, chunksIn: 0 };
  private carry = "";
  private inPaste = false;
  private flushTimer: ReturnType<typeof setTimeout> | undefined;
  private readonly onMouse: (event: MouseEvent) => void;

  constructor(
    source: NodeJS.ReadableStream & { setRawMode?: (on: boolean) => unknown },
    onMouse: (event: MouseEvent) => void,
  ) {
    this.onMouse = onMouse;
    const out = new PassThrough();
    this.stream = Object.assign(out, {
      isTTY: true,
      setRawMode: (on: boolean) => source.setRawMode?.(on),
      ref: () => (source as { ref?: () => void }).ref?.(),
      unref: () => (source as { unref?: () => void }).unref?.(),
    });
    source.on("data", (chunk: Buffer | string) => this.feed(String(chunk)));
  }

  feed(chunk: string): void {
    this.stats.chunksIn += 1;
    if (this.flushTimer !== undefined) clearTimeout(this.flushTimer);
    let text = this.carry + chunk;
    this.carry = "";
    let output = "";
    while (text.length > 0) {
      if (this.inPaste) {
        const end = text.indexOf(PASTE_CLOSE);
        if (end < 0) {
          output += text;
          text = "";
          break;
        }
        output += text.slice(0, end + PASTE_CLOSE.length);
        text = text.slice(end + PASTE_CLOSE.length);
        this.inPaste = false;
        continue;
      }
      const start = text.indexOf(PASTE_OPEN);
      const plain = start < 0 ? text : text.slice(0, start);
      const tail = start < 0 ? PARTIAL_TAIL.exec(plain) : null;
      const complete = tail ? plain.slice(0, tail.index) : plain;
      if (tail) this.carry = tail[0];
      output += rewriteKeys(complete, this.onMouse, this.stats);
      if (start < 0) break;
      output += PASTE_OPEN;
      text = text.slice(start + PASTE_OPEN.length);
      this.inPaste = true;
    }
    if (output) this.stream.write(output);
    if (this.carry) {
      // A lone ESC (the Escape key) must still reach Ink if nothing follows.
      this.flushTimer = setTimeout(() => {
        const held = this.carry;
        this.carry = "";
        if (held) this.stream.write(held);
      }, 15);
    }
  }
}
