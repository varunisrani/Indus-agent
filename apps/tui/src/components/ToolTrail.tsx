/**
 * The tool area of a turn: a "Tool calls (N)" heading over a tree of the turn's calls —
 * finished ones as `name(args)` with their duration (a failure adds its error text), running ones
 * with a spinner, verb and elapsed time, ones the model is still drafting — and the activity lane.
 * Each section obeys its detail mode (collapsed: the heading alone); when every section is hidden,
 * warnings and errors still show as a floating alert.
 */
import { Box, useAnimation } from "ink";
import { useStore } from "zustand";
import { liveTail } from "../render/streamingMarkdown.tsx";
import { Lines, oneLine, type Span, type StyledLine, wrapSpans } from "../render/styled.tsx";
import { colour } from "../render/tones.ts";
import type { ToolRow, TurnState } from "../state/turnStore.ts";
import { useTurn } from "../state/turnStore.ts";
import { useUi } from "../state/uiStore.ts";
import type { Theme } from "../theme/theme.ts";
import { useTheme } from "../theme/theme.ts";
import { allHidden, detailsStore, type ResolvedSections, resolveSections } from "./details.ts";
import { toolVerb } from "./toolVerbs.ts";

export const TRAIL_LIMIT = 8;
export const ACTIVITY_LIMIT = 8;
export const ACTIVITY_SHOWN = 4;
export const VERBOSE_MAX_CHARS = 800;
export const VERBOSE_MAX_LINES = 12;
export const ALERTS_SHOWN = 2;
/** The heading over a turn's tool calls (expanded as a tree, collapsed to one line). */
export const TOOLS_TITLE = "Tool calls";

/** Tool rows may carry a nesting depth (subagent calls render indented under their parent). */
export type TrailRow = ToolRow & { depth?: number };
export type ActivityItem = TurnState["activity"][number];

const TOOL_FRAMES = ["◐", "◓", "◑", "◒"];

/** Under 10 s one decimal, under 60 s whole seconds, else minutes and seconds. */
export function formatDuration(ms: number): string {
  const seconds = Number.isFinite(ms) && ms > 0 ? ms / 1000 : 0;
  const tenths = Math.round(seconds * 10) / 10;
  if (tenths < 10) return `${tenths.toFixed(1)}s`;
  const whole = Math.round(seconds);
  if (whole < 60) return `${whole}s`;
  return `${Math.floor(whole / 60)}m ${whole % 60}s`;
}

/** Appends a finished row and keeps the newest `limit` (the store's trail rule). */
export function appendTrail<T>(trail: readonly T[], row: T, limit = TRAIL_LIMIT): T[] {
  const next = [...trail, row];
  return next.length > limit ? next.slice(next.length - limit) : next;
}

/** Verbose tool text cut to its tail: 800 chars / 12 lines, with a note of what was dropped. */
export function capVerbose(text: string): string {
  const tail = liveTail(text.replace(/\s+$/, ""), VERBOSE_MAX_CHARS, VERBOSE_MAX_LINES);
  if (tail.omittedChars === 0) return tail.text;
  const what = tail.omittedLines > 0 ? `${tail.omittedLines} lines` : `${tail.omittedChars} chars`;
  return `… ${what} above not shown\n${tail.text}`;
}

/** `name(args)`: the call as one line, its argument preview cut to `max` cells. */
function toolLabel(row: TrailRow, max = 56): string {
  const args = row.args ? oneLine(row.args, max) : "";
  return args ? `${row.name}(${args})` : row.name;
}

function indentOf(row: TrailRow): string {
  return "  ".repeat(Math.max(0, row.depth ?? 0));
}

const BRANCH_MID = "├ ";
const BRANCH_LAST = "└ ";
const RAIL = "│ ";
const BRANCH_WIDTH = 2;

/** Hangs one row's lines off the tree: a branch glyph on its first line, a rail (or gap) after. */
function inTree(lines: StyledLine[], last: boolean, theme: Theme): StyledLine[] {
  const tone = colour(theme, "border");
  return lines.map((line, i) => [
    { ...tone, text: i === 0 ? (last ? BRANCH_LAST : BRANCH_MID) : last ? "  " : RAIL },
    ...line,
  ]);
}

export interface TrailView {
  active: readonly TrailRow[];
  pending: readonly TrailRow[];
  trail: readonly TrailRow[];
  activity: readonly ActivityItem[];
  now: number;
  frame: number;
}

function activeLine(row: TrailRow, view: TrailView, theme: Theme, width: number): StyledLine[] {
  const spinner = TOOL_FRAMES[view.frame % TOOL_FRAMES.length] as string;
  const elapsed =
    row.startedAt !== undefined ? ` (${formatDuration(view.now - row.startedAt)})` : "";
  const spans: Span[] = [
    { text: indentOf(row) },
    { ...colour(theme, "accent"), text: `${spinner} ` },
    { ...colour(theme, "text"), bold: true, text: row.verb ?? toolVerb(row.name) },
    { ...colour(theme, "text"), text: ` ${toolLabel(row)}` },
    { ...colour(theme, "muted"), dim: true, text: elapsed },
  ];
  return wrapSpans(spans, width, { rest: [{ text: `${indentOf(row)}  ` }] });
}

function finishedLine(row: TrailRow, theme: Theme, width: number, verbose: boolean): StyledLine[] {
  const failed = row.status === "error";
  const cancelled = row.status === "cancelled";
  const mark = failed ? "✗" : cancelled ? "⊘" : "✓";
  const markTone = failed
    ? colour(theme, "error")
    : cancelled
      ? colour(theme, "muted")
      : colour(theme, "ok");
  const took =
    row.startedAt !== undefined && row.endedAt !== undefined
      ? ` (${formatDuration(row.endedAt - row.startedAt)})`
      : "";
  const lead = indentOf(row);
  const lines = wrapSpans(
    [
      { text: lead },
      { ...markTone, text: `${mark} ` },
      { ...(failed ? colour(theme, "error") : colour(theme, "text")), text: toolLabel(row) },
      { ...colour(theme, "muted"), dim: true, text: took },
    ],
    width,
    { rest: [{ text: `${lead}  ` }] },
  );
  const shown = failed && verbose ? readableOutput(row.output ?? "") : "";
  if (shown) {
    const body = capVerbose(shown);
    const first: StyledLine = [{ text: lead }, { ...colour(theme, "muted"), text: "  ⎿ " }];
    const rest: StyledLine = [{ text: `${lead}    ` }];
    lines.push(
      ...wrapSpans([{ ...colour(theme, "error"), text: body }], width, {
        first,
        rest,
        mode: "char",
      }),
    );
  }
  return lines;
}

/**
 * The words a result carries for a person: a JSON object's `error` (else `output`) text, any
 * other result as it came. An empty string when there is nothing to say.
 */
export function readableOutput(raw: string): string {
  const text = raw.trim();
  if (!text.startsWith("{")) return text;
  try {
    const value: unknown = JSON.parse(text);
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const record = value as Record<string, unknown>;
      for (const key of ["error", "output"]) {
        const field = record[key];
        if (typeof field === "string" && field.trim()) return field.trim();
      }
      return "";
    }
  } catch {
    // not JSON after all: show it as text
  }
  return text;
}

function header(open: boolean, title: string, theme: Theme, extra = ""): StyledLine {
  return [
    { ...colour(theme, "accent"), text: open ? "▾ " : "▸ " },
    { ...colour(theme, "muted"), text: title },
    ...(extra ? [{ ...colour(theme, "muted"), dim: true, text: extra }] : []),
  ];
}

function alertLines(activity: readonly ActivityItem[], theme: Theme, width: number): StyledLine[] {
  return activity
    .filter((item) => item.level !== "info")
    .slice(-ALERTS_SHOWN)
    .flatMap((item) =>
      wrapSpans(
        [
          {
            ...colour(theme, item.level === "error" ? "error" : "warn"),
            text: `${item.level === "error" ? "✗" : "!"} ${item.text}`,
          },
        ],
        width,
        { rest: [{ text: "  " }] },
      ),
    );
}

/** Lays out the tool area for the given resolved section modes. */
export function layoutToolTrail(
  view: TrailView,
  sections: ResolvedSections,
  width: number,
  theme: Theme,
): StyledLine[] {
  if (allHidden(sections)) return alertLines(view.activity, theme, width);
  const keep = (row: TrailRow) => (row.depth ?? 0) === 0 || sections.subagents !== "hidden";
  const finished = view.trail.slice(-TRAIL_LIMIT).filter(keep);
  const running = view.active.filter(keep);
  // `pending` may also hold finished rows waiting for a segment (the turn store's pendingTools);
  // those already show in the trail, so only rows the model is still writing draw a drafting line.
  const drafting = view.pending.filter((row) => row.status === "pending" && keep(row));
  const out: StyledLine[] = [];
  const count = finished.length + running.length + drafting.length;

  if (sections.tools !== "hidden" && count > 0) {
    if (sections.tools === "collapsed") {
      const live = running.at(-1);
      const extra = live
        ? ` · ${live.verb ?? toolVerb(live.name)} ${live.name}${
            live.startedAt !== undefined ? ` ${formatDuration(view.now - live.startedAt)}` : ""
          }`
        : "";
      out.push(header(false, `${TOOLS_TITLE} (${count})`, theme, extra));
    } else {
      out.push(header(true, `${TOOLS_TITLE} (${count})`, theme));
      const inner = Math.max(1, width - BRANCH_WIDTH);
      const rows: StyledLine[][] = [
        ...finished.map((row) => {
          const sub = (row.depth ?? 0) > 0;
          return finishedLine(row, theme, inner, !sub || sections.subagents === "expanded");
        }),
        ...running.map((row) => activeLine(row, view, theme, inner)),
        ...drafting.map((row): StyledLine[] => [
          [
            { text: indentOf(row) },
            { ...colour(theme, "muted"), dim: true, text: `✎ drafting ${row.name}…` },
          ],
        ]),
      ];
      rows.forEach((lines, i) => {
        out.push(...inTree(lines, i === rows.length - 1, theme));
      });
    }
  }

  const activity = view.activity.slice(-ACTIVITY_LIMIT);
  if (sections.activity !== "hidden" && activity.length > 0) {
    if (sections.activity === "collapsed") {
      out.push(header(false, `activity (${activity.length})`, theme));
    } else {
      for (const item of activity.slice(-ACTIVITY_SHOWN)) {
        const tone = item.level === "error" ? "error" : item.level === "warn" ? "warn" : "muted";
        const glyph = item.level === "error" ? "✗" : item.level === "warn" ? "!" : "·";
        out.push(
          ...wrapSpans(
            [{ ...colour(theme, tone), dim: tone === "muted", text: `${glyph} ${item.text}` }],
            width,
          ),
        );
      }
    }
  }
  return out;
}

/** Ticks while something runs, so elapsed times and spinners move. */
function useClock(active: boolean): { now: number; frame: number } {
  const { frame } = useAnimation({ interval: 120, isActive: active });
  return { now: Date.now(), frame };
}

export interface ToolTrailProps {
  width: number;
}

/** The live tool area of the running turn, read from the turn store. */
export function ToolTrail({ width }: ToolTrailProps) {
  const theme = useTheme();
  const active = useTurn((s) => s.activeTools);
  const pending = useTurn((s) => s.pendingTools);
  const trail = useTurn((s) => s.trail);
  const activity = useTurn((s) => s.activity);
  const global = useUi((s) => s.detailsMode);
  const overrides = useStore(detailsStore, (s) => s.overrides);
  const fromCommand = useStore(detailsStore, (s) => s.globalFromCommand);
  const { now, frame } = useClock(active.length > 0);
  const sections = resolveSections(global, overrides, fromCommand);
  const lines = layoutToolTrail(
    { active, pending, trail, activity, now, frame },
    sections,
    width,
    theme,
  );
  if (lines.length === 0) return null;
  return (
    <Box flexDirection="column">
      <Lines lines={lines} />
    </Box>
  );
}
