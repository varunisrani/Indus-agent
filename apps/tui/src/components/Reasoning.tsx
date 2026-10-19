/**
 * The model's reasoning for the running turn. Expanded shows the text (live-capped); collapsed
 * shows a header and, while reasoning streams, a one-line preview of its latest words; hidden
 * shows nothing.
 */
import { Box } from "ink";
import { useStore } from "zustand";
import { liveTail } from "../render/streamingMarkdown.tsx";
import { Lines, type StyledLine, wrapSpans } from "../render/styled.tsx";
import { colour } from "../render/tones.ts";
import { useTurn } from "../state/turnStore.ts";
import { useUi } from "../state/uiStore.ts";
import type { Theme } from "../theme/theme.ts";
import { useTheme } from "../theme/theme.ts";
import { type DetailMode, detailsStore, sectionMode } from "./details.ts";

export const THINKING_PREVIEW_CHARS = 160;

/** The latest `max` characters of reasoning on one line, led by `…` when cut. */
export function thinkingPreview(text: string, max = THINKING_PREVIEW_CHARS): string {
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  return `…${flat.slice(flat.length - (max - 1)).trimStart()}`;
}

export function layoutReasoning(
  text: string,
  mode: DetailMode,
  active: boolean,
  width: number,
  theme: Theme,
): StyledLine[] {
  if (mode === "hidden" || (!text.trim() && !active)) return [];
  const open = mode === "expanded";
  const head: StyledLine = [
    { ...colour(theme, "accent"), text: open ? "▾ " : "▸ " },
    {
      ...colour(theme, active ? "text" : "muted"),
      bold: active,
      text: active ? "thinking…" : "thought",
    },
  ];
  const body = { ...colour(theme, "muted"), italic: true };
  const pad: StyledLine = [{ text: "  " }];
  if (!open) {
    const preview = active ? thinkingPreview(text) : "";
    if (!preview) return [head];
    return [
      head,
      ...wrapSpans([{ ...body, dim: true, text: preview }], width, { first: pad, rest: pad }).slice(
        0,
        2,
      ),
    ];
  }
  const tail = liveTail(text.trim());
  const lines: StyledLine[] = [head];
  if (tail.omittedChars > 0)
    lines.push([...pad, { ...body, dim: true, text: `… ${tail.omittedLines} earlier lines` }]);
  if (tail.text)
    lines.push(...wrapSpans([{ ...body, text: tail.text }], width, { first: pad, rest: pad }));
  return lines;
}

export interface ReasoningProps {
  width: number;
  /** Reasoning is still streaming. */
  active?: boolean;
}

export function Reasoning({ width, active = false }: ReasoningProps) {
  const theme = useTheme();
  const text = useTurn((s) => s.reasoning);
  const global = useUi((s) => s.detailsMode);
  const overrides = useStore(detailsStore, (s) => s.overrides);
  const fromCommand = useStore(detailsStore, (s) => s.globalFromCommand);
  const lines = layoutReasoning(
    text,
    sectionMode("thinking", global, overrides, fromCommand),
    active,
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
