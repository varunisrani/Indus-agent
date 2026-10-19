// Our filter in front of Ink's stdin: modifyOtherKeys reports become the CSI-u form Ink decodes,
// SGR mouse reports leave the key stream as mouse events, and bracketed paste bodies pass
// through untouched. A sequence cut across reads waits for the rest; a lone ESC goes on after
// a short pause (it is the Escape key).

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

const ESC = "\u001b";
const PASTE_START = `${ESC}[200~`;
const PASTE_END = `${ESC}[201~`;
// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal input is escape sequences
const OTHER_KEYS_RE = /\u001b\[27;(\d+);(\d+)~/g;
// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal input is escape sequences
const SGR_MOUSE_RE = /\u001b\[<(\d+);(\d+);(\d+)([Mm])/g;
/** A tail that could be the start of a sequence we rewrite or a paste bracket. */
// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal input is escape sequences
const OPEN_TAIL_RE = /\u001b(?:\[(?:<[\d;]*|27(?:;[\d;]*)?|2?|20[01]?)?)?$/;
export const LONE_ESC_MS = 15;

export function decodeMouse(code: number, col: number, row: number, final: string): MouseEvent {
  const mods = { shift: (code & 4) !== 0, alt: (code & 8) !== 0, ctrl: (code & 16) !== 0 };
  const button = code & 3;
  if ((code & 64) !== 0)
    return { kind: button === 0 ? "wheel-up" : "wheel-down", button, col, row, ...mods };
  if ((code & 32) !== 0) return { kind: "drag", button, col, row, ...mods };
  return { kind: final === "m" ? "release" : "press", button, col, row, ...mods };
}

/** Rewrite one complete piece of input outside a paste. */
export function rewriteInput(text: string, onMouse: (event: MouseEvent) => void): string {
  return text
    .replace(SGR_MOUSE_RE, (_all, code: string, col: string, row: string, final: string) => {
      onMouse(decodeMouse(Number(code), Number(col), Number(row), final));
      return "";
    })
    .replace(OTHER_KEYS_RE, (_all, mods: string, code: string) => `${ESC}[${code};${mods}u`);
}

type Source = NodeJS.ReadableStream & { setRawMode?: (on: boolean) => unknown };

export class InputFilter {
  readonly stream: PassThrough & { isTTY: boolean; setRawMode: (on: boolean) => unknown };
  private held = "";
  private pasting = false;
  private escTimer: ReturnType<typeof setTimeout> | undefined;
  private readonly onMouse: (event: MouseEvent) => void;

  constructor(source: Source, onMouse: (event: MouseEvent) => void) {
    this.onMouse = onMouse;
    const out = new PassThrough();
    this.stream = Object.assign(out, {
      isTTY: true,
      setRawMode: (on: boolean) => source.setRawMode?.(on),
      ref: () => (source as { ref?: () => void }).ref?.(),
      unref: () => (source as { unref?: () => void }).unref?.(),
    });
    source.on("data", (chunk: Buffer | string) => this.push(String(chunk)));
  }

  /** Feed one read from the terminal. */
  push(chunk: string): void {
    if (this.escTimer !== undefined) clearTimeout(this.escTimer);
    let rest = this.held + chunk;
    this.held = "";
    let out = "";
    while (rest.length > 0) {
      if (this.pasting) {
        const end = rest.indexOf(PASTE_END);
        if (end < 0) {
          out += rest;
          rest = "";
          break;
        }
        out += rest.slice(0, end + PASTE_END.length);
        rest = rest.slice(end + PASTE_END.length);
        this.pasting = false;
        continue;
      }
      const start = rest.indexOf(PASTE_START);
      const plain = start < 0 ? rest : rest.slice(0, start);
      const tail = start < 0 ? OPEN_TAIL_RE.exec(plain) : null;
      if (tail !== null) this.held = tail[0];
      out += rewriteInput(tail === null ? plain : plain.slice(0, tail.index), this.onMouse);
      if (start < 0) break;
      out += PASTE_START;
      rest = rest.slice(start + PASTE_START.length);
      this.pasting = true;
    }
    if (out !== "") this.stream.write(out);
    if (this.held !== "") {
      this.escTimer = setTimeout(() => {
        const held = this.held;
        this.held = "";
        if (held !== "") this.stream.write(held);
      }, LONE_ESC_MS);
    }
  }
}
