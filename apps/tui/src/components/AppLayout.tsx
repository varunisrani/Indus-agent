/**
 * The full-screen shell, top to bottom: transcript (takes the free height), prompt zone (the one
 * blocking card), composer pane (queued preview, status rule at its top or bottom, completion
 * list, input). Heights of the fixed parts are known, so the transcript gets an exact viewport.
 */
import { Box } from "ink";
import type { ReactNode } from "react";
import { oneLine, type StyledLine, StyledLineView } from "../render/styled.tsx";
import { colour } from "../render/tones.ts";
import { useUi } from "../state/uiStore.ts";
import { useTheme } from "../theme/theme.ts";
import type { TranscriptItem } from "./MessageRow.tsx";
import { StatusRule } from "./StatusRule.tsx";
import type { StatusExtras } from "./statusSegments.ts";
import { Transcript } from "./Transcript.tsx";

export const QUEUE_PREVIEW = 3;

/** Lines for the queued-input preview: up to three entries and a "+N more" tail. */
export function queuedPreview(queued: readonly string[], cols: number): string[] {
  if (queued.length === 0) return [];
  const room = Math.max(8, cols - 12);
  const shown = queued
    .slice(0, QUEUE_PREVIEW)
    .map((text, i) => `${i === 0 ? "queued" : "      "} ${i + 1}. ${oneLine(text, room)}`);
  if (queued.length > QUEUE_PREVIEW) shown.push(`       +${queued.length - QUEUE_PREVIEW} more`);
  return shown;
}

export interface AppLayoutProps {
  cols: number;
  rows: number;
  items: readonly TranscriptItem[];
  /** The active blocking card (approval, clarify, …) and the rows it takes. */
  prompt?: ReactNode;
  promptHeight?: number;
  /** The composer input and the rows it takes (default 1). */
  input?: ReactNode;
  inputHeight?: number;
  /** A floating completion list shown above the input, and its rows. */
  completion?: ReactNode;
  completionHeight?: number;
  queued?: readonly string[];
  hyperlinks?: boolean;
  status?: Omit<StatusExtras, "cols">;
}

export function AppLayout(props: AppLayoutProps) {
  const theme = useTheme();
  const statusBar = useUi((s) => s.statusBar);
  const cols = Math.max(1, Math.floor(props.cols));
  const queued = queuedPreview(props.queued ?? [], cols);
  const promptHeight = props.prompt ? Math.max(0, props.promptHeight ?? 0) : 0;
  const inputHeight = Math.max(1, props.inputHeight ?? 1);
  const completionHeight = props.completion ? Math.max(0, props.completionHeight ?? 0) : 0;
  const statusHeight = statusBar === "off" ? 0 : 1;
  const composerHeight = queued.length + statusHeight + completionHeight + inputHeight;
  const transcriptHeight = Math.max(0, props.rows - promptHeight - composerHeight);
  const muted = colour(theme, "muted");
  const status = { cols, ...(props.status ?? {}) };

  return (
    <Box flexDirection="column" width={cols} height={Math.max(1, props.rows)}>
      <Transcript
        items={props.items}
        width={cols}
        height={transcriptHeight}
        hyperlinks={props.hyperlinks ?? false}
      />
      {props.prompt ? (
        <Box flexDirection="column" height={promptHeight} flexShrink={0}>
          {props.prompt}
        </Box>
      ) : null}
      <Box flexDirection="column" flexShrink={0}>
        {queued.map((text, i) => {
          const line: StyledLine = [{ ...muted, dim: i > 0, text }];
          // biome-ignore lint/suspicious/noArrayIndexKey: preview lines are positional
          return <StyledLineView key={i} line={line} />;
        })}
        <StatusRule at="top" {...status} />
        {props.completion ? (
          <Box flexDirection="column" height={completionHeight}>
            {props.completion}
          </Box>
        ) : null}
        <Box flexDirection="column" height={inputHeight}>
          {props.input ?? (
            <StyledLineView line={[{ ...colour(theme, "accent"), bold: true, text: "› " }]} />
          )}
        </Box>
        <StatusRule at="bottom" {...status} />
      </Box>
    </Box>
  );
}
