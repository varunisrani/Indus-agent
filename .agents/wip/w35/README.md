# Wave 35 parked patches (P20-25..39). Base for every patch: LOCAL commit c3891d6 = main 454e486 + w34 integration,
# which landed as 2a1991b (same tree). Apply on main >= 2a1991b with `git apply --3way`.

## w35-d5.patch: P20-38 packaging (unsigned, three OSes) + P20-39 diagnostics and small surfaces — done
- Builder checks: pnpm lint, fmt:check, -r typecheck clean; root pnpm test 1877 passed / 13 skipped (Electron-gated);
  desktop 645 passed; check-ipc, gen-bridge-types --check, dump-slash-catalog --check ok; tooling.checks ok. No Python.
  No real installer built (no Electron binary; electron-builder needs network for AppImage/NSIS tools) — offline `--dir`
  probe with a stand-in Electron folder worked. New CI job `desktop-package` (ubuntu/macos/windows) has never run.
- New: apps/desktop electron-builder.config.cjs, scripts write-build-stamp / stage-app / asar / check-asar /
  dist-unsigned / make-icons (+ tests), own icons (resources/icons, drawn by make-icons.mjs), entitlements.mac.plist,
  electron/linuxLauncher.ts (+test), testing/electron/packaged.electron.test.ts (skips without Electron);
  electron/diagnostics.ts, contextMenu.ts (+tests), src/app FindBar, findInTranscript, errorForwarding, LogsViewer,
  clipboard (+tests); notes docs/notes/P20/packaging.md, diagnostics.md.
- Shared: apps/desktop/package.json (stamp, icons, dist:unsigned, pack:unsigned, check:asar; electron-builder dev dep),
  pnpm-workspace.yaml (electron-winstaller: false), pnpm-lock.yaml, .github/workflows/ci.yml (desktop-package job in the
  gate's needs), ipc channels.ts / schemas.ts / src/global.d.ts (4 new channels: indus:diagnostics:copy,
  indus:clipboard:read-text, indus:clipboard:write-text, indus:find:open), appMenu.ts (help items, enabled), en.ts,
  styles.css, main.tsx (2 lines), 00-layout §4 electron-builder pin row.
- WIRING OWED: `w35-d5-main-ts-wiring.patch` for electron/main.ts (installDiagnostics after installBackend;
  installContextMenus before registry.seal(); help: diagnostics.menuItems to appMenuTemplate; windows.openMain() wrapped
  in installLinuxLauncher). P20-32 mounts <LogsViewer/>; P20-31 declares node-pty and keeps it external in
  build-electron.mjs; P20-37 reads desktop-build-stamp.json (readBuildStamp / validateBuildStamp); P20-40 adds the
  packaged smoke test to desktop-package; add the 4 channels to preload-bridge.md.
- Packages: electron-builder 26.17.0 (MIT, 2026-09-26) dev only, +246 lock entries; newest transitive @electron/rebuild
  4.2.1 (2026-10-06). LICENCE RULING OWED (dev/build-only, never shipped; prod licence guard stays green): BlueOak-1.0.0
  (minimatch 10.2.6, tar 7.5.22, minipass 7.1.3, chownr 3.0.0, yallist 5.0.0, sax 1.6.1, isexe 3.1.5/4.0.0) and WTFPL
  (truncate-utf8-bytes 1.0.2). Python-2.0 (argparse) and dual (MIT OR CC0-1.0), (WTFPL OR ISC/MIT) take the allowed side.
- Decisions (D-ids): output apps/desktop/build/release (not dist); minimal staged build/app; CI keeps unsigned artefacts 7
  days as workflow artefacts, nothing published (drop the upload if "no uploads" covers it); default channel canary,
  version from INDUS_DESKTOP_VERSION else pyproject; updater electron-updater on macOS/Linux, manual on Windows (unsigned
  macOS can check/download, not apply, until P23 signs); Linux binary indus-desktop; renderer find via CSS Highlight API
  (Ctrl/Cmd+F; Linux main claims Ctrl+F); native context menu (spelling, copy/paste, Copy Link http/https/mailto only,
  Copy Image, never opens links, Inspect in dev only); diagnostics bundle in main, redacted (patterns, known values, ~),
  256 KiB cap, to the clipboard, renderer gets {copied, bytes}; renderer errors rate-limited (20/window/min); Copy
  Diagnostics under Help.
- Follow-ups: green desktop-package run on all three OSes; serve token + boot state into diagnostics; preload-bridge.md rows.

## w35-d3.patch: P20-32 settings overlay + P20-33 provider onboarding + P20-34 deep links and media scheme — done
- Builder checks: lint, fmt:check (587 files), -r typecheck clean; check-ipc, gen-bridge-types --check,
  dump-slash-catalog --check ok; root pnpm test 1908 passed / 12 skipped; ruff, pyright 0, lint-imports; pytest
  tests/contract/web 264 passed (one earlier failure in test_tui_publish.py::test_a_refused_credential_stops_the_feed_at_once,
  not this slice, passed alone + rerun — integrator: watch it); tooling.checks ok. No packages. No stand-ins.
- New: electron/{deepLinks,mediaProtocol,prefs}.ts (+tests); src/settings/* (13 views, search, schema pages via
  /api/config/schema + PUT /api/config, keys via /api/env), src/onboarding/* (state machine), src/deepLinks/*,
  src/app/settingsSlots.tsx; tests/contract/web/test_model_endpoint_route.py; notes settings.md, onboarding.md,
  deep-links-media.md.
- Shared: ipc channels/schemas (indus:prefs:get|set|changed) + global.d.ts; electron/main.ts (deep-link router before
  ready; second instance delivers links without raising the window; installPrefsIpc, installMediaProtocol, installDeepLinks;
  installBackend's return captured; startDeepLinks() after openMain()) — D5's main-ts wiring patch also edits main.ts;
  src/app/App.tsx (AppSlots.overlays / layers); src/main.tsx; en.ts; src/indus/web/routes/models.py (NEW route POST
  /api/model/test-endpoint); apps/web/src/test/routes.generated.ts; traps R08.21.
- Wiring: P20-35/36 read tray/keep-awake/quick-entry/HUD shortcuts from the DesktopPrefs installPrefsIpc returns; P20-28
  listens on preview.onCloseRequested; P20-29 adds workspaces via MediaRoots.add (roots: home, <home>/media, <home>/cache);
  Settings › Providers opens onboarding via requestOnboarding(); D5's <LogsViewer/> goes in Settings › About/Advanced.
- Decisions (D-ids): config writes via REST PUT /api/config (daemon config.set takes live keys only); new
  /api/model/test-endpoint; prefs in <userData>/desktop-prefs.json; reveal shows only what the person typed, no route
  returns stored values; "Choose later" not saved; deep links add only https / loopback-http MCP servers, never command
  servers, skill links never force install; deep-link delivery shows without focus (close-preview focuses); media serves
  images (not SVG), one stream host for local and remote (remote via single-use download ticket minted in main).
- Follow-ups: repo-scan settings page (P20-25); config export/import/reset not built; keybinding overrides read through
  effectiveBindings by their owners; onboarding in the main window only?

## w35-d2.patch: P20-28 preview pane + P20-29 files pane and fs IPC + P20-30 git review pane + P20-31 terminal pane — done
- Builder checks: lint (586 files), fmt:check, -r typecheck (8 projects) clean; desktop vitest 690 passed / 12 skipped
  (Electron-binary tests + one real-PTY test needing a built node-pty); check-ipc, gen-bridge-types --check ok;
  tooling.checks ok; root pnpm test NOT run. No Python.
- New (apps/desktop): electron/fs/{pathGuard,fsIpc}.ts, electron/preview/{guestPreload,guestPreloadEntry,fileWatch,guest}.ts,
  electron/git/gitOps.ts, electron/terminal/{ptyHost,terminalEnv}.ts (+tests), electron/panesInstall.ts, testing/paneIpc.ts,
  src/panes/{strings.en.ts, d2Panes.tsx, preview/*, files/*, review/*, terminal/*} (+tests); notes preview-pane.md,
  files-pane.md, git-review.md, terminal-pane.md.
- STAND-IN: src/panes/_stubs_d2.ts (D1's pane registry; interface at the top: context sessionId, workspace, mode, hidden,
  connection, openPreview?, addToChat?; preview + terminal stateful). d2Panes.tsx registerWorkingPanes() must be called by
  the shell at startup.
- Shared: ipc channels (+6: indus:fs:set-roots, fs:stat, fs:limits:get|set, git:changes, git:commit-context) and RESHAPED
  schemas for fs/git/terminal/preview (fs.readDir drops workspace, answers truncated, entries sensitive/target; fs.readText
  answers binary; git.status root/head/counts/truncated; git.diff base?/staged?; terminal.start answers shell + cwd) +
  global.d.ts; electron/main.ts (guest preload path, did-attach-webview → guests.adopt, isGuestOf, installPanes before
  seal(), paneElectron() helper) — D3 and D5 also edit main.ts; scripts/build-electron.mjs (3rd bundle preview-guest.cjs,
  node-pty external); en.ts; apps/desktop/package.json + pnpm-lock (+16 importer lines; @xterm/* 6.0.0 / addons, node-pty
  1.1.0 optional — already in the lock for apps/web / apps/tui); realWindow.electron.test.ts (outside-roots); traps R08.8,
  R08.9.
- Wiring: composer can use fs.stat {purpose:"attach"} and accept application/x-indus-path drags; preview onAttachCapture
  needs an image hand-off in the composer; settings use fs.getLimits/setLimits; Ctrl/Cmd+Shift+W in preview sends
  indus:preview:close-requested (D3's deep links listen); node-pty asarUnpack + spawn-helper +x in D5's packaging.
- Decisions (D-ids): per-window allowed roots (home + fs.setRoots folders + dialog-picked files; system folders / fs root /
  above home refused; checked before and after symlink resolution); sensitive files listed with a lock, never read
  (.env.example still refused); git without a shell, -c core.fsmonitor=false, --no-ext-diff, GIT_DIR-style env dropped,
  relative in-repo paths never starting with '-', last-turn base a commit id recorded at turn.started; "Write it for me"
  commit message via prompt.background + side session message.complete (VERIFY on a real daemon); terminal env mirrors
  child_env.py's floor (test compares) + drops *_API_KEY, INDUS_*, npm settings, colour-disabling vars; only the starting
  window can write/resize/close; remote mode: files via REST /api/files (no rename/reveal, delete confirms), terminal + git
  review show "not available here"; preview pop-out is a prop; file-watch storm breaker on all platforms.
- Follow-ups: real PTY run + hostile guest E2E (P20-40); remote terminals via the serve PTY bridge (P18-07); PR badges / gh
  auth; restart terminal in the last folder.

## w35-d1.patch: P20-25 sidebar (sessions + projects) + P20-26 profile rail, routing, switch shapes + P20-27 pane shell and interface modes — done
- Builder checks: biome (583 files), -r typecheck clean; check-ipc, gen-bridge-types --check, dump-slash-catalog --check ok;
  root pnpm test 1969 passed / 12 skipped (desktop 737); ruff, format, pyright, lint-imports; tests/contract/web +
  tests/unit/web 618 passed; tooling.checks ok; vite + electron main builds ok. Wired-app Electron test NOT run (no
  binary). No stand-ins, no packages.
- New: apps/desktop/src/sidebar/*, src/profiles/*, electron/profiles/routing.ts, src/panes/{paneTree, presets,
  layoutStore, PaneShell, shortcuts, strings.en, panes.css} (+tests incl. seeded property test); notes sidebar.md,
  profile-routing.md, pane-shell.md.
- Shared: ipc channels/schemas (indus:profile:prepare-change, indus:profile:commit-change → 78 channels; profileState gains
  launch; launch facts gain homeDir) + global.d.ts; homeDir defaults in electron/ipc/bridge.ts, electron/windows/ipc.ts,
  src/app/bridge.ts, registry.test.ts; electron/main.ts (installProfiles + BackendPort from installBackend) — D2, D3, D5
  also edit main.ts; electron/backend/install.ts ({launchProfile}); electron/backend/localAttach.ts (+test: always
  `-p default` explicitly); src/gateway/connectionStore.ts (setPrimary/release); src/main.tsx, App.tsx, Shell.tsx (rail,
  sidebar, pane shell, shortcuts, interface mode; per-profile workspaces); Composer.tsx (+test) + chatSlots.tsx
  (newSessionCwd → cwd on session.create); en.ts (nav.sessionsLater → nav.more); styles.css; PYTHON
  src/indus/web/routes/sessions_store.py (lineage_root on /api/sessions rows) + contract test; traps R08.6, R08.7;
  preload-bridge.md P20-26 row.
- Wiring: D2 panes register via paneRegistry.register({id, stateful, title?, render(ctx {sessionId, profile, active,
  hidden})}) from src/panes/PaneShell.tsx — D2 assumed ctx {sessionId, workspace, mode, hidden, connection, openPreview?,
  addToChat?}: reconcile at integration (add workspace/mode/connection/openPreview/addToChat to D1's ctx); preview +
  terminal stateful. D3 settings: Gateways page wraps remote.save/forget in switcher.applyConnectionChange(...); profile
  rename/delete via switcher.renameProfile/deleteProfile; Workspace page uses ProjectStore setRepoScan/setDefaultDirectory.
  Fake backend's 404 on /api/sessions now shows an empty list.
- Decisions (D-ids): pin/archive as fenced writes (snapshot first, one re-read on stale revision); projects are local per-
  profile records; profile storage keys registered in one place (test fails on unlisted keys); main keeps one published
  connection, background redial re-homes REST target (REST calls name their profile); simple mode refuses hidden panes;
  one chat pane per window ("new tab" = new chat); layout saved by the main window only; launch profile fixed per launch.
- Follow-ups: per-profile connections in main; Settings UI for projects/scan/default dir; worktree lanes; all-profiles
  sidebar; remember a closed pane's size.

## w35-d4.patch: P20-35 HUD, quick entry, extra windows + P20-36 tray, keep awake, notifications + P20-37 updates (unsigned 1.0) — done
- Builder checks: lint, fmt:check, -r typecheck, check-ipc, gen-bridge-types --check clean; root pnpm test 1979 passed /
  12 skipped; ambient tests 112; tooling.checks ok (one similarity hit in defaultHudBounds rewritten). Real HUD / tray /
  notifications / update flows only with fakes (no Electron binary, no desktop session). Update checks against a loopback
  fake feed (testing/fakeFeed.ts). No Python.
- New: apps/desktop/electron/ambient/{shortcuts,hud,quickEntry,activeSession,tray,trayIcon,keepAwake,notifications,dbus,
  linkPreview,settings,install}.ts, electron/updates/{updater,marker,skew,feed,engine,version,install}.ts (+tests);
  src/{hud/Hud.tsx, quick/QuickEntry.tsx, quick/deliver.ts, ambient/*, updates/UpdateNotice.tsx, updates/skew.ts};
  testing/{fakeFeed,fakeFloating}.ts; notes hud-quick-entry.md, tray-keep-awake.md, updates.md.
- Shared: ipc channels/schemas (+21 channels; update status shape) + global.d.ts; electron/windows/{factory,manager,
  windowUrl}.ts (openHud, openQuick, onWindowCreated, kindOf, windowOf); quitGuard.ts (subscribe); backend/install.ts
  (optional updateGate); electron/main.ts (wiring below) — D1, D2, D3, D5 also edit main.ts; src/app/App.tsx (HUD + quick
  roots, <AmbientHost>); src/main.tsx; en.ts; apps/desktop/package.json; pnpm-lock.yaml; pnpm-workspace.yaml (OVERRIDE
  sax -> 1.4.1 ISC to avoid BlueOak 1.6.1 — CONFLICTS with D5's electron-builder tree which also has sax 1.6.1; reconcile:
  one override for the whole workspace, or drop it if the owner allows BlueOak for build-only); traps R08.13, R08.14;
  00-layout pin row.
- main.ts wiring: `const updateGate = createUpdateGate(context);` before installBackend(..., {updateGate:
  gateHooks(updateGate)}); then installAmbient(context, ambientElectron(), backend) AFTER installBackend; installUpdates(
  context, {app, fetch: net.fetch, runtimeVersion}, {gate: updateGate, stopAppWork}); all before registry.seal().
- Cross-slice: settings (D3) uses shortcuts.get/set, ambient.get/set/onChanged, updates.*; sidebar (D1) hud.open({sessionId})
  "pop out"; links.preview({url}); packaging (D5) writes desktop-build-stamp.json {version, commit, channel, updateOwner}
  (also into app.asar.unpacked) and ships electron-updater in the app's node_modules (loaded by name); P23 `indus update`
  writes the marker {format: 1, owner, pid, process_start, created_at} (updates.md); preload-bridge.md channel count.
- Packages: electron-updater 6.8.9 (MIT, 2026-06-05) + builder-util-runtime 9.7.0, fs-extra 10.1.0, jsonfile 6.2.1,
  universalify 2.0.1, js-yaml 4.3.2, argparse 2.0.1 (Python-2.0), lazy-val 1.0.5, lodash.escaperegexp 4.1.2,
  lodash.isequal 4.5.0 (deprecated upstream), tiny-typed-emitter 2.1.0, semver 7.7.4 — all allowed.
- Decisions (D-ids): D-Bus via the gdbus CLI (no library); tray glyph drawn in code, chime synthesised; link previews refuse
  loopback/private hosts; snap chord only while a HUD is open; main window returns unfocused after the HUD (reference
  focuses); quick entry targets the last-used window, no picker; single-JSON update marker (not the reference's v2);
  availability via the releases API, electron-updater feed at apply; default releases repo varunisrani/indus-agent
  (owner-open, same as D-16.690); background checks only when packaged or INDUS_DESKTOP_UPDATE_FEED is set.
- Follow-ups: P20-40 E2E for real HUD / quick window / global shortcuts / tray with and without a StatusNotifier host;
  signed-update install (P23); a transcript user of links.preview.
