// The window shell (P20-15; P20-25..27): the profile rail, the left sidebar (new chat, pages,
// the sessions sidebar, overlay entries, theme), the pane shell in the middle (the route's
// content is its chat pane) and the bottom status bar. The interface mode (Advanced / Simple)
// decides which surfaces show; the tab and window shortcuts are handled here.

import { Monitor, Moon, PenSquare, Sun } from "lucide-react";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";
import { NavLink, useLocation, useNavigate } from "react-router";
import { draftOf, FRESH, setDraft, windowDrafts } from "../chat/composer/goneSession.ts";
import {
  CommandPalette,
  ContributedTitleBar,
  type PaletteEntry,
  useContributedKeybinds,
} from "../contrib/areas.tsx";
import { ContributedNav } from "../contrib/shell.tsx";
import type { GatewayConnection } from "../gateway/connectionStore.ts";
import { useConnectionState, useMaybeConnectionStore } from "../gateway/react.tsx";
import { type MessageKey, useT } from "../i18n/index.tsx";
import { previewTargets } from "../panes/d2Panes.tsx";
import { LayoutStore } from "../panes/layoutStore.ts";
import { focusedPane, PaneShell, paneRegistry, useLayout } from "../panes/PaneShell.tsx";
import { CHAT_PANE, isHidden, zones } from "../panes/paneTree.ts";
import { surfaceShown } from "../panes/presets.ts";
import type { PreviewTarget } from "../panes/preview/target.ts";
import { type PaneAction, usePaneShortcuts } from "../panes/shortcuts.ts";
import { ProfileRail, type ProfilesApi, useProfileNames } from "../profiles/ProfileRail.tsx";
import { useSwitcher, useSwitcherState } from "../profiles/switcherReact.tsx";
import { freshDraftFolder } from "../sidebar/projects.ts";
import { beginNewChat, Sidebar, SidebarModel } from "../sidebar/Sidebar.tsx";
import { desktopBridge, launchFacts } from "./bridge.ts";
import { OVERLAY_ICONS, OVERLAY_TEXT, PAGE_ICONS, PAGE_TEXT } from "./pages.tsx";
import {
  isContributedPage,
  NEW_CHAT_PATH,
  OVERLAYS,
  overlayPath,
  overlayStateFrom,
  PAGES,
  pagePath,
  sessionPath,
} from "./routes.ts";
import { type ThemeMode, useTheme } from "./theme.tsx";

// -- chrome: the window's sidebar model and pane layout ------------------------------------------

export interface ShellChrome {
  readonly sidebar: SidebarModel;
  readonly layout: LayoutStore;
}

const ChromeContext = createContext<ShellChrome | null>(null);

export function ShellChromeProvider(props: { value: ShellChrome | null; children: ReactNode }) {
  return <ChromeContext.Provider value={props.value}>{props.children}</ChromeContext.Provider>;
}

/** The provided chrome, or null (Settings reads the sidebar's per-profile project stores). */
export function useProvidedShellChrome(): ShellChrome | null {
  return useContext(ChromeContext);
}

/** The provided chrome, else one made for this shell (previews and tests). */
function useChrome(): ShellChrome {
  const provided = useContext(ChromeContext);
  const [own] = useState<ShellChrome | null>(() =>
    provided === null
      ? {
          sidebar: new SidebarModel(),
          layout: new LayoutStore({ available: () => paneRegistry.ids() }),
        }
      : null,
  );
  return provided ?? (own as ShellChrome);
}

// -- small pieces --------------------------------------------------------------------------------

const THEME_OPTIONS: ReadonlyArray<{ mode: ThemeMode; key: MessageKey; icon: ReactNode }> = [
  { mode: "light", key: "theme.light", icon: <Sun aria-hidden="true" size={14} /> },
  { mode: "dark", key: "theme.dark", icon: <Moon aria-hidden="true" size={14} /> },
  { mode: "system", key: "theme.system", icon: <Monitor aria-hidden="true" size={14} /> },
];

const PHASE_KEYS: Readonly<Record<string, MessageKey>> = {
  idle: "connection.idle",
  connecting: "connection.connecting",
  open: "connection.open",
  reconnecting: "connection.reconnecting",
  failed: "connection.failed",
  closed: "connection.closed",
};

function ThemeControl() {
  const t = useT();
  const { mode, setMode } = useTheme();
  return (
    <div className="segmented" role="radiogroup" aria-label={t("theme.label")}>
      {THEME_OPTIONS.map((option) => (
        // biome-ignore lint/a11y/useSemanticElements: a segmented control, not a native radio
        <button
          key={option.mode}
          type="button"
          role="radio"
          aria-checked={mode === option.mode}
          aria-label={t(option.key)}
          className="segmented-option"
          onClick={() => setMode(option.mode)}
        >
          {option.icon}
        </button>
      ))}
    </div>
  );
}

function LeftSidebar(props: {
  chrome: ShellChrome;
  profile: string;
  sessionId: string | null;
  connection: GatewayConnection | null;
  switchError: string | null;
}) {
  const t = useT();
  const navigate = useNavigate();
  const location = useLocation();
  const newChat = () => {
    beginNewChat(props.chrome.sidebar, props.profile, null);
    navigate(NEW_CHAT_PATH);
  };
  // The list's first load may run before the backend is up: load again each time it is ready.
  const ready = useOptionalConnectionState(props.connection)?.ready ?? false;
  const sessions = props.chrome.sidebar.sessions(props.profile);
  useEffect(() => {
    if (ready) void sessions.refresh();
  }, [ready, sessions]);
  return (
    <aside className="sidebar">
      <nav aria-label={t("nav.label")}>
        <button type="button" className="nav-item nav-primary" onClick={newChat}>
          <PenSquare aria-hidden="true" size={16} />
          <span>{t("nav.newChat")}</span>
        </button>
        <ul className="nav-list">
          {PAGES.filter((page) => !isContributedPage(page)).map((page) => (
            <li key={page}>
              <NavLink to={pagePath(page)} className="nav-item">
                {PAGE_ICONS[page]}
                <span>{t(PAGE_TEXT[page].title)}</span>
              </NavLink>
            </li>
          ))}
          {/* the registry's sidebar entries (P24-32): bundled surfaces and plugins */}
          <ContributedNav />
        </ul>
      </nav>
      {props.switchError !== null && (
        <p className="switch-error" role="alert">
          {t("profiles.switchFailed", { error: props.switchError })}
        </p>
      )}
      <Sidebar
        model={props.chrome.sidebar}
        profile={props.profile}
        activeSessionId={props.sessionId}
        onOpenSession={(id) => navigate(sessionPath(id))}
        onPopOut={(id) => void desktopBridge()?.hud.open({ sessionId: id, profile: props.profile })}
        onNewChat={() => navigate(NEW_CHAT_PATH)}
        events={props.connection}
        homeDir={launchFacts().homeDir || null}
      />
      <nav aria-label={t("nav.more")}>
        <ul className="nav-list nav-bottom">
          {OVERLAYS.map((overlay) => (
            <li key={overlay}>
              <button
                type="button"
                className="nav-item"
                onClick={() =>
                  navigate(overlayPath(overlay), {
                    state: overlayStateFrom(location.pathname, location.state),
                  })
                }
              >
                {OVERLAY_ICONS[overlay]}
                <span>{t(OVERLAY_TEXT[overlay].title)}</span>
              </button>
            </li>
          ))}
        </ul>
      </nav>
      <ThemeControl />
    </aside>
  );
}

function ConnectionBadge(props: { connection: GatewayConnection }) {
  const t = useT();
  const state = useConnectionState(props.connection);
  return (
    <span className="connection-badge" data-phase={state.phase}>
      <span className="connection-dot" aria-hidden="true" />
      {t(PHASE_KEYS[state.phase] ?? "connection.idle")}
    </span>
  );
}

function ConnectionWarning(props: { connection: GatewayConnection }) {
  const t = useT();
  const state = useConnectionState(props.connection);
  if (!state.warning) return null;
  return (
    <div className="toast" role="status">
      {t("connection.warning")}
    </div>
  );
}

/** Local or remote, from the connection's descriptor (null until the first boot answers). */
function useConnectionMode(connection: GatewayConnection | null): "local" | "remote" | null {
  const store = useMaybeConnectionStore();
  const fallback = connection ?? store?.primary() ?? null;
  const state = useOptionalConnectionState(fallback);
  return state?.descriptor?.mode ?? null;
}

function useOptionalConnectionState(connection: GatewayConnection | null) {
  return useSyncExternalStore(
    (listener) => (connection === null ? () => undefined : connection.subscribe(listener)),
    () => (connection === null ? null : connection.getState()),
  );
}

/** The desktop's default profile (main's remembered state), kept current. */
function useDefaultProfile(): string | null {
  const [value, setValue] = useState<string | null>(null);
  useEffect(() => {
    const profile = desktopBridge()?.profile;
    if (profile === undefined) return;
    let live = true;
    profile.get().then(
      (state) => {
        if (live) setValue(state.defaultProfile);
      },
      () => undefined,
    );
    const off = profile.onChanged((state) => setValue(state.defaultProfile));
    return () => {
      live = false;
      off();
    };
  }, []);
  return value;
}

function bridgeProfilesApi(): ProfilesApi | null {
  const api = desktopBridge()?.api;
  return api === undefined ? null : (payload) => api.request(payload);
}

// -- what the tool panes get (P20-28..31) ---------------------------------------------------------

/**
 * The working folder of the route's conversation: its row's `cwd` in the sidebar list, or for a
 * fresh draft the folder its session will be created in (project or default directory).
 */
function useWorkspaceFolder(
  chrome: ShellChrome,
  profile: string,
  sessionId: string | null,
): string | null {
  const list = chrome.sidebar.sessions(profile);
  const projects = chrome.sidebar.projects(profile);
  const rows = useSyncExternalStore(
    (listener) => list.subscribe(listener),
    () => list.getState().rows,
  );
  useSyncExternalStore(
    (listener) => projects.subscribe(listener),
    () => projects.getPrefs(),
  );
  if (sessionId !== null) return rows.find((row) => row.ids.includes(sessionId))?.cwd ?? null;
  return freshDraftFolder(chrome.sidebar.storage, profile, projects);
}

/** Append `text` to the composer draft of the conversation (a fresh draft when null). */
export function addToComposer(sessionId: string | null, text: string): void {
  const key = sessionId ?? FRESH;
  windowDrafts.update((state) => {
    const current = draftOf(state, key);
    const joined = current.trim() === "" ? text : `${current.replace(/\s+$/, "")}\n\n${text}`;
    return setDraft(state, key, joined);
  });
}

// -- the shell -----------------------------------------------------------------------------------

export function Shell(props: {
  statusBar?: ReactNode;
  /** The route's conversation (null: a new chat or a page). */
  sessionId?: string | null;
  children: ReactNode;
}) {
  const t = useT();
  const navigate = useNavigate();
  const chrome = useChrome();
  const store = useMaybeConnectionStore();
  const switcher = useSwitcher();
  const switchState = useSwitcherState(switcher);
  const profile = switchState?.foreground ?? "default";
  const connection = switcher?.foreground().connection ?? store?.primary() ?? null;
  const layout = useLayout(chrome.layout);
  const mode = layout.mode;
  const api = useMemo(() => bridgeProfilesApi(), []);
  // asked again whenever the backend becomes ready (the first ask may beat the boot)
  const backendReady = useOptionalConnectionState(connection)?.ready ?? false;
  const profiles = useProfileNames(api, `${profile}|${backendReady}`);
  const defaultProfile = useDefaultProfile();
  const [leftOpen, setLeftOpen] = useState(true);
  const platform = launchFacts().platform;
  const sessionId = props.sessionId ?? null;
  const workspace = useWorkspaceFolder(chrome, profile, sessionId);
  const connectionMode = useConnectionMode(connection);
  const openPreview = useCallback(
    (target: PreviewTarget) => {
      previewTargets.set(target);
      if (chrome.layout.open("preview")) chrome.layout.activate("preview");
    },
    [chrome],
  );
  const addToChat = useCallback((text: string) => addToComposer(sessionId, text), [sessionId]);

  // The window's folders for the fs, git and terminal handlers in main (P20-29): the route's
  // working folder, the profile's project folders and its default folder. Main refuses system
  // folders and anything above the home; everything else stays confined to these and the home.
  const projectPrefs = useSyncExternalStore(
    (listener) => chrome.sidebar.projects(profile).subscribe(listener),
    () => chrome.sidebar.projects(profile).getPrefs(),
  );
  const rootsKey = [
    workspace,
    projectPrefs.defaultDirectory,
    ...projectPrefs.projects.flatMap((project) => project.folders),
  ]
    .filter((folder): folder is string => typeof folder === "string" && folder.trim() !== "")
    .filter((folder, index, all) => all.indexOf(folder) === index)
    .join("\n");
  useEffect(() => {
    const fs = desktopBridge()?.fs;
    if (fs?.setRoots === undefined || connectionMode === "remote") return;
    void fs.setRoots({ roots: rootsKey === "" ? [] : rootsKey.split("\n") }).catch(() => {});
  }, [rootsKey, connectionMode]);

  // Ctrl/Cmd+Shift+W inside the preview, or an `indus://close-preview` link (P20-28, P20-34)
  useEffect(
    () => desktopBridge()?.preview?.onCloseRequested?.(() => chrome.layout.close("preview")),
    [chrome],
  );

  // a popped-out session window has no global sidebar or rail
  const secondary = launchFacts().windowKind === "secondary";
  const surface = { profileCount: profiles.length };
  const showRail = !secondary && profiles.length > 0 && surfaceShown(mode, "profileRail", surface);
  const showStatus = surfaceShown(mode, "statusBar", surface);

  const onAction = (action: PaneAction): boolean => {
    const panes = chrome.layout;
    switch (action.kind) {
      case "new-chat":
        beginNewChat(chrome.sidebar, profile, null);
        navigate(NEW_CHAT_PATH);
        return true;
      case "cycle":
        panes.cycle(action.step);
        return true;
      case "jump":
        panes.jump(action.slot);
        return true;
      case "close": {
        const active = focusedPane(panes);
        const inTerminal =
          active === "terminal" ||
          (document.activeElement?.closest('[data-pane="terminal"]') ?? null) !== null;
        if (inTerminal || active === null || active === CHAT_PANE) return false;
        panes.close(active);
        return true;
      }
      case "reopen":
        return panes.reopenLast() !== null;
      case "new-window":
        void desktopBridge()?.windows.openInstance({ profile });
        return true;
      case "toggle-left":
        setLeftOpen((open) => !open);
        return true;
      case "toggle-right": {
        const snapshot = panes.layout();
        const tools = zones(snapshot.root).filter((z) => !z.tabs.includes(CHAT_PANE));
        const showing = tools.some(
          (z) => !z.minimized && z.tabs.some((pane) => !isHidden(snapshot, pane)),
        );
        for (const z of tools) {
          const stateful = z.tabs.filter((pane) => paneRegistry.isStateful(pane));
          if (stateful.length === 0) panes.setMinimized(z.id, showing);
          for (const pane of stateful) panes.setHidden(pane, showing);
        }
        return tools.length > 0;
      }
    }
  };
  const showSlots = usePaneShortcuts(platform, { onAction });
  // the registry's keybinds (P24-32); a key the app uses is never given to a plugin
  useContributedKeybinds(platform);
  const paletteEntries = useMemo<PaletteEntry[]>(
    () => [
      { id: "app.new-chat", title: t("nav.newChat"), run: () => navigate(NEW_CHAT_PATH) },
      ...PAGES.filter((page) => !isContributedPage(page)).map((page) => ({
        id: `app.page.${page}`,
        title: t("palette.goTo", { page: t(PAGE_TEXT[page].title) }),
        run: () => navigate(pagePath(page)),
      })),
    ],
    [t, navigate],
  );

  const selectProfile = (next: string) => {
    if (switcher === null) return;
    void switcher.select(next, { explicit: true }).then((result) => {
      if (result.ok && result.freshDraft) navigate(NEW_CHAT_PATH);
    });
  };

  return (
    <div
      className="shell"
      data-interface-mode={mode}
      data-rail={showRail ? "true" : "false"}
      data-left={leftOpen ? "open" : "closed"}
    >
      {showRail && (
        <ProfileRail
          profiles={profiles}
          foreground={profile}
          pending={switchState?.pending ?? null}
          defaultProfile={defaultProfile}
          onSelect={selectProfile}
          onOpenInWindow={(name) => void desktopBridge()?.windows.openInstance({ profile: name })}
          onSetDefault={(name) => void desktopBridge()?.profile.setDefault({ profile: name })}
        />
      )}
      {leftOpen && !secondary && (
        <LeftSidebar
          chrome={chrome}
          profile={profile}
          sessionId={sessionId}
          connection={connection}
          switchError={switchState?.status === "error" ? switchState.error : null}
        />
      )}
      <main className="pane-area">
        <PaneShell
          store={chrome.layout}
          chat={props.children}
          sessionId={sessionId}
          profile={profile}
          showSlots={showSlots}
          workspace={workspace}
          mode={connectionMode}
          connection={connection}
          openPreview={openPreview}
          addToChat={addToChat}
        />
      </main>
      {showStatus &&
        (props.statusBar === undefined ? (
          <footer className="status-bar">
            <section className="status-items" aria-label={t("status.label")}>
              {connection !== null && <ConnectionBadge connection={connection} />}
            </section>
          </footer>
        ) : (
          // The slot brings its own footer (slice C's StatusBar); the badge stays beside it.
          <div className="status-bar status-bar-host">
            {connection !== null && <ConnectionBadge connection={connection} />}
            <div className="status-slot">{props.statusBar}</div>
          </div>
        ))}
      {connection !== null && <ConnectionWarning connection={connection} />}
      {/* the registry's title bar items and palette commands (P24-32) */}
      {!secondary && (
        <ContributedTitleBar platform={platform} bandHeight={launchFacts().titleBarHeight} />
      )}
      <CommandPalette builtIn={paletteEntries} />
    </div>
  );
}
