// The renderer root (P20-15): providers, the hash router (transitions off, so route changes
// commit at normal priority while a reply streams), the boot overlay and the routes.

import type { ReactNode } from "react";
import { HashRouter, Navigate, useLocation, useNavigate } from "react-router";
import { BootOverlay, type BootOverlayProps } from "../boot/BootOverlay.tsx";
import { ChatView } from "../chat/ChatView.tsx";
import type { ErrorItem } from "../chat/messageStream.ts";
import { TranscriptStoreProvider } from "../chat/transcriptReact.tsx";
import type { TranscriptStore } from "../chat/transcriptStore.ts";
import type { ConnectionStore } from "../gateway/connectionStore.ts";
import { ConnectionStoreProvider } from "../gateway/react.tsx";
import { I18nProvider } from "../i18n/index.tsx";
import { hasShell, type WindowQuery } from "./bridge.ts";
import { RootErrorBoundary } from "./ErrorBoundary.tsx";
import { ComingLaterPage, LaterWindow, OverlayCard } from "./pages.tsx";
import { backgroundOf, classifyPath, closeTarget } from "./routes.ts";
import { Shell } from "./Shell.tsx";
import { ThemeProvider } from "./theme.tsx";

/** Slots the chat slices fill (slice C: Composer, CardStack, StatusBar). */
export interface AppSlots {
  composer?: (sessionId: string | null) => ReactNode;
  cards?: (sessionId: string | null) => ReactNode;
  /** The error card's Retry for a session's failed turn. */
  retry?: (sessionId: string, item: ErrorItem) => void;
  statusBar?: ReactNode;
}

export function AppRoutes(props: { slots?: AppSlots }) {
  const location = useLocation();
  const navigate = useNavigate();
  const view = classifyPath(location.pathname);
  if (view.kind === "unknown") return <Navigate to="/" replace />;
  const background =
    view.kind === "overlay" ? classifyPath(backgroundOf(location.state) ?? "/") : view;
  const slots = props.slots ?? {};
  let main: ReactNode;
  if (background.kind === "page") {
    main = <ComingLaterPage page={background.page} />;
  } else {
    const sessionId = background.kind === "chat" ? background.sessionId : null;
    main = (
      <ChatView
        sessionId={sessionId}
        composer={slots.composer?.(sessionId)}
        cards={slots.cards?.(sessionId)}
        {...(sessionId !== null && slots.retry !== undefined
          ? { onRetry: (item: ErrorItem) => slots.retry?.(sessionId, item) }
          : {})}
      />
    );
  }
  return (
    <Shell statusBar={slots.statusBar}>
      {main}
      {view.kind === "overlay" && (
        <OverlayCard
          overlay={view.overlay}
          onClose={() => navigate(closeTarget(location.state), { replace: true })}
        />
      )}
    </Shell>
  );
}

export interface RootProps {
  connections: ConnectionStore;
  transcripts: TranscriptStore;
  window: WindowQuery;
  slots?: AppSlots;
  boot?: Omit<BootOverlayProps, "onReady">;
}

export function Root(props: RootProps) {
  if (!hasShell(props.window.kind)) {
    return (
      <I18nProvider>
        <ThemeProvider>
          <LaterWindow />
        </ThemeProvider>
      </I18nProvider>
    );
  }
  return (
    <RootErrorBoundary>
      <I18nProvider>
        <ThemeProvider>
          <ConnectionStoreProvider store={props.connections}>
            <TranscriptStoreProvider store={props.transcripts}>
              <HashRouter useTransitions={false}>
                <AppRoutes {...(props.slots === undefined ? {} : { slots: props.slots })} />
                <BootOverlay {...props.boot} onReady={() => props.connections.primary().start()} />
              </HashRouter>
            </TranscriptStoreProvider>
          </ConnectionStoreProvider>
        </ThemeProvider>
      </I18nProvider>
    </RootErrorBoundary>
  );
}
