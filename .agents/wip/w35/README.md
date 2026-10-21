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
