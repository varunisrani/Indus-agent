/**
 * The transcript pane: finished rows plus the running turn's live rows, virtualized by line.
 *
 * Every row is laid out to exact lines (cached per row object and width), offsets come from the
 * heights, and only the lines inside the viewport are handed to Ink — each keyed by its row and
 * line number so Ink reuses them as the view moves.
 */
import { Box, useAnimation } from "ink";
import { useRef } from "react";
import { useStore } from "zustand";
import { newScanState, type ScanState } from "../render/streamingMarkdown.tsx";
import { type StyledLine, StyledLineView } from "../render/styled.tsx";
import { themeId } from "../render/tones.ts";
import type { Segment } from "../state/turnStore.ts";
import { useTurn } from "../state/turnStore.ts";
import { useUi } from "../state/uiStore.ts";
import type { Theme } from "../theme/theme.ts";
import { useTheme } from "../theme/theme.ts";
import { detailsStore, type ResolvedSections, resolveSections } from "./details.ts";
import {
  GUTTER,
  isDiffText,
  layoutMessage,
  type RowContext,
  type TranscriptItem,
} from "./MessageRow.tsx";
import { layoutReasoning } from "./Reasoning.tsx";
import { layoutTodos, parseTodos, todoView } from "./TodoPanel.tsx";
import { layoutToolTrail, type TrailRow } from "./ToolTrail.tsx";
import { Offsets, resolveOffset, scrollStore, setScrollGeometry, windowAt } from "./virtualRows.ts";

export interface TranscriptRow {
  key: string;
  lines: StyledLine[];
}

interface CacheEntry {
  sig: string;
  lines: StyledLine[];
}

// Two widths per row, so flipping back after a resize is free.
const rowCache = new WeakMap<TranscriptItem, CacheEntry[]>();

function cachedLayout(item: TranscriptItem, width: number, ctx: RowContext): StyledLine[] {
  const sections = ctx.sections;
  const sig = [
    width,
    themeId(ctx.theme),
    ctx.timestamps ? 1 : 0,
    ctx.hyperlinks ? 1 : 0,
    ctx.turnRule ? 1 : 0,
    item.role === "banner" ? (ctx.sid ?? "") : "",
    sections.tools,
    sections.subagents,
    sections.thinking,
  ].join(":");
  const entries = rowCache.get(item) ?? [];
  const hit = entries.find((e) => e.sig === sig);
  if (hit) return hit.lines;
  const lines = layoutMessage(item, width, ctx);
  rowCache.set(item, [{ sig, lines }, ...entries].slice(0, 2));
  return lines;
}

const segmentItems = new WeakMap<Segment, TranscriptItem>();

function segmentItem(segment: Segment): TranscriptItem {
  let item = segmentItems.get(segment);
  if (!item) {
    const diff = segment.id.startsWith("diff") || isDiffText(segment.text);
    item = {
      id: `seg:${segment.id}`,
      role: diff ? "diff" : "assistant",
      text: segment.text,
      tools: segment.tools,
    };
    segmentItems.set(segment, item);
  }
  return item;
}

function indent(lines: StyledLine[]): StyledLine[] {
  return lines.map((line) => [{ text: " ".repeat(GUTTER) }, ...line]);
}

export interface LiveTurn {
  busy: boolean;
  streamBuffer: string;
  segments: readonly Segment[];
  activeTools: readonly TrailRow[];
  pendingTools: readonly TrailRow[];
  trail: readonly TrailRow[];
  activity: readonly { id: string; text: string; level: "info" | "warn" | "error" }[];
  reasoning: string;
  todos: unknown;
  todoCollapsed: boolean;
  now: number;
  frame: number;
}

export interface RowsInput {
  items: readonly TranscriptItem[];
  live: LiveTurn | null;
  width: number;
  theme: Theme;
  sections: ResolvedSections;
  timestamps: boolean;
  hyperlinks: boolean;
  sid: string | null;
  scan: { current: ScanState };
}

/** Every transcript row, finished then live, laid out at `width`. */
export function transcriptRows(input: RowsInput): TranscriptRow[] {
  const { theme, sections, width } = input;
  const base: RowContext = {
    theme,
    sections,
    timestamps: input.timestamps,
    hyperlinks: input.hyperlinks,
    sid: input.sid,
  };
  const rows: TranscriptRow[] = [];
  let seenUser = false;
  for (const item of input.items) {
    const turnRule = item.role === "user" && seenUser;
    if (item.role === "user") seenUser = true;
    rows.push({ key: item.id, lines: cachedLayout(item, width, { ...base, turnRule }) });
  }
  const live = input.live;
  if (!live) return rows;
  const attached = new Set<string>();
  for (const segment of live.segments) {
    for (const tool of segment.tools) attached.add(tool.id);
    rows.push({
      key: `live:seg:${segment.id}`,
      lines: cachedLayout(segmentItem(segment), width, base),
    });
  }
  const body = Math.max(1, width - GUTTER);
  if (live.busy || live.reasoning.trim()) {
    const thinking = layoutReasoning(
      live.reasoning,
      sections.thinking,
      live.busy && !live.streamBuffer,
      body,
      theme,
    );
    if (thinking.length) rows.push({ key: "live:thinking", lines: indent(thinking) });
  }
  const tools = layoutToolTrail(
    {
      active: live.activeTools,
      pending: live.pendingTools,
      trail: live.trail.filter((row) => !attached.has(row.id)),
      activity: live.activity,
      now: live.now,
      frame: live.frame,
    },
    sections,
    body,
    theme,
  );
  if (tools.length) rows.push({ key: "live:tools", lines: indent(tools) });
  if (live.streamBuffer) {
    const lines = layoutMessage(
      { id: "live:stream", role: "assistant", text: live.streamBuffer },
      width,
      {
        ...base,
        scan: input.scan.current,
        onScan: (state) => {
          input.scan.current = state;
        },
      },
    );
    rows.push({ key: "live:stream", lines });
  } else if (input.scan.current.scanned) {
    input.scan.current = newScanState();
  }
  const todos = layoutTodos(parseTodos(live.todos), live.todoCollapsed, body, theme);
  if (todos.length) rows.push({ key: "live:todos", lines: indent(todos) });
  return rows;
}

export interface VisibleLine {
  key: string;
  line: StyledLine;
}

/** Heights include the blank line that separates a row from the one above it. */
export function rowHeights(rows: readonly TranscriptRow[]): number[] {
  return rows.map((row, i) => row.lines.length + (i > 0 ? 1 : 0));
}

/** The lines inside the viewport, keyed by row and line number. */
export function visibleLines(
  rows: readonly TranscriptRow[],
  offsets: Offsets,
  offset: number,
  height: number,
): VisibleLine[] {
  const win = windowAt(offsets, offset, height);
  const out: VisibleLine[] = [];
  let skip = win.skip;
  for (let r = win.start; r < win.end && out.length < height; r++) {
    const row = rows[r] as TranscriptRow;
    const gap = r > 0 ? 1 : 0;
    const count = row.lines.length + gap;
    for (let l = 0; l < count && out.length < height; l++) {
      if (skip > 0) {
        skip--;
        continue;
      }
      const index = l - gap;
      out.push({
        key: `${row.key}:${index}`,
        line: index < 0 ? [] : (row.lines[index] as StyledLine),
      });
    }
  }
  return out;
}

/** What the last render showed (for tests and the scroll indicator). */
export const transcriptStats = { rows: 0, totalLines: 0, rendered: 0, offset: 0, topKey: "" };

export interface TranscriptProps {
  items: readonly TranscriptItem[];
  width: number;
  height: number;
  hyperlinks?: boolean;
  /** Include the running turn's live rows (default on). */
  live?: boolean;
}

export function Transcript({
  items,
  width,
  height,
  hyperlinks = false,
  live = true,
}: TranscriptProps) {
  const theme = useTheme();
  const global = useUi((s) => s.detailsMode);
  const timestamps = useUi((s) => s.timestamps);
  const busy = useUi((s) => s.busy);
  const sid = useUi((s) => s.sid);
  const overrides = useStore(detailsStore, (s) => s.overrides);
  const fromCommand = useStore(detailsStore, (s) => s.globalFromCommand);
  const todoCollapsed = useStore(todoView, (s) => s.collapsed);
  const scroll = useStore(scrollStore);
  const streamBuffer = useTurn((s) => s.streamBuffer);
  const segments = useTurn((s) => s.segments);
  const activeTools = useTurn((s) => s.activeTools);
  const pendingTools = useTurn((s) => s.pendingTools);
  const trail = useTurn((s) => s.trail);
  const activity = useTurn((s) => s.activity);
  const reasoning = useTurn((s) => s.reasoning);
  const todos = useTurn((s) => s.todos);
  const { frame } = useAnimation({ interval: 120, isActive: live && activeTools.length > 0 });
  const scan = useRef<ScanState>(newScanState());

  const cols = Math.max(GUTTER + 1, Math.floor(width));
  const sections = resolveSections(global, overrides, fromCommand);
  const rows = transcriptRows({
    items,
    live: live
      ? {
          busy,
          streamBuffer,
          segments,
          activeTools,
          pendingTools,
          trail,
          activity,
          reasoning,
          todos,
          todoCollapsed,
          now: Date.now(),
          frame,
        }
      : null,
    width: cols,
    theme,
    sections,
    timestamps,
    hyperlinks,
    sid,
    scan,
  });
  const offsets = new Offsets(rowHeights(rows));
  const viewport = Math.max(0, Math.floor(height));
  const geometry = { keys: rows.map((r) => r.key), offsets, viewport };
  setScrollGeometry(geometry);
  const offset = resolveOffset(scroll, geometry);
  const lines = visibleLines(rows, offsets, offset, viewport);

  transcriptStats.rows = rows.length;
  transcriptStats.totalLines = offsets.total;
  transcriptStats.rendered = lines.length;
  transcriptStats.offset = offset;
  transcriptStats.topKey = lines[0]?.key ?? "";

  return (
    <Box flexDirection="column" height={viewport} overflow="hidden" flexShrink={0}>
      {lines.map((v) => (
        <StyledLineView key={v.key} line={v.line} hyperlinks={hyperlinks} />
      ))}
    </Box>
  );
}
