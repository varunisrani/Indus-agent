// The chat slice's surfaces in the shell's slots (merge of P20-15..18 with P20-19..24): the
// composer under the transcript, the card stack after its rows, the status bar in the shell, and
// the error card's Retry.

import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import { ChatActions, type Request, sharedActions } from "../chat/busyActions.ts";
import { CardStack } from "../chat/cards/CardStack.tsx";
import { Composer } from "../chat/composer/Composer.tsx";
import { InflightJournal } from "../chat/inflightJournal.ts";
import { sendShown } from "../chat/localEcho.ts";
import type { ErrorItem, TranscriptItem } from "../chat/messageStream.ts";
import { useTranscriptStore } from "../chat/transcriptReact.tsx";
import type { TranscriptStore } from "../chat/transcriptStore.ts";
import type { ChatConnection } from "../gateway/connectionStore.ts";
import type { AppSlots } from "./App.tsx";
import { backgroundOf, classifyPath, NEW_CHAT_PATH, sessionPath } from "./routes.ts";
import { StatusBar } from "./StatusBar.tsx";

/** System lines (slash output) kept above the composer. */
const SYSTEM_LINES = 5;

/** The message of a failed turn: its own user row, else the last one before the error card. */
export function failedPrompt(items: readonly TranscriptItem[], failed: ErrorItem): string | null {
  let before: string | null = null;
  for (const item of items) {
    if (item.key === failed.key) break;
    if (item.kind !== "user") continue;
    if (item.turnId !== null && item.turnId === failed.turnId) return item.text;
    before = item.text;
  }
  return before;
}

function ComposerSlot(props: {
  sessionId: string | null;
  profile: string | null;
  inflight: InflightJournal;
}) {
  const navigate = useNavigate();
  const transcripts = useTranscriptStore();
  const [lines, setLines] = useState<readonly string[]>([]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: a new conversation starts clean
  useEffect(() => setLines([]), [props.sessionId]);
  return (
    <>
      {lines.length > 0 && (
        <output className="chat-system-lines" aria-live="polite">
          {lines.map((line, index) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: lines are append-only
            <pre key={index} className="msg-note">
              {line}
            </pre>
          ))}
        </output>
      )}
      <Composer
        sessionId={props.sessionId}
        profile={props.profile}
        inflight={props.inflight}
        localRows={transcripts}
        onSessionCreated={(id) => navigate(sessionPath(id))}
        onSessionGone={() => navigate(NEW_CHAT_PATH, { replace: true })}
        onNewChat={() => navigate(NEW_CHAT_PATH)}
        onReasoningDisplay={(show) => transcripts.setShowReasoning(show)}
        onSystemLine={(line) => setLines((now) => [...now.slice(1 - SYSTEM_LINES), line])}
      />
    </>
  );
}

function StatusBarSlot() {
  const location = useLocation();
  const view = classifyPath(location.pathname);
  const shown = view.kind === "overlay" ? classifyPath(backgroundOf(location.state) ?? "/") : view;
  return <StatusBar sessionId={shown.kind === "chat" ? shown.sessionId : null} />;
}

export interface ChatSlotDeps {
  /** The window's connection (the primary one). */
  connection: ChatConnection;
  transcripts: TranscriptStore;
  profile: string | null;
  inflight?: InflightJournal;
}

/** The slots `Root` mounts for a main window. */
export function chatSlots(deps: ChatSlotDeps): AppSlots {
  const inflight = deps.inflight ?? new InflightJournal();
  return {
    composer: (sessionId) => (
      <ComposerSlot sessionId={sessionId} profile={deps.profile} inflight={inflight} />
    ),
    cards: (sessionId) => (sessionId === null ? null : <CardStack sessionId={sessionId} />),
    statusBar: <StatusBarSlot />,
    retry: (sessionId, item) => {
      const text = failedPrompt(deps.transcripts.get(sessionId).items, item);
      if (text === null) return;
      // A failed turn ended for good: its message goes again as a new input (new request id).
      const actions = sharedActions(
        deps.connection,
        sessionId,
        () =>
          new ChatActions({
            sessionId,
            request: deps.connection.request.bind(deps.connection) as Request,
            inflight,
          }),
      );
      void sendShown(actions, deps.transcripts, text, text);
    },
  };
}
