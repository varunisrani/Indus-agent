/**
 * The plan the agent keeps with its todo tool: items `{ id, content, status, parent? }`, nested
 * under their parent, with a done count in the header. Collapsible.
 */
import { Box } from "ink";
import { useStore } from "zustand";
import { createStore } from "zustand/vanilla";
import { Lines, type StyledLine, wrapSpans } from "../render/styled.tsx";
import { colour } from "../render/tones.ts";
import { useTurn } from "../state/turnStore.ts";
import type { Theme } from "../theme/theme.ts";
import { useTheme } from "../theme/theme.ts";

export type TodoStatus = "pending" | "in_progress" | "completed" | "cancelled";

export interface TodoEntry {
  id: string;
  content: string;
  status: TodoStatus;
  parent?: string;
}

const STATUSES: readonly TodoStatus[] = ["pending", "in_progress", "completed", "cancelled"];
const MAX_DEPTH = 4;

function asStatus(value: unknown): TodoStatus {
  const text = String(value ?? "")
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, "_");
  if (text === "done" || text === "complete") return "completed";
  if (text === "active" || text === "running" || text === "doing") return "in_progress";
  return (STATUSES as readonly string[]).includes(text) ? (text as TodoStatus) : "pending";
}

/** Reads todos from a tool payload: an array, or an object holding `todos` / `items`. */
export function parseTodos(raw: unknown): TodoEntry[] {
  const list = Array.isArray(raw)
    ? raw
    : raw && typeof raw === "object"
      ? ((raw as Record<string, unknown>).todos ?? (raw as Record<string, unknown>).items)
      : null;
  if (!Array.isArray(list)) return [];
  const out: TodoEntry[] = [];
  list.forEach((item, index) => {
    if (!item || typeof item !== "object") return;
    const record = item as Record<string, unknown>;
    const content = String(record.content ?? record.text ?? record.title ?? "").trim();
    if (!content) return;
    const entry: TodoEntry = {
      id: String(record.id ?? index + 1),
      content,
      status: asStatus(record.status),
    };
    if (record.parent !== undefined && record.parent !== null && record.parent !== "") {
      entry.parent = String(record.parent);
    }
    out.push(entry);
  });
  return out;
}

/** Depth-first order with depths; unknown parents and cycles fall back to the top level. */
export function todoTree(entries: readonly TodoEntry[]): [TodoEntry, number][] {
  const ids = new Set(entries.map((e) => e.id));
  const children = new Map<string, TodoEntry[]>();
  const roots: TodoEntry[] = [];
  for (const entry of entries) {
    if (entry.parent && entry.parent !== entry.id && ids.has(entry.parent)) {
      const list = children.get(entry.parent) ?? [];
      list.push(entry);
      children.set(entry.parent, list);
    } else {
      roots.push(entry);
    }
  }
  const out: [TodoEntry, number][] = [];
  const seen = new Set<string>();
  const walk = (entry: TodoEntry, depth: number) => {
    if (seen.has(entry.id)) return;
    seen.add(entry.id);
    out.push([entry, depth]);
    for (const child of children.get(entry.id) ?? []) walk(child, depth + 1);
  };
  for (const root of roots) walk(root, 0);
  for (const entry of entries) if (!seen.has(entry.id)) walk(entry, 0); // parent cycles
  return out;
}

const GLYPH: Record<TodoStatus, string> = {
  pending: "○",
  in_progress: "◉",
  completed: "✓",
  cancelled: "✕",
};

export function layoutTodos(
  entries: readonly TodoEntry[],
  collapsed: boolean,
  width: number,
  theme: Theme,
): StyledLine[] {
  if (entries.length === 0) return [];
  const done = entries.filter((e) => e.status === "completed").length;
  const lines: StyledLine[] = [
    [
      { ...colour(theme, "accent"), text: collapsed ? "▸ " : "▾ " },
      { ...colour(theme, "text"), bold: true, text: "plan" },
      { ...colour(theme, "muted"), dim: true, text: ` ${done}/${entries.length} done` },
    ],
  ];
  if (collapsed) return lines;
  for (const [entry, depth] of todoTree(entries)) {
    const lead = `  ${"  ".repeat(Math.min(depth, MAX_DEPTH))}`;
    const glyph = GLYPH[entry.status];
    const tone =
      entry.status === "in_progress"
        ? colour(theme, "accent")
        : entry.status === "completed"
          ? colour(theme, "ok")
          : colour(theme, "muted");
    const body =
      entry.status === "in_progress"
        ? { ...colour(theme, "text"), bold: true }
        : entry.status === "cancelled"
          ? { ...colour(theme, "muted"), strike: true }
          : entry.status === "completed"
            ? { ...colour(theme, "muted") }
            : colour(theme, "text");
    lines.push(
      ...wrapSpans([{ ...body, text: entry.content }], width, {
        first: [{ text: lead }, { ...tone, text: `${glyph} ` }],
        rest: [{ text: `${lead}  ` }],
      }),
    );
  }
  return lines;
}

/** Whether the live plan panel is folded; key routing or a click toggles it. */
export const todoView = createStore<{ collapsed: boolean; toggle(): void }>((set) => ({
  collapsed: false,
  toggle: () => set((s) => ({ collapsed: !s.collapsed })),
}));

export interface TodoPanelProps {
  width: number;
  /** Items to show; defaults to the running turn's todos. */
  todos?: readonly TodoEntry[];
  /** Fixed fold state (transcript copies); the live panel follows `todoView`. */
  collapsed?: boolean;
}

export function TodoPanel({ width, todos, collapsed }: TodoPanelProps) {
  const theme = useTheme();
  const live = useTurn((s) => s.todos);
  const folded = useStore(todoView, (s) => s.collapsed);
  const entries = todos ?? parseTodos(live);
  const lines = layoutTodos(entries, collapsed ?? folded, width, theme);
  if (lines.length === 0) return null;
  return (
    <Box flexDirection="column">
      <Lines lines={lines} />
    </Box>
  );
}
