// The sessions sidebar (P20-25): search, the live / archived views, pinned conversations,
// projects (their three most recent, "show all"), and the remaining chats. Rows open their
// conversation; pin and archive go to the backend (PATCH to the durable id); unread and running
// dots come from `sessionList.ts`. Lists are kept per profile in `SidebarModel` so a profile swap
// shows the other profile's list at once and keeps this one.

import {
  Archive,
  ArchiveRestore,
  FolderPlus,
  GitBranch,
  PictureInPicture2,
  Pin,
  PinOff,
  Plus,
  Search,
  X,
} from "lucide-react";
import {
  type FormEvent,
  type ReactNode,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { desktopBridge } from "../app/bridge.ts";
import { Loader } from "../app/Loader.tsx";
import { type MessageKey, type Translate, useT } from "../i18n/index.tsx";
import { browserStorage, type StorageLike } from "../profiles/storageFamilies.ts";
import {
  groupByProject,
  type ListDir,
  newChatCwd,
  newChatTarget,
  type Project,
  ProjectStore,
  scanRepos,
} from "./projects.ts";
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

function rowTitle(t: Translate, row: SessionRow): string {
  return row.title ?? row.preview ?? t("sidebar.untitled");
}

function useStore<S>(store: { subscribe(listener: () => void): () => void; getState(): S }): S {
  return useSyncExternalStore(
    (listener) => store.subscribe(listener),
    () => store.getState(),
  );
}

function SessionItem(props: {
  row: SessionRow;
  list: SessionListStore;
  activeSessionId: string | null;
  running: boolean;
  now: number;
  onOpen(id: string): void;
  onPopOut?: ((id: string) => void) | undefined;
}) {
  const t = useT();
  const { row, list } = props;
  const title = rowTitle(t, row);
  const current = props.activeSessionId !== null && row.ids.includes(props.activeSessionId);
  const unread = list.unread.isUnread(row, props.activeSessionId);
  const running = props.running || row.active;
  return (
    <li className="session-row" data-current={current ? "true" : undefined}>
      <button
        type="button"
        className="session-open"
        aria-current={current ? "page" : undefined}
        onClick={() => props.onOpen(row.id)}
        title={row.cwd ?? undefined}
      >
        <span className="session-dots" aria-hidden="true">
          {running ? (
            <span className="session-dot" data-kind="running" />
          ) : unread ? (
            <span className="session-dot" data-kind="unread" />
          ) : null}
        </span>
        {row.branch && <GitBranch aria-hidden="true" size={12} className="session-branch" />}
        <span className="session-title">{title}</span>
        {unread && <span className="sr-only">{t("sidebar.unread")}</span>}
        {running && <span className="sr-only">{t("sidebar.running")}</span>}
        {row.branch && <span className="sr-only">{t("sidebar.branch")}</span>}
        <span className="session-time">{relativeTime(t, row.lastActive, props.now)}</span>
      </button>
      <span className="session-actions">
        {props.onPopOut !== undefined && (
          <button
            type="button"
            className="icon-button"
            aria-label={t("sidebar.popOut", { title })}
            title={t("sidebar.popOut", { title })}
            onClick={() => props.onPopOut?.(row.id)}
          >
            <PictureInPicture2 aria-hidden="true" size={14} />
          </button>
        )}
        <button
          type="button"
          className="icon-button"
          aria-label={t(row.pinned ? "sidebar.unpin" : "sidebar.pin", { title })}
          title={t(row.pinned ? "sidebar.unpin" : "sidebar.pin", { title })}
          aria-pressed={row.pinned}
          onClick={() => void list.setPinned(row, !row.pinned)}
        >
          {row.pinned ? (
            <PinOff aria-hidden="true" size={14} />
          ) : (
            <Pin aria-hidden="true" size={14} />
          )}
        </button>
        <button
          type="button"
          className="icon-button"
          aria-label={t(row.archived ? "sidebar.unarchive" : "sidebar.archive", { title })}
          title={t(row.archived ? "sidebar.unarchive" : "sidebar.archive", { title })}
          onClick={() => void list.setArchived(row, !row.archived)}
        >
          {row.archived ? (
            <ArchiveRestore aria-hidden="true" size={14} />
          ) : (
            <Archive aria-hidden="true" size={14} />
          )}
        </button>
      </span>
    </li>
  );
}

function RowList(props: {
  rows: readonly SessionRow[];
  list: SessionListStore;
  running: ReadonlySet<string>;
  activeSessionId: string | null;
  now: number;
  onOpen(id: string): void;
  onPopOut?: ((id: string) => void) | undefined;
  label: string;
}) {
  return (
    <ul className="session-list" aria-label={props.label}>
      {props.rows.map((row) => (
        <SessionItem
          key={row.durableId}
          row={row}
          list={props.list}
          running={row.ids.some((id) => props.running.has(id))}
          activeSessionId={props.activeSessionId}
          now={props.now}
          onOpen={props.onOpen}
          onPopOut={props.onPopOut}
        />
      ))}
    </ul>
  );
}

function Section(props: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="sidebar-section" aria-label={props.title}>
      <header className="sidebar-section-header">
        <h2>{props.title}</h2>
        {props.actions}
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

  const openSession = (id: string) => props.onOpenSession(id);
  const newChat = (project: Project | null) => {
    beginNewChat(props.model, props.profile, project);
    props.onNewChat();
  };
  const common = {
    list,
    running: state.running,
    activeSessionId: props.activeSessionId,
    now,
    onOpen: openSession,
    onPopOut: props.onPopOut,
  };

  return (
    <div className="sidebar-sessions">
      <div className="sidebar-search">
        <Search aria-hidden="true" size={14} />
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
      <div
        className="segmented sidebar-views"
        role="radiogroup"
        aria-label={t("sidebar.view.label")}
      >
        {(["live", "archived"] as const).map((view) => (
          // biome-ignore lint/a11y/useSemanticElements: a segmented control, not a native radio
          <button
            key={view}
            type="button"
            role="radio"
            aria-checked={state.view === view}
            className="segmented-option segmented-text"
            onClick={() => list.setView(view)}
          >
            {t(VIEW_KEYS[view])}
          </button>
        ))}
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
              <p className="sidebar-note">{hits === null ? <Loader /> : t("sidebar.noResults")}</p>
            ) : (
              <RowList rows={results} label={t("sidebar.results")} {...common} />
            )}
          </Section>
        ) : (
          <>
            {pinned.length > 0 && (
              <Section title={t("sidebar.pinned")}>
                <RowList rows={pinned} label={t("sidebar.pinned")} {...common} />
              </Section>
            )}
            {state.view === "live" && (
              <Section
                title={t("sidebar.projects")}
                actions={
                  <button
                    type="button"
                    className="icon-button"
                    aria-label={t("sidebar.project.new")}
                    title={t("sidebar.project.new")}
                    aria-expanded={creating}
                    onClick={() => setCreating(!creating)}
                  >
                    <FolderPlus aria-hidden="true" size={14} />
                  </button>
                }
              >
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
            >
              {(state.view === "archived" ? state.rows : grouping.chats).length === 0 &&
              !state.loading ? (
                <p className="sidebar-note">{t("sidebar.empty")}</p>
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
    </div>
  );
}
