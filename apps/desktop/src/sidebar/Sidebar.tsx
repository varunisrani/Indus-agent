// The sessions sidebar (P20-25): search, the live / archived views, pinned conversations,
// projects (their three most recent, "show all"), and the remaining chats. Rows open their
// conversation; pin and archive go to the backend (PATCH to the durable id); unread and running
// dots come from `sessionList.ts`. Lists are kept per profile in `SidebarModel` so a profile swap
// shows the other profile's list at once and keeps this one.

import {
  EllipsisVertical,
  FolderPlus,
  GitBranch,
  ListFilter,
  MessageSquareDashed,
  Pin,
  Plus,
  Search,
  X,
} from "lucide-react";
import {
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { desktopBridge } from "../app/bridge.ts";
import { copyText } from "../app/clipboard.ts";
import { Loader } from "../app/Loader.tsx";
import { useOpenServerRequests } from "../gateway/react.tsx";
import { type MessageKey, type Translate, useT } from "../i18n/index.tsx";
import { browserStorage, type StorageLike } from "../profiles/storageFamilies.ts";
import { useDisplayPrefs } from "../settings/displayPrefs.ts";
import {
  Button,
  EmptyState,
  GroupGlyph,
  MenuLabel,
  MenuPanel,
  MenuRow,
  MenuSeparator,
  SmallDialog,
  useHoverTip,
} from "../ui/components.tsx";
import {
  groupByProject,
  type ListDir,
  newChatCwd,
  newChatTarget,
  type Project,
  ProjectStore,
  scanRepos,
} from "./projects.ts";
import { SessionRowMenu } from "./SessionRowMenu.tsx";
import {
  type ApiRequest,
  type ListView,
  type SearchHit,
  SessionListStore,
  type SessionRow,
  searchResults,
} from "./sessionList.ts";
import "./sidebar.css";

export const SEARCH_DEBOUNCE_MS = 250;
export const REFRESH_DEBOUNCE_MS = 1_000;

/** The sidebar's per-profile stores (kept for the window's life; wiped on a soft re-home). */
export class SidebarModel {
  readonly api: ApiRequest | null;
  readonly storage: StorageLike | null;
  readonly listDir: ListDir | null;
  private readonly lists = new Map<string, SessionListStore>();
  private readonly projectStores = new Map<string, ProjectStore>();

  constructor(
    options: {
      api?: ApiRequest | null;
      storage?: StorageLike | null;
      listDir?: ListDir | null;
    } = {},
  ) {
    this.api = options.api === undefined ? bridgeApi() : options.api;
    this.storage = options.storage === undefined ? browserStorage() : options.storage;
    this.listDir = options.listDir === undefined ? bridgeListDir() : options.listDir;
  }

  sessions(profile: string): SessionListStore {
    let store = this.lists.get(profile);
    if (store === undefined) {
      store = new SessionListStore(profile, this.api, this.storage);
      this.lists.set(profile, store);
    }
    return store;
  }

  projects(profile: string): ProjectStore {
    let store = this.projectStores.get(profile);
    if (store === undefined) {
      store = new ProjectStore(profile, this.storage);
      this.projectStores.set(profile, store);
    }
    return store;
  }

  /** Forget the rows of one profile (or every profile): they belonged to the old connection. */
  wipe(profile: string | null): void {
    for (const [name, store] of this.lists) if (profile === null || name === profile) store.wipe();
    if (profile !== null) {
      this.lists.delete(profile);
      this.projectStores.delete(profile);
    }
  }
}

/**
 * Decide where the profile's next fresh draft starts: the project's folder, else the profile's
 * default directory, else nowhere (a detached chat). The composer sends it on `session.create`.
 */
export function beginNewChat(model: SidebarModel, profile: string, project: Project | null): void {
  const prefs = model.projects(profile).getPrefs();
  newChatTarget.set(model.storage, profile, newChatCwd(project, prefs.defaultDirectory));
}

function bridgeApi(): ApiRequest | null {
  const api = desktopBridge()?.api;
  return api === undefined ? null : (payload) => api.request(payload);
}

function bridgeListDir(): ListDir | null {
  const fs = desktopBridge()?.fs;
  if (fs === undefined) return null;
  return async (path) => (await fs.readDir({ path })).entries;
}

/** Live events of the profile's connection (`GatewayConnection.onEvent`). */
export interface EventSource {
  onEvent(listener: (event: { type?: unknown; session_id?: unknown }) => void): () => void;
}

export interface SidebarProps {
  model: SidebarModel;
  profile: string;
  /** The conversation the window shows (its row is marked, never unread). */
  activeSessionId: string | null;
  onOpenSession(sessionId: string): void;
  /** Pop the conversation out into the HUD (P20-35); no button without it. */
  onPopOut?(sessionId: string): void;
  /** Branch the conversation (`session.branch`, W54-D1-8); no menu item without it. */
  onBranch?(sessionId: string): void;
  onNewChat(): void;
  events?: EventSource | null;
  /** The default root of the repo scan. */
  homeDir?: string | null;
}

// -- small pieces --------------------------------------------------------------------------------

export function relativeTime(t: Translate, then: number, now: number): string {
  if (then <= 0) return "";
  const minutes = Math.floor((now - then) / 60_000);
  if (minutes < 1) return t("sidebar.time.now");
  if (minutes < 60) return t("sidebar.time.minutes", { n: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t("sidebar.time.hours", { n: hours });
  return t("sidebar.time.days", { n: Math.floor(hours / 24) });
}

/** A row's display title (also the chat tab's label, W51-b). */
export function rowTitle(t: Translate, row: SessionRow): string {
  return row.title ?? row.preview ?? t("sidebar.untitled");
}

/** The lines under a row's title (W55-F3): "comfortable" adds what the chat is about (its preview
 * when the title is not already the preview, else its size); "detailed" adds both and where it
 * ran (branch, folder). Only fields the sessions list already carries. */
export function rowDetails(
  t: Translate,
  row: SessionRow,
  density: "compact" | "comfortable" | "detailed",
): string[] {
  if (density === "compact") return [];
  const preview = row.title !== null && row.preview !== null && row.preview !== row.title;
  const size =
    row.messageCount > 0
      ? t(row.messageCount === 1 ? "sidebar.detail.message" : "sidebar.detail.messages", {
          count: row.messageCount,
        })
      : null;
  if (density === "comfortable") {
    const line = preview ? row.preview : size;
    return line === null ? [] : [line];
  }
  const folder = row.cwd === null ? null : (row.cwd.split(/[\\/]/).filter(Boolean).pop() ?? null);
  const meta = [size, row.gitBranch, folder].filter((part): part is string => part !== null);
  const lines: string[] = [];
  if (preview && row.preview !== null) lines.push(row.preview);
  if (meta.length > 0) lines.push(meta.join(" \u00b7 "));
  return lines;
}

function useStore<S>(store: { subscribe(listener: () => void): () => void; getState(): S }): S {
  return useSyncExternalStore(
    (listener) => store.subscribe(listener),
    () => store.getState(),
  );
}

/** What a row asks the sidebar to do (the delete confirmation lives at the sidebar level). */
interface RowHost {
  list: SessionListStore;
  running: ReadonlySet<string>;
  /** Sessions with an approval or question waiting for the person (W54-D1-2). */
  attention: ReadonlySet<string>;
  activeSessionId: string | null;
  now: number;
  onOpen(id: string): void;
  onPopOut?: ((id: string) => void) | undefined;
  onBranch?: ((id: string) => void) | undefined;
  onDelete(row: SessionRow): void;
}

/**
 * The row's dot (W54-D1-2): waiting for the person, a turn running (live events), new activity,
 * recently live (the list's `is_active`: not ended, active lately), or the quiet bullet.
 */
export type SessionDot = "attention" | "running" | "unread" | "live" | "idle";

export function sessionDot(state: {
  attention: boolean;
  running: boolean;
  unread: boolean;
  live?: boolean;
}): SessionDot {
  if (state.attention) return "attention";
  if (state.running) return "running";
  if (state.unread) return "unread";
  return state.live === true ? "live" : "idle";
}

function SessionItem(
  props: Omit<RowHost, "running" | "attention"> & {
    row: SessionRow;
    running: boolean;
    attention: boolean;
  },
) {
  const t = useT();
  const { row, list } = props;
  const title = rowTitle(t, row);
  const current = props.activeSessionId !== null && row.ids.includes(props.activeSessionId);
  const unread = list.unread.isUnread(row, props.activeSessionId);
  const running = props.running;
  const dot = sessionDot({ attention: props.attention, running, unread, live: row.active });
  const density = useDisplayPrefs().sessionDensity;
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const [renaming, setRenaming] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const titleRef = useRef<HTMLSpanElement>(null);
  const [truncated, setTruncated] = useState(false);
  const tip = useHoverTip(truncated ? title : null);
  const closeMenu = useCallback(() => setMenu(null), []);
  const openMenuAtRow = () => {
    const rect = button.current?.getBoundingClientRect();
    setMenu({ x: (rect?.left ?? 0) + 24, y: rect?.bottom ?? 0 });
  };
  if (renaming) {
    return (
      <li className="session-row" data-current={current ? "true" : undefined}>
        <input
          className="session-rename"
          aria-label={t("sidebar.renameField")}
          defaultValue={row.title ?? title}
          // biome-ignore lint/a11y/noAutofocus: the person just asked to rename this row
          autoFocus
          onFocus={(event) => event.currentTarget.select()}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void list.setTitle(row, event.currentTarget.value);
              setRenaming(false);
            } else if (event.key === "Escape") {
              event.preventDefault();
              setRenaming(false);
            }
          }}
          onBlur={() => setRenaming(false)}
        />
      </li>
    );
  }
  return (
    <li
      className="session-row"
      data-current={current ? "true" : undefined}
      data-density={density === "compact" ? undefined : density}
    >
      <button
        ref={button}
        type="button"
        className="session-open"
        aria-current={current ? "page" : undefined}
        aria-haspopup="menu"
        onClick={(event) => {
          // Shift-click pins (or unpins) without opening the chat
          if (event.shiftKey) {
            void list.setPinned(row, !row.pinned);
            return;
          }
          props.onOpen(row.id);
        }}
        onContextMenu={(event) => {
          // ours instead of Electron's native menu (a prevented event never reaches main)
          event.preventDefault();
          setMenu({ x: event.clientX, y: event.clientY });
        }}
        onKeyDown={(event) => {
          if ((event.key === "F10" && event.shiftKey) || event.key === "ContextMenu") {
            event.preventDefault();
            openMenuAtRow();
          }
        }}
        onMouseEnter={(event) => {
          const node = titleRef.current;
          setTruncated(node !== null && node.scrollWidth > node.clientWidth);
          tip.bind.onMouseEnter(event);
        }}
        onMouseLeave={tip.bind.onMouseLeave}
      >
        <span className="session-dots" aria-hidden="true">
          <span className="session-dot" data-kind={dot} />
        </span>
        {row.branch && <GitBranch aria-hidden="true" size={12} className="session-branch" />}
        {density === "compact" ? (
          <span ref={titleRef} className="session-title">
            {title}
          </span>
        ) : (
          // W55-F3: Settings › Window & layout › Session list density
          <span className="session-lines">
            <span ref={titleRef} className="session-title">
              {title}
            </span>
            {rowDetails(t, row, density).map((line) => (
              <span key={line} className="session-detail">
                {line}
              </span>
            ))}
          </span>
        )}
        {unread && <span className="sr-only">{t("sidebar.unread")}</span>}
        {running && <span className="sr-only">{t("sidebar.running")}</span>}
        {props.attention && <span className="sr-only">{t("sidebar.attention")}</span>}
        {row.branch && <span className="sr-only">{t("sidebar.branch")}</span>}
        <span className="session-time">{relativeTime(t, row.lastActive, props.now)}</span>
      </button>
      {/* the row's actions on hover (W54-D1-2): the same menu as a right-click */}
      <button
        type="button"
        className="session-more"
        aria-label={t("sidebar.more", { title })}
        aria-haspopup="menu"
        aria-expanded={menu !== null}
        onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          setMenu({ x: rect.left, y: rect.bottom });
        }}
      >
        <EllipsisVertical aria-hidden="true" size={13} />
      </button>
      {truncated ? tip.tip : null}
      {menu !== null && (
        <SessionRowMenu
          x={menu.x}
          y={menu.y}
          title={title}
          pinned={row.pinned}
          unread={unread}
          archived={row.archived}
          onClose={closeMenu}
          actions={{
            popOut: props.onPopOut === undefined ? undefined : () => props.onPopOut?.(row.id),
            rename: () => setRenaming(true),
            togglePin: () => void list.setPinned(row, !row.pinned),
            toggleUnread: () => (unread ? list.markSeen(row) : list.markUnread(row)),
            copyId: () => void copyText(row.durableId),
            branch: props.onBranch === undefined ? undefined : () => props.onBranch?.(row.id),
            toggleArchive: () => void list.setArchived(row, !row.archived),
            remove: () => props.onDelete(row),
          }}
        />
      )}
    </li>
  );
}

function RowList(
  props: Omit<RowHost, "running" | "attention"> & {
    rows: readonly SessionRow[];
    running: ReadonlySet<string>;
    attention: ReadonlySet<string>;
    label: string;
  },
) {
  const { rows, label, running, attention, ...host } = props;
  return (
    <ul className="session-list" aria-label={label}>
      {rows.map((row) => (
        <SessionItem
          key={row.durableId}
          {...host}
          row={row}
          running={row.ids.some((id) => running.has(id))}
          attention={row.ids.some((id) => attention.has(id))}
        />
      ))}
    </ul>
  );
}

function Section(props: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="sidebar-section" aria-label={props.title}>
      <header className="sidebar-section-header desk-section-label">
        <GroupGlyph />
        <h2>{props.title}</h2>
        {props.actions !== undefined && (
          <span className="desk-section-label-actions">{props.actions}</span>
        )}
      </header>
      {props.children}
    </section>
  );
}

// -- new project ---------------------------------------------------------------------------------

function NewProjectForm(props: {
  projects: ProjectStore;
  listDir: ListDir | null;
  homeDir: string | null;
  onDone(): void;
}) {
  const t = useT();
  const [name, setName] = useState("");
  const [folders, setFolders] = useState("");
  const [scanning, setScanning] = useState(false);
  const [found, setFound] = useState<string[] | null>(null);
  const [picked, setPicked] = useState<ReadonlySet<string>>(new Set());
  const abort = useRef({ aborted: false });
  useEffect(
    () => () => {
      abort.current.aborted = true;
    },
    [],
  );
  const scan = async () => {
    if (props.listDir === null) return;
    setScanning(true);
    try {
      const repos = await scanRepos({
        settings: props.projects.getPrefs().repoScan,
        homeDir: props.homeDir,
        listDir: props.listDir,
        signal: abort.current,
      });
      if (!abort.current.aborted) setFound(repos);
    } finally {
      if (!abort.current.aborted) setScanning(false);
    }
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const typed = folders.split("\n").map((line) => line.trim());
    const project = props.projects.add(name, [...typed, ...picked]);
    if (project !== null) props.onDone();
  };
  return (
    <form className="project-form" onSubmit={submit} aria-label={t("sidebar.project.new")}>
      <label className="field">
        <span>{t("sidebar.project.name")}</span>
        <input value={name} onChange={(event) => setName(event.currentTarget.value)} required />
      </label>
      <label className="field">
        <span>{t("sidebar.project.folders")}</span>
        <textarea
          value={folders}
          rows={2}
          onChange={(event) => setFolders(event.currentTarget.value)}
          placeholder={t("sidebar.project.foldersHint")}
        />
      </label>
      {props.listDir !== null && (
        <button
          type="button"
          className="text-button"
          onClick={() => void scan()}
          disabled={scanning}
        >
          {t("sidebar.project.scan")}
        </button>
      )}
      {scanning && <Loader />}
      {found !== null && found.length === 0 && (
        <p className="sidebar-note">{t("sidebar.project.noRepos")}</p>
      )}
      {found !== null && found.length > 0 && (
        <fieldset className="repo-list">
          <legend>{t("sidebar.project.repos")}</legend>
          {found.map((repo) => (
            <label key={repo} className="repo-choice">
              <input
                type="checkbox"
                checked={picked.has(repo)}
                onChange={(event) => {
                  const next = new Set(picked);
                  if (event.currentTarget.checked) next.add(repo);
                  else next.delete(repo);
                  setPicked(next);
                }}
              />
              <span title={repo}>{repo}</span>
            </label>
          ))}
        </fieldset>
      )}
      <div className="project-form-actions">
        <button type="submit" className="button button-primary">
          {t("sidebar.project.create")}
        </button>
        <button type="button" className="button button-secondary" onClick={props.onDone}>
          {t("sidebar.cancel")}
        </button>
      </div>
    </form>
  );
}

// -- the sidebar ---------------------------------------------------------------------------------

const VIEW_KEYS: Readonly<Record<ListView, MessageKey>> = {
  live: "sidebar.view.live",
  archived: "sidebar.view.archived",
};

export function Sidebar(props: SidebarProps) {
  const t = useT();
  const list = props.model.sessions(props.profile);
  const projectStore = props.model.projects(props.profile);
  const state = useStore(list);
  const prefs = useSyncExternalStore(
    (listener) => projectStore.subscribe(listener),
    () => projectStore.getPrefs(),
  );
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [creating, setCreating] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [deleting, setDeleting] = useState<SessionRow | null>(null);
  const [filterOpen, setFilterOpen] = useState(false);
  const filterButton = useRef<HTMLButtonElement>(null);
  const closeFilter = useCallback(() => setFilterOpen(false), []);

  // load the profile's list; refresh after turns end
  useEffect(() => {
    void list.refresh();
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, [list]);
  useEffect(() => {
    if (props.events === null || props.events === undefined) return;
    let pending: ReturnType<typeof setTimeout> | undefined;
    const off = props.events.onEvent((event) => {
      if (!list.noteEvent(event) || pending !== undefined) return;
      pending = setTimeout(() => {
        pending = undefined;
        void list.refresh();
      }, REFRESH_DEBOUNCE_MS);
    });
    return () => {
      off();
      if (pending !== undefined) clearTimeout(pending);
    };
  }, [props.events, list]);

  // the open conversation is seen (and stays seen while it grows)
  const open = props.activeSessionId;
  useEffect(() => {
    if (open === null) return;
    const row = state.rows.find((candidate) => candidate.ids.includes(open));
    if (row !== undefined) list.markSeen(row);
  }, [open, state.rows, list]);

  // server search, debounced; local matches show at once
  const trimmed = query.trim();
  useEffect(() => {
    setHits(null);
    if (trimmed === "") return;
    let live = true;
    const timer = setTimeout(() => {
      void list.search(trimmed).then((found) => {
        if (live) setHits(found);
      });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [trimmed, list]);

  const pinned = useMemo(() => state.rows.filter((row) => row.pinned), [state.rows]);
  const unpinned = useMemo(() => state.rows.filter((row) => !row.pinned), [state.rows]);
  const grouping = useMemo(
    () => groupByProject(unpinned, prefs.projects, expanded),
    [unpinned, prefs.projects, expanded],
  );
  const results = useMemo(
    () => (trimmed === "" ? [] : searchResults(state.rows, trimmed, hits)),
    [state.rows, trimmed, hits],
  );

  // approvals and questions waiting anywhere on the connection mark their rows (W54-D1-2)
  const waiting = useOpenServerRequests(undefined);
  const attention = useMemo(() => new Set(waiting.map((open) => open.sessionId)), [waiting]);
  const openSession = (id: string) => props.onOpenSession(id);
  const newChat = (project: Project | null) => {
    beginNewChat(props.model, props.profile, project);
    props.onNewChat();
  };
  const common = {
    list,
    running: state.running,
    attention,
    activeSessionId: props.activeSessionId,
    now,
    onOpen: openSession,
    onPopOut: props.onPopOut,
    onBranch: props.onBranch,
    onDelete: (row: SessionRow) => setDeleting(row),
  };
  const confirmDelete = async () => {
    const row = deleting;
    setDeleting(null);
    if (row === null) return;
    const open = props.activeSessionId !== null && row.ids.includes(props.activeSessionId);
    if ((await list.remove(row)) && open) props.onNewChat();
  };

  return (
    <div className="sidebar-sessions">
      <div className="sidebar-search desk-search">
        <Search aria-hidden="true" size={13} />
        <input
          type="search"
          value={query}
          aria-label={t("sidebar.search")}
          placeholder={t("sidebar.searchPlaceholder")}
          onChange={(event) => setQuery(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") setQuery("");
          }}
        />
        {query !== "" && (
          <button
            type="button"
            className="icon-button"
            aria-label={t("sidebar.clearSearch")}
            onClick={() => setQuery("")}
          >
            <X aria-hidden="true" size={14} />
          </button>
        )}
      </div>
      {state.error !== null && (
        <p className="sidebar-error" role="alert">
          {t("sidebar.loadFailed")}{" "}
          <button type="button" className="text-button" onClick={() => void list.refresh()}>
            {t("sidebar.retry")}
          </button>
        </p>
      )}
      {state.loading && state.rows.length === 0 && <Loader />}
      <div className="sidebar-scroll">
        {trimmed !== "" ? (
          <Section title={t("sidebar.results")}>
            {results.length === 0 ? (
              hits === null ? (
                <Loader />
              ) : (
                // W52-l: a quiet caption, no icon (reference look)
                <p className="sidebar-no-match">{t("sidebar.noMatch", { query: trimmed })}</p>
              )
            ) : (
              <RowList rows={results} label={t("sidebar.results")} {...common} />
            )}
          </Section>
        ) : (
          <>
            {(pinned.length > 0 || (state.view === "live" && state.rows.length > 0)) && (
              <Section title={t("sidebar.pinned")}>
                {pinned.length > 0 ? (
                  <RowList rows={pinned} label={t("sidebar.pinned")} {...common} />
                ) : (
                  <p className="sidebar-hint">
                    <Pin aria-hidden="true" size={12} />
                    {t("sidebar.pinnedHint")}
                  </p>
                )}
              </Section>
            )}
            {state.view === "live" && (creating || grouping.projects.length > 0) && (
              <Section title={t("sidebar.projects")}>
                {creating && (
                  <NewProjectForm
                    projects={projectStore}
                    listDir={props.model.listDir}
                    homeDir={props.homeDir ?? null}
                    onDone={() => setCreating(false)}
                  />
                )}
                {grouping.projects.map((group) => (
                  <div key={group.project.id} className="project-group">
                    <header className="project-header">
                      <span className="project-name" title={group.project.folders.join("\n")}>
                        {group.project.name}
                      </span>
                      <button
                        type="button"
                        className="icon-button"
                        aria-label={t("sidebar.project.newChat", { name: group.project.name })}
                        title={t("sidebar.project.newChat", { name: group.project.name })}
                        onClick={() => newChat(group.project)}
                      >
                        <Plus aria-hidden="true" size={14} />
                      </button>
                      <button
                        type="button"
                        className="icon-button"
                        aria-label={t("sidebar.project.remove", { name: group.project.name })}
                        title={t("sidebar.project.remove", { name: group.project.name })}
                        onClick={() => projectStore.remove(group.project.id)}
                      >
                        <X aria-hidden="true" size={14} />
                      </button>
                    </header>
                    {group.total === 0 ? (
                      <p className="sidebar-note">{t("sidebar.project.empty")}</p>
                    ) : (
                      <RowList rows={group.rows} label={group.project.name} {...common} />
                    )}
                    {group.total > group.rows.length && (
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => setExpanded(new Set([...expanded, group.project.id]))}
                      >
                        {t("sidebar.showAll", { n: group.total })}
                      </button>
                    )}
                    {group.expanded && group.total > 3 && (
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => {
                          const next = new Set(expanded);
                          next.delete(group.project.id);
                          setExpanded(next);
                        }}
                      >
                        {t("sidebar.showLess")}
                      </button>
                    )}
                  </div>
                ))}
              </Section>
            )}
            <Section
              title={state.view === "archived" ? t("sidebar.view.archived") : t("sidebar.chats")}
              actions={
                <>
                  {state.view === "live" && (state.rows.length > 0 || state.loading) && (
                    <button
                      type="button"
                      className="desk-icon-btn"
                      data-size="sm"
                      aria-label={t("sidebar.project.new")}
                      aria-expanded={creating}
                      onClick={() => setCreating(!creating)}
                    >
                      <FolderPlus aria-hidden="true" />
                    </button>
                  )}
                  <span className="desk-menu-root">
                    <button
                      ref={filterButton}
                      type="button"
                      className="desk-icon-btn"
                      data-size="sm"
                      aria-label={t("sidebar.filter")}
                      aria-haspopup="menu"
                      aria-expanded={filterOpen}
                      onClick={() => setFilterOpen(!filterOpen)}
                    >
                      <ListFilter aria-hidden="true" />
                    </button>
                    {filterOpen && (
                      <MenuPanel
                        label={t("sidebar.filter")}
                        onClose={closeFilter}
                        keepOpenFor={filterButton}
                        className="sidebar-filter-menu"
                      >
                        <MenuLabel>{t("sidebar.view.label")}</MenuLabel>
                        {(["live", "archived"] as const).map((view) => (
                          <MenuRow
                            key={view}
                            kind="radio"
                            label={t(VIEW_KEYS[view])}
                            checked={state.view === view}
                            onSelect={() => {
                              setFilterOpen(false);
                              list.setView(view);
                            }}
                          />
                        ))}
                        <MenuSeparator />
                        <MenuLabel>{t("sidebar.sort.label")}</MenuLabel>
                        <MenuRow
                          kind="radio"
                          label={t("sidebar.sort.recent")}
                          checked
                          onSelect={() => setFilterOpen(false)}
                        />
                      </MenuPanel>
                    )}
                  </span>
                </>
              }
            >
              {(state.view === "archived" ? state.rows : grouping.chats).length === 0 &&
              !state.loading ? (
                state.rows.length === 0 ? (
                  <EmptyState
                    icon={<MessageSquareDashed aria-hidden="true" size={18} />}
                    title={
                      state.view === "archived" ? t("sidebar.emptyArchived") : t("sidebar.empty")
                    }
                    action={
                      state.view === "live" && !creating ? (
                        <button
                          type="button"
                          className="desk-btn sidebar-empty-action"
                          data-variant="ghost"
                          data-size="sm"
                          onClick={() => setCreating(true)}
                        >
                          <Plus aria-hidden="true" size={13} />
                          {t("sidebar.project.new")}
                        </button>
                      ) : undefined
                    }
                  />
                ) : (
                  <p className="sidebar-note">{t("sidebar.allInProjects")}</p>
                )
              ) : (
                <RowList
                  rows={state.view === "archived" ? state.rows : grouping.chats}
                  label={
                    state.view === "archived" ? t("sidebar.view.archived") : t("sidebar.chats")
                  }
                  {...common}
                />
              )}
            </Section>
          </>
        )}
      </div>
      {deleting !== null && (
        <SmallDialog
          title={t("sidebar.delete.title")}
          description={t("sidebar.delete.body", { title: rowTitle(t, deleting) })}
          onClose={() => setDeleting(null)}
          actions={
            <>
              <Button variant="ghost" onClick={() => setDeleting(null)}>
                {t("sidebar.cancel")}
              </Button>
              <Button variant="danger" onClick={() => void confirmDelete()}>
                {t("sidebar.delete.confirm")}
              </Button>
            </>
          }
        />
      )}
    </div>
  );
}
