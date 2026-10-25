/**
 * Transcript rows: the banner with the session panel, then user, assistant, system, tool, diff
 * and notice rows. Each row is laid out into styled lines (so its height is exact) behind a
 * two-cell gutter glyph.
 */
import { layoutMarkdown } from "../render/markdown.tsx";
import { layoutStream, type ScanState } from "../render/streamingMarkdown.tsx";
import {
  cellWidth,
  hasAnsi,
  Lines,
  oneLine,
  type Span,
  type StyledLine,
  stripAnsi,
  truncateLine,
  wrapAnsiText,
  wrapSpans,
} from "../render/styled.tsx";
import { colour, type ToneName } from "../render/tones.ts";
import type { Theme } from "../theme/theme.ts";
import { useTheme } from "../theme/theme.ts";
import type { ResolvedSections } from "./details.ts";
import { layoutReasoning } from "./Reasoning.tsx";
import { layoutToolTrail, type TrailRow } from "./ToolTrail.tsx";

export type TranscriptRole =
  | "banner"
  | "user"
  | "assistant"
  | "system"
  | "tool"
  | "diff"
  | "notice";

export interface TranscriptItem {
  id: string;
  role: TranscriptRole;
  text: string;
  /** Epoch milliseconds, for optional timestamps. */
  createdAt?: number;
  /** Tools that ran for this assistant segment. */
  tools?: readonly TrailRow[];
  /** The reasoning behind this assistant reply (finished turns). */
  reasoning?: string;
  /** A long system row shown in full. */
  expanded?: boolean;
  /** Session facts for the banner row. */
  info?: Readonly<Record<string, unknown>> | null;
}

export const SYSTEM_COLLAPSE_CHARS = 400;
export const LONG_USER_CHARS = 300;
export const GUTTER = 2;

const PASTE_TOKEN = /\[\[?paste(?::| #)\d+[^\]\n]*\]\]?/;

/** True when the message carries a paste token from the composer's paste collapse. */
export function isPasteBacked(text: string): boolean {
  return PASTE_TOKEN.test(text);
}

/** `[HH:MM]` in local time, or null for a missing or bad time. */
export function formatStamp(ms: number | undefined): string | null {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms <= 0) return null;
  const date = new Date(ms);
  return `[${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}]`;
}

/** True for text that reads as a unified diff (used for inline-diff segments). */
export function isDiffText(text: string): boolean {
  return /^(?:diff --git |--- \S|\+\+\+ \S|@@ )/m.test(text) && /^[+-]/m.test(text);
}

export interface RowContext {
  theme: Theme;
  sections: ResolvedSections;
  timestamps?: boolean;
  hyperlinks?: boolean;
  /** Draw the short rule that starts a new turn above this user row. */
  turnRule?: boolean;
  /** The session id shown in the banner. */
  sid?: string | null;
  /** Live assistant text: settled blocks as markdown, the tail plain. */
  scan?: ScanState;
  onScan?: (state: ScanState) => void;
}

const GLYPHS: Record<TranscriptRole, { glyph: string; tone: ToneName }> = {
  banner: { glyph: "  ", tone: "muted" },
  user: { glyph: "› ", tone: "accent" },
  assistant: { glyph: "◆ ", tone: "accent" },
  system: { glyph: "· ", tone: "muted" },
  tool: { glyph: "  ", tone: "muted" },
  diff: { glyph: "  ", tone: "muted" },
  notice: { glyph: "◇ ", tone: "muted" },
};

function withGutter(role: TranscriptRole, body: StyledLine[], theme: Theme): StyledLine[] {
  const { glyph, tone } = GLYPHS[role];
  const blank: Span = { text: " ".repeat(GUTTER) };
  return body.map((line, i) => [
    i === 0 ? { ...colour(theme, tone), bold: role === "user", text: glyph } : blank,
    ...line,
  ]);
}

function diffLines(text: string, width: number, theme: Theme): StyledLine[] {
  return text
    .replace(/\n+$/, "")
    .split("\n")
    .flatMap((raw) => {
      let tone: ToneName = "text";
      let bold = false;
      if (raw.startsWith("+++") || raw.startsWith("---") || raw.startsWith("diff ")) bold = true;
      else if (raw.startsWith("+")) tone = "added";
      else if (raw.startsWith("-")) tone = "removed";
      else if (raw.startsWith("@@")) tone = "muted";
      return wrapSpans([{ ...colour(theme, tone), bold, text: raw.replace(/\t/g, "  ") }], width, {
        mode: "char",
      });
    });
}

// "INDUS" in three rows of box-drawing strokes.
const LETTERS = [
  ["╷", "│", "╵"],
  ["┌╮╷", "│││", "╵╰┘"],
  ["┌─╮", "│ │", "└─╯"],
  ["╷ ╷", "│ │", "╰─╯"],
  ["╭─╴", "╰─╮", "╶─╯"],
];
const WORDMARK = [0, 1, 2].map((row) => LETTERS.map((letter) => letter[row]).join(" "));

function bannerLines(item: TranscriptItem, width: number, ctx: RowContext): StyledLine[] {
  const { theme } = ctx;
  const out: StyledLine[] = [];
  if (width >= 32) {
    for (const row of WORDMARK) out.push([{ ...colour(theme, "accent"), text: row }]);
    out[1]?.push({ ...colour(theme, "text"), bold: true, text: "  indus agent" });
  } else {
    out.push([{ ...colour(theme, "accent"), bold: true, text: "indus agent" }]);
  }
  const info = item.info ?? {};
  const facts: [string, string][] = [];
  const model = typeof info.model === "string" ? info.model : "";
  const provider = typeof info.provider === "string" ? info.provider : "";
  if (model) facts.push(["model", provider ? `${model} · ${provider}` : model]);
  if (typeof info.cwd === "string" && info.cwd) facts.push(["folder", info.cwd]);
  if (Array.isArray(info.tools)) facts.push(["tools", `${info.tools.length} available`]);
  if (typeof info.title === "string" && info.title) facts.push(["title", info.title]);
  if (ctx.sid) facts.push(["session", ctx.sid]);
  if (facts.length > 0) out.push([]);
  for (const [label, value] of facts) {
    out.push(
      ...wrapSpans([{ ...colour(theme, "text"), text: value }], width, {
        first: [{ ...colour(theme, "muted"), text: label.padEnd(9, " ") }],
        rest: [{ text: " ".repeat(9) }],
        mode: "char",
      }),
    );
  }
  if (item.text.trim()) {
    out.push([]);
    out.push(...wrapSpans([{ ...colour(theme, "muted"), dim: true, text: item.text }], width));
  }
  return out.map((line) => truncateLine(line, width));
}

function textBody(
  text: string,
  width: number,
  theme: Theme,
  tone: ToneName = "text",
): StyledLine[] {
  if (hasAnsi(text)) return wrapAnsiText(text, width);
  return wrapSpans([{ ...colour(theme, tone), text: text.replace(/\n+$/, "") }], width);
}

/** Lays out one transcript row (gutter included) at `width` cells. */
export function layoutMessage(item: TranscriptItem, width: number, ctx: RowContext): StyledLine[] {
  const { theme } = ctx;
  const cols = Math.max(GUTTER + 1, Math.floor(width));
  const body = cols - GUTTER;
  if (item.role === "banner") return bannerLines(item, cols, ctx);

  const out: StyledLine[] = [];
  if (item.role === "user" && ctx.turnRule) out.push([{ ...colour(theme, "border"), text: "───" }]);
  const stamp =
    ctx.timestamps && (item.role === "user" || item.role === "assistant")
      ? formatStamp(item.createdAt)
      : null;
  if (stamp)
    out.push([{ text: " ".repeat(GUTTER) }, { ...colour(theme, "muted"), dim: true, text: stamp }]);

  let lines: StyledLine[];
  switch (item.role) {
    case "user": {
      if (item.text.length > LONG_USER_CHARS && isPasteBacked(item.text)) {
        const first = item.text.split("\n").find((l) => l.trim()) ?? "";
        const head = oneLine(first, Math.max(8, body - 24));
        const rest = item.text.length - head.length;
        lines = wrapSpans(
          [
            { ...colour(theme, "text"), text: head },
            {
              ...colour(theme, "muted"),
              dim: true,
              text: ` ⋯ ${rest.toLocaleString("en-US")} more chars pasted`,
            },
          ],
          body,
        );
      } else {
        lines = wrapSpans([{ ...colour(theme, "text"), text: item.text }], body);
      }
      break;
    }
    case "assistant": {
      const head: StyledLine[] = [
        // A finished turn's reasoning stays folded (only the live turn opens it).
        ...(item.reasoning && ctx.sections.thinking !== "hidden"
          ? layoutReasoning(item.reasoning, "collapsed", false, body, theme)
          : []),
        ...(item.tools?.length
          ? layoutToolTrail(
              { active: [], pending: [], trail: item.tools, activity: [], now: 0, frame: 0 },
              ctx.sections,
              body,
              theme,
            )
          : []),
      ];
      let text: StyledLine[];
      if (ctx.scan) {
        const laid = layoutStream(
          item.text,
          body,
          theme,
          { hyperlinks: ctx.hyperlinks === true },
          ctx.scan,
        );
        ctx.onScan?.(laid.state);
        text = laid.lines;
      } else {
        text = layoutMarkdown(item.text, body, theme, { hyperlinks: ctx.hyperlinks === true });
      }
      lines = head.length && text.length ? [...head, [], ...text] : [...head, ...text];
      break;
    }
    case "system": {
      if (item.text.length > SYSTEM_COLLAPSE_CHARS && !item.expanded) {
        const first = item.text.split("\n").find((l) => l.trim()) ?? "system message";
        const size = ` (${item.text.length.toLocaleString("en-US")} chars)`;
        lines = [
          truncateLine(
            [
              { ...colour(theme, "accent"), text: "▸ " },
              {
                ...colour(theme, "muted"),
                text: oneLine(first, Math.max(8, body - cellWidth(size) - 2)),
              },
              { ...colour(theme, "muted"), dim: true, text: size },
            ],
            body,
          ),
        ];
      } else {
        lines = textBody(item.text, body, theme, "muted");
      }
      break;
    }
    case "tool": {
      const preview = oneLine(stripAnsi(item.text), Math.max(8, body - 4));
      lines = [
        [
          { ...colour(theme, "muted"), text: "⎿ " },
          { ...colour(theme, "muted"), dim: true, text: preview || "(no output)" },
        ],
      ];
      break;
    }
    case "diff":
      lines = diffLines(item.text, body, theme);
      break;
    case "notice":
      lines = textBody(item.text, body, theme, "muted");
      break;
  }
  if (lines.length === 0) lines = [[]];
  out.push(...withGutter(item.role, lines, theme));
  return out;
}

export interface MessageRowProps {
  item: TranscriptItem;
  width: number;
  sections: ResolvedSections;
  timestamps?: boolean;
  hyperlinks?: boolean;
  turnRule?: boolean;
}

export function MessageRow({
  item,
  width,
  sections,
  timestamps = false,
  hyperlinks = false,
  turnRule = false,
}: MessageRowProps) {
  const theme = useTheme();
  const lines = layoutMessage(item, width, { theme, sections, timestamps, hyperlinks, turnRule });
  return <Lines lines={lines} hyperlinks={hyperlinks} />;
}
