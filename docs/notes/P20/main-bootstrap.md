# Task: Main bootstrap and single-instance lock (P20-01) Phase: 20 Owner: wave-33 builder D1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/main.ts` (top of
the file, the single-instance and pre-ready parts around 1,220–1,460 and 1,836–1,870, the
second-instance block near 18,080–18,160, `createWindow` 13,873–14,344 for ordering only),
`apps/desktop/electron/shell-path.ts` (all), `apps/desktop/electron/renderer-heap-flags.ts` (all),
`apps/desktop/electron/log-rotation.ts`, `apps/desktop/electron/desktop-log-line.ts`,
`apps/desktop/scripts/bundle-env.mjs`, `apps/desktop/electron/hud-windowing.ts` (`linuxOzoneBackend` only),
`apps/desktop/AGENTS.md` (skim), `apps/desktop/ENGINEERING.md`
(skim). Research 08 §3.1, §3.2, §3.23 and traps R08.3, R08.16. Closed before writing.
Evidence: none.

What it does (own words): the Electron main process starts in a fixed order. Before Electron is
ready it fills in the env defaults baked into the bundle, reads two launch knobs from the profile's
`config.yaml`, picks the Wayland ozone backend when the session is Wayland, takes the
single-instance lock and registers the privileged `indus-media` scheme and the `indus://` deep-link
client. Only the instance that holds the lock goes further: a second launch is inert — it hands its
argv to the running app (Electron delivers it as `second-instance`) and exits before `ready`, and
every piece of process machinery (spawn, reap, claim a backend) refuses to run in it. On `ready` the
holder merges the login-shell `PATH` into its env (bounded by 3 s; a broken profile never blocks
boot), installs the session security, the IPC registry and the app menu, then opens the main
window. Everything the app logs goes to `<home>/logs/desktop.log` through a 64 KiB buffer flushed
every 120 ms, rotated at 10 MiB (three backups) and redacted.

Inputs → outputs:
- bundle defaults `{ KEY: "value" | null }` (build-time JSON, allow-listed keys only) + `process.env`
  → env with defaults filled in (an explicit value wins, an empty string counts as explicit, `null`
  clears an inherited value to empty).
- `<home>/config.yaml` text → `{ electronFlags: string[], rendererMaxOldSpaceMb: number }` → planned
  Chromium switches (all `--js-flags` sources merged into one switch; flags already on argv are not
  re-applied).
- env (`XDG_SESSION_TYPE`, `WAYLAND_DISPLAY`, WSL markers) + argv + `ELECTRON_OZONE_PLATFORM_HINT`
  → `ozone-platform=wayland` or nothing.
- lock result → `SingleInstance { holder, machinery }`; `machinery.guard(fn)` throws
  `NotLockHolderError` in a loser; `second-instance` argv → deep links (`indus://…`, dev
  `indus-dev://…`) handed to a callback.
- login shell (`$SHELL -ilc`, then `-lc`, sentinel-wrapped `PATH`) → merged `PATH` (login entries
  first, inherited extras kept, duplicates dropped) or the original `PATH` on any failure.
- `DesktopLog.write(line)` → stamped, redacted lines in `desktop.log`.

Files: `apps/desktop/electron/{main,singleInstance,envDefaults,launchConfig,shellPath,log,redact,
paths,appMenu,appOrigin,context,fsSecure}.ts`, tests `singleInstance.test.ts`, `shellPath.test.ts`, `envDefaults.test.ts`,
`launchConfig.test.ts`, `log.test.ts`; `apps/desktop/{package.json,tsconfig.json,vitest.config.ts}`,
`apps/desktop/scripts/build-electron.mjs`; fixtures `apps/desktop/testing/fakeBackend/` and
`apps/desktop/testing/guestPages/` (shared with P20-02..17, see the fixture section below).

Edge cases / failure modes:
- bundle defaults with a key outside the allow-list, a non-string value or a NUL → refused at build
  time (the build script fails), ignored at run time;
- `config.yaml` missing, unreadable, or `desktop:` written with another indentation → defaults, one
  log line when the block clearly meant to set one of the two keys;
- `--js-flags` given as `--js-flags=…` or `--js-flags …` on argv → merged with the config's flags,
  `--max-old-space-size` from argv beats the config's ceiling;
- ozone: explicit `--ozone-platform` / `--ozone-platform-hint` on argv or the hint env → left alone;
  SSH-forwarded X display (`SSH_CONNECTION` + `DISPLAY`, no `WAYLAND_DISPLAY`) → X11;
- shell that exits 1, prints nothing, prints banners around the sentinel, or hangs → original PATH;
  a hung shell is killed at the deadline (3 s for the whole merge);
- loser instance: `ready` never runs boot code; anything wrapped by `machinery.guard` throws;
- deep links: only our scheme (dev scheme only in dev builds); other argv ignored; at most one per
  argv (the last), length-capped;
- log: directory missing → created; write failure → dropped silently (a log must never crash
  main); a burst above 64 KiB flushes early; process exit flushes synchronously.

Our design differences / decisions:
- Our names: `indus-media`, `indus://` (dev `indus-dev://`), `INDUS_HOME`, `~/.indus`,
  `desktop.log`, env defaults allow-list `INDUS_HOME`, `INDUS_DESKTOP_USER_DATA_DIR`.
- The loser never reaches the `ready` code path at all (boot is one function called only by the
  holder) *and* the machinery gate throws, so a later refactor cannot reintroduce R08.3 silently.
- PATH merge deadline is 3 s for the whole merge (the row), not per attempt.
- Bundling: the row names esbuild; we bundle main (ESM) and the sandboxed preload (CJS) with the
  workspace's Vite 8 (rolldown) through `scripts/build-electron.mjs`, so no new bundler package.
  Same output shape (one plain-JS file each).
- Crash-loop/GPU markers (research §3.2 step 2) are not here: P20-13 (D-16.81).

Tests we will write:
- singleInstance: holder vs loser (fake app), guard throws for a loser only, `second-instance`
  argv → deep link callback, dev scheme only in dev, macOS registers the protocol before the lock.
- envDefaults: the precedence table (absent → default, explicit wins, empty explicit wins, null
  clears, bad keys refused).
- launchConfig: YAML subset (flow list, block list, one string, integer, bad indentation → defaults
  + one warning), switch planning (js-flags merge, argv wins), ozone table.
- shellPath: fake shell exits 1 / hangs 5 s → original PATH within the deadline; sentinel parsing
  with banners; merge order and de-duplication; win32 skipped.
- log: buffered flush at 120 ms (fake timers), early flush above 64 KiB, rotation cascade, a token
  pattern redacted.

## Fixtures created here (shared, P20-01 to P20-17)

- `apps/desktop/testing/fakeBackend/ensure.mjs`: a fake `indus` executable (`--version`,
  `[-p NAME] gateway ensure --json`, `[-p NAME] serve ensure --json`) driven by a scenario file
  (`INDUS_FAKE_SCENARIO`, a name under `scenarios/` or a path) and state under `INDUS_HOME`.
- `apps/desktop/testing/fakeBackend/server.mjs`: the long-lived fake daemon + serve: owner-only
  control socket `<home>/gateway.sock` speaking the P15-27 wire (`identify`, `describe`,
  `session-ticket`), a loopback WebSocket `/ws` that accepts exactly one use of each ticket offered
  as `indus-gateway-ticket.<t>` beside `indus-gateway-v1` (4401 otherwise) and answers JSON-RPC
  through `@indus/client`'s `FakeServer`, and REST `/api/*` gated on the session token.
- `apps/desktop/testing/fakeBackend/scenarios/*.json`: healthy, slow-ensure, ensure-fails,
  version-skew, stale-socket (first mint), rest-errors (503 twice, slow, 403), version-probe-fails,
  version-probe-hangs (10 s, for P20-08's 5 s probe).
- `apps/desktop/testing/fakeBackend/control.mjs`: the control-socket client (tests, ensure.mjs). A
  home too long for a socket path (this sandbox) is bound and reached by a relative name from the
  home, so every file stays inside the home; the real daemon uses its pointer-file fallback instead.
- Fake-only control verbs for later tests: `fake-emit {event}` (push an event to every connection),
  `fake-drop` (cut every WS connection), `fake-received` (frames the clients sent).
- `apps/desktop/testing/guestPages/*.html`: hostile pages (window.open, synthetic click, `file:`
  and `indus:` links, a sandboxed iframe without popups calling the opener and the bridge).

## Decisions for the lead
- esbuild → Vite (rolldown) for the main/preload bundles (above). Swap by replacing
  `scripts/build-electron.mjs`.
- Electron 44 downloads its binary lazily (no install script any more); `pnpm-workspace.yaml` needs
  no `allowBuilds` entry. The desktop tests that need a binary call the package's own installer
  (`node node_modules/electron/install.js`, cache in `ELECTRON_CACHE`/`electron_config_cache`) or
  skip with a reason.
- Desktop home resolution: `INDUS_HOME` when set (absolute), else `~/.indus`.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Loser inert, guarded machinery throws, argv/deep link forwarded (test: `electron/singleInstance.test.ts::a loser stays inert…`, `…receives a second launch's argv and deep link`); real two-launch smoke under xvfb: second launch exits 0, holder logs the deep link and raises its window.
- [x] macOS registers the protocol client before the lock (test: `singleInstance.test.ts::macOS registers the protocol client before taking the lock…`).
- [x] Env defaults precedence incl. empty-explicit and null-clears (test: `electron/envDefaults.test.ts`).
- [x] YAML subset: flow list, block list, string, integer, bad indentation → one warning (test: `electron/launchConfig.test.ts`); `--name value` argv pairs merged into one `js-flags` (test: `planSwitches`).
- [x] Shell PATH: exit 1, 5 s hang, banners + non-zero exit, `-i` refused, missing shell (test: `electron/shellPath.test.ts`, real child processes).
- [x] Log: 120 ms flush, 64 KiB early flush, rotation cascade, discard past 40 MiB, token redacted (test: `electron/log.test.ts`).
- [x] Gap: the reference clears a provably dead owner's `SingletonLock` (Linux zombie pid) and retries once — added `clearStaleLock` + one retry (test: `singleInstance.test.ts::a stale lock … is removed once and the lock retried`).
- [x] Gap: a TMPDIR too long for Chromium's lock socket aborts the launch (hit for real in this sandbox) — the lock alone now uses `/tmp` on Linux when needed (`lockTempDir`, test: `lock robustness`). The reference shortens TMPDIR in its launcher instead.
- [x] Gap: PATH merge must finish before anything spawns — `MainContext.shellPath` (promise) for P20-08 to await.
- [x] Gap: lines buffered at a SIGTERM were lost — signals now quit in order and `exit` flushes synchronously (smoke run shows the lines).
- [x] Log line shape aligned with our Python logs (`<stamp> <LEVEL> desktop: …`, parsed by `indus logs`).
- Deliberate differences: one deadline (3 s) for the whole PATH merge, random markers per run; the loser exits before `ready` *and* the gate throws; spellcheck dictionaries not fetched (factory sets `spellcheck: false` until P20-39 decides the source).
- Not taken: `desktop.ssh_path` and `renderer_accessibility` launch keys (N17 / later), the reference's bundle-env keys beyond ours, crash-loop/GPU markers (P20-13, D-16.81), `--close-preview` argv flag (P20-34), cold-start `--profile` persistence (P20-26).

## Status

Done (2026-10-21). `apps/desktop/package.json` created (deps below); `pnpm-lock.yaml` regenerated in the worktree (lead re-locks; it also gained the missing `apps/photon-sidecar: {}` importer). No shared source file changed. Fixtures `testing/fakeBackend/` (server, ensure, control client, 8 scenarios, test) and `testing/guestPages/` (4 hostile pages) are ready for P20-02..17. Follow-ups: `indus logs desktop` (add `desktop.log` to the CLI's LOG_FILES), `indus desktop` launcher may also shorten TMPDIR (P20-08).
