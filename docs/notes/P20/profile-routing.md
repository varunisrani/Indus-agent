# Task: Profile rail, routing and switch shapes (P20-26) Phase: 20 Owner: wave-35 builder D1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/profile-session-routing.ts`
(structure and the profile/route ladder of session reads), `apps/desktop/src/store/session-states.ts`
(the drop / rename routines for profile-keyed state, the soft-apply wipe, constants),
`apps/desktop/src/app/chat/sidebar/profile-switcher.tsx` (skimmed: rail, context menu, default
handling). Research 08 §3.11, §3.14, traps R08.6 and R08.7. Closed before writing.
Evidence: none.

What it does (own words): a window shows one profile at a time (the foreground profile) but may keep
others connected. Main remembers the active profile in userData (`profile-state.json`, with the
default profile) and makes **one launch decision per boot attempt**: an explicit `--profile`/`-p` on
the app's command line, else the remembered active profile, else the default one, else `default`. That
one value feeds the route the window shows, the `-p` flag of every `indus` command main runs and the
child environment together: every `indus` command main runs gets `INDUS_HOME` = the root and an
explicit `-p <profile>` — `default` included (without a flag the CLI would pick its own sticky
profile, so the renderer could believe "default" while the daemon serves another; the CLI's flag
wins over the environment). A "remember" (set active) that lands while
an attempt runs is saved but applies to the next attempt.

There are three switch shapes and they must not be confused (R08.6):
- **connection apply** (local ↔ remote): soft re-home. The shell stays mounted; every gateway-bound
  store is wiped explicitly (transcripts, open server requests, the sidebar's lists — not just marked
  stale), main applies the change, the sockets redial, and the agreement check runs.
- **runtime home change**: hard re-home; the window reloads.
- **live profile swap** in one window: the other profile's socket is started (or reused), the swap
  commits when it is ready (20 s budget, else the foreground stays and an error shows), background
  profiles keep their sockets and keep streaming, per-profile lists stay and merge, and only an
  explicit selection on the rail starts a fresh draft. The choice is remembered in main (5 s budget;
  a slow remember never undoes the swap).
After every switch the foreground profile, its connection object and the socket's descriptor
(`descriptor.profile`, `mode`) must agree; a mismatch is an error (shown, never silently routed).

Renaming or deleting a profile: main first tears down connection-scoped state (when the profile is the
one main is attached to, main re-homes to the default profile so the serve can rename/delete it), the
renderer closes that profile's socket and stores, the REST call runs, then the change is committed:
main renames or clears its remembered active/default, and every profile-keyed local-storage family is
migrated (rename) or dropped (delete) through **one registry** (R08.7). A test scans the renderer's
sources for storage-key literals and fails when one is neither a registered profile family nor a
listed window-wide key.

The rail: a column of coloured squares (one per profile, colour from a fixed palette by name, initial
letter inside), the foreground one marked, the default one badged; click swaps; a context menu (right
click, the "…" key, Shift+F10) offers "Open in new window" (an instance window for that profile) and
"Set as default".

Inputs → outputs: `indus:profile:get|set|set-default|prepare-change|commit-change`,
`indus:profile:changed` (event); `GET /api/profiles` for the list → rail; selections → switcher state
(`foreground`, `status`, `error`), connection starts, remembered profile.

Files: `apps/desktop/electron/profiles/routing.ts`, `electron/profiles/routing.test.ts`,
`src/profiles/ProfileRail.tsx`, `src/profiles/switching.ts`, `src/profiles/storageFamilies.ts`,
`src/profiles/strings.en.ts`, `src/profiles/profiles.css`, `src/profiles/switching.test.ts`,
`src/profiles/storageFamilies.test.ts`, `src/profiles/ProfileRail.test.tsx`. Shared:
`electron/ipc/{channels,schemas}.ts` + `src/global.d.ts` (regenerated), `electron/main.ts` (install
step), `electron/backend/install.ts` (launch profile for a request that names none),
`electron/backend/localAttach.ts` (`-p default` explicit), `electron/windows/ipc.ts` +
`src/app/bridge.ts` + `electron/ipc/bridge.ts` (`homeDir` fact), `src/main.tsx`, `src/app/App.tsx`.

Edge cases / failure modes:
- two swaps in quick succession → the later wins; the earlier's late ready changes nothing (serial);
- swap to the profile already in front → no-op; swap while a soft re-home runs → waits for it;
- the other profile's socket never gets ready → after 20 s the swap fails, foreground unchanged;
- remember answers late or fails → logged, the swap stands;
- rename to an existing family key → the moved value wins (the new name is the profile's now);
- delete of the foreground profile → swap to default first, then drop;
- state file unreadable/corrupt → defaults (no active, no default), rewritten on the next change;
- profile names are validated the same way as the backend (1–64, lowercase, digits, `-`, `_`).

Our design differences / decisions:
- Main keeps one published connection (P20-12); a background profile keeps streaming on its open
  socket, but a background redial asks main for that profile, which re-homes main's REST target; the
  renderer always names the profile on REST calls so this stays correct (follow-up: per-profile
  connections in main).
- "Connection apply" is driven by `switcher.applyConnectionChange(run)` (Settings → Gateways calls it
  around `remote.save/forget`); a mode change seen on `connection:changed` also wipes as a safety net.
- Profile colours are derived from the name (no colour editor in 1.0).

Tests we will write:
- main: launch precedence table; one decision per attempt; remember mid-attempt applies next time;
  `-p default` explicit; prepare re-homes away from the attached profile; commit renames/clears.
- switcher: soft switch leaves no store entry from the old connection; live swap keeps the background
  socket and its store; explicit vs implicit fresh draft; 20 s timeout keeps foreground (fake clock);
  agreement trips on a forced mismatch; rename migrates, delete drops.
- registry: every family migrated/dropped; lint scan of storage-key literals.
- rail: renders squares, click swaps, context menu opens a window / sets default.

## Decisions for the lead
- New channels `indus:profile:prepare-change` and `indus:profile:commit-change`; `profileState` gains
  `launch` (the profile of the current boot attempt). Regenerated `src/global.d.ts`.
- `localAttach.profileArgs` passes `-p default` explicitly for the default profile (shared file).
- `installBackend(context, electron, { launchProfile })`: a `connection.get` that names no profile
  runs the launch decision (shared file, one line); `main.ts` wires `installProfiles` before it and
  hands back a `BackendPort` (`attachedProfile`, `rehome` = `orchestrator.start`).
- The decision is taken once when main installs the profile routing (the launch's boot attempt); a
  Retry/Repair re-runs that attempt's profile (the orchestrator pins it), a remember applies next launch.
- Main still publishes one connection at a time (P20-12): a background profile keeps streaming on its
  open socket, but if it must redial it asks main for that profile, which re-homes main's REST target
  (REST calls name their profile, so they stay correct). Follow-up: per-profile connections in main.
- `ConnectionStore` gained `setPrimary(profile)` (the chat view and status follow the foreground) and
  `release(profile)` (a renamed/deleted profile's connection is closed and forgotten).
- The renderer is wired per profile: `src/main.tsx` builds a `ProfileSwitcher<ChatWorkspace>`;
  `Root` renders the foreground workspace's transcripts, connection and server requests.
- Settings → Gateways (P20-32) should call `switcher.applyConnectionChange(() => remote.save(...))`
  (and the rename/delete UI `switcher.renameProfile/deleteProfile(..., rest call)`); until then a mode
  change seen on `connection:changed` wipes the stores as a safety net.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Three switch shapes (tests `src/profiles/switching.test.ts::leaves no session store entry from the old connection`, `::applies and reloads the window`, `::commits once the other socket is ready and keeps the old one streaming`, `::only an explicit selection starts a fresh draft`).
- [x] Agreement assertion (`::trips on a forced mismatch between the profile and its socket`, `::trips when main reported another mode than the socket has`).
- [x] Rename/drop through one registry; main tears down first (`storageFamilies.test.ts::rename migrates every registered family and leaves the rest`, `::every storage key in the renderer's sources is registered (lint)`, `switching.test.ts::rename: main detaches first, then the backend, then every family moves`, `electron/profiles/routing.test.ts::prepare leaves the profile main is attached to (re-homes to default first)`).
- [x] Timings: switch commit 20,000 ms, remember 5,000 ms (`SWITCH_COMMIT_TIMEOUT_MS`, `REMEMBER_TIMEOUT_MS`; `::a socket that is not ready in 20 s leaves the foreground where it was`, `::remembers the profile in main, and a slow remember never undoes the swap`).
- [x] Rail: open in new window, set as default, keyboard context menu (`ProfileRail.test.tsx`).
- [x] Gap found: the soft wipe must also cover open server requests and cached lists, not only
  transcripts — `ChatWorkspace.wipe` clears the request store, the sidebar registers its wipe.
- [x] Gap found: a deleted profile's leftover state could resurrect it — its workspace is disposed and
  its connection released before the REST delete; storage families dropped after.
- Deliberate differences: the reference routes session reads across several registered gateways and
  an all-profiles aggregate; ours has one connection (local or the saved remote) and one foreground
  profile per window. Profile colours are derived from the name (no recolour, no drag reorder).
- Not taken: registry read 5 s / descriptor lookup 20 s / active gateway open wait 8 s / secondary
  socket minimum life 30 s (multi-gateway routing, N17); the "all profiles" rail toggle; the dropdown
  form of the rail past 13 profiles; profile create/import from the rail (Profiles overlay, later).

## Status

Done (2026-10-21). Shared changes: `electron/ipc/{channels,schemas}.ts`, `src/global.d.ts`,
`electron/main.ts`, `electron/backend/install.ts`, `electron/backend/localAttach.ts` (+ test),
`src/gateway/connectionStore.ts`, `src/main.tsx`, `src/app/App.tsx`, `src/app/Shell.tsx`,
`src/i18n/en.ts`, `docs/notes/P20/preload-bridge.md` (P20-26 row), traps ledger R08.6/R08.7.
