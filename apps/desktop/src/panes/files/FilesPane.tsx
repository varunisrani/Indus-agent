// The files pane (P20-29): the session's working folder as a lazily expanded tree. Local mode reads
// this computer through main's fs IPC (confined to the window's folders and home, no secrets read);
// remote mode reads the remote host through the REST proxy. Rows can be previewed, renamed, moved
// to the trash (deleted on a remote, after asking), shown in the OS file manager, copied as a path
// and dragged (as a path) onto the composer. Expanded folders are watched in local mode.

import {
  ChevronsDownUp,
  File,
  Folder,
  FolderOpen,
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
import type { IndusDesktopBridge, IndusDesktopGit } from "../../global.d.ts";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import { PanelLabel } from "../../ui/components.tsx";
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
  /** Shift-click puts the row's path into the chat's composer. */
  addToChat?(text: string): void;
  /** Git status for the change tint (W52-m); defaults to the bridge's in local mode. */
  git?: Pick<IndusDesktopGit, "status"> | null;
}

/** How a changed file's name is tinted in the tree. */
type Tint = "modified" | "added" | "conflict";

function tintOf(index: string, worktree: string): Tint | null {
  if (index === "U" || worktree === "U" || (index === "A" && worktree === "A")) return "conflict";
  if (index === "?" || index === "A" || worktree === "A") return "added";
  if (index !== " " || worktree !== " ") return "modified";
  return null;
}

/** The path below `root` with forward slashes, or null outside it. */
function relativeTo(root: string, path: string): string | null {
  const norm = (value: string) => value.replaceAll("\\", "/").replace(/\/+$/, "");
  const base = norm(root);
  const full = norm(path);
  return full.startsWith(`${base}/`) ? full.slice(base.length + 1) : null;
}

type DirState =
  | { phase: "loading" }
  | { phase: "ready"; listing: Listing }
  | { phase: "error"; message: string };

const ACTION = "files-action";

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
  const [selected, setSelected] = useState<string | null>(null);
  const [tints, setTints] = useState<{ root: string; byPath: Map<string, Tint> } | null>(null);
  const [tintKey, setTintKey] = useState(0);
  const watchers = useRef(new Map<string, () => void>());
  const root = props.workspace;
  const git = useMemo(() => {
    if (props.git !== undefined) return props.git;
    if (props.mode === "remote") return null;
    return (desktopBridge() as IndusDesktopBridge | null)?.git ?? null;
  }, [props.git, props.mode]);

  // the change tint: git status of the folder, refreshed with the tree; not a repo → no tint
  // biome-ignore lint/correctness/useExhaustiveDependencies: tintKey asks again after a refresh
  useEffect(() => {
    setTints(null);
    if (git === null || root === null) return;
    let live = true;
    git
      .status({ repo: root })
      .then((status) => {
        if (!live) return;
        const byPath = new Map<string, Tint>();
        for (const file of status.files) {
          const tint = tintOf(file.index, file.worktree);
          if (tint !== null) byPath.set(file.path.replace(/\/$/, ""), tint);
        }
        setTints({ root: status.root, byPath });
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [git, root, tintKey]);

  const tintFor = (entry: FileEntry): Tint | undefined => {
    if (tints === null || entry.isDir) return undefined;
    const rel = relativeTo(tints.root, entry.path);
    return rel === null ? undefined : tints.byPath.get(rel);
  };

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
      <div className="files-menu">
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
              setMenuFor(null);
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
          onClick={() => {
            setMenuFor(null);
            void navigator.clipboard?.writeText(entry.path).catch(() => {});
          }}
        >
          {t("files.copyPath")}
        </button>
      </div>
    );
  };

  const indent = (depth: number) => ({ paddingLeft: `${depth * 10 + 16}px` });

  const renderDir = (dir: string, depth: number): ReactNode => {
    const state = dirs[dir];
    if (state === undefined || state.phase === "loading") {
      return (
        <li className="files-skeleton" aria-hidden={depth === 0 ? undefined : "true"}>
          {depth === 0 ? (
            [0, 1, 2, 3, 4].map((n) => <span key={n} style={{ width: `${70 - n * 9}%` }} />)
          ) : (
            <span style={{ ...indent(depth), width: "50%" }} />
          )}
        </li>
      );
    }
    if (state.phase === "error") {
      return depth === 0 ? (
        <li className="files-state">
          <p className="files-state-title">{t("files.errorTitle")}</p>
          <p role="alert" className="files-state-body">
            {t("files.loadFailed", { message: state.message })}
          </p>
          <button type="button" className="files-state-retry" onClick={() => void load(dir)}>
            {t("files.retry")}
          </button>
        </li>
      ) : (
        <li role="alert" className="files-note files-note-danger" style={indent(depth)}>
          {t("files.loadFailed", { message: state.message })}
        </li>
      );
    }
    const { entries, truncated } = state.listing;
    return (
      <>
        {entries.length === 0 ? (
          depth === 0 ? (
            <li className="files-state">
              <PanelLabel muted>{t("files.noFiles")}</PanelLabel>
            </li>
          ) : (
            <li className="files-note" style={indent(depth)}>
              {t("files.empty")}
            </li>
          )
        ) : null}
        {entries.map((entry) => {
          const open = expanded.has(entry.path);
          const tint = tintFor(entry);
          return (
            <li key={entry.path} className="files-item">
              {/* biome-ignore lint/a11y/noStaticElementInteractions: dragging and the context menu are pointer extras; the actions button is the keyboard way */}
              <div
                className="files-row"
                style={indent(depth)}
                data-selected={selected === entry.path ? "true" : undefined}
                data-tint={tint}
                draggable
                onDragStart={(event) => onDragStart(event, entry)}
                onContextMenu={(event) => {
                  event.preventDefault();
                  setSelected(entry.path);
                  setMenuFor(entry.path);
                }}
              >
                {renaming === entry.path ? (
                  <form
                    className="files-rename"
                    onSubmit={(event) => void submitRename(event, entry)}
                  >
                    <input
                      className="files-rename-input"
                      aria-label={t("files.renameTo", { name: entry.name })}
                      value={newName}
                      // biome-ignore lint/a11y/noAutofocus: the person just asked to rename this row
                      autoFocus
                      onChange={(event) => setNewName(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Escape") setRenaming(null);
                      }}
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
                      className="files-name"
                      title={entry.sensitive ? t("files.sensitive") : entry.path}
                      aria-expanded={entry.isDir ? open : undefined}
                      onClick={(event) => {
                        setSelected(entry.path);
                        if (event.shiftKey && props.addToChat !== undefined) {
                          props.addToChat(entry.path);
                          return;
                        }
                        if (entry.isDir) toggle(entry);
                        else if (!entry.sensitive && !source?.remote) props.onPreview?.(entry.path);
                      }}
                      onKeyDown={(event) => {
                        if (event.key === "F2" && source?.rename !== null && source !== null) {
                          event.preventDefault();
                          setRenaming(entry.path);
                          setNewName(entry.name);
                        }
                      }}
                    >
                      {entry.isDir ? (
                        open ? (
                          <FolderOpen size={14} aria-hidden="true" className="files-icon" />
                        ) : (
                          <Folder size={14} aria-hidden="true" className="files-icon" />
                        )
                      ) : (
                        <File size={14} aria-hidden="true" className="files-icon" />
                      )}
                      <span className="files-label">{entry.name}</span>
                      {entry.sensitive ? (
                        <Lock size={12} aria-label={t("files.sensitive")} className="files-lock" />
                      ) : null}
                    </button>
                    <button
                      type="button"
                      className="files-more"
                      aria-label={t("files.actions", { name: entry.name })}
                      aria-expanded={menuFor === entry.path}
                      onClick={() => setMenuFor(menuFor === entry.path ? null : entry.path)}
                    >
                      <MoreHorizontal size={13} aria-hidden="true" />
                    </button>
                  </>
                )}
              </div>
              {renderActions(entry)}
              {confirming?.path === entry.path ? (
                <div role="alertdialog" aria-label={t("files.delete")} className="files-confirm">
                  <span>{t("files.confirmDelete", { name: entry.name })}</span>
                  <span className="files-confirm-actions">
                    <button type="button" className={ACTION} onClick={() => setConfirming(null)}>
                      {t("files.cancel")}
                    </button>
                    <button
                      type="button"
                      className={`${ACTION} files-action-danger`}
                      onClick={() => void remove(entry)}
                    >
                      {t("files.delete")}
                    </button>
                  </span>
                </div>
              ) : null}
              {entry.isDir && open ? (
                <ul className="files-list">{renderDir(entry.path, depth + 1)}</ul>
              ) : null}
            </li>
          );
        })}
        {truncated ? (
          <li className="files-note" style={indent(depth)}>
            {t("files.truncated", { count: entries.length })}
          </li>
        ) : null}
      </>
    );
  };

  if (root === null || source === null) {
    return (
      <section className="files-pane" aria-label={t("pane.files")} data-testid="files-pane">
        <div className="files-state files-state-fill">
          <PanelLabel muted>{t("files.noFolder")}</PanelLabel>
          <p className="files-state-body">
            {root === null ? t("pane.noWorkspace") : t("pane.unavailable")}
          </p>
        </div>
      </section>
    );
  }
  const folder = basenameOf(root) || root;
  return (
    <section
      className="files-pane"
      data-hidden={props.hidden === true ? "true" : undefined}
      aria-label={t("pane.files")}
      inert={props.hidden === true}
      data-testid="files-pane"
    >
      <header className="side-pane-header">
        <PanelLabel title={root}>{folder}</PanelLabel>
        <span className="side-pane-spacer" />
        <span className="side-pane-actions" data-reveal="true">
          <button
            type="button"
            className="side-pane-btn"
            aria-label={t("files.refresh")}
            onClick={() => {
              void load(root);
              for (const dir of expanded) void load(dir);
              setTintKey((key) => key + 1);
            }}
          >
            <RefreshCw
              size={13}
              aria-hidden="true"
              className={dirs[root]?.phase === "loading" ? "side-pane-spin" : undefined}
            />
          </button>
        </span>
        {expanded.size > 0 ? (
          <button
            type="button"
            className="side-pane-btn"
            aria-label={t("files.collapseAll")}
            onClick={() => {
              for (const dir of expanded) unwatch(dir);
              setExpanded(new Set());
            }}
          >
            <ChevronsDownUp size={13} aria-hidden="true" />
          </button>
        ) : null}
      </header>
      {source.remote ? <p className="files-note files-remote">{t("files.remote")}</p> : null}
      {notice !== null ? (
        <p role="status" className="files-note files-note-warn">
          {notice}
        </p>
      ) : null}
      <ul aria-label={t("files.tree", { folder })} className="files-list files-tree">
        {renderDir(root, 0)}
      </ul>
    </section>
  );
}
