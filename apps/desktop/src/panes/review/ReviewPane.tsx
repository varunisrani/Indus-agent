// The git review pane (P20-30). Everything runs in main through registered `indus:git:*` channels
// (git confined to the window's folders, no shell, no option injection); this pane only shows and
// asks. Scopes: Uncommitted (stage / unstage / revert, commit, commit & push), Branch and Last turn
// (read-only). "Write it for me" asks the agent for a message; Create PR uses the GitHub CLI when it
// is installed and otherwise offers to ask the agent.

import {
  Check,
  ChevronDown,
  Folder,
  GitPullRequest,
  List,
  ListTree,
  Minus,
  Plus,
  RefreshCw,
  Sparkles,
  SquareArrowRight,
  SquareDot,
  SquareMinus,
  SquarePlus,
  TriangleAlert,
  Undo2,
  X,
} from "lucide-react";
import {
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { desktopBridge } from "../../app/bridge.ts";
import { notify, type ToastTone } from "../../app/Toasts.tsx";
import type { IndusDesktopBridge } from "../../global.d.ts";
import { type MessageKey, useT } from "../../i18n/index.tsx";
import { Button, Menu, PanelLabel, SmallDialog } from "../../ui/components.tsx";
import { commitMessagePrompt } from "./agentDraft.ts";

export type ReviewScope = "uncommitted" | "branch" | "last-turn";
export type GitBridge = Pick<IndusDesktopBridge, "git">;

type Status = Awaited<ReturnType<IndusDesktopBridge["git"]["status"]>>;
type Changes = Awaited<ReturnType<IndusDesktopBridge["git"]["changes"]>>;
type ChangedFile = Changes["files"][number];

export interface ReviewPaneProps {
  /** A folder inside the repository (the session's working folder). */
  repo: string | null;
  hidden?: boolean;
  bridge?: GitBridge | null;
  /** HEAD when the session's last turn started (see `trackTurnBase`). */
  lastTurnBase?: string | null;
  /** Ask the agent for a commit message from this prompt (absent: the button is hidden). */
  draftMessage?(prompt: string): Promise<string>;
  /** Send a request to the agent in the conversation (the PR fallback without `gh`). */
  askAgent?(prompt: string): void;
  /** Close the pane (the header's X; absent: no close button). */
  onClose?(): void;
  /** Open a changed file (absolute path) in the preview (double click on a row). */
  onPreview?(path: string): void;
}

/** The main action of the split commit button, remembered for this window (W52-m). */
let rememberedCommit: "commit" | "push" = "commit";

const SCOPES: ReadonlyArray<{ scope: ReviewScope; key: MessageKey }> = [
  { scope: "uncommitted", key: "review.scope.uncommitted" },
  { scope: "branch", key: "review.scope.branch" },
  { scope: "last-turn", key: "review.scope.lastTurn" },
];

function errorText(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error);
  const at = text.indexOf(": ");
  return at > 0 && /^[a-z-]+$/.test(text.slice(0, at)) ? text.slice(at + 2) : text;
}

function letterOf(file: ChangedFile): string {
  if (file.index === "?") return "?";
  return file.index !== " " ? file.index : file.worktree;
}

function isStaged(file: ChangedFile): boolean {
  return file.index !== " " && file.index !== "?";
}

function lineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) return "text-muted review-diff-file";
  if (line.startsWith("diff ") || line.startsWith("index ")) return "review-diff-file";
  if (line.startsWith("+")) return "text-ok review-diff-add";
  if (line.startsWith("-")) return "text-danger review-diff-del";
  if (line.startsWith("@@")) return "text-accent review-diff-hunk";
  return "";
}

/** The square status glyph of a changed file, by its git letter. */
function statusGlyph(letter: string): ReactNode {
  switch (letter) {
    case "M":
      return (
        <SquareDot size={13} aria-hidden="true" className="review-glyph" data-tone="modified" />
      );
    case "A":
    case "C":
      return <SquarePlus size={13} aria-hidden="true" className="review-glyph" data-tone="added" />;
    case "D":
      return (
        <SquareMinus size={13} aria-hidden="true" className="review-glyph" data-tone="deleted" />
      );
    case "R":
      return (
        <SquareArrowRight
          size={13}
          aria-hidden="true"
          className="review-glyph"
          data-tone="renamed"
        />
      );
    case "U":
      return (
        <TriangleAlert size={13} aria-hidden="true" className="review-glyph" data-tone="deleted" />
      );
    default:
      return <SquarePlus size={13} aria-hidden="true" className="review-glyph" data-tone="new" />;
  }
}

function Counts(props: { added?: number | undefined; removed?: number | undefined }) {
  if (props.added === undefined && props.removed === undefined) return null;
  return (
    <span className="review-counts">
      {(props.added ?? 0) > 0 || props.removed === undefined ? (
        <span className="review-add">+{props.added ?? 0}</span>
      ) : null}{" "}
      {(props.removed ?? 0) > 0 ? <span className="review-del">−{props.removed}</span> : null}
    </span>
  );
}

/** A file path split into its name and its folder. */
function splitPath(path: string): { name: string; dir: string } {
  const trimmed = path.replace(/\/$/, "");
  const at = trimmed.lastIndexOf("/");
  return at < 0
    ? { name: trimmed, dir: "" }
    : { name: trimmed.slice(at + 1), dir: trimmed.slice(0, at) };
}

function DiffView(props: { diff: string; truncated: boolean }) {
  const t = useT();
  const lines = props.diff.replace(/\n$/, "").split("\n");
  return (
    <div className="review-diff-body">
      {props.truncated ? <p className="review-note">{t("review.diffTruncated")}</p> : null}
      {props.diff.trim() === "" ? <p className="review-diff-empty">{t("review.noDiff")}</p> : null}
      <pre className="review-diff" data-testid="review-diff">
        {lines.map((line, index) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: diff lines have no identity beyond position
          <div key={index} className={lineClass(line)}>
            {line === "" ? " " : line}
          </div>
        ))}
      </pre>
    </div>
  );
}

/** Group paths by folder for the tree view. */
function byFolder(files: ChangedFile[]): Array<[string, ChangedFile[]]> {
  const groups = new Map<string, ChangedFile[]>();
  for (const file of files) {
    const trimmed = file.path.replace(/\/$/, "");
    const at = trimmed.lastIndexOf("/");
    const folder = at < 0 ? "" : trimmed.slice(0, at);
    const list = groups.get(folder) ?? [];
    list.push(file);
    groups.set(folder, list);
  }
  return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

export function ReviewPane(props: ReviewPaneProps) {
  const t = useT();
  const bridge = props.bridge === undefined ? (desktopBridge() as GitBridge | null) : props.bridge;
  const git = bridge?.git ?? null;
  const [scope, setScope] = useState<ReviewScope>("uncommitted");
  const [layout, setLayout] = useState<"list" | "tree">("list");
  const [status, setStatus] = useState<Status | null>(null);
  const [changes, setChanges] = useState<Changes | null>(null);
  const [error, setError] = useState<string | null>(null);
  // the pane's last outcome: shown as a toast in the window's region, mirrored for screen readers
  const [notice, setNotice] = useState<{ text: string; tone: ToastTone } | null>(null);
  useEffect(() => {
    if (notice !== null) notify({ id: "review-notice", tone: notice.tone, title: notice.text });
  }, [notice]);
  const [selected, setSelected] = useState<string | null>(null);
  const [diff, setDiff] = useState<{ diff: string; truncated: boolean } | null>(null);
  const [confirm, setConfirm] = useState<{ paths: string[]; label: string } | null>(null);
  const [message, setMessage] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [prTitle, setPrTitle] = useState<string | null>(null);
  const [noGh, setNoGh] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [commitMode, setCommitMode] = useState<"commit" | "push">(rememberedCommit);
  const messageRef = useRef<HTMLTextAreaElement>(null);

  // the message field grows with its text up to 160px
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-measure when the text changes
  useEffect(() => {
    const field = messageRef.current;
    if (field === null) return;
    field.style.height = "auto";
    field.style.height = `${Math.min(field.scrollHeight, 160)}px`;
    field.style.overflowY = field.scrollHeight > 160 ? "auto" : "hidden";
  }, [message]);

  const root = status?.root ?? props.repo;
  const base = props.lastTurnBase ?? undefined;

  const refresh = useCallback(() => setRefreshKey((key) => key + 1), []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refreshKey asks main again after a change
  useEffect(() => {
    if (git === null || props.repo === null) return;
    let live = true;
    const repo = props.repo;
    (async () => {
      try {
        const nextStatus = await git.status({ repo });
        const nextChanges =
          scope === "uncommitted"
            ? {
                files: nextStatus.files,
                base: null,
                readOnly: false,
                truncated: nextStatus.truncated,
              }
            : await git.changes({
                repo: nextStatus.root,
                scope,
                ...(scope === "last-turn" && base !== undefined ? { base } : {}),
              });
        if (!live) return;
        setStatus(nextStatus);
        setChanges(nextChanges);
        setError(null);
      } catch (cause) {
        if (live) setError(errorText(cause));
      }
    })();
    return () => {
      live = false;
    };
  }, [git, props.repo, scope, base, refreshKey]);

  // The selected file's diff.
  useEffect(() => {
    if (git === null || root === null || selected === null || changes === null) {
      setDiff(null);
      return;
    }
    const file = changes.files.find((f) => f.path === selected);
    if (file === undefined) {
      setDiff(null);
      return;
    }
    let live = true;
    git
      .diff({
        repo: root,
        scope,
        path: file.path,
        ...(scope === "last-turn" && base !== undefined ? { base } : {}),
        ...(scope === "uncommitted" && isStaged(file) && file.worktree === " "
          ? { staged: true }
          : {}),
      })
      .then((answer) => {
        if (live) setDiff({ diff: answer.diff, truncated: answer.truncated });
      })
      .catch((cause: unknown) => {
        if (live) setNotice({ text: errorText(cause), tone: "error" });
      });
    return () => {
      live = false;
    };
  }, [git, root, selected, changes, scope, base]);

  const run = async (action: MessageKey, work: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await work();
      setNotice(null);
      return true;
    } catch (cause) {
      setNotice({
        text: t("review.failed", { action: t(action), message: errorText(cause) }),
        tone: "error",
      });
      return false;
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const mutate = (action: "stage" | "unstage" | "revert", paths: string[]) => {
    if (git === null || root === null) return;
    const key: MessageKey =
      action === "stage"
        ? "review.stage"
        : action === "unstage"
          ? "review.unstage"
          : "review.revert";
    void run(key, () => git[action]({ repo: root, paths }));
  };

  const commit = async (push: boolean) => {
    if (git === null || root === null || message.trim() === "") return;
    let answer: { commit: string; pushed: boolean } | null = null;
    const ok = await run(push ? "review.commitPush" : "review.commit", async () => {
      answer = await git.commit({ repo: root, message: message.trim(), ...(push ? { push } : {}) });
    });
    const done = answer as { commit: string; pushed: boolean } | null;
    if (ok && done !== null) {
      setMessage("");
      setSelected(null);
      const short = done.commit.slice(0, 7);
      setNotice({
        text: done.pushed
          ? t("review.pushed", { commit: short })
          : t("review.committed", { commit: short }),
        tone: "success",
      });
    }
  };

  const generate = async () => {
    if (git === null || root === null || props.draftMessage === undefined) return;
    setDrafting(true);
    try {
      const context = await git.commitContext({ repo: root });
      setMessage(await props.draftMessage(commitMessagePrompt(context)));
      setNotice(null);
    } catch (cause) {
      setNotice({
        text: t("review.failed", { action: t("review.generate"), message: errorText(cause) }),
        tone: "error",
      });
    } finally {
      setDrafting(false);
    }
  };

  const createPr = async (event: FormEvent) => {
    event.preventDefault();
    if (git === null || root === null || prTitle === null || prTitle.trim() === "") return;
    setBusy(true);
    try {
      const answer = await git.createPr({ repo: root, title: prTitle.trim() });
      if (!answer.ghAvailable) setNoGh(true);
      else {
        setPrTitle(null);
        setNotice(
          answer.url === null
            ? null
            : { text: t("review.prOpened", { url: answer.url }), tone: "success" },
        );
      }
    } catch (cause) {
      setNotice({
        text: t("review.failed", { action: t("review.createPr"), message: errorText(cause) }),
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  };

  const files = changes?.files ?? [];
  const readOnly = changes?.readOnly ?? scope !== "uncommitted";
  const groups = useMemo(
    () => (layout === "tree" ? byFolder(files) : [["", files] as [string, ChangedFile[]]]),
    [layout, files],
  );

  if (props.repo === null || git === null) {
    return (
      <section className="review-pane" aria-label={t("pane.review")} data-testid="review-pane">
        <div className="review-empty">
          <PanelLabel muted>{t("files.noFolder")}</PanelLabel>
          <p className="review-note">
            {props.repo === null ? t("pane.noWorkspace") : t("pane.unavailable")}
          </p>
        </div>
      </section>
    );
  }

  const notRepo = error !== null && /not in a git repository/.test(error);
  const branchLine =
    status === null
      ? ""
      : [
          status.branch === null
            ? t("review.detached")
            : t("review.branch", { branch: status.branch }),
          status.upstream === null
            ? t("review.noUpstream")
            : t("review.aheadBehind", {
                ahead: status.ahead,
                behind: status.behind,
                upstream: status.upstream,
              }),
        ].join(" · ");
  const drifted = status !== null && (status.ahead !== 0 || status.behind !== 0);
  const anyStaged = files.some(isStaged);
  const commitPush = commitMode === "push";
  const canCommit = !busy && message.trim() !== "";

  const fileRow = (file: ChangedFile, depth: number) => {
    const { name, dir } = splitPath(file.path);
    return (
      <li
        key={file.path}
        className="review-row"
        data-selected={selected === file.path ? "true" : undefined}
        style={{ paddingLeft: `${14 + depth * 12}px` }}
      >
        {statusGlyph(letterOf(file))}
        <button
          type="button"
          className="review-row-main"
          aria-pressed={selected === file.path}
          title={file.path}
          onClick={() => setSelected(file.path)}
          onDoubleClick={() => {
            if (root !== null) props.onPreview?.(`${root.replace(/\/$/, "")}/${file.path}`);
          }}
        >
          <span className="review-name">{name}</span>
          {layout === "list" && dir !== "" ? <span className="review-dir">{dir}</span> : null}
        </button>
        <Counts added={file.added} removed={file.removed} />
        {readOnly ? null : (
          <span className="review-row-actions">
            {isStaged(file) ? (
              <button
                type="button"
                className="review-icon-btn"
                aria-label={t("review.unstage")}
                title={t("review.unstage")}
                disabled={busy}
                onClick={() => mutate("unstage", [file.path])}
              >
                <Minus size={12} aria-hidden="true" />
              </button>
            ) : (
              <button
                type="button"
                className="review-icon-btn"
                aria-label={t("review.stage")}
                title={t("review.stage")}
                disabled={busy}
                onClick={() => mutate("stage", [file.path])}
              >
                <Plus size={12} aria-hidden="true" />
              </button>
            )}
            <button
              type="button"
              className="review-icon-btn"
              data-tone="danger"
              aria-label={t("review.revert")}
              title={t("review.revert")}
              disabled={busy}
              onClick={() =>
                setConfirm({
                  paths: [file.path],
                  label: t("review.confirmRevert", { name: file.path }),
                })
              }
            >
              <Undo2 size={12} aria-hidden="true" />
            </button>
          </span>
        )}
      </li>
    );
  };

  const selectedFile = files.find((file) => file.path === selected) ?? null;
  const onMessageKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      if (canCommit) void commit(commitPush);
    }
  };

  return (
    <section
      className="review-pane"
      data-hidden={props.hidden === true ? "true" : undefined}
      aria-label={t("pane.review")}
      inert={props.hidden === true}
      data-testid="review-pane"
    >
      {notRepo ? null : (
        <header className="side-pane-header">
          <PanelLabel title={branchLine}>{t("pane.review")}</PanelLabel>
          <span className="side-pane-spacer" />
          <span className="review-header-actions">
            <span role="radiogroup" aria-label={t("review.files")} className="review-view-toggle">
              {(["list", "tree"] as const).map((mode) => (
                // biome-ignore lint/a11y/useSemanticElements: a two-state toggle shown as one icon
                <button
                  key={mode}
                  type="button"
                  role="radio"
                  aria-checked={layout === mode}
                  aria-label={t(mode === "list" ? "review.view.list" : "review.view.tree")}
                  title={t(mode === "list" ? "review.view.list" : "review.view.tree")}
                  className="side-pane-btn"
                  disabled={files.length === 0}
                  onClick={() => setLayout(mode)}
                >
                  {mode === "list" ? (
                    <List size={13} aria-hidden="true" />
                  ) : (
                    <ListTree size={13} aria-hidden="true" />
                  )}
                </button>
              ))}
            </span>
            {readOnly ? null : (
              <>
                {anyStaged ? (
                  <button
                    type="button"
                    className="side-pane-btn"
                    aria-label={t("review.unstageAll")}
                    title={t("review.unstageAll")}
                    disabled={busy}
                    onClick={() => mutate("unstage", [])}
                  >
                    <Minus size={13} aria-hidden="true" />
                  </button>
                ) : null}
                <button
                  type="button"
                  className="side-pane-btn"
                  aria-label={t("review.stageAll")}
                  title={t("review.stageAll")}
                  disabled={busy || files.length === 0}
                  onClick={() => mutate("stage", [])}
                >
                  <Plus size={13} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  className="side-pane-btn"
                  aria-label={t("review.revertAll")}
                  title={t("review.revertAll")}
                  disabled={busy || files.length === 0}
                  onClick={() => setConfirm({ paths: [], label: t("review.confirmRevertAll") })}
                >
                  <Undo2 size={13} aria-hidden="true" />
                </button>
              </>
            )}
            <button
              type="button"
              className="side-pane-btn"
              aria-label={t("files.refresh")}
              title={t("files.refresh")}
              onClick={refresh}
            >
              <RefreshCw
                size={13}
                aria-hidden="true"
                className={changes === null && error === null ? "side-pane-spin" : undefined}
              />
            </button>
            {props.onClose === undefined ? null : (
              <button
                type="button"
                className="side-pane-btn"
                aria-label={t("review.close")}
                title={t("review.close")}
                onClick={props.onClose}
              >
                <X size={13} aria-hidden="true" />
              </button>
            )}
          </span>
        </header>
      )}

      {notRepo ? null : (
        <div className="review-scope">
          <div role="radiogroup" aria-label={t("review.scopes")} className="review-segmented">
            {SCOPES.map((entry) => (
              // biome-ignore lint/a11y/useSemanticElements: a segmented control, not a native radio
              <button
                key={entry.scope}
                type="button"
                role="radio"
                aria-checked={scope === entry.scope}
                className="review-segment"
                onClick={() => {
                  setScope(entry.scope);
                  setSelected(null);
                }}
              >
                {t(entry.key)}
              </button>
            ))}
          </div>
          {status === null ? null : (
            <p className={drifted ? "review-branch" : "sr-only"}>
              <span>
                {status.branch === null
                  ? t("review.detached")
                  : t("review.branch", { branch: status.branch })}
              </span>
              {" · "}
              <span>
                {status.upstream === null
                  ? t("review.noUpstream")
                  : t("review.aheadBehind", {
                      ahead: status.ahead,
                      behind: status.behind,
                      upstream: status.upstream,
                    })}
              </span>
            </p>
          )}
        </div>
      )}

      {error !== null ? (
        notRepo ? (
          <div className="review-empty">
            <PanelLabel muted>{t("review.noChangesLabel")}</PanelLabel>
            <p role="alert" className="review-note">
              {t("review.notRepo")}
            </p>
          </div>
        ) : (
          <p role="alert" className="review-note review-note-danger">
            {error}
          </p>
        )
      ) : null}

      <div className="review-files">
        {changes === null && error === null ? (
          <div className="files-skeleton" aria-hidden="true">
            {[0, 1, 2].map((n) => (
              <span key={n} style={{ width: `${70 - n * 12}%` }} />
            ))}
          </div>
        ) : files.length === 0 && error === null ? (
          <div className="review-empty">
            <PanelLabel muted>{t("review.noChangesLabel")}</PanelLabel>
            <span className="sr-only">
              {scope !== "uncommitted" && changes?.base === null
                ? t("review.noBase")
                : t("review.noChanges")}
            </span>
          </div>
        ) : files.length === 0 ? null : (
          <section aria-label={t("review.files")}>
            {groups.map(([folder, list]) => (
              <div key={folder}>
                {layout === "tree" && folder !== "" ? (
                  <p className="review-folder">
                    <Folder size={13} aria-hidden="true" className="files-icon" />
                    <span className="review-name">{folder}</span>
                  </p>
                ) : null}
                <ul className="files-list">
                  {list.map((file) => fileRow(file, layout === "tree" && folder !== "" ? 1 : 0))}
                </ul>
              </div>
            ))}
            {changes?.truncated ? <p className="review-note">{t("review.truncated")}</p> : null}
          </section>
        )}
      </div>

      {selected === null ? null : (
        <section aria-label={t("review.diffFor", { name: selected })} className="review-diff-strip">
          <div className="review-diff-head">
            <span className="review-diff-path" title={selected}>
              {selected}
            </span>
            {selectedFile === null ? null : (
              <Counts added={selectedFile.added} removed={selectedFile.removed} />
            )}
            {readOnly || selectedFile === null || isStaged(selectedFile) ? null : (
              <button
                type="button"
                className="side-pane-btn"
                aria-label={t("review.stage")}
                title={t("review.stage")}
                disabled={busy}
                onClick={() => mutate("stage", [selectedFile.path])}
              >
                <Plus size={13} aria-hidden="true" />
              </button>
            )}
            <button
              type="button"
              className="side-pane-btn"
              aria-label={t("review.closeDiff")}
              title={t("review.closeDiff")}
              onClick={() => setSelected(null)}
            >
              <X size={13} aria-hidden="true" />
            </button>
          </div>
          {diff === null ? (
            <div className="files-skeleton" aria-hidden="true">
              <span style={{ width: "60%" }} />
              <span style={{ width: "40%" }} />
            </div>
          ) : (
            <DiffView diff={diff.diff} truncated={diff.truncated} />
          )}
        </section>
      )}

      {notice !== null ? (
        <p role="status" className="sr-only">
          {notice.text}
        </p>
      ) : null}

      {confirm !== null ? (
        <SmallDialog
          role="alertdialog"
          title={t("review.revert")}
          description={confirm.label}
          onClose={() => setConfirm(null)}
          actions={
            <>
              <Button variant="ghost" onClick={() => setConfirm(null)}>
                {t("files.cancel")}
              </Button>
              <Button
                variant="danger"
                onClick={() => {
                  const paths = confirm.paths;
                  setConfirm(null);
                  mutate("revert", paths);
                }}
              >
                {t("review.confirm")}
              </Button>
            </>
          }
        >
          {confirm.paths.length > 0 ? (
            <p className="review-dialog-path">{confirm.paths.join(", ")}</p>
          ) : null}
        </SmallDialog>
      ) : null}

      {readOnly && !notRepo ? <p className="review-readonly">{t("review.readOnly")}</p> : null}

      {scope === "uncommitted" && !notRepo && files.length > 0 ? (
        <footer className="review-ship">
          <div className="review-message">
            <textarea
              ref={messageRef}
              rows={1}
              className="review-message-input"
              aria-label={t("review.message")}
              placeholder={t("review.messagePlaceholder")}
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              onKeyDown={onMessageKey}
            />
            {props.draftMessage !== undefined ? (
              <button
                type="button"
                className="review-sparkle"
                aria-label={drafting ? t("review.generating") : t("review.generate")}
                title={drafting ? t("review.generating") : t("review.generate")}
                disabled={drafting || busy}
                onClick={() => void generate()}
              >
                <Sparkles
                  size={13}
                  aria-hidden="true"
                  className={drafting ? "side-pane-spin" : undefined}
                />
              </button>
            ) : null}
          </div>
          <div className="review-split">
            <button
              type="button"
              className="review-split-main"
              disabled={!canCommit}
              onClick={() => void commit(commitPush)}
            >
              <Check size={13} aria-hidden="true" />
              {commitPush ? t("review.commitPush") : t("review.commit")}
            </button>
            <Menu
              label={t("review.commitOptions")}
              trigger={<ChevronDown size={13} aria-hidden="true" />}
              triggerClassName="review-split-more"
              side="up"
              align="end"
              items={[
                {
                  id: "commit",
                  label: t("review.commit"),
                  run: () => {
                    rememberedCommit = "commit";
                    setCommitMode("commit");
                    if (canCommit) void commit(false);
                  },
                },
                {
                  id: "push",
                  label: t("review.commitPush"),
                  run: () => {
                    rememberedCommit = "push";
                    setCommitMode("push");
                    if (canCommit) void commit(true);
                  },
                },
              ]}
            />
          </div>
          <div className="review-agent">
            {props.askAgent !== undefined ? (
              <button
                type="button"
                className="review-agent-link"
                onClick={() =>
                  props.askAgent?.(
                    `Open a pull request for the current branch${status?.branch ? ` (${status.branch})` : ""}. Push the branch first if needed.`,
                  )
                }
              >
                {t("review.askAgentShort")}
              </button>
            ) : (
              <span />
            )}
            <button
              type="button"
              className="side-pane-btn review-pr"
              aria-label={t("review.createPr")}
              title={t("review.createPr")}
              disabled={busy}
              onClick={() => {
                setNoGh(false);
                setPrTitle(status?.branch ?? "");
              }}
            >
              <GitPullRequest size={13} aria-hidden="true" />
            </button>
          </div>
          {prTitle !== null ? (
            <form className="review-pr-form" onSubmit={(event) => void createPr(event)}>
              <input
                className="review-pr-input"
                aria-label={t("review.prTitle")}
                value={prTitle}
                onChange={(event) => setPrTitle(event.target.value)}
              />
              <button
                type="submit"
                className="files-action"
                disabled={busy || prTitle.trim() === ""}
              >
                {t("review.createPr")}
              </button>
              <button type="button" className="files-action" onClick={() => setPrTitle(null)}>
                {t("files.cancel")}
              </button>
            </form>
          ) : null}
          {noGh ? (
            <div role="status" className="review-pr-form">
              <span className="review-note">{t("review.noGh")}</span>
              {props.askAgent !== undefined ? (
                <button
                  type="button"
                  className="files-action"
                  onClick={() => {
                    props.askAgent?.(
                      `Open a pull request for the current branch${status?.branch ? ` (${status.branch})` : ""} titled "${prTitle ?? ""}". Push the branch first if needed.`,
                    );
                    setNoGh(false);
                    setPrTitle(null);
                  }}
                >
                  {t("review.askAgentPr")}
                </button>
              ) : null}
            </div>
          ) : null}
        </footer>
      ) : null}
    </section>
  );
}
