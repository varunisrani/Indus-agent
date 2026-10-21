# Task: Updates, unsigned 1.0 scope (P20-37) Phase: 20 Owner: wave-35 builder D4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/updater/index.ts`
(all: mechanisms, status/apply shapes), `apps/desktop/electron/updater/mac.ts` (all: check, apply
order, waiting for the native acceptance), `apps/desktop/electron/update-marker.ts` (the rules in
the comments: liveness by pid + creation time, never delete or rewrite, empty-marker grace, legacy
age ceiling; process creation time sources), `apps/desktop/electron/update-gate.ts` (all: the three
signals, the wait loop, timeouts), `website/docs/developer-guide/macos-bundle-updates.md` (all).
Research 08 §3.26, traps R08.13, R08.14. Closed before writing.
Evidence: none.

What it does (own words):
- **One owner, declared by the build.** The build stamp (`desktop-build-stamp.json`, written by
  P20-38) names the update owner: `electron-updater` (macOS bundles and the Linux AppImage) or
  `manual` (the unsigned Windows build, source checkouts, a missing or unreadable stamp). The
  runtime never guesses it from the platform or the install layout. The manual owner shows the
  command (`indus update` for a checkout) or the download page; it never applies anything.
- **Check never downloads.** A check (button, or in the background 30 s after launch and every
  6 hours) looks up the newest release of the chosen channel (stable or canary, D-16.56; canary is
  also offered a newer stable) and reports `available` with the version, or `idle`. The lookup
  uses a GitHub token from the environment, else the GitHub CLI's login, else none; a token the
  feed rejects is logged once by its source (never its value) and the check is repeated without it.
  A token is only ever sent to the configured releases API origin.
- **Apply (electron-updater).** Refused while another update owns the runtime (live marker, below)
  or while one runs in this process. Otherwise: recheck with the updater, download (progress on
  `indus:updates:progress`), wait for the updater's acceptance (on macOS, Squirrel accepting the
  signed app; bounded at 120 s) — and only then stop app-owned work and quit-and-install. A failed
  download, verification or acceptance leaves everything running and reports the error.
- **Shared update marker** `<home>/.indus-update-in-progress` (the same file `indus update` writes
  in P23). It names its owner by pid and process start time. While a live owner holds it the app
  starts no backend from the runtime under replacement (the boot waits in its "update-wait" phase)
  and starts no update of its own. A marker whose pid is gone, is a zombie, or now belongs to
  another process (start time differs by more than 2 s) is stale: ignored, never deleted or
  rewritten. A marker that names no comparable start time counts as live only for 20 minutes from
  its creation; an empty one only for 5 s (it is being written). An in-process "update running"
  flag gates starts too, because an apply stops things before the marker would say so.
- **After a backend update** (the marker went from live to gone) the app compares its own version
  with the runtime's (`code_version` from the daemon descriptor) and offers an app update when it
  is behind.
- **Skew.** A renderer whose lazy chunk fails to load (a 404 after a broken or half-applied
  update), a bundle whose `app.asar` and `app.asar.unpacked` stamps disagree, or an app older than
  its runtime gets a notice offering update / reinstall instead of crashing on the first lazy
  import.

Inputs → outputs:
- build stamp + `updates.json` (the per-device channel) → `UpdateStatus { owner, channel, state,
  currentVersion, version?, progress?, error?, manualHint?, downloadUrl? }`.
- `updates.status/check/apply/setChannel` (invoke), `updates.onProgress` (event),
  `updates.onSkew` (event `{ kind, detail }`).
- marker file → `{ state: absent | live | stale | ours, reason }`; gate → `waitUntilClear()`,
  `isRunning()` for the boot orchestrator (P20-12's `waitForUpdate` / `updateRunning`).

Files: `apps/desktop/electron/updates/{updater,marker,skew,feed,engine,install}.ts`, tests
`updates/{updater,marker,skew,feed}.test.ts`, `apps/desktop/testing/fakeFeed.ts` (a loopback
releases feed), `apps/desktop/src/updates/{UpdateNotice.tsx,skew.ts}` (+ test). Shared: channel and
schema rows (`indus:updates:*`, the status shape grows), `electron/backend/install.ts` (takes the
update gate instead of the no-op), `electron/main.ts` (one install line), `apps/desktop/package.json`
(`electron-updater`).

Edge cases / failure modes:
- stamp missing / corrupt / unknown owner → manual; electron-updater owner on Linux without an
  AppImage → status error "this build cannot update itself" (no inference of another owner);
- feed unreachable, 5xx, malformed JSON, no matching release → `error` with a short message, no
  crash; drafts ignored; tags that are not our version grammar ignored;
- token: env token rejected (401) → logged once ("from GH_TOKEN"), anonymous retry; the CLI not
  installed or hanging (3 s) → anonymous;
- apply twice at once → the second refused; apply while a live marker is held → refused, nothing
  stopped; download failure → `error`, the stop hook never called (spy);
- marker: live owner (start time matches) → backend start waits; pid reused (different start
  time) → ignored and the file left as it was; zombie → stale; our own pid → `ours`; unreadable
  JSON older than 5 s → stale (logged once); the wait gives up on an in-process flag after 20
  minutes, never on a live marker;
- skew: `ChunkLoadError`, "Failed to fetch dynamically imported module", Vite's `vite:preloadError`
  → notice; ordinary errors → not a skew.

Our design differences / decisions:
- Our names: `.indus-update-in-progress`, `updates.json`, `INDUS_DESKTOP_UPDATE_FEED` (the
  releases API base; tests point it at a loopback fake), `INDUS_DESKTOP_UPDATE_TOKEN` read before
  the vendor names `GH_TOKEN` / `GITHUB_TOKEN`.
- Marker format is ours (JSON, one object): `{ "format": 1, "owner": "<who>", "pid": N,
  "process_start": <unix seconds>, "created_at": <unix seconds> }`; P23's `indus update` must
  write exactly this (contract below).
- The availability check reads the releases API for every owner; the electron-updater feed is
  consulted at apply (recheck) — one testable lookup path with the token rules, the native feed
  stays authoritative for what gets installed.
- Not taken (1.0, D-16.15 and P23): MSIX / App Installer / Store, the checkout hand-off updaters,
  fleet updates of remote gateways, channel retirement, signed feeds.

Tests we will write:
- marker: live / stale (pid reused) / zombie / dead / ours / empty-grace / legacy ceiling /
  corrupt; the file is byte-identical afterwards (never rewritten or deleted); gate waits for a
  live owner and returns when it goes; in-process flag; timeout only for the flag.
- feed: stable vs canary selection, canary offered newer stable, drafts and foreign tags ignored;
  token cascade (env, CLI, none), rejected token logged once by source then anonymous, token never
  sent to another origin; against a loopback fake feed (no real network).
- updater: owner from stamp (missing → manual); manual status hint; check never calls download;
  apply order (recheck → download → acceptance → stop work → install) with spies; download failure
  leaves work running; acceptance timeout; second apply refused; live marker refuses apply;
  channel persisted.
- skew: version comparison (canary ordering), mixed asar/unpacked stamps, app behind runtime;
  renderer: lazy-chunk 404 → skew notice.

## Contract for P23 (`indus update`)

`<INDUS_HOME>/.indus-update-in-progress`, created exclusively (`O_EXCL` or link) with a complete
body, removed by its owner when done: `{"format": 1, "owner": "indus-update", "pid": <pid>,
"process_start": <float unix seconds of that pid's creation>, "created_at": <float unix seconds>}`.
Readers judge it by pid + start time (± 2 s); nobody but the owner removes it.

## Decisions for the lead
- Background check 30 s after launch, then every 6 h; acceptance wait 120 s; marker poll 1 s;
  in-process flag wait ceiling 20 min; start-time tolerance 2 s; empty-marker grace 5 s; legacy
  ceiling 20 min.
- Default releases API: `https://api.github.com/repos/varunisrani/indus-agent/releases` (owner-open,
  same question as D-16.690); override `INDUS_DESKTOP_UPDATE_FEED`. Since the P23 landing (D-16.768)
  it is built from `RELEASE_REPOSITORY` in `electron/releaseBase.ts` (`indusagi/indus-agent`, held
  equal to `src/indus/foundation/versions.py`).
- `electron-updater` is loaded by its package name at run time (not bundled into `main.js`): it
  must ship in the packaged app's `node_modules` (it is a production dependency; P20-38 packaging).
- `sax` (reached through electron-updater's `builder-util-runtime`) moved to BlueOak-1.0.0 after
  1.4.1; `pnpm-workspace.yaml` overrides it to 1.4.1 (ISC). Drop the override if the owner allows
  BlueOak-1.0.0 (the same question as Photon's).
- Background checks only in a packaged build or with `INDUS_DESKTOP_UPDATE_FEED` set, never with
  `INDUS_DESKTOP_NO_UPDATE_CHECK=1` (a dev run never reaches the network on its own).

- Wiring for other slices: Settings › About (P20-32) uses `indusDesktop.updates.status/check/apply/
  setChannel` and `onProgress`; the passive notice is `src/updates/UpdateNotice.tsx` (mounted by the
  ambient host). P20-38 writes `desktop-build-stamp.json` into the app (and the same file into
  `app.asar.unpacked` when anything is unpacked) with `{ version, commit, channel, updateOwner }`
  (`update_owner` also accepted). P23's `indus update` writes the marker described above.

Compare checklist (filled after the rewrite; reopened `updater/index.ts`, `updater/mac.ts`,
`update-marker.ts`, `update-gate.ts`, `macos-bundle-updates.md`):
- [x] Owner from the stamp only; manual owner never applies (tests:
  `electron/updates/updater.test.ts::update owner …`, `::manual owner: …`).
- [x] Checks never download; concurrent checks do not disturb an apply (test: `updater.test.ts::a check never downloads`).
- [x] Apply order recheck → download → acceptance → stop app work → install; download failure leaves
  everything running (tests: `updater.test.ts::apply: …`, `::a download failure leaves everything running …`).
- [x] Acceptance waited for whichever order the updater reports it, bounded at 120 s; updater error
  rejects (tests: `updater.test.ts::download resolves only after the acceptance …`, `::a failed download or a missing acceptance rejects …`).
- [x] An unrelated quit never installs (`autoInstallOnAppQuit = false`), a check never downloads
  (`autoDownload = false`), no downgrade through a channel switch (test: `updater.test.ts::never downloads on a check …`).
- [x] Marker held by a live owner (matching start time) → backend start waits with no deadline;
  stale marker (PID reused, different start time) → ignored, not deleted; zombie stale; empty-claim
  grace; 20-minute ceiling without a start time; in-process flag 20-minute ceiling (tests:
  `electron/updates/marker.test.ts`).
- [x] Token cascade env → CLI → anonymous; a rejected token logged once by source and retried
  anonymously, against a loopback fake feed (tests: `electron/updates/feed.test.ts`).
- [x] Lazy-chunk 404 → skew notice; mixed asar/unpacked stamps; app behind runtime (tests:
  `src/updates/skew.test.ts`, `electron/updates/skew.test.ts`, `src/quick/QuickEntry.test.tsx::a stale renderer …`).
- Deliberate differences: our marker body is one JSON object (format 1, contract above) instead of
  the reference's multi-line v2 with run lines and a lock sidecar — nothing in Indus writes a hand-off
  claim from the desktop in 1.0; the availability check reads the releases API (one testable path
  with the token rules) and electron-updater's native feed is consulted at apply.
- Not taken: App Installer / Store / checkout hand-off strategies, channel retirement, fleet updates,
  pinned-channel version checks, a signed-feed verification hook (P23).

## Status

Done (2026-10-21) within the unsigned 1.0 scope. Not verifiable here: a real Squirrel.Mac or
AppImage install (no Electron binary, no signed build; unsigned macOS builds cannot be accepted by
Squirrel, which this code reports as an error while leaving everything running).
