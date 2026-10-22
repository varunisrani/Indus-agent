// Our filter in front of Ink's stdin: modifyOtherKeys reports become the CSI-u form Ink decodes,
// SGR mouse reports leave the key stream as mouse events, operating-system-command replies (a
// colour answer to our OSC 11 probe that arrives after the probe gave up — the dashboard's
// browser terminal answers over a WebSocket) are dropped, since no key ever types one, and
// bracketed paste bodies pass through untouched. A sequence cut across reads waits for the rest;
// a lone ESC goes on after a short pause (it is the Escape key).
//
// Colour-scheme reports (W47): a background answer (OSC 11, however late) and the terminal's
// light/dark report (`CSI ? 997 ; 1 n` dark, `; 2 n` light — the answer to `CSI ? 996 n`, and sent
// unasked on a change once `CSI ? 2031 h` is on) are taken out of the key stream and handed to
// `onScheme`, so the theme can follow a terminal whose colours change while we run (the
// dashboard's Chat tab switching between its light and dark themes).

import { PassThrough } from "node:stream";
import { LIGHT_LUMINANCE, parseOscColour, trustedProbeColour } from "../theme/detect.ts";
import { luminance } from "../theme/palette.ts";

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
/** A complete OSC reply: ESC ] … ended by BEL or ST (ESC \\). */
// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal input is escape sequences
const OSC_REPLY_RE = /\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)/g;
/** An OSC 11 (background) reply, captured: its payload. */
// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal input is escape sequences
const OSC_BACKGROUND_RE = /^\u001b\]11;([^\u0007\u001b]*)(?:\u0007|\u001b\\)$/;
/** The terminal's colour-scheme report: `CSI ? 997 ; 1 n` (dark) or `; 2 n` (light). */
// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal input is escape sequences
const SCHEME_REPORT_RE = /\u001b\[\?997;([12])n/g;
/** A tail that could be the start of a sequence we rewrite or a paste bracket. */
const OPEN_TAIL_RE =
  // biome-ignore lint/suspicious/noControlCharactersInRegex: terminal input is escape sequences
  /\u001b(?:\[(?:<[\d;]*|\?[\d;]*|27(?:;[\d;]*)?|2?|20[01]?)?|\][^\u0007\u001b]{0,512}\u001b?)?$/;

/** What a colour-scheme report says: light or dark, and the background when one was named. */
export interface SchemeReport {
  light: boolean;
  background: string | null;
}
export const LONE_ESC_MS = 15;

export function decodeMouse(code: number, col: number, row: number, final: string): MouseEvent {
  const mods = { shift: (code & 4) !== 0, alt: (code & 8) !== 0, ctrl: (code & 16) !== 0 };
  const button = code & 3;
  if ((code & 64) !== 0)
    return { kind: button === 0 ? "wheel-up" : "wheel-down", button, col, row, ...mods };
  if ((code & 32) !== 0) return { kind: "drag", button, col, row, ...mods };
  return { kind: final === "m" ? "release" : "press", button, col, row, ...mods };
}

/** The scheme an OSC reply reports, or null (another OSC, or a colour we distrust). */
export function schemeFromOsc(reply: string): SchemeReport | null {
  const match = OSC_BACKGROUND_RE.exec(reply);
  if (match === null) return null;
  const background = trustedProbeColour(parseOscColour(match[1] ?? ""));
  if (background === null) return null;
  return { light: luminance(background) >= LIGHT_LUMINANCE, background };
}

/** Rewrite one complete piece of input outside a paste. */
export function rewriteInput(
  text: string,
  onMouse: (event: MouseEvent) => void,
  onScheme?: (report: SchemeReport) => void,
): string {
  return text
    .replace(OSC_REPLY_RE, (reply) => {
      const report = schemeFromOsc(reply);
      if (report !== null) onScheme?.(report);
      return "";
    })
    .replace(SCHEME_REPORT_RE, (_all, mode: string) => {
      onScheme?.({ light: mode === "2", background: null });
      return "";
    })
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
  private readonly onScheme: ((report: SchemeReport) => void) | undefined;

  constructor(
    source: Source,
    onMouse: (event: MouseEvent) => void,
    onScheme?: (report: SchemeReport) => void,
  ) {
    this.onMouse = onMouse;
    this.onScheme = onScheme;
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
      out += rewriteInput(
        tail === null ? plain : plain.slice(0, tail.index),
        this.onMouse,
        this.onScheme,
      );
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
