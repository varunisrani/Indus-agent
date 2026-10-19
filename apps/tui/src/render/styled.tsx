/**
 * Styled spans and lines: the renderer lays text out itself (wrapping, padding, widths) so every
 * block has an exact height before Ink sees it. Ink only prints ready lines, one `<Text>` each.
 */
import { Box, Text } from "ink";
import type { ReactNode } from "react";

export interface SpanStyle {
  color?: string;
  bg?: string;
  bold?: boolean;
  dim?: boolean;
  italic?: boolean;
  underline?: boolean;
  strike?: boolean;
  inverse?: boolean;
  /** Target for an OSC 8 hyperlink; only written when the caller enabled hyperlinks. */
  link?: string;
}

export interface Span extends SpanStyle {
  text: string;
  /** The text carries its own escape sequences and is printed untouched. */
  raw?: boolean;
}

export type StyledLine = Span[];

const ESC = String.fromCharCode(27);
const BEL = String.fromCharCode(7);
// CSI sequences, OSC strings (ended by BEL or ESC \), and two-byte escapes.
const ESCAPE_SOURCE = `${ESC}\\[[0-9;?<>=!]*[ -/]*[@-~]|${ESC}\\][^${BEL}${ESC}]*(?:${BEL}|${ESC}\\\\)|${ESC}[@-Z\\\\-_]`;
const ESCAPES = new RegExp(ESCAPE_SOURCE, "g");
const HAS_ESCAPE = new RegExp(`${ESC}[[\\]@-Z\\\\-_]`);
const SGR = new RegExp(`^${ESC}\\[[0-9;]*m$`);

export function hasAnsi(text: string): boolean {
  return HAS_ESCAPE.test(text);
}

export function stripAnsi(text: string): string {
  return text.includes(ESC) ? text.replace(ESCAPES, "") : text;
}

const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

export function graphemes(text: string): string[] {
  const out: string[] = [];
  for (const piece of segmenter.segment(text)) out.push(piece.segment);
  return out;
}

const ZERO_WIDTH = /^[\p{Mn}\p{Me}\p{Cf}]+$/u;
const EMOJI_WIDE = /\p{Emoji_Presentation}|\p{Extended_Pictographic}️/u;
const ASCII_ONLY = /^[\x20-\x7e]*$/;

function isWideCodePoint(cp: number): boolean {
  return (
    (cp >= 0x1100 && cp <= 0x115f) ||
    (cp >= 0x2e80 && cp <= 0x303e) ||
    (cp >= 0x3041 && cp <= 0x33ff) ||
    (cp >= 0x3400 && cp <= 0x4dbf) ||
    (cp >= 0x4e00 && cp <= 0x9fff) ||
    (cp >= 0xa000 && cp <= 0xa4cf) ||
    (cp >= 0xac00 && cp <= 0xd7a3) ||
    (cp >= 0xf900 && cp <= 0xfaff) ||
    (cp >= 0xfe30 && cp <= 0xfe4f) ||
    (cp >= 0xff00 && cp <= 0xff60) ||
    (cp >= 0xffe0 && cp <= 0xffe6) ||
    (cp >= 0x1f300 && cp <= 0x1f64f) ||
    (cp >= 0x1f900 && cp <= 0x1f9ff) ||
    (cp >= 0x20000 && cp <= 0x3fffd)
  );
}

/** Terminal cells one grapheme cluster takes. */
export function clusterWidth(cluster: string): number {
  if (cluster.length === 1) {
    const code = cluster.charCodeAt(0);
    if (code >= 0x20 && code < 0x7f) return 1;
    if (code < 0x20 || code === 0x7f) return 0;
  }
  if (ZERO_WIDTH.test(cluster)) return 0;
  if (EMOJI_WIDE.test(cluster)) return 2;
  const first = cluster.codePointAt(0) ?? 0;
  return isWideCodePoint(first) ? 2 : 1;
}

/** Display width in terminal cells; escape sequences count as zero. */
export function cellWidth(text: string): number {
  const plain = stripAnsi(text);
  if (ASCII_ONLY.test(plain)) return plain.length;
  let width = 0;
  for (const cluster of graphemes(plain)) width += clusterWidth(cluster);
  return width;
}

export function lineText(line: StyledLine): string {
  let out = "";
  for (const span of line) out += span.raw ? stripAnsi(span.text) : span.text;
  return out;
}

export function lineWidth(line: StyledLine): number {
  let width = 0;
  for (const span of line) width += cellWidth(span.text);
  return width;
}

function sameStyle(a: SpanStyle, b: SpanStyle): boolean {
  return (
    a.color === b.color &&
    a.bg === b.bg &&
    a.bold === b.bold &&
    a.dim === b.dim &&
    a.italic === b.italic &&
    a.underline === b.underline &&
    a.strike === b.strike &&
    a.inverse === b.inverse &&
    a.link === b.link
  );
}

function styleOf(span: Span): SpanStyle {
  const { text: _text, raw: _raw, ...style } = span;
  return style;
}

/** Joins neighbouring spans of the same style and drops empty ones. */
export function mergeSpans(spans: readonly Span[]): StyledLine {
  const out: StyledLine = [];
  for (const span of spans) {
    if (!span.text) continue;
    const last = out.at(-1);
    if (last && !last.raw && !span.raw && sameStyle(last, span)) {
      out[out.length - 1] = { ...last, text: last.text + span.text };
    } else {
      out.push({ ...span });
    }
  }
  return out;
}

/** Pads a line with spaces up to `width` cells (never cuts). */
export function padLine(line: StyledLine, width: number, style: SpanStyle = {}): StyledLine {
  const gap = width - lineWidth(line);
  return gap > 0 ? [...line, { ...style, text: " ".repeat(gap) }] : line;
}

/** Cuts a line to at most `width` cells, adding `…` when something was dropped. */
export function truncateLine(line: StyledLine, width: number): StyledLine {
  if (lineWidth(line) <= width) return line;
  if (width <= 0) return [];
  const out: StyledLine = [];
  let used = 0;
  const room = width - 1;
  for (const span of line) {
    const text = span.raw ? stripAnsi(span.text) : span.text;
    let kept = "";
    for (const cluster of graphemes(text)) {
      const w = clusterWidth(cluster);
      if (used + w > room) break;
      kept += cluster;
      used += w;
    }
    if (kept) out.push({ ...styleOf(span), text: kept });
    if (used >= room || kept.length < text.length) break;
  }
  out.push({ ...(line.at(-1) ? styleOf(line.at(-1) as Span) : {}), text: "…" });
  return mergeSpans(out);
}

interface Token {
  text: string;
  width: number;
  space: boolean;
  style: SpanStyle;
}

function tokenize(spans: readonly Span[]): Token[][] {
  const paragraphs: Token[][] = [[]];
  for (const span of spans) {
    const style = styleOf(span);
    const text = span.raw ? stripAnsi(span.text) : span.text;
    const parts = text.split("\n");
    parts.forEach((part, index) => {
      if (index > 0) paragraphs.push([]);
      const current = paragraphs[paragraphs.length - 1] as Token[];
      for (const piece of part.match(/\s+|\S+/g) ?? []) {
        const space = /^\s/.test(piece);
        const clean = space ? piece.replace(/\t/g, "  ") : piece;
        current.push({ text: clean, width: cellWidth(clean), space, style });
      }
    });
  }
  return paragraphs;
}

export interface WrapOptions {
  /** Spans put in front of the first line (bullets, gutters); their width is reserved. */
  first?: StyledLine;
  /** Spans put in front of every following line; defaults to blanks as wide as `first`. */
  rest?: StyledLine;
  /** `word` breaks at spaces (long words are cut); `char` fills every line (code). */
  mode?: "word" | "char";
}

/**
 * Wraps spans to `width` cells. Newlines inside span text start a new line. Trailing spaces are
 * dropped at a soft break; leading spaces of the very first line are kept (indentation).
 */
export function wrapSpans(
  spans: readonly Span[],
  width: number,
  opts: WrapOptions = {},
): StyledLine[] {
  const first = opts.first ?? [];
  const firstWidth = lineWidth(first);
  const rest = opts.rest ?? (firstWidth > 0 ? [{ text: " ".repeat(firstWidth) }] : []);
  const restWidth = lineWidth(rest);
  const mode = opts.mode ?? "word";
  const lines: StyledLine[] = [];
  let line: Span[] = [...first];
  let contentStart = line.length;
  let room = Math.max(1, width - firstWidth);
  let used = 0;
  let softStart = false; // the current line began at a soft break

  const push = (text: string, style: SpanStyle, w: number) => {
    line.push({ ...style, text });
    used += w;
  };
  const breakLine = (soft: boolean) => {
    while (line.length > contentStart && /^\s+$/.test(line.at(-1)?.text ?? "x")) line.pop();
    lines.push(mergeSpans(line));
    line = [...rest];
    contentStart = line.length;
    room = Math.max(1, width - restWidth);
    used = 0;
    softStart = soft;
  };
  const hardSplit = (token: Token) => {
    for (const cluster of graphemes(token.text)) {
      const w = clusterWidth(cluster);
      if (used + w > room && used > 0) breakLine(true);
      push(cluster, token.style, w);
    }
  };

  tokenize(spans).forEach((paragraph, index) => {
    if (index > 0) breakLine(false);
    for (const token of paragraph) {
      if (mode === "char") {
        if (used + token.width <= room) push(token.text, token.style, token.width);
        else hardSplit(token);
        continue;
      }
      if (token.space) {
        if (used === 0 && softStart) continue; // no leading blanks after a soft break
        if (used + token.width > room) {
          breakLine(true);
          continue;
        }
        push(token.text, token.style, token.width);
        continue;
      }
      if (used + token.width <= room) {
        push(token.text, token.style, token.width);
      } else if (token.width <= room) {
        breakLine(true);
        push(token.text, token.style, token.width);
      } else {
        if (used > 0) breakLine(true);
        hardSplit(token);
      }
    }
  });
  lines.push(mergeSpans(line));
  return lines;
}

const ANSI_PIECE = new RegExp(`(${ESCAPE_SOURCE})`);

/**
 * Hard-wraps text that already carries escape sequences. Every output line starts with the colour
 * state that was open where it begins and ends with a reset, so lines can be printed one by one.
 */
export function wrapAnsiText(text: string, width: number): StyledLine[] {
  const out: StyledLine[] = [];
  const room = Math.max(1, width);
  let carried: string[] = [];
  for (const source of text.replace(/\r\n?/g, "\n").split("\n")) {
    let current = carried.join("");
    let used = 0;
    let open = carried.length > 0;
    const flush = () => {
      out.push([{ text: open ? `${current}\x1b[0m` : current, raw: true }]);
      current = carried.join("");
      used = 0;
    };
    for (const piece of source.split(ANSI_PIECE)) {
      if (!piece) continue;
      if (piece.startsWith("\x1b")) {
        current += piece;
        if (SGR.test(piece)) {
          if (piece === "\x1b[m" || piece === "\x1b[0m") carried = [];
          else carried.push(piece);
          open = carried.length > 0;
        }
        continue;
      }
      for (const cluster of graphemes(piece.replace(/\t/g, "  "))) {
        const w = clusterWidth(cluster);
        if (used + w > room && used > 0) flush();
        current += cluster;
        used += w;
      }
    }
    flush();
  }
  return out;
}

export interface LinesProps {
  lines: readonly StyledLine[];
  /** Write OSC 8 links for spans that carry one. */
  hyperlinks?: boolean;
}

function osc8(url: string, text: string): string {
  return `\x1b]8;;${url}\x07${text}\x1b]8;;\x07`;
}

function renderSpan(span: Span, key: number, hyperlinks: boolean): ReactNode {
  if (span.raw) return span.text;
  const text = hyperlinks && span.link ? osc8(span.link, span.text) : span.text;
  const plain =
    !span.color &&
    !span.bg &&
    !span.bold &&
    !span.dim &&
    !span.italic &&
    !span.underline &&
    !span.strike &&
    !span.inverse;
  if (plain) return text;
  return (
    <Text
      key={key}
      {...(span.color ? { color: span.color } : {})}
      {...(span.bg ? { backgroundColor: span.bg } : {})}
      bold={span.bold === true}
      dimColor={span.dim === true}
      italic={span.italic === true}
      underline={span.underline === true}
      strikethrough={span.strike === true}
      inverse={span.inverse === true}
    >
      {text}
    </Text>
  );
}

/** One Ink row per styled line; lines are already wrapped, so Ink must not wrap them again. */
export function StyledLineView({
  line,
  hyperlinks = false,
}: {
  line: StyledLine;
  hyperlinks?: boolean;
}) {
  if (line.length === 0) return <Text> </Text>;
  return <Text wrap="truncate-end">{line.map((span, i) => renderSpan(span, i, hyperlinks))}</Text>;
}

export function Lines({ lines, hyperlinks = false }: LinesProps) {
  return (
    <Box flexDirection="column">
      {lines.map((line, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: lines are positional by nature
        <StyledLineView key={i} line={line} hyperlinks={hyperlinks} />
      ))}
    </Box>
  );
}

/** Whitespace folded to single spaces and cut to `max` characters with a trailing `…`. */
export function oneLine(text: string, max: number): string {
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  return `${flat.slice(0, Math.max(0, max - 1)).trimEnd()}…`;
}
