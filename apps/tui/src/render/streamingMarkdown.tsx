/**
 * Live rendering of a reply that is still arriving.
 *
 * The text up to the last stable boundary — an empty line outside any open code fence — is cut
 * into settled blocks and laid out as markdown (each block once, then from the markdown cache).
 * Whatever follows the boundary may still change shape (a fence or table that is half there), so
 * it is shown as plain wrapped text until a boundary passes it. Very long replies keep only their
 * tail: the last 16,000 characters or 240 lines, with a note saying how much is hidden.
 */
import { useRef } from "react";
import type { Theme } from "../theme/theme.ts";
import { useTheme } from "../theme/theme.ts";
import { layoutMarkdown, type MarkdownOptions } from "./markdown.tsx";
import { hasAnsi, Lines, type StyledLine, wrapAnsiText, wrapSpans } from "./styled.tsx";
import { colour } from "./tones.ts";

export const LIVE_MAX_CHARS = 16_000;
export const LIVE_MAX_LINES = 240;

export interface LiveTail {
  text: string;
  omittedChars: number;
  omittedLines: number;
}

/** Keeps the end of `text` within the live caps, starting at a line boundary when it can. */
export function liveTail(
  text: string,
  maxChars = LIVE_MAX_CHARS,
  maxLines = LIVE_MAX_LINES,
): LiveTail {
  let start = 0;
  let newlines = 0;
  for (let at = text.length - 1; at >= 0; at--) {
    if (text.charCodeAt(at) === 10 && at < text.length - 1) {
      newlines++;
      if (newlines >= maxLines) {
        start = at + 1;
        break;
      }
    }
  }
  if (text.length - start > maxChars) {
    start = text.length - maxChars;
    const nextBreak = text.indexOf("\n", start);
    if (nextBreak >= 0 && nextBreak < text.length - 1) start = nextBreak + 1;
  }
  if (start === 0) return { text, omittedChars: 0, omittedLines: 0 };
  let omittedLines = 0;
  for (let at = 0; at < start; at++) if (text.charCodeAt(at) === 10) omittedLines++;
  return { text: text.slice(start), omittedChars: start, omittedLines };
}

export interface ScanState {
  /** The prefix already scanned (only whole lines). */
  scanned: string;
  /** The open fence's marker character and length, or null outside a fence. */
  fence: { char: string; size: number } | null;
  /** Settled block sources, in order. */
  blocks: string[];
  /** Length of the settled prefix. */
  settled: number;
}

export function newScanState(): ScanState {
  return { scanned: "", fence: null, blocks: [], settled: 0 };
}

function trackFence(state: ScanState, line: string): void {
  const marker = /^\s{0,3}(`{3,}|~{3,})(.*)$/.exec(line);
  if (!marker) return;
  const run = marker[1] as string;
  if (!state.fence) {
    state.fence = { char: run[0] as string, size: run.length };
  } else if (
    run[0] === state.fence.char &&
    run.length >= state.fence.size &&
    !(marker[2] ?? "").trim()
  ) {
    state.fence = null;
  }
}

/**
 * Scans whole lines of `text` not seen yet and settles a block at each empty line outside a
 * fence. A partial last line is never scanned (it may still turn into a fence). If `text` no
 * longer starts with what was scanned, the state starts over.
 */
export function advanceScan(state: ScanState, text: string): ScanState {
  let current = state;
  if (!text.startsWith(current.scanned)) current = newScanState();
  let at = current.scanned.length;
  const begin = at;
  while (at < text.length) {
    const end = text.indexOf("\n", at);
    if (end < 0) break;
    const line = text.slice(at, end);
    if (!line.trim()) {
      if (!current.fence && end > 0) {
        const block = text.slice(current.settled, end + 1);
        if (block.trim()) {
          current.blocks.push(block);
          current.settled = end + 1;
        }
      }
    } else {
      trackFence(current, line);
    }
    at = end + 1;
  }
  if (at > begin) current.scanned = text.slice(0, at);
  return current;
}

/** Index just past the last stable boundary, or -1 when nothing has settled yet. */
export function findStableBoundary(text: string): number {
  const state = advanceScan(newScanState(), text);
  return state.settled > 0 ? state.settled : -1;
}

/**
 * The unsettled tail. While it could still change shape — a fence is open, or it holds table
 * rows — it is plain text. Prose is laid out as markdown so emphasis markers do not flash raw.
 */
function layoutTail(
  rest: string,
  state: ScanState,
  cols: number,
  theme: Theme,
  opts: MarkdownOptions,
): StyledLine[] {
  const unstable =
    state.fence !== null || /^\s*\|/m.test(rest) || /^\s{0,3}(?:`{3,}|~{3,})/m.test(rest);
  if (!unstable) return layoutMarkdown(rest, cols, theme, opts);
  return wrapSpans([{ ...colour(theme, "text"), text: rest.replace(/\n+$/, "") }], cols);
}

/** Lays out a live reply: notice line (if trimmed), settled blocks as markdown, tail as text. */
export function layoutStream(
  text: string,
  width: number,
  theme: Theme,
  opts: MarkdownOptions = {},
  state: ScanState = newScanState(),
): { lines: StyledLine[]; state: ScanState } {
  const cols = Math.max(1, Math.floor(width));
  const tail = liveTail(text);
  const lines: StyledLine[] = [];
  if (tail.omittedChars > 0) {
    const note = `… ${tail.omittedLines.toLocaleString("en-US")} earlier lines (${tail.omittedChars.toLocaleString("en-US")} chars) not shown while streaming`;
    lines.push(...wrapSpans([{ ...colour(theme, "muted"), dim: true, text: note }], cols));
  }
  if (hasAnsi(tail.text)) {
    lines.push(...wrapAnsiText(tail.text, cols));
    return { lines, state };
  }
  const next = advanceScan(state, tail.text);
  for (const block of next.blocks) {
    const laid = layoutMarkdown(block, cols, theme, opts);
    if (laid.length === 0) continue;
    if (lines.length > 0) lines.push([]);
    lines.push(...laid);
  }
  const rest = tail.text.slice(next.settled).replace(/^\n+/, "");
  if (rest.trim()) {
    if (lines.length > 0 && next.blocks.length > 0) lines.push([]);
    lines.push(...layoutTail(rest, next, cols, theme, opts));
  }
  return { lines, state: next };
}

export interface StreamingMarkdownProps extends MarkdownOptions {
  text: string;
  width: number;
}

export function StreamingMarkdown({ text, width, hyperlinks = false }: StreamingMarkdownProps) {
  const theme = useTheme();
  const scan = useRef<ScanState>(newScanState());
  const { lines, state } = layoutStream(text, width, theme, { hyperlinks }, scan.current);
  scan.current = state;
  return <Lines lines={lines} hyperlinks={hyperlinks} />;
}
