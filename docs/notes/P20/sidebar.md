# Task: Sidebar: sessions and projects (P20-25) Phase: 20 Owner: wave-35 builder D1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/app/chat/sidebar/index.tsx`
(skimmed: sections, search merge, pinned resolution), `apps/desktop/src/app/chat/sidebar/projects/workspace-groups.ts`
(skimmed: project/lane shapes, folder-prefix placement), plus the two helpers they lean on for the same rules:
`apps/desktop/src/app/chat/sidebar/session-index.ts` (pinned rows by any id) and
`apps/desktop/src/store/session-unread.ts` (head comment: persisted unread design). Research 08 §3.14.
Closed before writing.
Evidence: none.

What it does (own words): the left sidebar lists the conversations of the profile the window shows.
Rows come from the profile's `indus serve` (`GET /api/sessions?order=recent`, through main's REST
proxy with the profile named on every call) and are kept per profile, so switching back to a
profile shows its list at once. A row shows the title (else the first ask, else "Untitled"), how long
ago it was active, a dot when it has news the person has not seen ("unread") and a pulsing dot while a
turn or background process runs there. Rows can be pinned (own section on top), archived (an
"Archived" view brings them back) and searched: typing filters the loaded rows at once (title, first
ask, folder, branch, and the session id — a pasted id finds its row) and the server's ranked search
(`/api/sessions/search`) adds hits that are not loaded yet, server hits first. A compressed
conversation is one row (the tip of its chain, keyed by the chain's root id, which pins and unread use
so they survive the id rotation); a branch is its own row with its own title and a branch mark.

Projects are the person's own grouping of workspace folders: a project owns one or more folders
(repos or plain folders). A session belongs to the project whose folder is the longest prefix of its
folder; each project shows its three most recent sessions with "Show all (n)". Everything no project
claims stays in "Chats". A plain new chat is detached (no folder) unless a project's "new chat" button
or the profile's default directory gives it one; the folder is sent on `session.create`. Repo
discovery walks the home folder (or the roots the person set) to a bounded depth through main's fs
bridge, skipping excluded names, and offers the repositories it finds when a project is created.
Projects, the default directory and the scan settings are kept per profile in local storage (a
profile-keyed family, P20-26 registry).

Unread survives a restart: a per-profile record keeps, per durable id, the message count the person
last saw. A row whose count grew past it is unread; a session seen for the first time is seeded (a fresh
install does not paint every row); opening a session marks it seen; the record is bounded.

Inputs → outputs: REST rows (`id, title, preview, source, started_at, last_active, is_active, archived,
pinned, kind, cwd, git_branch, parent_session_id, end_reason, message_count, lineage_root`) and live
turn events → sidebar sections; pin/archive → `PATCH /api/sessions/<durable id> {pinned|archived}`;
search text → filtered rows + server hits; project actions → per-profile store; scan → repo list.

Files: `apps/desktop/src/sidebar/Sidebar.tsx`, `src/sidebar/sessionList.ts`, `src/sidebar/projects.ts`,
`src/sidebar/strings.en.ts`, `src/sidebar/sidebar.css`, `src/sidebar/sessionList.test.ts`,
`src/sidebar/projects.test.ts`, `src/sidebar/Sidebar.test.tsx`. Shared: `src/i18n/en.ts` (spreads the
block), `src/app/Shell.tsx` (mounts it), `src/chat/composer/Composer.tsx` + `src/app/chatSlots.tsx`
(the new chat's folder on `session.create`), `src/indus/web/routes/sessions_store.py` (`lineage_root` on
list rows) + its contract test.

Edge cases / failure modes:
- a list page arrives while an older refresh is still out → generation check, the older answer is
  dropped; pages merge by durable id (never replace the whole list);
- a chain's root and tip both arrive (search hit + list) → one row, the tip;
- a branch child whose parent ended with compression is still a branch (its marker wins);
- pin/unpin while the PATCH is out → optimistic, rolled back on failure with an inline error;
- search with only spaces → no search; an id pasted with surrounding spaces → trimmed;
- Windows folders compare case-insensitively with either separator; POSIX case-sensitively;
  `C:\Work` is not under `C:\Workshop`;
- scan: unreadable folder skipped; symlinks not followed; hidden folders skipped below a root;
  a repo is not descended into; a cap on folders visited (2,000) ends a runaway scan;
- no bridge (plain preview, tests) → empty list, no error; REST 404/503 → inline "could not load" +
  Retry, the old rows stay.

Our design differences / decisions:
- Projects are local per-profile records in 1.0 (no projects table in our backend); the reference's
  backend-computed project tree, worktree lanes and kanban lanes are not built (worktrees: later).
- Unread is our own watermark design keyed by durable id; background-process dot = `is_active` or a
  live `turn.started` without its `turn.done`.
- The REST list gains `lineage_root` (the chain's root id) so pins/unread/PATCH address the row the
  server reads `pinned`/`archived` from.

Tests we will write:
- lineage collapse to the tip with the root as durable id; branch rows stay; merge by durable id;
- search by id (exact, prefix, padded) and by title/folder; server hits first, synthetic rows;
- unread persisted across a reload (new tracker over the same storage), seeded on first sight, seen
  on open, bounded;
- project placement by longest prefix (Windows case/separators), three most recent + show all,
  new-chat folder rule; scan respects depth, excludes, hidden folders, repos not descended, cap;
- Sidebar renders sections, pins via PATCH to the durable id, search finds a pasted id.

## Decisions for the lead
- `lineage_root` added to `GET /api/sessions` rows (Python, additive, `sessions_store.py`
  `info_from_summary`); without it a pin on a compressed conversation lands on the tip while the list
  reads the root's flag. Contract test extended (`test_a_compressed_conversation_reads_as_one`).
- Pin / archive are fenced writes: the REST PATCH refuses a write without the mutation identity, so the
  sidebar reads `GET /api/sessions/<id>/mutation-snapshot` first and sends `request_id`,
  `expected_revision` (+ `expected_generation`); a `stale_revision` 409 is read again once.
- The default repo-scan root is the person's home folder, from a new launch fact `homeDir`
  (`indus:facts`, `electron/windows/ipc.ts`); folders are listed through P20-29's `fs.readDir`
  (main confines it to home and workspace). Until D2's fs handler lands the scan finds nothing.
- The new chat's folder travels as a new `newSessionCwd` prop on the composer (sent as `cwd` on
  `session.create`), chosen by the sidebar in `newChatTarget` (a profile-keyed record family) and
  taken once the session exists (`src/app/chatSlots.tsx`).
- Projects are local per-profile records (no projects table in our backend); worktree lanes,
  backend-computed project trees and the all-profiles view are not built in 1.0.
- A backend that answers 404 for `/api/sessions` (older serve, fake backend) shows an empty list, not
  an error.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Pin, archive, search by id, unread across restart, background dots (tests:
  `src/sidebar/sessionList.test.ts::finds a row by id: exact, prefix, padded, and by its root`,
  `::survives a reload: a new tracker over the same storage still shows the dot`,
  `src/sidebar/Sidebar.test.tsx::pins on the durable id`, `::a running turn shows the working dot`,
  `::unread survives a remount; the open conversation is marked seen`).
- [x] Lineage one row keyed by its root, branch its own row (`sessionList.test.ts::a compression lineage is one row: the tip, keyed by the root`, `::a branch child is its own titled row, even from a compressed parent`).
- [x] Project grouping, 3 most recent + show all, detached new chat, scan depth/excludes
  (`projects.test.ts::each project shows its three most recent; show all opens the rest`,
  `::respects depth and excludes, skips hidden folders and links, never walks into a repo`,
  `::a new chat starts in the project's folder, else the default directory, else detached`).
- [x] Gap found: the search's server snippet carries highlight markers — stripped (`plainSnippet`).
- [x] Gap found: a pinned conversation must leave the other sections — pinned rows render only in
  Pinned; the server's own `pinned` flag is the truth (no local pin set to drift from it).
- [x] Gap found (our backend): PATCH needs the mutation identity — `fencedPatch`, pinned by
  `sessionList.test.ts::a write carries the session's mutation identity and rereads a stale one once`.
- [x] Folder comparison: separator/trailing-slash agnostic, Windows case-folded, NFC (`projects.test.ts::compares whole segments; Windows folds case and separators`).
- Deliberate differences: the reference renders a backend-computed project/worktree tree and an
  all-profiles grouped view; ours groups locally by explicit projects per profile. Unread is a
  message-count watermark only (the reference also persists explicit live "finished" markers; ours
  refreshes the list on `turn.done`, which moves the count).
- Not taken: worktree lanes and kanban lanes, cron / messaging sections, drag-to-reorder pins, row
  rename, the virtualised list (P3 / later), manual "mark unread" UI (the tracker supports it).

## Status

Done (2026-10-21). Shared changes: `src/i18n/en.ts` (spreads `SIDEBAR_STRINGS`), `src/app/Shell.tsx`
(mounts the sidebar), `src/chat/composer/Composer.tsx` (`newSessionCwd`) + test,
`src/app/chatSlots.tsx`, `electron/ipc/schemas.ts` + `electron/ipc/bridge.ts` + `src/app/bridge.ts` +
`electron/windows/ipc.ts` + `electron/ipc/registry.test.ts` (`homeDir` fact), regenerated
`src/global.d.ts`, `src/indus/web/routes/sessions_store.py` + `tests/contract/web/test_sessions_routes.py`.

Integration (wave 35, D-16.717, D-16.724): each row has a pop-out button that opens the HUD on that conversation; the list loads again when the connection becomes ready (the first load usually beats the boot).

## Wave 47 (W47-b): the folder of a fresh draft

Status: done (2026-10-22, wave-47 builder b). Read: none (our own wiring).

`newChatTarget` was set only by the sidebar's "new chat" (`beginNewChat`). A draft opened from app
start, Ctrl+N, `/new` or the command center had no target, so the composer created its session
with no `cwd` and the daemon ran its tools in its own work folder. The Shell's tool panes already
fell back to the profile's default directory, so the Terminal opened in the right folder while the
file tools did not. `freshDraftFolder(storage, profile, store?)` in `projects.ts` is now the one
rule (the sidebar's choice, else the default directory, else none), used by the composer slot
(`chatSlots.tsx`) and by the Shell's `useWorkspaceFolder`. The daemon half (the tools follow the
session's folder) is in `docs/notes/P05/file-ops.md`, "Wave 47". Test: `projects.test.ts` "a
fresh draft not started from the sidebar opens in the default directory (W47-b)" (fail-first).
