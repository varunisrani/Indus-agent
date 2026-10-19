// Newline-delimited JSON framing: one frame per line, whatever the read boundaries.

import { MAX_FRAME_LINE_BYTES, PROTOCOL_ERROR_PREVIEW_CHARS } from "./limits.ts";

export interface SplitterOptions {
  /** Longest line kept; a longer one is reported through `onOversize` and skipped. */
  maxLineBytes?: number;
  onOversize?: (bytes: number, preview: string) => void;
}

/**
 * Turns a byte (or text) stream into complete lines. Handles a frame cut across reads, a
 * multi-byte character cut across reads, CRLF endings and very long lines without re-scanning.
 */
export class LineSplitter {
  private readonly onLine: (line: string) => void;
  private readonly maxLineBytes: number;
  private readonly onOversize: ((bytes: number, preview: string) => void) | undefined;
  private readonly decoder = new TextDecoder();
  private parts: string[] = [];
  private partLength = 0;
  private skipping = false;

  constructor(onLine: (line: string) => void, options: SplitterOptions = {}) {
    this.onLine = onLine;
    this.maxLineBytes = options.maxLineBytes ?? MAX_FRAME_LINE_BYTES;
    this.onOversize = options.onOversize;
  }

  push(chunk: Uint8Array | string): void {
    const text = typeof chunk === "string" ? chunk : this.decoder.decode(chunk, { stream: true });
    let from = 0;
    for (;;) {
      const newline = text.indexOf("\n", from);
      if (newline < 0) {
        this.hold(text.slice(from));
        return;
      }
      this.hold(text.slice(from, newline));
      this.emit();
      from = newline + 1;
    }
  }

  /** End of stream: a last line without a newline still counts. */
  end(): void {
    const rest = this.decoder.decode();
    if (rest) this.hold(rest);
    if (this.partLength > 0 || this.skipping) this.emit();
  }

  private hold(piece: string): void {
    if (piece === "" || this.skipping) return;
    this.parts.push(piece);
    // UTF-16 length is a cheap lower bound; exact bytes are counted only near the limit.
    this.partLength += piece.length;
    if (this.partLength * 3 > this.maxLineBytes) {
      const bytes = new TextEncoder().encode(this.parts.join("")).length;
      if (bytes > this.maxLineBytes) {
        this.onOversize?.(bytes, preview(this.parts[0] ?? ""));
        this.parts = [];
        this.partLength = 0;
        this.skipping = true;
      }
    }
  }

  private emit(): void {
    if (this.skipping) {
      this.skipping = false;
      return;
    }
    let line = this.parts.length === 1 ? (this.parts[0] as string) : this.parts.join("");
    this.parts = [];
    this.partLength = 0;
    if (line.endsWith("\r")) line = line.slice(0, -1);
    if (line.trim() === "") return;
    this.onLine(line);
  }
}

/** The start of a bad line for an error event, single-line and at most 120 characters. */
export function preview(line: string): string {
  const flat = line.replace(/[\r\n\t]+/g, " ").trim();
  return flat.length <= PROTOCOL_ERROR_PREVIEW_CHARS
    ? flat
    : `${flat.slice(0, PROTOCOL_ERROR_PREVIEW_CHARS - 1)}…`;
}

export type ParsedLine =
  | { ok: true; frame: Record<string, unknown> }
  | { ok: false; preview: string };

/** One line → one frame object, or the preview of a line that is not a JSON object. */
export function parseFrameLine(line: string): ParsedLine {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch {
    return { ok: false, preview: preview(line) };
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return { ok: false, preview: preview(line) };
  }
  return { ok: true, frame: value as Record<string, unknown> };
}

/** One frame → one line (with its newline). */
export function encodeFrame(frame: object): string {
  return `${JSON.stringify(frame)}\n`;
}
