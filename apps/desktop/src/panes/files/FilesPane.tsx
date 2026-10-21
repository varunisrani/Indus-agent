// The files pane (P20-29): the session's working folder as a lazily expanded tree. Local mode reads
// this computer through main's fs IPC (confined to the window's folders and home, no secrets read);
// remote mode reads the remote host through the REST proxy. Rows can be previewed, renamed, moved
// to the trash (deleted on a remote, after asking), shown in the OS file manager, copied as a path
// and dragged (as a path) onto the composer. Expanded folders are watched in local mode.

import {
  ChevronDown,
  ChevronRight,
  File,
  Folder,
  Lock,
  MoreHorizontal,
  RefreshCw,
} from "lucide-react";
import {
  type DragEvent,
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { desktopBridge } from "../../app/bridge.ts";
import { Loader } from "../../app/Loader.tsx";
import type { IndusDesktopBridge } from "../../global.d.ts";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import {
  basenameOf,
  type FileEntry,
  type FileSource,
  type Listing,
  localSource,
  remoteSource,
  validName,
} from "./source.ts";

/** The drag type other surfaces (the composer) accept for a dragged row. */
export const PATH_DRAG_TYPE = "application/x-indus-path";

export interface FilesPaneProps {
  workspace: string | null;
  mode: "local" | "remote" | null;
  hidden?: boolean;
  /** Overrides the source (tests); defaults from the bridge and the mode. */
  source?: FileSource | null;
  onPreview?(path: string): void;
}

type DirState =
  | { phase: "loading" }
  | { phase: "ready"; listing: Listing }
  | { phase: "error"; message: string };

const ACTION =
  "rounded border border-line px-2 py-0.5 text-xs hover:bg-raised focus-visible:outline-2 focus-visible:outline-focus";

function errorText(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error);
  // Bridge errors read "code: message"; the person needs the message.
  const at = text.indexOf(": ");
  return at > 0 && /^[a-z-]+$/.test(text.slice(0, at)) ? text.slice(at + 2) : text;
}

function defaultSource(mode: FilesPaneProps["mode"]): FileSource | null {
  const bridge = desktopBridge() as IndusDesktopBridge | null;
  if (bridge === null) return null;
  return mode === "remote" ? remoteSource(bridge) : localSource(bridge);
}

export function FilesPane(props: FilesPaneProps) {
  const t = useT();
  const source = useMemo(
    () => (props.source === undefined ? defaultSource(props.mode) : props.source),
    [props.source, props.mode],
  );
  const [dirs, setDirs] = useState<Record<string, DirState>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [confirming, setConfirming] = useState<FileEntry | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const watchers = useRef(new Map<string, () => void>());
  const root = props.workspace;

  const load = useCallback(
    async (dir: string) => {
      if (source === null) return;
      setDirs((prev) => ({
        ...prev,
        [dir]: prev[dir]?.phase === "ready" ? prev[dir] : { phase: "loading" },
      }));
      try {
        const listing = await source.list(dir);
        setDirs((prev) => ({ ...prev, [dir]: { phase: "ready", listing } }));
      } catch (error) {
        setDirs((prev) => ({ ...prev, [dir]: { phase: "error", message: errorText(error) } }));
      }
    },
    [source],
  );

  const watch = useCallback(
    (dir: string) => {
      if (source?.watch === null || source === null || watchers.current.has(dir)) return;
      watchers.current.set(dir, () => {});
      source
        .watch?.(dir, () => void load(dir))
        .then((stop) => {
          if (watchers.current.has(dir)) watchers.current.set(dir, stop);
          else stop();
        })
        .catch(() => watchers.current.delete(dir));
    },
    [source, load],
  );

  const unwatch = useCallback((dir: string) => {
    const stop = watchers.current.get(dir);
    watchers.current.delete(dir);
    stop?.();
  }, []);

  // A new folder or source: declare the root to main, load and watch it.
  useEffect(() => {
    const current = watchers.current;
    setDirs({});
    setExpanded(new Set());
    if (root === null || source === null) return;
    let live = true;
    void source
      .setRoots([root])
      .catch(() => {})
      .then(() => {
        if (!live) return;
        void load(root);
        watch(root);
      });
    return () => {
      live = false;
      for (const stop of current.values()) stop();
      current.clear();
    };
  }, [root, source, load, watch]);

  const toggle = (entry: FileEntry) => {
    const open = expanded.has(entry.path);
    const next = new Set(expanded);
    if (open) {
      next.delete(entry.path);
      unwatch(entry.path);
    } else {
      next.add(entry.path);
      void load(entry.path);
      watch(entry.path);
    }
    setExpanded(next);
  };

  const reloadParentOf = (path: string) => {
    for (const dir of Object.keys(dirs)) {
      const state = dirs[dir];
      if (state?.phase === "ready" && state.listing.entries.some((entry) => entry.path === path)) {
        void load(dir);
      }
    }
  };

  const failed = (action: MessageKey, error: unknown) =>
    setNotice(t("files.failed", { action: t(action), message: errorText(error) }));

  const submitRename = async (event: FormEvent, entry: FileEntry) => {
    event.preventDefault();
    if (source?.rename === null || source === null) return;
    if (!validName(newName)) {
      setNotice(t("files.badName"));
      return;
    }
    try {
      await source.rename?.(entry.path, newName);
      setRenaming(null);
      setNotice(null);
      reloadParentOf(entry.path);
    } catch (error) {
      failed("files.rename", error);
    }
  };

  const remove = async (entry: FileEntry) => {
    if (source === null) return;
    try {
      await source.remove(entry.path, entry.isDir);
      setConfirming(null);
      setMenuFor(null);
      reloadParentOf(entry.path);
    } catch (error) {
      failed(source.remote ? "files.delete" : "files.trash", error);
    }
  };

  const onDragStart = (event: DragEvent, entry: FileEntry) => {
    event.dataTransfer.setData("text/plain", entry.path);
    event.dataTransfer.setData(
      PATH_DRAG_TYPE,
      JSON.stringify({ path: entry.path, remote: source?.remote === true }),
    );
    event.dataTransfer.effectAllowed = "copy";
  };

  const renderActions = (entry: FileEntry) => {
    if (menuFor !== entry.path) return null;
    return (
      <div className="flex flex-wrap gap-1 py-1 pl-8">
        {!entry.isDir && !entry.sensitive && props.onPreview !== undefined && !source?.remote ? (
          <button type="button" className={ACTION} onClick={() => props.onPreview?.(entry.path)}>
            {t("files.preview")}
          </button>
        ) : null}
        {source?.rename !== null && source !== null ? (
          <button
            type="button"
            className={ACTION}
            onClick={() => {
              setRenaming(entry.path);
              setNewName(entry.name);
            }}
          >
            {t("files.rename")}
          </button>
        ) : null}
        {source?.remote ? (
          <button type="button" className={ACTION} onClick={() => setConfirming(entry)}>
            {t("files.delete")}
          </button>
        ) : (
          <button type="button" className={ACTION} onClick={() => void remove(entry)}>
            {t("files.trash")}
          </button>
        )}
        {source?.reveal !== null && source !== null ? (
          <button
            type="button"
            className={ACTION}
            onClick={() =>
              void source
                .reveal?.(entry.path)
                .catch((error: unknown) => failed("files.reveal", error))
            }
          >
            {t("files.reveal")}
          </button>
        ) : null}
        <button
          type="button"
          className={ACTION}
          onClick={() => void navigator.clipboard?.writeText(entry.path).catch(() => {})}
        >
          {t("files.copyPath")}
        </button>
      </div>
    );
  };

  const renderDir = (dir: string, depth: number): ReactNode => {
    const state = dirs[dir];
    if (state === undefined || state.phase === "loading") {
      return (
        <li className="px-2 py-1" style={{ paddingLeft: `${depth * 12 + 8}px` }}>
          <Loader size="sm" />
        </li>
      );
    }
    if (state.phase === "error") {
      return (
        <li
          role="alert"
          className="px-2 py-1 text-xs text-danger"
          style={{ paddingLeft: `${depth * 12 + 8}px` }}
        >
          {t("files.loadFailed", { message: state.message })}
        </li>
      );
    }
    const { entries, truncated } = state.listing;
    return (
      <>
        {entries.length === 0 ? (
          <li
            className="px-2 py-1 text-xs text-muted"
            style={{ paddingLeft: `${depth * 12 + 8}px` }}
          >
            {t("files.empty")}
          </li>
        ) : null}
        {entries.map((entry) => {
          const open = expanded.has(entry.path);
          return (
            <li key={entry.path}>
              {/* biome-ignore lint/a11y/noStaticElementInteractions: dragging is a pointer extra; "Copy path" is the keyboard way */}
              <div
                className="group flex items-center gap-1 px-2 py-0.5 text-sm hover:bg-raised"
                style={{ paddingLeft: `${depth * 12 + 8}px` }}
                draggable
                onDragStart={(event) => onDragStart(event, entry)}
              >
                {renaming === entry.path ? (
                  <form
                    className="flex flex-1 items-center gap-1"
                    onSubmit={(event) => void submitRename(event, entry)}
                  >
                    <input
                      className="min-w-0 flex-1 rounded border border-line bg-bg px-1 text-sm"
                      aria-label={t("files.renameTo", { name: entry.name })}
                      value={newName}
                      // biome-ignore lint/a11y/noAutofocus: the person just asked to rename this row
                      autoFocus
                      onChange={(event) => setNewName(event.target.value)}
                    />
                    <button type="submit" className={ACTION}>
                      {t("files.renameSave")}
                    </button>
                    <button type="button" className={ACTION} onClick={() => setRenaming(null)}>
                      {t("files.cancel")}
                    </button>
                  </form>
                ) : (
                  <>
                    <button
                      type="button"
                      className="flex min-w-0 flex-1 items-center gap-1 text-left"
                      title={entry.sensitive ? t("files.sensitive") : entry.path}
                      aria-expanded={entry.isDir ? open : undefined}
                      onClick={() => {
                        if (entry.isDir) toggle(entry);
                        else if (!entry.sensitive && !source?.remote) props.onPreview?.(entry.path);
                      }}
                    >
                      {entry.isDir ? (
                        open ? (
                          <ChevronDown size={12} aria-hidden="true" />
                        ) : (
                          <ChevronRight size={12} aria-hidden="true" />
                        )
                      ) : (
                        <span className="inline-block w-3" />
                      )}
                      {entry.isDir ? (
                        <Folder size={14} aria-hidden="true" />
                      ) : (
                        <File size={14} aria-hidden="true" />
                      )}
                      <span className="truncate">{entry.name}</span>
                      {entry.sensitive ? (
                        <Lock size={12} aria-label={t("files.sensitive")} />
                      ) : null}
                    </button>
                    <button
                      type="button"
                      className="rounded p-0.5 text-muted opacity-0 hover:text-fg focus:opacity-100 group-hover:opacity-100"
                      aria-label={t("files.actions", { name: entry.name })}
                      aria-expanded={menuFor === entry.path}
                      onClick={() => setMenuFor(menuFor === entry.path ? null : entry.path)}
                    >
                      <MoreHorizontal size={14} aria-hidden="true" />
                    </button>
                  </>
                )}
              </div>
              {renderActions(entry)}
              {confirming?.path === entry.path ? (
                <div
                  role="alertdialog"
                  aria-label={t("files.delete")}
                  className="flex items-center gap-2 py-1 pl-8 text-xs"
                >
                  <span className="flex-1">{t("files.confirmDelete", { name: entry.name })}</span>
                  <button type="button" className={ACTION} onClick={() => void remove(entry)}>
                    {t("files.delete")}
                  </button>
                  <button type="button" className={ACTION} onClick={() => setConfirming(null)}>
                    {t("files.cancel")}
                  </button>
                </div>
              ) : null}
              {entry.isDir && open ? (
                <ul className="m-0 list-none p-0">{renderDir(entry.path, depth + 1)}</ul>
              ) : null}
            </li>
          );
        })}
        {truncated ? (
          <li
            className="px-2 py-1 text-xs text-muted"
            style={{ paddingLeft: `${depth * 12 + 8}px` }}
          >
            {t("files.truncated", { count: entries.length })}
          </li>
        ) : null}
      </>
    );
  };

  if (root === null) {
    return <p className="p-3 text-sm text-muted">{t("pane.noWorkspace")}</p>;
  }
  if (source === null) {
    return <p className="p-3 text-sm text-muted">{t("pane.unavailable")}</p>;
  }
  return (
    <section
      className={`flex h-full min-h-0 flex-col ${props.hidden === true ? "invisible" : ""}`}
      aria-label={t("pane.files")}
      inert={props.hidden === true}
      data-testid="files-pane"
    >
      <header className="flex items-center gap-2 border-b border-line px-2 py-1 text-xs text-muted">
        <span className="min-w-0 flex-1 truncate" title={root}>
          {basenameOf(root) || root}
          {source.remote ? ` · ${t("files.remote")}` : ""}
        </span>
        <button
          type="button"
          className="rounded p-1 hover:bg-raised"
          aria-label={t("files.refresh")}
          onClick={() => {
            void load(root);
            for (const dir of expanded) void load(dir);
          }}
        >
          <RefreshCw size={12} aria-hidden="true" />
        </button>
      </header>
      {notice !== null ? (
        <p role="status" className="border-b border-line px-3 py-1 text-xs text-warn">
          {notice}
        </p>
      ) : null}
      <ul
        aria-label={t("files.tree", { folder: basenameOf(root) || root })}
        className="m-0 min-h-0 flex-1 list-none overflow-auto p-0"
      >
        {renderDir(root, 0)}
      </ul>
    </section>
  );
}
