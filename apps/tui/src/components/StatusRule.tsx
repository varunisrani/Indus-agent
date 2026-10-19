/**
 * The one-line status rule over (or under) the input: segments from `statusSegments`, a filler
 * rule, and the title badge or folder label at the right edge.
 */
import { Box, useAnimation } from "ink";
import { cellWidth, type Span, type StyledLine, StyledLineView } from "../render/styled.tsx";
import { colour, type ToneName } from "../render/tones.ts";
import { useUi } from "../state/uiStore.ts";
import type { Theme } from "../theme/theme.ts";
import { useTheme } from "../theme/theme.ts";
import {
  type SegmentTone,
  type StatusExtras,
  type StatusLayout,
  statusFromStores,
  statusSegments,
} from "./statusSegments.ts";

const TONES: Record<SegmentTone, ToneName> = {
  text: "text",
  muted: "muted",
  accent: "accent",
  ok: "ok",
  warn: "warn",
  error: "error",
};

/** Paints a status layout into exactly `cols` cells. */
export function statusLine(layout: StatusLayout, cols: number, theme: Theme): StyledLine {
  const border = colour(theme, "border");
  const line: Span[] = [{ ...border, text: "─ " }];
  layout.left.forEach((seg, i) => {
    if (i > 0) line.push({ ...border, text: " │ " });
    line.push({ ...colour(theme, TONES[seg.tone]), bold: seg.id === "yolo", text: seg.text });
  });
  const used = line.reduce((sum, span) => sum + cellWidth(span.text), 0);
  const rightWidth = layout.right ? cellWidth(layout.right.text) : 0;
  const fill = Math.max(0, cols - used - rightWidth - (layout.right ? 1 : 0));
  if (fill > 0) line.push({ ...border, text: ` ${"─".repeat(Math.max(0, fill - 1))}` });
  if (layout.right) {
    line.push({ text: " " });
    line.push({
      ...colour(theme, TONES[layout.right.tone]),
      bold: layout.right.bold,
      text: layout.right.text,
    });
  }
  return line;
}

export interface StatusRuleProps extends Omit<StatusExtras, "frame"> {
  /** Where this instance sits; it renders only when the setting matches. */
  at: "top" | "bottom";
}

export function StatusRule({ at, cols, ...extras }: StatusRuleProps) {
  const theme = useTheme();
  const position = useUi((s) => s.statusBar);
  const busy = useUi((s) => s.busy);
  const statusText = useUi((s) => s.statusText);
  const info = useUi((s) => s.info);
  const usage = useUi((s) => s.usage);
  const indicatorStyle = useUi((s) => s.indicatorStyle);
  const { frame } = useAnimation({ interval: 120, isActive: busy && position === at });
  if (position !== at) return null;
  const input = statusFromStores(
    { busy, statusText, info, usage, indicatorStyle },
    { ...extras, cols, frame },
  );
  return (
    <Box height={1} flexShrink={0}>
      <StyledLineView line={statusLine(statusSegments(input), cols, theme)} />
    </Box>
  );
}
