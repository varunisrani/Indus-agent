// Thin Ink view over the composer (P12-16). It draws the continuation lines, the live text with
// an inverse cursor cell or a highlighted selection, and a muted placeholder. When `ownKeys` is
// on (standalone use and tests) it also reads keys itself; inside the app the global key router
// (input/routeKeys.ts) owns the keyboard and calls into the composer instead.

import { Box, Text, useInput, usePaste } from "ink";
import type { ReactNode } from "react";
import { fromInk } from "../input/keymap.ts";
import { useTheme } from "../theme/theme.ts";
import { type Composer, composer as sharedComposer, useComposer } from "./composerStore.ts";
import { boundaries, type KeyOutcome, offsetAt } from "./editorModel.ts";
import { cleanPaste } from "./paste.ts";

export interface EditorMouse {
  press(row: number, col: number, now: number): void;
  drag(row: number, col: number): void;
  release(): void;
}

export interface EditorProps {
  composer?: Composer;
  placeholder?: string;
  /** Draw the cursor and selection. */
  focus?: boolean;
  /** Read keys and pastes directly (off when the app's key router is mounted). */
  ownKeys?: boolean;
  /** Width the text wraps at, for mouse hit-testing. */
  width?: number;
  prompt?: string;
  onSubmit?: (text: string) => void;
  onEdge?: (direction: "up" | "down") => void;
  onPaste?: (text: string) => void;
  /** Receives the mouse entry points once mounted. */
  mouseRef?: { current: EditorMouse | null };
}

interface Painted {
  readonly cursor: string;
  readonly selection: string;
}

function paintText(
  text: string,
  cursor: number,
  span: { start: number; end: number } | null,
  colors: Painted,
  focus: boolean,
): ReactNode[] {
  if (!focus) return [text];
  if (span !== null) {
    return [
      text.slice(0, span.start),
      <Text key="sel" inverse {...(colors.selection ? { backgroundColor: colors.selection } : {})}>
        {text.slice(span.start, span.end)}
      </Text>,
      text.slice(span.end),
    ];
  }
  const stops = boundaries(text);
  const next = stops.find((stop) => stop > cursor) ?? text.length;
  const under = text.slice(cursor, next);
  const shown = under === "" || under === "\n" ? " " : under;
  return [
    text.slice(0, cursor),
    <Text key="cur" inverse {...(colors.cursor ? { color: colors.cursor } : {})}>
      {shown}
    </Text>,
    under === "\n" ? `\n${text.slice(next)}` : text.slice(next),
  ];
}

export function Editor(props: EditorProps): ReactNode {
  const target = useComposer(props.composer ?? sharedComposer);
  const theme = useTheme();
  const focus = props.focus ?? true;
  const ownKeys = (props.ownKeys ?? true) && focus;
  const muted = theme.color["fg-muted"];
  const colors: Painted = {
    cursor: theme.color.accent ?? "",
    selection: theme.color.selection ?? "",
  };

  useInput(
    (input, key) => {
      const outcome: KeyOutcome = target.key(fromInk(input, key));
      if (outcome === "submit") props.onSubmit?.(target.text);
      else if (outcome === "edge-up") props.onEdge?.("up");
      else if (outcome === "edge-down") props.onEdge?.("down");
    },
    { isActive: ownKeys },
  );

  usePaste(
    (text) => {
      if (props.onPaste) props.onPaste(text);
      else target.insertText(cleanPaste(text));
    },
    { isActive: ownKeys },
  );

  if (props.mouseRef) {
    const width = props.width ?? 80;
    props.mouseRef.current = {
      press: (row, col, now) => {
        target.editor.press(offsetAt(target.text, row, col, width), now);
        target.notify(false);
      },
      drag: (row, col) => {
        target.editor.drag(offsetAt(target.text, row, col, width));
        target.notify(false);
      },
      release: () => target.editor.release(),
    };
  }

  const { text, cursor } = target.editor;
  const prompt = props.prompt ?? "› ";
  const empty = text === "" && target.continuation.length === 0;
  const placeholder = props.placeholder ?? "";

  return (
    <Box flexDirection="column">
      {target.continuation.map((line, index) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: continuation lines have no identity
        <Text key={index} {...(muted ? { color: muted } : { dimColor: true })}>
          {index === 0 ? prompt : " ".repeat(prompt.length)}
          {line}
        </Text>
      ))}
      <Box>
        <Text>{target.continuation.length === 0 ? prompt : " ".repeat(prompt.length)}</Text>
        {empty && placeholder !== "" ? (
          <Text>
            {focus ? <Text inverse>{placeholder.slice(0, 1)}</Text> : placeholder.slice(0, 1)}
            <Text {...(muted ? { color: muted } : { dimColor: true })}>{placeholder.slice(1)}</Text>
          </Text>
        ) : (
          <Text>{paintText(text, cursor, target.editor.range(), colors, focus)}</Text>
        )}
      </Box>
    </Box>
  );
}
