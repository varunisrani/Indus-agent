/**
 * Markdown subset for assistant text, laid out into styled lines at a given width.
 *
 * Blocks: ATX and setext headings, bullet / numbered / task lists, block quotes, pipe tables,
 * fenced code (token colours, `diff` colouring, `md` fences rendered as markdown), footnote
 * definitions, horizontal rules and `MEDIA:` rows. Inline: code, bold, italic, strike, links,
 * autolinks, bare URLs, images (as a labelled link) and footnote references. Text that already
 * holds escape sequences is printed as it is.
 */
import { useMemo } from "react";
import type { Theme } from "../theme/theme.ts";
import { currentTheme, useTheme } from "../theme/theme.ts";
import { canHighlight, highlightLine } from "./highlight.ts";
import {
  cellWidth,
  hasAnsi,
  Lines,
  lineWidth,
  mergeSpans,
  padLine,
  type Span,
  type SpanStyle,
  type StyledLine,
  wrapAnsiText,
  wrapSpans,
} from "./styled.tsx";
import { colour, themeId } from "./tones.ts";

export interface MarkdownOptions {
  /** The terminal understands OSC 8; links then carry their target invisibly. */
  hyperlinks?: boolean;
}

// ---------------------------------------------------------------- inline

const URL_BODY = String.raw`[^\s()<>]+(?:\([^\s()<>]*\)[^\s()<>]*)*`;
const INLINE = new RegExp(
  [
    String.raw`!\[(?<imgAlt>[^\]\n]*)\]\((?<imgUrl>${URL_BODY})\)`,
    String.raw`\[(?<linkLabel>[^\]\n]+)\]\((?<linkUrl>${URL_BODY})\)`,
    String.raw`<(?<auto>(?:https?:\/\/|mailto:)[^\s>]+|[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,})>`,
    "`(?<code>[^`\\n]+)`",
    String.raw`\[\^(?<foot>[^\]\s]+)\]`,
    String.raw`~~(?<strike>\S(?:[^\n]*?\S)?)~~`,
    String.raw`\*\*(?<boldStar>\S(?:[^\n]*?\S)?)\*\*`,
    String.raw`(?<!\w)__(?<boldUnder>\S(?:[^\n]*?\S)?)__(?!\w)`,
    String.raw`(?<![*\w])\*(?<italicStar>[^*\s](?:[^*\n]*?[^*\s])?)\*(?![*\w])`,
    String.raw`(?<![\w_])_(?<italicUnder>[^_\s](?:[^_\n]*?[^_\s])?)_(?![\w_])`,
    String.raw`(?<url>https?:\/\/[^\s<>]+)`,
  ].join("|"),
  "g",
);

const IDENTIFIER = /^\w+$/;

interface InlineContext {
  theme: Theme;
  hyperlinks: boolean;
}

function linkSpans(label: string, url: string, base: SpanStyle, ctx: InlineContext): Span[] {
  const style: SpanStyle = { ...base, ...colour(ctx.theme, "accent"), underline: true, link: url };
  const out: Span[] = [{ ...style, text: label || url }];
  if (!ctx.hyperlinks && label && label !== url) {
    out.push({ ...base, ...colour(ctx.theme, "muted"), text: ` (${url})` });
  }
  return out;
}

function trimUrl(raw: string): { url: string; rest: string } {
  let url = raw.replace(/[.,;:!?'"]+$/, "");
  while (url.endsWith(")") && (url.match(/\(/g)?.length ?? 0) < (url.match(/\)/g)?.length ?? 0)) {
    url = url.slice(0, -1);
  }
  return { url, rest: raw.slice(url.length) };
}

/** Inline markdown of one line into spans, on top of `base`. */
export function inlineSpans(text: string, base: SpanStyle, ctx: InlineContext): Span[] {
  const out: Span[] = [];
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    const at = match.index ?? 0;
    if (at > last) out.push({ ...base, text: text.slice(last, at) });
    last = at + match[0].length;
    const g = match.groups ?? {};
    if (g.imgUrl !== undefined) {
      const label = `image: ${g.imgAlt || g.imgUrl}`;
      out.push(...linkSpans(label, g.imgUrl, { ...base, ...colour(ctx.theme, "muted") }, ctx));
    } else if (g.linkUrl !== undefined) {
      const label = g.linkLabel?.trim() ?? "";
      out.push(...linkSpans(label, g.linkUrl, base, ctx));
    } else if (g.auto !== undefined) {
      const target = /^(?:https?:|mailto:)/.test(g.auto) ? g.auto : `mailto:${g.auto}`;
      out.push(...linkSpans(g.auto.replace(/^mailto:/, ""), target, base, ctx));
    } else if (g.code !== undefined) {
      out.push({ ...base, ...colour(ctx.theme, "code"), text: g.code });
    } else if (g.foot !== undefined) {
      out.push({ ...base, ...colour(ctx.theme, "muted"), text: `[${g.foot}]` });
    } else if (g.strike !== undefined) {
      out.push(...inlineSpans(g.strike, { ...base, strike: true }, ctx));
    } else if (g.boldStar !== undefined) {
      out.push(...inlineSpans(g.boldStar, { ...base, bold: true }, ctx));
    } else if (g.boldUnder !== undefined) {
      // `__init__`-style identifiers are names, not emphasis.
      if (IDENTIFIER.test(g.boldUnder)) out.push({ ...base, text: match[0] });
      else out.push(...inlineSpans(g.boldUnder, { ...base, bold: true }, ctx));
    } else if (g.italicStar !== undefined) {
      out.push(...inlineSpans(g.italicStar, { ...base, italic: true }, ctx));
    } else if (g.italicUnder !== undefined) {
      out.push(...inlineSpans(g.italicUnder, { ...base, italic: true }, ctx));
    } else if (g.url !== undefined) {
      const { url, rest } = trimUrl(g.url);
      out.push(...linkSpans(url, url, base, ctx));
      if (rest) out.push({ ...base, text: rest });
    }
  }
  if (last < text.length) out.push({ ...base, text: text.slice(last) });
  return mergeSpans(out);
}

/** The visible text of a line of inline markdown (markers removed). */
export function inlinePlain(text: string): string {
  return inlineSpans(text, {}, { theme: currentTheme(), hyperlinks: true })
    .map((span) => span.text)
    .join("");
}

// ---------------------------------------------------------------- blocks

export type Block =
  | { kind: "blank" }
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "item"; indent: number; marker: string; task: boolean | null; text: string }
  | { kind: "quote"; lines: { depth: number; text: string }[] }
  | { kind: "code"; lang: string; lines: string[] }
  | { kind: "table"; rows: string[][] }
  | { kind: "rule" }
  | { kind: "footnote"; label: string; lines: string[] }
  | { kind: "media"; path: string };

const FENCE = /^\s{0,3}(`{3,}|~{3,})(.*)$/;
const ATX = /^\s{0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
const SETEXT_LINE = /^\s{0,3}(?:=+|-+)\s*$/;
const BULLET = /^(\s*)[-*+][ \t]+(.*)$/;
const ORDERED = /^(\s*)(\d{1,9})[.)][ \t]+(.*)$/;
const TASK = /^\[([ xX])\][ \t]+(.*)$/;
const FOOTNOTE = /^\[\^([^\]]+)\]:[ \t]*(.*)$/;

function isRule(line: string): boolean {
  if (/^\s{4,}/.test(line)) return false;
  const compact = line.replace(/\s+/g, "");
  return /^(?:-{3,}|\*{3,}|_{3,})$/.test(compact);
}

function splitRow(line: string): string[] {
  let body = line.trim();
  if (body.startsWith("|")) body = body.slice(1);
  if (body.endsWith("|") && !body.endsWith("\\|")) body = body.slice(0, -1);
  return body.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, "|"));
}

function isDelimiterRow(line: string): boolean {
  if (!line.includes("-")) return false;
  const cells = splitRow(line);
  return cells.length > 0 && cells.every((cell) => /^:?-+:?$/.test(cell));
}

function mediaPath(line: string): string | null {
  const body = line.trim().replace(/^[`"']|[`"']$/g, "");
  if (!body.startsWith("MEDIA:")) return null;
  const path = body.slice(6).trim();
  return path && !/\s/.test(path) ? path : null;
}

function indentLevel(lead: string): number {
  return Math.floor(lead.replace(/\t/g, "  ").length / 2);
}

/** Splits markdown source into blocks. Blank lines are kept (they decide the gaps). */
export function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i] as string;
    const next = lines[i + 1];
    if (!line.trim()) {
      blocks.push({ kind: "blank" });
      i++;
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence) {
      const marker = fence[1] as string;
      const lang = (fence[2] ?? "").trim().split(/\s+/)[0]?.toLowerCase() ?? "";
      const body: string[] = [];
      i++;
      while (i < lines.length) {
        const close = /^\s{0,3}(`{3,}|~{3,})\s*$/.exec(lines[i] as string)?.[1];
        if (close && close[0] === marker[0] && close.length >= marker.length) {
          i++;
          break;
        }
        body.push(lines[i] as string);
        i++;
      }
      blocks.push({ kind: "code", lang, lines: body });
      continue;
    }
    const path = mediaPath(line);
    if (path) {
      blocks.push({ kind: "media", path });
      i++;
      continue;
    }
    const atx = ATX.exec(line);
    if (atx?.[2]) {
      blocks.push({ kind: "heading", level: (atx[1] as string).length, text: atx[2] });
      i++;
      continue;
    }
    if (line.includes("|") && next !== undefined && isDelimiterRow(next)) {
      const rows = [splitRow(line)];
      i += 2;
      while (
        i < lines.length &&
        (lines[i] as string).trim() &&
        (lines[i] as string).includes("|")
      ) {
        rows.push(splitRow(lines[i] as string));
        i++;
      }
      blocks.push({ kind: "table", rows });
      continue;
    }
    if (line.trim().startsWith("|") && next?.trim().startsWith("|")) {
      // Pipe rows with no delimiter row still read as a table; delimiter-like rows are dropped.
      const rows: string[][] = [];
      while (i < lines.length && (lines[i] as string).trim().startsWith("|")) {
        if (!isDelimiterRow(lines[i] as string)) rows.push(splitRow(lines[i] as string));
        i++;
      }
      if (rows.length > 0) blocks.push({ kind: "table", rows });
      continue;
    }
    if (isRule(line)) {
      blocks.push({ kind: "rule" });
      i++;
      continue;
    }
    const foot = FOOTNOTE.exec(line);
    if (foot) {
      const body = [foot[2] ?? ""];
      i++;
      while (i < lines.length && /^\s{2,}\S/.test(lines[i] as string)) {
        body.push((lines[i] as string).trim());
        i++;
      }
      blocks.push({ kind: "footnote", label: foot[1] as string, lines: body });
      continue;
    }
    if (/^\s*>/.test(line)) {
      const quoted: { depth: number; text: string }[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i] as string)) {
        const lead = /^\s*(?:>\s?)+/.exec(lines[i] as string)?.[0] ?? "";
        quoted.push({
          depth: lead.split(">").length - 1,
          text: (lines[i] as string).slice(lead.length),
        });
        i++;
      }
      blocks.push({ kind: "quote", lines: quoted });
      continue;
    }
    const bullet = BULLET.exec(line);
    if (bullet) {
      const task = TASK.exec(bullet[2] as string);
      blocks.push({
        kind: "item",
        indent: indentLevel(bullet[1] as string),
        marker: "bullet",
        task: task ? (task[1] as string).toLowerCase() === "x" : null,
        text: task ? (task[2] as string) : (bullet[2] as string),
      });
      i++;
      continue;
    }
    const ordered = ORDERED.exec(line);
    if (ordered) {
      blocks.push({
        kind: "item",
        indent: indentLevel(ordered[1] as string),
        marker: `${ordered[2]}.`,
        task: null,
        text: ordered[3] as string,
      });
      i++;
      continue;
    }
    if (next !== undefined && SETEXT_LINE.test(next)) {
      blocks.push({
        kind: "heading",
        level: next.trim().startsWith("=") ? 1 : 2,
        text: line.trim(),
      });
      i += 2;
      continue;
    }
    blocks.push({ kind: "paragraph", text: line });
    i++;
  }
  return blocks;
}

// ---------------------------------------------------------------- tables

const TABLE_INDENT = 2;
const COLUMN_GAP = 2;
const MIN_COLUMN = 3;

function longestWord(spans: Span[]): number {
  let best = 0;
  for (const word of spans
    .map((s) => s.text)
    .join("")
    .split(/\s+/))
    best = Math.max(best, cellWidth(word));
  return best;
}

function share(base: number[], weights: number[], extra: number): number[] {
  const total = weights.reduce((a, b) => a + b, 0);
  if (total <= 0 || extra <= 0) return [...base];
  const raw = base.map((b, i) => b + ((weights[i] ?? 0) / total) * extra);
  const out = raw.map(Math.floor);
  let left = Math.round(base.reduce((a, b) => a + b, 0) + extra - out.reduce((a, b) => a + b, 0));
  const order = raw.map((v, i) => ({ i, frac: v - Math.floor(v) })).sort((a, b) => b.frac - a.frac);
  for (const { i } of order) {
    if (left <= 0) break;
    out[i] = (out[i] ?? 0) + 1;
    left--;
  }
  return out;
}

/** Column widths for cell contents that must fit `avail` cells (gaps excluded). */
export function fitColumns(
  ideal: number[],
  minimum: number[],
  avail: number,
): { widths: number[]; hard: boolean } {
  const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0);
  if (sum(ideal) <= avail) return { widths: ideal, hard: false };
  if (sum(minimum) <= avail) {
    const room = ideal.map((w, i) => w - (minimum[i] ?? 0));
    return { widths: share(minimum, room, avail - sum(minimum)), hard: false };
  }
  const floor = minimum.map(() => MIN_COLUMN);
  const room = minimum.map((w) => w - MIN_COLUMN);
  return { widths: share(floor, room, Math.max(0, avail - sum(floor))), hard: true };
}

function layoutTable(rows: string[][], width: number, ctx: InlineContext): StyledLine[] {
  const columns = rows[0]?.length ?? 0;
  if (columns === 0) return [];
  const grid = rows.map((row) =>
    Array.from({ length: columns }, (_, c) => inlineSpans(row[c] ?? "", {}, ctx)),
  );
  const ideal = Array.from({ length: columns }, (_, c) =>
    Math.max(MIN_COLUMN, ...grid.map((row) => lineWidth(row[c] ?? []))),
  );
  const minimum = Array.from({ length: columns }, (_, c) =>
    Math.max(MIN_COLUMN, ...grid.map((row) => longestWord(row[c] ?? []))),
  );
  const avail = width - TABLE_INDENT - (columns - 1) * COLUMN_GAP;
  const header: SpanStyle = { bold: true, ...colour(ctx.theme, "accent") };
  const muted = colour(ctx.theme, "muted");
  const tallLimit = columns <= 3 ? 6 : 4;

  if (avail >= columns * MIN_COLUMN) {
    // Long words are cut where they must be; everything else breaks at spaces.
    const { widths } = fitColumns(ideal, minimum, avail);
    const out: StyledLine[] = [];
    let tooTall = false;
    grid.forEach((row, r) => {
      const cells = row.map((spans, c) =>
        wrapSpans(
          r === 0 ? spans.map((s) => ({ ...header, ...s })) : spans,
          widths[c] ?? MIN_COLUMN,
        ),
      );
      const height = Math.max(1, ...cells.map((lines) => lines.length));
      if (r > 0 && height > tallLimit) tooTall = true;
      for (let l = 0; l < height; l++) {
        const line: Span[] = [{ text: " ".repeat(TABLE_INDENT) }];
        cells.forEach((lines, c) => {
          if (c > 0) line.push({ text: " ".repeat(COLUMN_GAP) });
          line.push(...padLine(lines[l] ?? [], widths[c] ?? MIN_COLUMN));
        });
        out.push(mergeSpans(line));
      }
      if (r === 0 && grid.length > 1) {
        const rule = widths.map((w) => "─".repeat(w)).join(" ".repeat(COLUMN_GAP));
        out.push([{ text: " ".repeat(TABLE_INDENT) }, { ...muted, text: rule }]);
      }
    });
    if (!tooTall && out.every((line) => lineWidth(line) <= width)) return out.map(trimEnd);
  }

  // Record view: each body row becomes "header: value" lines.
  const heads = (grid[0] ?? []).map((spans, c) =>
    spans.length ? spans : [{ text: `column ${c + 1}` }],
  );
  const out: StyledLine[] = [];
  const body = grid.slice(1);
  if (body.length === 0) {
    const joined: Span[] = [];
    heads.forEach((spans, c) => {
      if (c > 0) joined.push({ ...muted, text: " · " });
      joined.push(...spans.map((s) => ({ ...header, ...s })));
    });
    return wrapSpans(joined, width, { first: [{ text: " ".repeat(TABLE_INDENT) }] });
  }
  body.forEach((row, r) => {
    if (r > 0)
      out.push([
        { text: " ".repeat(TABLE_INDENT) },
        { ...muted, text: "·".repeat(Math.min(24, width - TABLE_INDENT)) },
      ]);
    row.forEach((cell, c) => {
      const label = (heads[c] ?? []).map((s) => ({ ...header, ...s }));
      out.push(
        ...wrapSpans([...label, { ...header, text: ":" }, { text: " " }, ...cell], width, {
          first: [{ text: " ".repeat(TABLE_INDENT) }],
          rest: [{ text: " ".repeat(TABLE_INDENT + 2) }],
        }),
      );
    });
  });
  return out;
}

function trimEnd(line: StyledLine): StyledLine {
  const out = [...line];
  while (out.length > 0) {
    const last = out[out.length - 1] as Span;
    const trimmed = last.text.replace(/\s+$/, "");
    if (trimmed === last.text) break;
    if (trimmed) {
      out[out.length - 1] = { ...last, text: trimmed };
      break;
    }
    out.pop();
  }
  return out;
}

// ---------------------------------------------------------------- layout

const BULLETS = ["•", "◦", "▪"];
const QUOTE_BAR = "▎ ";

function layoutCode(
  lang: string,
  body: string[],
  width: number,
  ctx: InlineContext,
  opts: MarkdownOptions,
): StyledLine[] {
  if (lang === "md" || lang === "markdown")
    return layoutBlocks(parseBlocks(body.join("\n")), width, ctx, opts);
  const theme = ctx.theme;
  const out: StyledLine[] = [];
  const pad: StyledLine = [{ text: "  " }];
  const diff = lang === "diff" || lang === "patch";
  if (lang && !diff)
    out.push([{ text: "  " }, { ...colour(theme, "muted"), dim: true, text: lang }]);
  const highlight = canHighlight(lang);
  for (const raw of body) {
    const text = raw.replace(/\t/g, "  ");
    let spans: Span[];
    if (diff) {
      let style: SpanStyle = colour(theme, "code");
      if (text.startsWith("+++") || text.startsWith("---")) style = { bold: true };
      else if (text.startsWith("+")) style = colour(theme, "added");
      else if (text.startsWith("-")) style = colour(theme, "removed");
      else if (text.startsWith("@@")) style = { ...colour(theme, "muted"), dim: true };
      spans = [{ ...style, text }];
    } else {
      spans = highlight ? highlightLine(text, lang, theme) : [{ ...colour(theme, "code"), text }];
    }
    out.push(
      ...wrapSpans(spans.length ? spans : [{ text: "" }], width, {
        first: pad,
        rest: pad,
        mode: "char",
      }),
    );
  }
  return out;
}

function layoutBlock(
  block: Block,
  width: number,
  ctx: InlineContext,
  opts: MarkdownOptions,
): StyledLine[] {
  const theme = ctx.theme;
  const muted = colour(theme, "muted");
  const text = colour(theme, "text");
  switch (block.kind) {
    case "blank":
      return [];
    case "heading": {
      const style: SpanStyle =
        block.level <= 2 ? { bold: true, ...colour(theme, "accent") } : { bold: true, ...text };
      if (block.level === 1) style.underline = true;
      return wrapSpans(inlineSpans(block.text, style, ctx), width);
    }
    case "paragraph":
      return wrapSpans(inlineSpans(block.text, text, ctx), width);
    case "item": {
      const lead = "  ".repeat(block.indent);
      const glyph =
        block.task === null
          ? block.marker === "bullet"
            ? (BULLETS[block.indent % BULLETS.length] as string)
            : block.marker
          : block.task
            ? "▣"
            : "□";
      const done = block.task === true;
      const first: StyledLine = [
        { text: lead },
        { ...(done ? colour(theme, "ok") : muted), text: `${glyph} ` },
      ];
      const spans = inlineSpans(block.text, done ? { ...text, dim: true } : text, ctx);
      return wrapSpans(spans, width, {
        first,
        rest: [{ text: " ".repeat(lead.length + cellWidth(glyph) + 1) }],
      });
    }
    case "quote":
      return block.lines.flatMap(({ depth, text: body }) => {
        const bar: StyledLine = [{ ...muted, text: QUOTE_BAR.repeat(Math.max(1, depth)) }];
        if (!body.trim()) return [bar];
        return wrapSpans(inlineSpans(body, { ...muted, italic: true }, ctx), width, {
          first: bar,
          rest: bar,
        });
      });
    case "code":
      return layoutCode(block.lang, block.lines, width, ctx, opts);
    case "table":
      return layoutTable(block.rows, width, ctx);
    case "rule":
      return [[{ ...colour(theme, "border"), text: "─".repeat(Math.max(1, Math.min(width, 48))) }]];
    case "footnote": {
      const first: StyledLine = [{ ...muted, text: `[${block.label}] ` }];
      return block.lines.flatMap((body, i) =>
        wrapSpans(inlineSpans(body, muted, ctx), width, {
          first: i === 0 ? first : [{ text: "    " }],
          rest: [{ text: "    " }],
        }),
      );
    }
    case "media": {
      const target = /^(?:\/|[a-zA-Z]:[\\/])/.test(block.path)
        ? `file://${block.path}`
        : block.path;
      const label: StyledLine = [{ ...muted, text: "⇲ attachment " }];
      return wrapSpans(
        [{ ...colour(theme, "accent"), underline: true, link: target, text: block.path }],
        width,
        { first: label, rest: [{ text: "  " }], mode: "char" },
      );
    }
  }
}

function layoutBlocks(
  blocks: Block[],
  width: number,
  ctx: InlineContext,
  opts: MarkdownOptions,
): StyledLine[] {
  const out: StyledLine[] = [];
  let gap = false;
  for (const block of blocks) {
    if (block.kind === "blank") {
      gap = out.length > 0;
      continue;
    }
    if (gap) out.push([]);
    gap = false;
    out.push(...layoutBlock(block, width, ctx, opts));
  }
  return out;
}

// ---------------------------------------------------------------- cache

export const MARKDOWN_CACHE_LIMIT = 512;
const cache = new Map<string, StyledLine[]>();
const stats = { hits: 0, misses: 0 };

export function markdownCacheStats(): { hits: number; misses: number; size: number } {
  return { ...stats, size: cache.size };
}

export function resetMarkdownCache(): void {
  cache.clear();
  stats.hits = 0;
  stats.misses = 0;
}

/** Lays markdown out at `width` cells. Results are cached by text, width, theme and options. */
export function layoutMarkdown(
  source: string,
  width: number,
  theme: Theme,
  opts: MarkdownOptions = {},
): StyledLine[] {
  const cols = Math.max(1, Math.floor(width));
  const key = `${themeId(theme)}:${cols}:${opts.hyperlinks ? 1 : 0}:${source}`;
  const hit = cache.get(key);
  if (hit) {
    stats.hits++;
    cache.delete(key);
    cache.set(key, hit);
    return hit;
  }
  stats.misses++;
  const ctx: InlineContext = { theme, hyperlinks: opts.hyperlinks === true };
  const lines = !source.trim()
    ? []
    : hasAnsi(source)
      ? wrapAnsiText(source.replace(/\n+$/, ""), cols)
      : layoutBlocks(parseBlocks(source), cols, ctx, opts);
  cache.set(key, lines);
  if (cache.size > MARKDOWN_CACHE_LIMIT) cache.delete(cache.keys().next().value as string);
  return lines;
}

export interface MarkdownProps extends MarkdownOptions {
  text: string;
  width: number;
}

export function Markdown({ text, width, hyperlinks = false }: MarkdownProps) {
  const theme = useTheme();
  const lines = useMemo(
    () => layoutMarkdown(text, width, theme, { hyperlinks }),
    [text, width, theme, hyperlinks],
  );
  return <Lines lines={lines} hyperlinks={hyperlinks} />;
}
