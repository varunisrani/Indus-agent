# Task: Git review pane (P20-30) Phase: 20 Owner: wave-35 builder D2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/git-review-ops.ts`
(all: scopes, list/diff/stage/unstage/revert/commit/push, commit context, gh probes and PR),
`apps/desktop/electron/git-ipc.ts` (all). Closed before writing.
Evidence: none.

What it does (own words): the review pane shows what changed in the session's repository and lets
the person act on it. Header: branch, upstream, ahead/behind. Scopes: **Uncommitted** (staged,
unstaged and untracked, one row per path; stage / unstage / revert per file or all), **Branch**
(everything since the merge-base with the default branch; read-only) and **Last turn** (everything
since the commit that was HEAD when the agent's last turn started, plus files it created; read-only).
Changed files as a list or a folder tree; selecting one shows its unified diff (an untracked file
shows as all-added). Commit box: message (or "Generate" — asks the agent for a message from the
staged diff, the untracked names and the recent subjects), Commit, Commit & push (first push sets
the upstream). Create PR runs the GitHub CLI when it is installed; without it the pane offers to ask
the agent instead.

Git runs only in main: the bundled git on Windows (`resources/git/cmd/git.exe` when packaged, else
the system git), the system git elsewhere; always `execFile` with an argument array (never a shell),
`windowsHide` (no console window), `GIT_TERMINAL_PROMPT=0`, `-c core.fsmonitor=false` (a repo's
config cannot make status run a program), bounded time (30 s, 120 s for push) and output (16 MiB).
Every channel is a registered IPC handler; the repository must lie inside the window's allowed roots
(P20-29's guard) and is replaced by its top level; file paths must be relative, stay inside the
repository and never start with `-` (they always follow `--`); the last-turn base must be a commit
id. So `--upload-pack=…` as a path or ref is refused before git runs.

Inputs → outputs: `git.status {repo}` → branch, upstream, ahead, behind, head, files (index and
worktree codes, added/removed); `git.changes {repo, scope, base?}` → files + base + readOnly;
`git.diff {repo, scope, path?, base?, staged?}` → diff text (capped, `truncated`) + readOnly;
`git.stage|unstage|revert {repo, paths}`; `git.commit {repo, message, push?}` → commit, pushed;
`git.commitContext {repo}` → diff, untracked, recent; `git.createPr {repo, title, body?}` → url or
`ghAvailable: false`.

Files: `apps/desktop/electron/git/gitOps.ts`, `apps/desktop/electron/git/gitOps.test.ts`,
`apps/desktop/src/panes/review/ReviewPane.tsx`, `apps/desktop/src/panes/review/agentDraft.ts`
(asks the agent through `prompt.background`), `apps/desktop/src/panes/review/ReviewPane.test.tsx`.
Shared: channel table/schemas + `src/global.d.ts`, `electron/main.ts` (install line).

Edge cases / failure modes:
- not a repository / repo outside the roots → a clear error, nothing runs;
- a repository with no commit yet: unstage uses `rm --cached`; branch scope empty;
- no upstream: ahead/behind 0, push sets `-u origin <branch>`; no `origin` → push refused with a
  message;
- untracked folder rows (`dir/`) revert with `clean -fd -- dir/`;
- nothing staged at commit → everything is staged first (commit all);
- huge diffs → capped at 2 MiB with `truncated`; more than 2,000 files → capped;
- `gh` missing → `ghAvailable: false` (the pane offers to ask the agent); `gh` failing → its error.

Our design differences / decisions:
- Our own porcelain-v2 parser over `execFile` instead of a git wrapper library (no new package).
- Repo config cannot run programs from status/diff (`core.fsmonitor=false`); commit hooks still run
  (they are the person's own).
- The last-turn base is captured by the pane (HEAD at each `turn.started` of the session) and kept
  in memory per session.

Tests we will write:
- temp repos (real git, isolated config): status codes and branch info; stage / unstage / revert /
  commit; commit & push to a bare remote, ahead/behind against it; branch and last-turn scopes;
  untracked all-added diff; `--upload-pack` as path or base refused; repo outside roots refused;
  `gh` absent → `ghAvailable: false`; a fake `gh` script → URL;
- pane: scope tabs, read-only scopes hide mutations, stage calls, generate fills the message, PR
  offer when `gh` is absent.

## Decisions for the lead
- New channels `indus:git:changes` and `indus:git:commit-context`; `git.status` answers `head` and
  per-file counts; `git.diff` takes `base?` and `staged?` and answers `truncated`.
- "Generate" asks the agent with `prompt.background` on the session and waits for the side
  session's `message.complete` (2 minutes); without a session the button is hidden.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Temp repo: stage / unstage / revert / commit (test: `electron/git/gitOps.test.ts::stage / unstage / revert / commit`).
- [x] Ahead/behind against a bare remote; commit & push sets the upstream (test: `::ahead/behind against a bare remote; commit & push sets the upstream`).
- [x] `gh` absent → offer shown (tests: `::gh absent → ghAvailable false (the pane offers to ask the agent)`,
  `src/panes/review/ReviewPane.test.tsx::gh absent → offer shown to ask the agent`); `gh` present: PR after pushing (fake `gh`, no network).
- [x] Command arg injection (`--upload-pack`) refused (test: `::command arg injection (--upload-pack) refused before git runs`).
- [x] Branch scope = merge-base with origin/HEAD, origin/main, origin/master, main, master; last turn
  adds untracked files; both read-only (tests: `::branch and last-turn scopes are read-only and compare against their base`,
  `ReviewPane.test.tsx::branch and last-turn scopes are read-only (no stage, revert or commit)`).
- [x] Untracked file diff synthesised as all-added (`--no-index`); staged diff (test: `::uncommitted diffs: one file, staged, untracked shown as all added`).
- [x] File cap 2,000; commit context caps (120,000 chars, 80 untracked names, 10 subjects) (test: `::commit context for the agent: staged diff, untracked names, recent subjects`).
- [x] Gap found: the reference counts the lines of untracked files for the +N column — added
  (`untrackedLines`, pinned in `::status: branch, head, changed files with counts`).
- [x] Gap found: a repository without a commit (unstage needs `rm --cached`) (test: `::a repository without a commit yet`).
- [x] Repo confined to the window's roots (test: `::a repository outside the window's roots is refused; not a repo is said so`).
- Deliberate differences: our own porcelain-v2 / numstat parsers over `spawn` (no git wrapper
  library); `-c core.fsmonitor=false`, `--no-ext-diff` and repo-location variables dropped; `gh`
  found on the login-shell PATH (R08.16) rather than adding package-manager folders; PR via
  `gh pr create --title=… --body=…` after pushing the branch; the last-turn base is captured in the
  renderer at `turn.started` (`trackTurnBase`).
- Not taken: PR badges / `gh` auth + existing-PR probes (`shipInfo`, `prList`), worktree and branch
  management, repo discovery scan (P20-25 owns the scan), file-diff-vs-HEAD for the preview.

## Status

Done (2026-10-21). Shared: channel table/schemas + `src/global.d.ts`, `electron/main.ts`,
`electron/panesInstall.ts`. "Write it for me" uses `prompt.background` + the side session's
`message.complete` (`src/panes/review/agentDraft.ts`; checked against a fake connection only — the
integrator should confirm on a real daemon that the side session's events reach the window after
`session.subscribe`). The PR fallback hands a request to the composer (`addToChat`).
