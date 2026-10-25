// The whole screen (P12-27): transcript (settled messages plus the live turn), the open card or
// picker, the queued preview, the status rule, the completion list and the composer. Everything
// non-visual lives in `AppRuntime`; this component only reads stores and mounts the key router.

import { Box, Text, useStdout } from "ink";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { CardView } from "../cards/CardView.tsx";
import { AppLayout } from "../components/AppLayout.tsx";
import type { TranscriptItem, TranscriptRole } from "../components/MessageRow.tsx";
import { CompletionList } from "../composer/CompletionList.tsx";
import { useComposer } from "../composer/composerStore.ts";
import { Editor } from "../composer/Editor.tsx";
import { useKeyRouter } from "../input/routeKeys.ts";
import { SessionSwitcher } from "../pickers/SessionSwitcher.tsx";
import { VISIBLE_ROWS } from "../pickers/sessionSwitcherState.ts";
import { type Card, overlayStore, useOverlay } from "../state/overlayStore.ts";
import { type TranscriptMessage, useTranscript } from "../state/transcriptStore.ts";
import { uiStore, useUi } from "../state/uiStore.ts";
import { useQueueItems } from "../submit/queue.ts";
import { createResizeCoalescer } from "../terminal/resize.ts";
import type { AppRuntime } from "./appRuntime.ts";

export const MAX_INPUT_ROWS = 8;
export const COMPLETION_SHOWN = 8;
export const PLACEHOLDER = "Ask anything · / for commands · Ctrl+X sessions · Ctrl+O model";

const items = new WeakMap<TranscriptMessage, TranscriptItem>();

/** A settled message as a transcript row (cached per message object, so row layouts are reused). */
export function toItem(message: TranscriptMessage): TranscriptItem {
  const hit = items.get(message);
  if (hit) return hit;
  const role: TranscriptRole =
    message.kind === "diff" ? "diff" : message.kind === "notice" ? "notice" : message.role;
  const item: TranscriptItem = {
    id: message.id,
    role,
    text: message.text,
    createdAt: message.createdAt,
    ...(message.tools ? { tools: message.tools } : {}),
    ...(message.reasoning ? { reasoning: message.reasoning } : {}),
  };
  items.set(message, item);
  return item;
}

/** Rows a card takes: a fair estimate per kind, never more than the screen minus the composer. */
export function cardHeight(card: Card | null, rows: number): number {
  if (card === null) return 0;
  const room = Math.max(3, rows - 3);
  const wanted: Record<Card["kind"], number> = {
    approval: 18,
    confirm: 7,
    pager: room,
    clarify: 16,
    sudo: 7,
    secret: 7,
    modelPicker: 20,
    sessionPicker: VISIBLE_ROWS + 6,
  };
  return Math.min(room, wanted[card.kind]);
}

/** Terminal size, re-read at most every 32 ms during a resize storm (R05.16). */
function useTerminalSize(fixed?: { columns: number; rows: number }): {
  columns: number;
  rows: number;
} {
  const { stdout } = useStdout();
  const tty = stdout as NodeJS.WriteStream;
  const read = () => ({ columns: tty.columns || 80, rows: tty.rows || 24 });
  const [size, setSize] = useState(read);
  // biome-ignore lint/correctness/useExhaustiveDependencies: one listener per stream
  useEffect(() => {
    if (fixed) return;
    const coalescer = createResizeCoalescer(() => setSize(read()));
    stdout.on("resize", coalescer.schedule);
    return () => {
      coalescer.cancel();
      stdout.off("resize", coalescer.schedule);
    };
  }, [stdout]);
  return fixed ?? size;
}

export interface AppProps {
  runtime: AppRuntime;
  /** OSC 8 links in the transcript. */
  hyperlinks?: boolean;
  /** A fixed size (tests); otherwise the terminal's. */
  size?: { columns: number; rows: number };
}

export function App({ runtime, hyperlinks = false, size }: AppProps): ReactNode {
  const { columns, rows } = useTerminalSize(size);
  const messages = useTranscript((s) => s.messages);
  const info = useUi((s) => s.info);
  const sid = useUi((s) => s.sid);
  const card = useOverlay((s) => s.card);
  const queued = useQueueItems(runtime.queue);
  const composer = useComposer(runtime.composer);

  const list = useMemo(() => {
    const out = messages.map(toItem);
    if (info !== null) {
      out.unshift({ id: `banner:${sid ?? ""}`, role: "banner", text: "", info });
    }
    return out;
  }, [messages, info, sid]);

  const promptHeight = cardHeight(card, rows);
  useKeyRouter({
    actions: runtime.keyActions,
    ui: uiStore,
    card: () => overlayStore.getState().card,
    queue: runtime.queue,
    now: () => Date.now(),
    viewportRows: () => Math.max(1, rows - promptHeight - 3),
  });

  const ownsKeys = card?.ownsInput === true;
  const close = (id: string): void => overlayStore.getState().close(id);
  const prompt =
    card === null ? null : card.kind === "sessionPicker" ? (
      <SessionSwitcher
        key={card.id}
        ops={runtime.switcherOps}
        columns={columns}
        onClose={() => close(card.id)}
        systemLine={(text) => runtime.system(text)}
      />
    ) : (
      <CardView
        card={card}
        columns={columns}
        rows={promptHeight}
        rpc={runtime.rpc}
        close={close}
        systemLine={(text) => runtime.system(text)}
      />
    );

  const lines = composer.continuation.length + composer.text.split("\n").length;
  const inputHeight = ownsKeys ? 1 : Math.min(MAX_INPUT_ROWS, Math.max(1, lines));
  const completionRows = ownsKeys
    ? 0
    : Math.min(COMPLETION_SHOWN, composer.completion.items.length);

  return (
    <AppLayout
      cols={columns}
      rows={rows}
      items={list}
      prompt={prompt}
      promptHeight={promptHeight}
      input={
        ownsKeys ? (
          <Box>
            <Text> </Text>
          </Box>
        ) : (
          <Editor
            composer={runtime.composer}
            ownKeys={false}
            placeholder={PLACEHOLDER}
            width={columns - 2}
          />
        )
      }
      inputHeight={inputHeight}
      {...(completionRows > 0
        ? {
            completion: <CompletionList composer={runtime.composer} rows={completionRows} />,
            completionHeight: completionRows,
          }
        : {})}
      queued={queued.map((item) => item.display)}
      hyperlinks={hyperlinks}
      status={{ fields: runtime.statusFields, home: process.env.HOME ?? "" }}
    />
  );
}
