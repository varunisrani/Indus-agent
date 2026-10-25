// The renderer root (P20-15): providers, the hash router (transitions off, so route changes
// commit at normal priority while a reply streams), the boot overlay and the routes.

import type { ReactNode } from "react";
import { HashRouter, Navigate, useLocation, useNavigate } from "react-router";
import { AmbientHost } from "../ambient/AmbientHost.tsx";
import { BootOverlay, type BootOverlayProps } from "../boot/BootOverlay.tsx";
import { ChatView } from "../chat/ChatView.tsx";
import type { ErrorItem } from "../chat/messageStream.ts";
import { TranscriptStoreProvider } from "../chat/transcriptReact.tsx";
import { ContributedRoute, useContributedPaths } from "../contrib/shell.tsx";
import type { ConnectionStore } from "../gateway/connectionStore.ts";
import {
  ConnectionStoreProvider,
  GatewayConnectionProvider,
  ServerRequestStoreProvider,
} from "../gateway/react.tsx";
import { Hud } from "../hud/Hud.tsx";
import { I18nProvider } from "../i18n/index.tsx";
import { type LooseRpc, PetHost, PetOverlay } from "../pet/PetOverlay.tsx";
import {
  SwitcherProvider,
  useForeground,
  useSwitcher,
  useSwitcherState,
  type WindowSwitcher,
} from "../profiles/switcherReact.tsx";
import { QuickEntry } from "../quick/QuickEntry.tsx";
import { ProfileAppearanceStyle } from "../theme/AppearancePage.tsx";
import { WakeIndicator } from "../wake/WakeIndicator.tsx";
import { hasShell, type WindowQuery } from "./bridge.ts";
import { CapabilitiesRoute } from "./capabilities/CapabilitiesPage.tsx";
import { RootErrorBoundary } from "./ErrorBoundary.tsx";
import { ComingLaterPage, LaterWindow, OverlayCard } from "./pages.tsx";
import {
  backgroundOf,
  classifyPath,
  closeTarget,
  isContributedPage,
  type OverlayId,
  type PageId,
} from "./routes.ts";
import { Shell, type ShellChrome, ShellChromeProvider } from "./Shell.tsx";
import { ThemeProvider } from "./theme.tsx";

/** Slots the chat slices fill (slice C: Composer, CardStack, StatusBar). */
export interface AppSlots {
  composer?: (sessionId: string | null) => ReactNode;
  cards?: (sessionId: string | null) => ReactNode;
  /** The error card's Retry for a session's failed turn. */
  retry?: (sessionId: string, item: ErrorItem) => void;
  statusBar?: ReactNode;
  /** Built overlays (P20-32 Settings); the others keep their "coming later" card. */
  overlays?: Partial<Record<OverlayId, (onClose: () => void) => ReactNode>>;
  /** Built pages (P24-27/28/31 management pages); the others keep their "coming later" state. */
  pages?: Partial<Record<PageId, () => ReactNode>>;
  /** Window-wide layers inside the router (P20-33 onboarding, P20-34 deep links). */
  layers?: ReactNode;
}

export function AppRoutes(props: { slots?: AppSlots }) {
  const location = useLocation();
  const navigate = useNavigate();
  const foreground = useSwitcherState(useSwitcher())?.foreground;
  // the registry's route paths (P24-32): a plugin's or a bundled surface's page
  const contributed = useContributedPaths();
  const view = classifyPath(location.pathname, contributed);
  if (view.kind === "unknown") return <Navigate to="/" replace />;
  if (view.kind === "page" && isContributedPage(view.page)) return <Navigate to="/" replace />;
  const background =
    view.kind === "overlay"
      ? classifyPath(backgroundOf(location.state, contributed) ?? "/", contributed)
      : view;
  const slots = props.slots ?? {};
  const close = () => navigate(closeTarget(location.state, contributed), { replace: true });
  let main: ReactNode;
  const page = background.kind === "page" ? slots.pages?.[background.page] : undefined;
  if (background.kind === "contributed") {
    main = <ContributedRoute path={background.path} />;
  } else if (page !== undefined) {
    main = page(); // a workspace's page (P24-26..31), e.g. Capabilities with its counts
  } else if (background.kind === "page" && background.page === "capabilities") {
    // P24-26 without a workspace slot: the page over REST only (no counts, no desktop plugins)
    main = <CapabilitiesRoute {...(foreground === undefined ? {} : { profile: foreground })} />;
  } else if (background.kind === "page") {
    main = <ComingLaterPage page={background.page} />;
  } else {
    const sessionId = background.kind === "chat" ? background.sessionId : null;
    main = (
      <ChatView
        sessionId={sessionId}
        floatableComposer
        composer={slots.composer?.(sessionId)}
        cards={slots.cards?.(sessionId)}
        {...(sessionId !== null && slots.retry !== undefined
          ? { onRetry: (item: ErrorItem) => slots.retry?.(sessionId, item) }
          : {})}
      />
    );
  }
  return (
    <Shell
      statusBar={slots.statusBar}
      sessionId={background.kind === "chat" ? background.sessionId : null}
    >
      {main}
      {view.kind === "overlay" &&
        (slots.overlays?.[view.overlay]?.(close) ?? (
          <OverlayCard overlay={view.overlay} onClose={close} />
        ))}
      {slots.layers}
    </Shell>
  );
}

export interface RootProps {
  connections: ConnectionStore;
  window: WindowQuery;
  /** The window's profiles (P20-26); the foreground one's stores fill the chat. */
  switcher: WindowSwitcher;
  /** The sidebar's per-profile stores and the pane layout (P20-25, P20-27). */
  chrome?: ShellChrome;
  boot?: Omit<BootOverlayProps, "onReady">;
}

/** The HUD (P20-35) shows the foreground profile's conversation with its chat slots. */
function ForegroundHud(props: { switcher: WindowSwitcher }) {
  const workspace = useForeground(props.switcher);
  if (workspace === null) return null;
  return (
    <TranscriptStoreProvider store={workspace.transcripts}>
      <GatewayConnectionProvider value={workspace.connection}>
        <ServerRequestStoreProvider value={workspace.connection.serverRequests}>
          <Hud slots={workspace.slots} />
        </ServerRequestStoreProvider>
      </GatewayConnectionProvider>
    </TranscriptStoreProvider>
  );
}

/** The foreground profile's stores under the chat surfaces; re-renders on a swap or a wipe. */
function ForegroundRoutes(props: { switcher: WindowSwitcher; boot?: RootProps["boot"] }) {
  const workspace = useForeground(props.switcher);
  if (workspace === null) return null;
  return (
    <TranscriptStoreProvider store={workspace.transcripts}>
      <GatewayConnectionProvider value={workspace.connection}>
        <ServerRequestStoreProvider value={workspace.connection.serverRequests}>
          <AppRoutes slots={workspace.slots} />
          <AmbientHost connection={workspace.connection} transcripts={workspace.transcripts} />
          <PetHost connection={workspace.connection as unknown as LooseRpc} />
          <ProfileAppearanceStyle profile={workspace.profile} />
          <BootOverlay
            {...props.boot}
            onReady={() => props.switcher.foreground().connection.start()}
          />
        </ServerRequestStoreProvider>
      </GatewayConnectionProvider>
    </TranscriptStoreProvider>
  );
}

export function Root(props: RootProps) {
  // the overlays (P24-19 pet, P24-20 wake indicator): no connection, no router
  if (props.window.kind === "pet" || props.window.kind === "wake") {
    return (
      <I18nProvider>
        <ThemeProvider>
          {props.window.kind === "pet" ? <PetOverlay /> : <WakeIndicator />}
        </ThemeProvider>
      </I18nProvider>
    );
  }
  if (props.window.kind === "quick") {
    return (
      <I18nProvider>
        <ThemeProvider>
          <QuickEntry />
        </ThemeProvider>
      </I18nProvider>
    );
  }
  if (props.window.kind === "hud") {
    return (
      <RootErrorBoundary>
        <I18nProvider>
          <ThemeProvider>
            <ConnectionStoreProvider store={props.connections}>
              <SwitcherProvider switcher={props.switcher}>
                <HashRouter useTransitions={false}>
                  <ForegroundHud switcher={props.switcher} />
                </HashRouter>
              </SwitcherProvider>
            </ConnectionStoreProvider>
          </ThemeProvider>
        </I18nProvider>
      </RootErrorBoundary>
    );
  }
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
            <SwitcherProvider switcher={props.switcher}>
              <ShellChromeProvider value={props.chrome ?? null}>
                <HashRouter useTransitions={false}>
                  <ForegroundRoutes
                    switcher={props.switcher}
                    {...(props.boot === undefined ? {} : { boot: props.boot })}
                  />
                </HashRouter>
              </ShellChromeProvider>
            </SwitcherProvider>
          </ConnectionStoreProvider>
        </ThemeProvider>
      </I18nProvider>
    </RootErrorBoundary>
  );
}
