// The git review pane (P20-30). Everything runs in main through registered `indus:git:*` channels
// (git confined to the window's folders, no shell, no option injection); this pane only shows and
// asks. Scopes: Uncommitted (stage / unstage / revert, commit, commit & push), Branch and Last turn
// (read-only). "Write it for me" asks the agent for a message; Create PR uses the GitHub CLI when it
// is installed and otherwise offers to ask the agent.

import { RefreshCw } from "lucide-react";
import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { desktopBridge } from "../../app/bridge.ts";
import { Loader } from "../../app/Loader.tsx";
import type { IndusDesktopBridge } from "../../global.d.ts";
import { type MessageKey, useT } from "../../i18n/index.tsx";
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
}

const SCOPES: ReadonlyArray<{ scope: ReviewScope; key: MessageKey }> = [
  { scope: "uncommitted", key: "review.scope.uncommitted" },
  { scope: "branch", key: "review.scope.branch" },
  { scope: "last-turn", key: "review.scope.lastTurn" },
];

const BUTTON =
  "rounded border border-line px-2 py-0.5 text-xs hover:bg-raised disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-focus";

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
  if (line.startsWith("+++") || line.startsWith("---")) return "text-muted";
  if (line.startsWith("+")) return "text-ok";
  if (line.startsWith("-")) return "text-danger";
  if (line.startsWith("@@")) return "text-accent";
  return "";
}

function DiffView(props: { diff: string; truncated: boolean }) {
  const t = useT();
  const lines = props.diff.replace(/\n$/, "").split("\n");
  return (
    <div className="min-h-0 flex-1 overflow-auto">
      {props.truncated ? (
        <p className="px-3 py-1 text-xs text-warn">{t("review.diffTruncated")}</p>
      ) : null}
      <pre className="m-0 p-2 font-mono text-xs" data-testid="review-diff">
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
  const [notice, setNotice] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [diff, setDiff] = useState<{ diff: string; truncated: boolean } | null>(null);
  const [confirm, setConfirm] = useState<{ paths: string[]; label: string } | null>(null);
  const [message, setMessage] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [prTitle, setPrTitle] = useState<string | null>(null);
  const [noGh, setNoGh] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

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
        if (live) setNotice(errorText(cause));
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
      setNotice(t("review.failed", { action: t(action), message: errorText(cause) }));
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
      setNotice(
        done.pushed
          ? t("review.pushed", { commit: short })
          : t("review.committed", { commit: short }),
      );
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
      setNotice(t("review.failed", { action: t("review.generate"), message: errorText(cause) }));
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
        setNotice(answer.url === null ? null : t("review.prOpened", { url: answer.url }));
      }
    } catch (cause) {
      setNotice(t("review.failed", { action: t("review.createPr"), message: errorText(cause) }));
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

  if (props.repo === null) return <p className="p-3 text-sm text-muted">{t("pane.noWorkspace")}</p>;
  if (git === null) return <p className="p-3 text-sm text-muted">{t("pane.unavailable")}</p>;

  const fileRow = (file: ChangedFile) => (
    <li key={file.path} className="flex items-center gap-2 px-2 py-0.5 text-sm hover:bg-raised">
      <span className="w-4 font-mono text-xs text-muted" aria-hidden="true">
        {letterOf(file)}
      </span>
      <button
        type="button"
        className={`min-w-0 flex-1 truncate text-left ${selected === file.path ? "font-medium" : ""}`}
        aria-pressed={selected === file.path}
        onClick={() => setSelected(file.path)}
      >
        {layout === "tree" ? file.path.replace(/\/$/, "").split("/").pop() : file.path}
      </button>
      {file.added !== undefined || file.removed !== undefined ? (
        <span className="font-mono text-xs">
          <span className="text-ok">+{file.added ?? 0}</span>{" "}
          <span className="text-danger">−{file.removed ?? 0}</span>
        </span>
      ) : null}
      {readOnly ? null : (
        <>
          {isStaged(file) ? (
            <button
              type="button"
              className={BUTTON}
              disabled={busy}
              onClick={() => mutate("unstage", [file.path])}
            >
              {t("review.unstage")}
            </button>
          ) : (
            <button
              type="button"
              className={BUTTON}
              disabled={busy}
              onClick={() => mutate("stage", [file.path])}
            >
              {t("review.stage")}
            </button>
          )}
          <button
            type="button"
            className={BUTTON}
            disabled={busy}
            onClick={() =>
              setConfirm({
                paths: [file.path],
                label: t("review.confirmRevert", { name: file.path }),
              })
            }
          >
            {t("review.revert")}
          </button>
        </>
      )}
    </li>
  );

  return (
    <section
      className={`flex h-full min-h-0 flex-col ${props.hidden === true ? "invisible" : ""}`}
      aria-label={t("pane.review")}
      inert={props.hidden === true}
      data-testid="review-pane"
    >
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-2 py-1 text-xs">
        <span className="font-medium">
          {status === null
            ? null
            : status.branch === null
              ? t("review.detached")
              : t("review.branch", { branch: status.branch })}
        </span>
        {status === null ? null : status.upstream === null ? (
          <span className="text-muted">{t("review.noUpstream")}</span>
        ) : (
          <span className="text-muted">
            {t("review.aheadBehind", {
              ahead: status.ahead,
              behind: status.behind,
              upstream: status.upstream,
            })}
          </span>
        )}
        <span className="flex-1" />
        <button
          type="button"
          className="rounded p-1 hover:bg-raised"
          aria-label={t("files.refresh")}
          onClick={refresh}
        >
          <RefreshCw size={12} aria-hidden="true" />
        </button>
      </header>

      <div className="flex items-center gap-2 border-b border-line px-2 py-1">
        <div role="radiogroup" aria-label={t("review.scopes")} className="flex gap-1">
          {SCOPES.map((entry) => (
            // biome-ignore lint/a11y/useSemanticElements: a segmented control, not a native radio
            <button
              key={entry.scope}
              type="button"
              role="radio"
              aria-checked={scope === entry.scope}
              className={`${BUTTON} ${scope === entry.scope ? "bg-raised font-medium" : ""}`}
              onClick={() => {
                setScope(entry.scope);
                setSelected(null);
              }}
            >
              {t(entry.key)}
            </button>
          ))}
        </div>
        <span className="flex-1" />
        <div role="radiogroup" aria-label={t("review.files")} className="flex gap-1">
          {(["list", "tree"] as const).map((mode) => (
            // biome-ignore lint/a11y/useSemanticElements: a segmented control, not a native radio
            <button
              key={mode}
              type="button"
              role="radio"
              aria-checked={layout === mode}
              className={`${BUTTON} ${layout === mode ? "bg-raised" : ""}`}
              onClick={() => setLayout(mode)}
            >
              {t(mode === "list" ? "review.view.list" : "review.view.tree")}
            </button>
          ))}
        </div>
      </div>

      {error !== null ? (
        <p role="alert" className="px-3 py-2 text-sm text-danger">
          {/not in a git repository/.test(error) ? t("review.notRepo") : error}
        </p>
      ) : null}
      {notice !== null ? (
        <p role="status" className="border-b border-line px-3 py-1 text-xs">
          {notice}
        </p>
      ) : null}
      {readOnly ? <p className="px-3 py-1 text-xs text-muted">{t("review.readOnly")}</p> : null}

      {confirm !== null ? (
        <div
          role="alertdialog"
          aria-label={t("review.revert")}
          className="flex items-center gap-2 border-b border-line px-3 py-1 text-xs"
        >
          <span className="flex-1">{confirm.label}</span>
          <button
            type="button"
            className={BUTTON}
            onClick={() => {
              const paths = confirm.paths;
              setConfirm(null);
              mutate("revert", paths);
            }}
          >
            {t("review.confirm")}
          </button>
          <button type="button" className={BUTTON} onClick={() => setConfirm(null)}>
            {t("files.cancel")}
          </button>
        </div>
      ) : null}

      <div className="flex min-h-0 flex-1 flex-col">
        {changes === null && error === null ? (
          <Loader />
        ) : files.length === 0 && error === null ? (
          <p className="px-3 py-2 text-sm text-muted">
            {scope !== "uncommitted" && changes?.base === null
              ? t("review.noBase")
              : t("review.noChanges")}
          </p>
        ) : (
          <section
            className="max-h-[45%] overflow-auto border-b border-line"
            aria-label={t("review.files")}
          >
            {readOnly ? null : (
              <div className="flex gap-1 px-2 py-1">
                <button
                  type="button"
                  className={BUTTON}
                  disabled={busy}
                  onClick={() => mutate("stage", [])}
                >
                  {t("review.stageAll")}
                </button>
                <button
                  type="button"
                  className={BUTTON}
                  disabled={busy}
                  onClick={() => mutate("unstage", [])}
                >
                  {t("review.unstageAll")}
                </button>
                <button
                  type="button"
                  className={BUTTON}
                  disabled={busy}
                  onClick={() => setConfirm({ paths: [], label: t("review.confirmRevertAll") })}
                >
                  {t("review.revertAll")}
                </button>
              </div>
            )}
            {groups.map(([folder, list]) => (
              <div key={folder}>
                {layout === "tree" && folder !== "" ? (
                  <p className="m-0 px-2 pt-1 font-mono text-xs text-muted">{folder}/</p>
                ) : null}
                <ul className="m-0 list-none p-0">{list.map(fileRow)}</ul>
              </div>
            ))}
            {changes?.truncated ? (
              <p className="px-2 py-1 text-xs text-muted">{t("review.truncated")}</p>
            ) : null}
          </section>
        )}
        {selected === null ? (
          files.length > 0 ? (
            <p className="px-3 py-2 text-xs text-muted">{t("review.selectFile")}</p>
          ) : null
        ) : diff === null ? (
          <Loader />
        ) : (
          <section
            aria-label={t("review.diffFor", { name: selected })}
            className="flex min-h-0 flex-1 flex-col"
          >
            <DiffView diff={diff.diff} truncated={diff.truncated} />
          </section>
        )}
      </div>

      {scope === "uncommitted" ? (
        <footer className="flex flex-col gap-1 border-t border-line p-2">
          <textarea
            className="min-h-16 rounded border border-line bg-bg p-1 text-sm"
            aria-label={t("review.message")}
            placeholder={t("review.message")}
            value={message}
            onChange={(event) => setMessage(event.target.value)}
          />
          <div className="flex flex-wrap items-center gap-1">
            {props.draftMessage !== undefined ? (
              <button
                type="button"
                className={BUTTON}
                disabled={drafting || busy}
                onClick={() => void generate()}
              >
                {drafting ? t("review.generating") : t("review.generate")}
              </button>
            ) : null}
            <span className="flex-1" />
            <button
              type="button"
              className={BUTTON}
              disabled={busy || message.trim() === ""}
              onClick={() => void commit(false)}
            >
              {t("review.commit")}
            </button>
            <button
              type="button"
              className={BUTTON}
              disabled={busy || message.trim() === ""}
              onClick={() => void commit(true)}
            >
              {t("review.commitPush")}
            </button>
            <button
              type="button"
              className={BUTTON}
              disabled={busy}
              onClick={() => {
                setNoGh(false);
                setPrTitle(status?.branch ?? "");
              }}
            >
              {t("review.createPr")}
            </button>
          </div>
          {prTitle !== null ? (
            <form className="flex items-center gap-1" onSubmit={(event) => void createPr(event)}>
              <input
                className="min-w-0 flex-1 rounded border border-line bg-bg px-1 text-sm"
                aria-label={t("review.prTitle")}
                value={prTitle}
                onChange={(event) => setPrTitle(event.target.value)}
              />
              <button type="submit" className={BUTTON} disabled={busy || prTitle.trim() === ""}>
                {t("review.createPr")}
              </button>
              <button type="button" className={BUTTON} onClick={() => setPrTitle(null)}>
                {t("files.cancel")}
              </button>
            </form>
          ) : null}
          {noGh ? (
            <div role="status" className="flex items-center gap-2 text-xs">
              <span className="flex-1">{t("review.noGh")}</span>
              {props.askAgent !== undefined ? (
                <button
                  type="button"
                  className={BUTTON}
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
