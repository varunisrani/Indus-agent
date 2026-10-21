// The window shell (P20-15): left sidebar, the central pane area, the bottom status bar. The
// status bar is a slot (slice C's StatusBar); without one it shows the connection state.

import { Monitor, Moon, PenSquare, Sun } from "lucide-react";
import type { ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router";
import type { GatewayConnection } from "../gateway/connectionStore.ts";
import { useConnectionState, useMaybeConnectionStore } from "../gateway/react.tsx";
import { type MessageKey, useT } from "../i18n/index.tsx";
import { OVERLAY_ICONS, OVERLAY_TEXT, PAGE_ICONS, PAGE_TEXT } from "./pages.tsx";
import {
  NEW_CHAT_PATH,
  OVERLAYS,
  overlayPath,
  overlayStateFrom,
  PAGES,
  pagePath,
} from "./routes.ts";
import { type ThemeMode, useTheme } from "./theme.tsx";

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

function Sidebar() {
  const t = useT();
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <aside className="sidebar">
      <nav aria-label={t("nav.label")}>
        <button
          type="button"
          className="nav-item nav-primary"
          onClick={() => navigate(NEW_CHAT_PATH)}
        >
          <PenSquare aria-hidden="true" size={16} />
          <span>{t("nav.newChat")}</span>
        </button>
        <ul className="nav-list">
          {PAGES.map((page) => (
            <li key={page}>
              <NavLink to={pagePath(page)} className="nav-item">
                {PAGE_ICONS[page]}
                <span>{t(PAGE_TEXT[page].title)}</span>
              </NavLink>
            </li>
          ))}
        </ul>
        <p className="sidebar-note">{t("nav.sessionsLater")}</p>
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

export function Shell(props: { statusBar?: ReactNode; children: ReactNode }) {
  const t = useT();
  const store = useMaybeConnectionStore();
  const connection = store?.primary() ?? null;
  return (
    <div className="shell">
      <Sidebar />
      <main className="pane-area">{props.children}</main>
      {props.statusBar === undefined ? (
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
      )}
      {connection !== null && <ConnectionWarning connection={connection} />}
    </div>
  );
}
