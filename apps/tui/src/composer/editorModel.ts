// The composer's text model (P12-16): one string, a cursor that rests only on grapheme
// boundaries, an optional selection, and bounded undo/redo. No React, no terminal: the Ink view
// (Editor.tsx) and the key router feed it presses and read it back.

import { actionMod, isMac, type KeyPress } from "../input/keymap.ts";

export const UNDO_LIMIT = 200;
export const MULTI_CLICK_MS = 500;
export const DOUBLE_ESC_MS = 500;

const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

/** Every offset where the cursor may rest: 0, each grapheme start, and the end. */
export function boundaries(text: string): number[] {
  const stops: number[] = [];
  for (const piece of segmenter.segment(text)) stops.push(piece.index);
  if (stops[0] !== 0) stops.unshift(0);
  stops.push(text.length);
  return stops;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

/** The nearest boundary at or before `pos`. */
export function snap(text: string, pos: number): number {
  const target = clamp(pos, 0, text.length);
  let best = 0;
  for (const stop of boundaries(text)) {
    if (stop > target) break;
    best = stop;
  }
  return best;
}

export function stepLeft(text: string, pos: number): number {
  const here = snap(text, pos);
  let previous = 0;
  for (const stop of boundaries(text)) {
    if (stop >= here) break;
    previous = stop;
  }
  return previous;
}

export function stepRight(text: string, pos: number): number {
  const here = snap(text, pos);
  for (const stop of boundaries(text)) if (stop > here) return stop;
  return text.length;
}

const isSpace = (ch: string | undefined): boolean => ch !== undefined && /\s/.test(ch);

/** Start of the word left of `pos`: skip spaces, then the word itself. */
export function wordLeft(text: string, pos: number): number {
  let at = snap(text, pos);
  while (at > 0 && isSpace(text[at - 1])) at -= 1;
  while (at > 0 && !isSpace(text[at - 1])) at -= 1;
  return at;
}

/** End of the word right of `pos` plus the spaces after it (readline forward-word). */
export function wordRight(text: string, pos: number): number {
  let at = snap(text, pos);
  while (at < text.length && !isSpace(text[at])) at += 1;
  while (at < text.length && isSpace(text[at])) at += 1;
  return snap(text, at);
}

export function lineStart(text: string, pos: number): number {
  return text.lastIndexOf("\n", pos - 1) + 1;
}

export function lineEnd(text: string, pos: number): number {
  const next = text.indexOf("\n", pos);
  return next < 0 ? text.length : next;
}

/** Same column one line up, or null on the first line. */
export function lineAbove(text: string, pos: number): number | null {
  const here = snap(text, pos);
  const start = lineStart(text, here);
  if (start === 0) return null;
  const column = here - start;
  const aboveStart = lineStart(text, start - 1);
  return snap(text, Math.min(aboveStart + column, start - 1));
}

/** Same column one line down, or null on the last line. */
export function lineBelow(text: string, pos: number): number | null {
  const here = snap(text, pos);
  const end = lineEnd(text, here);
  if (end >= text.length) return null;
  const column = here - lineStart(text, here);
  const belowStart = end + 1;
  return snap(text, Math.min(belowStart + column, lineEnd(text, belowStart)));
}

/** The word (non-space run) around `pos`, or the space run when `pos` sits in spaces. */
export function wordAround(text: string, pos: number): { start: number; end: number } {
  const here = snap(text, pos);
  const probe = here < text.length ? text[here] : text[here - 1];
  const inSpace = isSpace(probe);
  let start = here;
  let end = here;
  while (start > 0 && isSpace(text[start - 1]) === inSpace && text[start - 1] !== "\n") start -= 1;
  while (end < text.length && isSpace(text[end]) === inSpace && text[end] !== "\n") end += 1;
  return { start: snap(text, start), end: snap(text, end) };
}

export interface Selection {
  readonly anchor: number;
  readonly head: number;
}

interface Snapshot {
  readonly text: string;
  readonly cursor: number;
}

/** Whether a second press at `now` counts as the double of one at `last`. */
export function isDouble(last: number, now: number, windowMs: number): boolean {
  return now - last <= windowMs;
}

export class EditorModel {
  text = "";
  cursor = 0;
  selection: Selection | null = null;
  /** Bumped on every change, text or cursor, so views can re-render cheaply. */
  version = 0;
  private undoStack: Snapshot[] = [];
  private redoStack: Snapshot[] = [];
  private click = { at: Number.NEGATIVE_INFINITY, offset: -1, count: 0 };
  private dragFrom: number | null = null;

  get undoDepth(): number {
    return this.undoStack.length;
  }

  get redoDepth(): number {
    return this.redoStack.length;
  }

  /** Replace everything from outside (history recall, clear): no undo record, stacks reset. */
  load(text: string, cursor: number = text.length): void {
    this.text = text;
    this.cursor = snap(text, cursor);
    this.selection = null;
    this.undoStack = [];
    this.redoStack = [];
    this.version += 1;
  }

  /** The selected span, ordered, or null when nothing (or nothing visible) is selected. */
  range(): { start: number; end: number } | null {
    if (this.selection === null || this.selection.anchor === this.selection.head) return null;
    const { anchor, head } = this.selection;
    return { start: Math.min(anchor, head), end: Math.max(anchor, head) };
  }

  selectedText(): string {
    const span = this.range();
    return span === null ? "" : this.text.slice(span.start, span.end);
  }

  /** A text change the user made: recorded for undo, redo forgotten. */
  edit(next: string, cursor: number): boolean {
    if (next === this.text) {
      this.moveTo(cursor);
      return false;
    }
    this.undoStack.push({ text: this.text, cursor: this.cursor });
    if (this.undoStack.length > UNDO_LIMIT) this.undoStack.shift();
    this.redoStack = [];
    this.text = next;
    this.cursor = snap(next, cursor);
    this.selection = null;
    this.version += 1;
    return true;
  }

  moveTo(pos: number, extend = false): void {
    const target = snap(this.text, pos);
    if (extend) {
      const anchor = this.selection?.anchor ?? this.cursor;
      this.selection = anchor === target ? null : { anchor, head: target };
    } else {
      this.selection = null;
    }
    this.cursor = target;
    this.version += 1;
  }

  insert(piece: string): boolean {
    const clean = piece.replace(/\r\n?/g, "\n");
    if (clean === "") return false;
    const span = this.range();
    const start = span?.start ?? this.cursor;
    const end = span?.end ?? this.cursor;
    return this.edit(
      this.text.slice(0, start) + clean + this.text.slice(end),
      start + clean.length,
    );
  }

  /** Removes the selection; false when there is none. */
  deleteSelection(): boolean {
    const span = this.range();
    if (span === null) return false;
    return this.edit(this.text.slice(0, span.start) + this.text.slice(span.end), span.start);
  }

  private cut(from: number, to: number, cursor: number): boolean {
    if (from === to) return false;
    return this.edit(this.text.slice(0, from) + this.text.slice(to), cursor);
  }

  backspace(): boolean {
    if (this.deleteSelection()) return true;
    const from = stepLeft(this.text, this.cursor);
    return this.cut(from, this.cursor, from);
  }

  deleteForward(): boolean {
    if (this.deleteSelection()) return true;
    return this.cut(this.cursor, stepRight(this.text, this.cursor), this.cursor);
  }

  deleteWordBack(): boolean {
    if (this.deleteSelection()) return true;
    const from = wordLeft(this.text, this.cursor);
    return this.cut(from, this.cursor, from);
  }

  deleteWordForward(): boolean {
    if (this.deleteSelection()) return true;
    return this.cut(this.cursor, wordRight(this.text, this.cursor), this.cursor);
  }

  /** Kill back to the line start; at a line start, take the newline before (repeat walks up). */
  killToLineStart(): boolean {
    if (this.deleteSelection()) return true;
    const start = lineStart(this.text, this.cursor);
    const from = start === this.cursor && start > 0 ? start - 1 : start;
    return this.cut(from, this.cursor, from);
  }

  /** Kill to the line end; at a line end, take the newline (joins the next line). */
  killToLineEnd(): boolean {
    if (this.deleteSelection()) return true;
    const end = lineEnd(this.text, this.cursor);
    const to = end === this.cursor && end < this.text.length ? end + 1 : end;
    return this.cut(this.cursor, to, this.cursor);
  }

  undo(): boolean {
    const back = this.undoStack.pop();
    if (back === undefined) return false;
    this.redoStack.push({ text: this.text, cursor: this.cursor });
    this.restore(back);
    return true;
  }

  redo(): boolean {
    const ahead = this.redoStack.pop();
    if (ahead === undefined) return false;
    this.undoStack.push({ text: this.text, cursor: this.cursor });
    this.restore(ahead);
    return true;
  }

  private restore(shot: Snapshot): void {
    this.text = shot.text;
    this.cursor = snap(shot.text, shot.cursor);
    this.selection = null;
    this.version += 1;
  }

  selectAll(): void {
    if (this.text === "") return;
    this.selection = { anchor: 0, head: this.text.length };
    this.cursor = this.text.length;
    this.version += 1;
  }

  /** Mouse press at `offset`: 1 click places, 2 select the word, 3 select the line. */
  press(offset: number, now: number): number {
    const at = snap(this.text, offset);
    const again = isDouble(this.click.at, now, MULTI_CLICK_MS) && this.click.offset === at;
    const count = again ? (this.click.count % 3) + 1 : 1;
    this.click = { at: now, offset: at, count };
    this.dragFrom = at;
    if (count === 1) {
      this.moveTo(at);
    } else if (count === 2) {
      const word = wordAround(this.text, at);
      this.selection = { anchor: word.start, head: word.end };
      this.cursor = word.end;
      this.version += 1;
    } else {
      const start = lineStart(this.text, at);
      const end = lineEnd(this.text, at);
      this.selection = { anchor: start, head: end };
      this.cursor = end;
      this.version += 1;
    }
    return count;
  }

  drag(offset: number): void {
    if (this.dragFrom === null) return;
    const at = snap(this.text, offset);
    this.selection = this.dragFrom === at ? null : { anchor: this.dragFrom, head: at };
    this.cursor = at;
    this.version += 1;
  }

  release(): void {
    this.dragFrom = null;
  }
}

/** What a key did to the model, so the caller knows whether to submit or walk history. */
export type KeyOutcome = "edited" | "moved" | "submit" | "edge-up" | "edge-down" | "ignored";

function changed(did: boolean): KeyOutcome {
  return did ? "edited" : "ignored";
}

/** The editor's own key table. The global router (input/routeKeys.ts) runs before this. */
export function applyKey(model: EditorModel, k: KeyPress, mac: boolean = isMac): KeyOutcome {
  const action = actionMod(k, mac);
  const wordMod = k.ctrl || k.meta || (mac && k.super);
  const letter = k.name === null ? k.input.toLowerCase() : "";

  if (k.name === "return") {
    if (k.shift || k.meta || k.ctrl || k.super) return changed(model.insert("\n"));
    return "submit";
  }
  if (k.name === null && !k.ctrl && !k.meta && k.input === "\n") return changed(model.insert("\n"));

  if (action && letter === "z" && !k.shift) return changed(model.undo());
  if (action && (letter === "y" || (letter === "z" && k.shift))) return changed(model.redo());

  const extend = k.shift;
  const go = (pos: number): KeyOutcome => {
    model.moveTo(pos, extend);
    return "moved";
  };
  if (mac && k.super && !k.ctrl && letter === "a") {
    model.selectAll();
    return "moved";
  }
  if (k.name === "home" || (action && letter === "a")) {
    return go(lineStart(model.text, model.cursor));
  }
  if (k.name === "end" || (action && letter === "e")) return go(lineEnd(model.text, model.cursor));
  if (k.name === "left" || k.name === "right") {
    const span = model.range();
    if (span !== null && !extend && !wordMod) return go(k.name === "left" ? span.start : span.end);
    if (k.name === "left")
      return go(wordMod ? wordLeft(model.text, model.cursor) : stepLeft(model.text, model.cursor));
    return go(wordMod ? wordRight(model.text, model.cursor) : stepRight(model.text, model.cursor));
  }
  if (k.meta && !k.ctrl && letter === "b") return go(wordLeft(model.text, model.cursor));
  if (k.meta && !k.ctrl && letter === "f") return go(wordRight(model.text, model.cursor));
  if (k.meta && !k.ctrl && letter === "d") return changed(model.deleteWordForward());

  if (k.name === "up" || k.name === "down") {
    const next =
      k.name === "up" ? lineAbove(model.text, model.cursor) : lineBelow(model.text, model.cursor);
    if (next === null) return k.name === "up" ? "edge-up" : "edge-down";
    return go(next);
  }

  if (k.name === "backspace") {
    if (k.super) return changed(model.killToLineStart());
    if (wordMod) return changed(model.deleteWordBack());
    return changed(model.backspace());
  }
  if (k.name === "delete") {
    if (k.super) return changed(model.killToLineEnd());
    if (wordMod) return changed(model.deleteWordForward());
    return changed(model.deleteForward());
  }
  if (action && letter === "w") return changed(model.deleteWordBack());
  if (action && letter === "u") return changed(model.killToLineStart());
  if (action && letter === "k") return changed(model.killToLineEnd());

  if (k.name !== null || k.ctrl || k.meta || k.super || k.input === "") return "ignored";

  // Plain text: one key, an IME commit or an unbracketed paste, always one insert / undo step.
  // Text that ends with a carriage return was committed together with Enter: keep it, then submit.
  const returnAt = k.input.indexOf("\r");
  if (returnAt >= 0 && k.input.slice(returnAt).replace(/[\r\n]/g, "") === "") {
    model.insert(k.input.slice(0, returnAt));
    return "submit";
  }
  return changed(model.insert(k.input));
}

/** Terminal cells a grapheme takes: wide for CJK, full-width forms and emoji pictographs. */
export function cellWidth(grapheme: string): number {
  if (grapheme === "" || /^\p{M}+$/u.test(grapheme)) return 0;
  if (/\p{Extended_Pictographic}/u.test(grapheme)) return 2;
  const code = grapheme.codePointAt(0) ?? 0;
  const wide =
    (code >= 0x1100 && code <= 0x115f) ||
    (code >= 0x2e80 && code <= 0xa4cf) ||
    (code >= 0xac00 && code <= 0xd7a3) ||
    (code >= 0xf900 && code <= 0xfaff) ||
    (code >= 0xfe30 && code <= 0xfe4f) ||
    (code >= 0xff00 && code <= 0xff60) ||
    (code >= 0xffe0 && code <= 0xffe6) ||
    (code >= 0x20000 && code <= 0x3fffd);
  return wide ? 2 : 1;
}

/**
 * Text offset under a screen cell (`row`, `col` relative to the text's first cell) when the text
 * wraps at `width` columns. Past a line's end the offset is that line's end.
 */
export function offsetAt(text: string, row: number, col: number, width: number): number {
  const wrap = Math.max(1, width);
  let screenRow = 0;
  let column = 0;
  for (const piece of segmenter.segment(text)) {
    if (piece.segment === "\n") {
      if (screenRow === row) return piece.index;
      screenRow += 1;
      column = 0;
      continue;
    }
    const cells = cellWidth(piece.segment);
    if (column + cells > wrap) {
      if (screenRow === row) return piece.index;
      screenRow += 1;
      column = 0;
    }
    if (screenRow === row && col < column + cells) return piece.index;
    column += cells;
  }
  return text.length;
}
