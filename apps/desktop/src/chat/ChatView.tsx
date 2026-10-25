// The chat pane: the session's transcript (or the new-chat empty state), the card stack host and
// the composer. Slice C's `CardStack` and `Composer` drop into the `cards` / `composer` slots.

import { type ReactNode, useEffect } from "react";
import { useLocation, useNavigate } from "react-router";
import { desktopBridge } from "../app/bridge.ts";
import { NEW_CHAT_PATH, overlayPath, overlayStateFrom } from "../app/routes.ts";
import type { GatewayConnection } from "../gateway/connectionStore.ts";
import { useConnectionState, useMaybeConnectionStore } from "../gateway/react.tsx";
import { useT } from "../i18n/index.tsx";
import { useDisplayPrefs } from "../settings/displayPrefs.ts";
import { Wordmark } from "../ui/components.tsx";
import { ComposerDock } from "./ComposerDock.tsx";
import type { FailureAction } from "./failure.ts";
import type { ErrorItem } from "./messageStream.ts";
import { Transcript } from "./Transcript.tsx";
import { useTranscriptStore } from "./transcriptReact.tsx";

export interface ChatViewProps {
  sessionId: string | null;
  composer?: ReactNode;
  cards?: ReactNode;
  /** The error card's Retry (slice C sends the failed turn's message again). */
  onRetry?: (item: ErrorItem) => void;
  onFailureAction?: (action: FailureAction, item: ErrorItem) => void;
  /** The main window's chat: the Floating composer setting applies (W56-G1); the HUD's never. */
  floatableComposer?: boolean;
}

/** A note while the watchdog finds the running turn silent (never "finished"). */
function StalledNote(props: { connection: GatewayConnection; sessionId: string }) {
  const t = useT();
  const state = useConnectionState(props.connection);
  if (!state.stalledSessions.includes(props.sessionId)) return null;
  return (
    <p className="msg-note chat-stalled" role="status">
      {t("transcript.stalled")}
    </p>
  );
}

export function ChatView(props: ChatViewProps) {
  const { sessionId } = props;
  const t = useT();
  const store = useTranscriptStore();
  const connection = useMaybeConnectionStore()?.primary() ?? null;
  const navigate = useNavigate();
  const location = useLocation();
  const display = useDisplayPrefs();
  // The actions this pane can take itself; "retry" goes to the chat slice's `onRetry`.
  const defaultAction = (action: FailureAction, item: ErrorItem): void => {
    switch (action) {
      case "reconnect":
        if (connection !== null) void connection.probeLiveness();
        return;
      case "open-logs":
        void desktopBridge()
          ?.logs.reveal()
          .catch(() => undefined);
        return;
      case "new-session":
        navigate(NEW_CHAT_PATH);
        return;
      case "open-settings":
      case "sign-in":
      case "choose-model":
        navigate(overlayPath("settings"), {
          state: overlayStateFrom(location.pathname, location.state),
        });
        return;
      case "retry":
        props.onRetry?.(item);
        return;
    }
  };

  useEffect(() => {
    if (sessionId === null) return;
    void store.open(sessionId);
    return () => store.close(sessionId);
  }, [store, sessionId]);

  return (
    <section className="chat" aria-label={t("chat.label")}>
      {sessionId === null ? (
        <div className="chat-empty">
          {display.introSplash && (
            <Wordmark text={t("chat.empty.title")} caption={t("chat.empty.body")} />
          )}
          {props.cards}
        </div>
      ) : (
        <>
          {display.chatBackdrop && (
            // W55-F3 chat backdrop: our wordmark, faint, behind the conversation
            <div className="chat-backdrop" aria-hidden="true">
              {t("chat.empty.title")}
            </div>
          )}
          <Transcript
            key={sessionId}
            sessionId={sessionId}
            footer={props.cards}
            onFailureAction={props.onFailureAction ?? defaultAction}
            canRetry={props.onRetry !== undefined}
          />
        </>
      )}
      {connection !== null && sessionId !== null && (
        <StalledNote connection={connection} sessionId={sessionId} />
      )}
      {props.composer !== undefined && (
        <ComposerDock floatable={props.floatableComposer === true && display.floatingComposer}>
          {props.composer}
        </ComposerDock>
      )}
    </section>
  );
}
