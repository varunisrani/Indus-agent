# Task: System page, status strip and banners (P18-25) Phase: 18 Owner: builder P1 (wave 32)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/pages/SystemPage.tsx` (skimmed by
search: action log viewer, daemon buttons, update flow), `web/src/components/MemoryPressureBanner.test.tsx`
(test names only), `web/src/components/MemoryPressureBanner.tsx` (header comment and trigger order, by
search), `web/src/components/SidebarStatusStrip.tsx` (refresh period, by search). Our side:
`src/indus/web/routes/{system,status,daemon}.py`, `src/indus/web/actions.py`, note
`rest-profiles-system.md`, `rest-status-logs.md`. Evidence: none.

What it does (own words): **System** groups the machine operations of the selected profile: host stats
(CPU, load, memory, disk); the update badge ("N commits behind", "Up to date", or what to run for a
non-git install) with "check again" and, for git installs with commits waiting, an Update button that
asks first; daemon state with Start / Stop (asks first) / Restart; the memory provider (select one of
the installed ones, or reset to built-in); the credential pool (masked previews only; add an API key —
the input is cleared after save and the value is never shown —, remove by 1-based index after a
confirm); operations (doctor, backup with a download of the archive once it is written, import by path
or by upload, checkpoint size listing and prune) — every long one is a background action and its
log is tailed live from `GET /api/actions/{name}/status` (appending lines, stopping when the run
ends); shell hooks (list with approval state; add needs the consent checkbox under a warning, else
the button stays disabled; remove after a confirm). **Status strip** (sidebar footer): daemon state,
version, active sessions and connected/configured platforms from `GET /api/status`, refreshed every
5 s while the tab is visible. **Pressure banner** (above the pages): at most one at a time, by
priority disk-critical > memory-critical > suspected OOM restart > disk-elevated > memory-elevated;
`unknown` or `ok` shows nothing; a dismissal is remembered per trigger and per daemon boot, so a new
boot shows it again; an escalation (elevated → critical) is a new trigger and opens; a domain that
recovers to `ok` forgets its dismissals.

Inputs → outputs: `GET /api/system/{stats,update}`, `POST /api/system/update`, `GET /api/daemon`,
`POST /api/daemon/{start,stop,restart}`, `GET/PUT /api/memory[/provider]`, `GET/POST
/api/credentials/pool`, `DELETE /api/credentials/pool/{provider}/{index}`, `POST /api/ops/{doctor,backup,
import,import-upload,checkpoints/prune}`, `GET /api/ops/{checkpoints,backup/download}`,
`GET/POST/DELETE /api/ops/hooks`, `GET /api/actions/{name}/status`, `GET /api/status` → sections, log
tails, the strip and the banner.

Files: `apps/web/src/pages/SystemPage.tsx`, `apps/web/src/pages/system/*.tsx` (sections),
`apps/web/src/shell/{StatusStrip,PressureBanner}.tsx`, `apps/web/src/shell/shellStatus.ts`
(the shared 5 s status and daemon queries), tests `apps/web/src/pages/SystemPage.test.tsx`,
`apps/web/src/shell/PressureBanner.test.tsx`, `apps/web/src/shell/StatusStrip.test.tsx`; shared edits in
`App.tsx` (strip in the sidebar footer, banner above the pages), `restTypes.ts`, `api.ts`, `mockApi.ts`,
`shell/routes.ts`.

Edge cases / failure modes:
- Status probe failed (only `version`, `daemon.state`, `overall`) → strip shows what it has; banner nothing.
- An action already running (409) → toast and keep tailing the running one.
- Update on a non-git install → no button, the server's hint; a 409 at apply shows its sentence.
- Backup download: the archive route takes the session header, so the page fetches it and offers the
  blob (no credential in a URL).
- Hook without consent → button disabled (the server would answer 400 anyway).
- Pool add with a placeholder key → the server's 400 sentence; an inherited pool → its 409 sentence.

Our design differences / decisions:
- The status probe carries no daemon boot id and no OOM flag (it is public and coarse). The banner
  keys dismissals on the daemon's pid from the authenticated `GET /api/daemon` (a new process = a new
  boot), and "suspected OOM restart" is inferred in the browser: the pid changed while the last memory
  reading of the previous process was critical. The copy says "may have".
- No prompt-size breakdown operation (not built on the server, D-16.622).

Tests we will write:
- Banner priority table (pure function), dismissed elevated reopens on critical, new boot clears the
  dismissal, unknown shows nothing, recovery forgets, suspected OOM after a pid change under critical.
- Strip refreshes every 5 s (fake timers) and shows daemon state / sessions / platforms.
- System: update badge and confirm-before-update; daemon stop asks then tails `daemon-stop`; doctor log
  tail appends lines across polls and stops at `done`; pool add clears the input and shows only the
  preview; hook add disabled until consent; memory provider select / reset.

## Decisions for the lead
- Boot id = daemon pid from `GET /api/daemon`; OOM suspicion as above. If the daemon's heartbeat later
  carries a boot id and an OOM hint, `shellStatus.ts` (`useBootWatch`) is the one place to switch.
- Dismissals in `sessionStorage["indus.pressure.dismissed"]` as `trigger:boot` keys (a new tab session shows live warnings again).
- Action log polls every 1 s while running (`ACTION_POLL_MS` in `ui/ActionLog.tsx`), 400 lines.

Compare checklist (filled after the rewrite; reopened the files above by search):
- [x] banner priority (test: `apps/web/src/shell/PressureBanner.test.tsx::picks one banner by priority`)
- [x] escalation reopens (test: `PressureBanner.test.tsx::reopens a dismissed elevated banner when it turns critical`)
- [x] new boot clears dismissal (test: `PressureBanner.test.tsx::shows a dismissed banner again after the daemon restarts`)
- [x] operations log tail appends (test: `apps/web/src/pages/SystemPage.test.tsx::tails the doctor log and stops when it is done`)

- [x] suspected OOM after a pid change under critical memory (test: `PressureBanner.test.tsx::suspects an out-of-memory restart when the daemon changes under critical memory`)
- [x] strip refreshes every 5 s (test: `apps/web/src/shell/StatusStrip.test.tsx::refreshes every 5 s`)
- [x] confirm before update, git installs only (tests: `SystemPage.test.tsx::badges commits behind and asks before updating`, `::offers no update button for an install that is not a git checkout`)
- [x] hook add needs consent (test: `SystemPage.test.tsx::adds a shell hook only with consent, and removes one after confirming`)
- [x] dismissals are session-scoped as in the reference (sessionStorage), keyed by trigger and boot
- Deliberate differences: boot id and OOM hint are worked out in the browser from `GET /api/daemon` (our public probe carries neither); the reference reads both from the gateway heartbeat.
- Not taken: prompt-size breakdown (no route, D-16.622), gateway migrate, audit action, the reference's shared-metrics and multiplex banners (dropped, L17).

## Status

Done (2026-10-20). Shared edits: `apps/web/src/App.tsx` (status strip at the top of the sidebar footer, pressure banner under the profile banner), `apps/web/src/lib/{api,restTypes}.ts` (`saveAttachment`, `forProfile`), `apps/web/src/testing/mockApi.ts`, `apps/web/src/shell/routes.ts` (`pending` now exported, no pending row left).

Integration (wave 32, P18c integrator, 2026-10-21): decisions numbered D-16.650 / D-16.651; status strip and auth widget both in the sidebar footer (strip first, `App.tsx`; widget from `main.tsx`). The backup download keeps the session-header fetch + blob (no credential in the URL); a download ticket like the Files page's is a follow-up.
