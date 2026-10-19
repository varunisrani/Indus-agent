// Pager for long slash output: one screen at a time, closes after the last page.

import { Box, Text, useInput } from "ink";
import { type ReactNode, useState } from "react";
import { isCtrl, type KeyFlags } from "./keys.ts";
import { cleanText } from "./text.ts";
import { useTones } from "./tones.ts";

/** Output longer than this (characters, or non-empty lines) opens the pager instead of a line. */
export const LONG_OUTPUT_CHARS = 180;
export const LONG_OUTPUT_LINES = 2;

export function isLongOutput(text: string): boolean {
  if (text.length > LONG_OUTPUT_CHARS) return true;
  return text.split("\n").filter((line) => line.trim() !== "").length > LONG_OUTPUT_LINES;
}

/** Rows of text per page for a terminal `rows` high. */
export const pageSizeFor = (rows: number): number => Math.max(5, rows - 6);

export const pagerLines = (text: string): string[] =>
  cleanText(text).replace(/\n+$/, "").split("\n");

export type PagerStep = { type: "close" } | { type: "move"; offset: number } | { type: "none" };

/** One key in the pager; `offset` is the first visible line. */
export function pagerStep(
  input: string,
  key: KeyFlags,
  offset: number,
  total: number,
  pageSize: number,
): PagerStep {
  if (key.escape || isCtrl(input, key, "c") || input === "q") return { type: "close" };
  const last = Math.max(0, total - pageSize);
  const to = (next: number): PagerStep => {
    const clamped = Math.min(last, Math.max(0, next));
    return clamped === offset ? { type: "none" } : { type: "move", offset: clamped };
  };
  if (key.upArrow || input === "k") return to(offset - 1);
  if (key.downArrow || input === "j") return to(offset + 1);
  if (key.pageUp || input === "b") return to(offset - pageSize);
  if (input === "g") return to(0);
  if (input === "G") return to(last);
  if (key.pageDown || key.return || input === " ") {
    return offset >= last ? { type: "close" } : to(offset + pageSize);
  }
  return { type: "none" };
}

export interface PagerProps {
  title?: string;
  lines: readonly string[];
  rows: number;
  onClose(): void;
  active?: boolean;
}

export function Pager({ title, lines, rows, onClose, active = true }: PagerProps): ReactNode {
  const tone = useTones();
  const [offset, setOffset] = useState(0);
  const pageSize = pageSizeFor(rows);
  useInput(
    (input, key) => {
      const step = pagerStep(input, key, offset, lines.length, pageSize);
      if (step.type === "close") onClose();
      else if (step.type === "move") setOffset(step.offset);
    },
    { isActive: active },
  );
  const end = Math.min(lines.length, offset + pageSize);
  const atEnd = end >= lines.length;
  return (
    <Box borderStyle="round" borderColor={tone.border} flexDirection="column" paddingX={1}>
      {title ? (
        <Text bold color={tone.accent}>
          {title}
        </Text>
      ) : null}
      {lines.slice(offset, end).map((line, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional within the page
        <Text key={offset + i}>{line === "" ? " " : line}</Text>
      ))}
      <Text color={tone.muted}>
        {atEnd
          ? `end of ${lines.length} lines · ↑↓ j/k · b back · g top · q close`
          : `lines ${offset + 1}-${end} of ${lines.length} · Space/Enter next page · b back · g/G top/bottom · q close`}
      </Text>
    </Box>
  );
}
