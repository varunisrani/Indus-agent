// Output clean-up while a resumed chat replays its history (P18-26, R09.8).
//
// When the TUI reopens a long session it redraws the whole transcript, and on the way it emits
// floods of blank lines and erase sequences (`ESC [ n K`, `ESC [ n X`) that leave xterm's viewport
// blank. For a bounded window after a resume (explicit `resume=`, or the server's announced
// implicit one) this filter folds every flood — a run of line breaks and erase sequences holding
// 50 or more line breaks — into a single blank line, erase codes and all. Erase codes outside a
// flood always pass: they are how the renderer repaints in place (Ink clears each line it redraws
// with `ESC [ 2 K`), so dropping them would leave stale text on screen for the whole window
// (seen in the P18-30 browser run: a closed approval card stayed drawn). Outside the window the
// stream passes through untouched.
//
// The PTY runs in cooked mode, so line breaks arrive as CR LF; frames are cut wherever a read
// ended, so an escape sequence, a CR/LF pair or a long run can span frames. The filter therefore
// holds back the end of a frame that might continue: a partial escape (`ESC`, `ESC [`,
// `ESC [ digits`), the run of line breaks (and erase codes between them) just before it, or a
// trailing run (a lone trailing CR included). Erase codes inside a run count as part of it, so
// erase codes sprinkled through a flood do not break it into short runs that slip past.

import { SANITIZE_WINDOW_MS } from "./reconnect.ts";

/** Line breaks in a row that count as a flood. */
export const FLOOD_BREAKS = 50;

// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal streams are escape sequences
const RUN = /(?:\r\n|\n|\r(?!\n)|\x1b\[\d*[KX])+/g;
/** A trailing run that may continue: it starts with a line break (or a lone CR). */
// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal streams are escape sequences
const TRAILING_RUN = /(?:(?:\r\n|\n|\r(?!\n))(?:\x1b\[\d*[KX])*)+$/;
// biome-ignore lint/suspicious/noControlCharactersInRegex: terminal streams are escape sequences
const PARTIAL_ESCAPE = /\x1b(?:\[\d*)?$/;
/** What a flood becomes: end the line, then one blank line. */
const FOLDED = "\r\n\r\n";

function lineBreaks(run: string): number {
  let count = 0;
  for (const char of run) if (char === "\n") count += 1;
  return count;
}

/** Fold each flood (a run of breaks and erase codes with 50+ breaks); leave everything else. */
export function scrub(text: string): string {
  return text.replace(RUN, (run) => (lineBreaks(run) >= FLOOD_BREAKS ? FOLDED : run));
}

/** Index where a trailing run of line breaks (erase codes between them) starts in `text[0, end)`. */
function breaksStart(text: string, end: number): number {
  const found = TRAILING_RUN.exec(text.slice(0, end));
  return found ? found.index : end;
}

/** Split `text` into what can be filtered now and a tail that may continue in the next frame. */
export function splitTail(text: string): [ready: string, tail: string] {
  const partial = PARTIAL_ESCAPE.exec(text);
  const end = partial ? partial.index : text.length;
  const cut = breaksStart(text, end);
  return [text.slice(0, cut), text.slice(cut)];
}

export class ResumeSanitizer {
  #until = Number.NEGATIVE_INFINITY;
  #held = "";
  readonly #now: () => number;
  readonly #windowMs: number;

  constructor(now: () => number = Date.now, windowMs: number = SANITIZE_WINDOW_MS) {
    this.#now = now;
    this.#windowMs = windowMs;
  }

  /** Open (or extend) the window: a resume replay starts now. */
  begin(): void {
    this.#until = Math.max(this.#until, this.#now() + this.#windowMs);
  }

  /** True while output is being filtered. */
  get active(): boolean {
    return this.#now() < this.#until;
  }

  /** How long the window still runs (0 when closed). */
  get remainingMs(): number {
    return Math.max(0, this.#until - this.#now());
  }

  /** Filter one decoded frame; returns what to write now. */
  feed(chunk: string): string {
    if (!this.active) {
      // Window over: whatever was still held goes first (a held escape completes with `chunk`).
      const released = this.settle();
      const out = released + this.#held + chunk;
      this.#held = "";
      return out;
    }
    const combined = this.#held + chunk;
    const [ready, tail] = splitTail(combined);
    this.#held = tail;
    return scrub(ready);
  }

  /**
   * The window ended: release held line breaks (folded if they already make a flood). A held
   * partial escape stays held and goes out in front of the next frame, where it completes.
   */
  settle(): string {
    const partial = PARTIAL_ESCAPE.exec(this.#held);
    const keep = partial ? this.#held.slice(partial.index) : "";
    const release = partial ? this.#held.slice(0, partial.index) : this.#held;
    this.#held = keep;
    return scrub(release);
  }

  /**
   * The stream closed: release held line breaks, drop a held partial escape (written into the
   * next stream it would swallow that stream's first bytes) and close the window.
   */
  end(): string {
    const out = this.settle();
    this.#held = "";
    this.#until = Number.NEGATIVE_INFINITY;
    return out;
  }
}
