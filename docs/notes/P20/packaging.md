# Task: Packaging: unsigned builds on three OSes (P20-38) Phase: 20 Owner: wave-35 builder D5

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron-builder.config.cjs`
(all 350 lines: targets, asar unpack list, mac Info.plist usage strings, hooks order, signing and
publish wiring), `apps/desktop/package.json` (scripts and deps), `apps/desktop/BUILDING.md` (all:
artefact/update-owner table, stamp, local test builds, signing sections). Research 08 §3.28 (fuses)
and D-16.15, D-16.70, D-16.84. Closed before writing.
Evidence: none.

What it does (own words): one command per OS turns the desktop workspace into unsigned installable
artefacts. `pnpm --filter @indus/desktop dist:unsigned` builds the renderer (Vite → `dist/`) and the
main process + sandboxed preload (`dist-electron/`), writes the build stamp, stages a minimal app
folder and runs electron-builder for the host OS with publishing off and every signing variable
removed from its environment:

- macOS: DMG (download) + ZIP (the update artefact electron-updater consumes); hardened runtime,
  entitlements and Info.plist usage strings are in the config so P23 only adds an identity, but
  `identity: null` and `notarize: false` keep the build unsigned.
- Windows: NSIS installer (per-user, no admin) and a portable ZIP, both unsigned (D-16.84);
  Authenticode is P23 (D-16.70); MSIX deferred (D-16.15).
- Linux: AppImage.

The staged app (`build/app/`) holds only what runs: a generated `package.json` (name, product
name, version, `main`, no dependencies), the built `dist/` and `dist-electron/` (no source maps), and
native modules the desktop declares (node-pty, P20-31) copied with their compiled `.node` files and
prebuilds. Everything the renderer needs is already bundled by Vite, so no `node_modules` tree
enters the archive. The archive is an asar; `.node` binaries and node-pty are unpacked next to it.

The build stamp `desktop-build-stamp.json` (schema 1: version, commit, channel, update owner, build
time) ships as an extra resource beside the asar. The update owner is declared, never inferred
(P20-37 reads it): `electron-updater` for macOS and Linux AppImage, `manual` for the unsigned
Windows build; a source checkout has no stamp and P20-37 treats it as manual.

electron-builder's `afterPack` runs P20-04's fuse hook (six fuses, ad-hoc re-sign on arm64 macOS)
and then the asar check: no TypeScript source in the archive, node-pty's binaries unpacked when
present, the entry files and the stamp present. A failure fails the build. CI repeats both checks
with `verify-fuses.mjs` and `check-asar.mjs` over the output folder and keeps the artefacts.

Launcher entry on Linux: an AppImage has no installed menu entry, so after the main window is first
visible the app writes `~/.local/share/applications/ai.indus.desktop.desktop` (Exec = the AppImage
file, `%U` so `indus://` links reach it, `MimeType=x-scheme-handler/indus;`) and its icon, only when
running packaged from an AppImage and only when the content changed.

Inputs → outputs:
- `node scripts/dist-unsigned.mjs [--dir] [--skip-build]` → `build/release/` artefacts
  (`Indus-<version>-mac-<arch>.dmg|zip`, `Indus-Setup-<version>-<arch>.exe`,
  `Indus-<version>-win-<arch>.zip`, `Indus-<version>-linux-<arch>.AppImage`; `--dir` = unpacked).
- `node scripts/write-build-stamp.mjs --out FILE [--channel stable|canary] [--platform P]
  [--version V] [--commit SHA] [--update-owner electron-updater|manual]` → stamp JSON; env
  fallbacks `INDUS_DESKTOP_CHANNEL`, `INDUS_DESKTOP_VERSION`, `GITHUB_SHA`, `SOURCE_DATE_EPOCH`.
- `node scripts/check-asar.mjs <release-dir|resources-dir>...` → listing summary; exit 1 on a
  problem, 2 on usage.
- `installLinuxLauncher(win, deps)` (main, P20-38) → desktop entry + icon on the first show.

Files: `apps/desktop/electron-builder.config.cjs`, `apps/desktop/scripts/write-build-stamp.mjs`,
`apps/desktop/scripts/write-build-stamp.test.ts`, `apps/desktop/scripts/stage-app.mjs`,
`apps/desktop/scripts/stage-app.test.ts`, `apps/desktop/scripts/asar.mjs` (header reader),
`apps/desktop/scripts/check-asar.mjs`, `apps/desktop/scripts/check-asar.test.ts`,
`apps/desktop/scripts/dist-unsigned.mjs`, `apps/desktop/scripts/builderConfig.test.ts`,
`apps/desktop/scripts/make-icons.mjs` + `apps/desktop/resources/icons/` (ours, generated),
`apps/desktop/resources/entitlements.mac.plist`, `apps/desktop/electron/linuxLauncher.ts` + test,
`apps/desktop/testing/electron/packaged.electron.test.ts` (offline `--dir` build when an Electron
binary is present). Shared: `apps/desktop/package.json` (scripts, `electron-builder` dev dep),
`pnpm-workspace.yaml` (`electron-winstaller` build script stays off), `pnpm-lock.yaml`,
`.github/workflows/ci.yml` (desktop-package matrix job, in the gate).

Edge cases / failure modes:
- a signing variable set in the caller's shell (CSC_*, APPLE_*, AZURE_*, WIN_CSC_*) → removed from
  the builder's environment; `GH_TOKEN`/`GITHUB_TOKEN` removed too (no publish provider can fire);
- a version that is not SemVer, an unknown channel or update owner → the stamp writer fails;
- no git and no `GITHUB_SHA` → commit `unknown` (a local tarball build still packs);
- node-pty declared but not built (no compiler) → staging fails naming it (an app whose terminal
  pane cannot load must not ship);
- a `.ts`, `.tsx`, `.mts` or `.cts` file in the archive, or a node-pty `.node` file packed inside
  the asar instead of unpacked → the build fails in `afterPack`;
- macOS arm64: fuse flip invalidates the ad-hoc signature → re-signed ad hoc by P20-04's hook;
- Linux: the binary is `indus-desktop`, never `indus`, so the CLI on PATH is not shadowed;
- the launcher writer: not an AppImage, dev build, unchanged content or an unwritable data dir →
  nothing written (logged once), never a crash.

Our design differences / decisions:
- One packaged variant (D-16.48), no bundled runtime payload (D-16.82), no Store/MSIX (D-16.15),
  no feed configuration (`publish: null`; P20-37/P23 own feeds).
- A staged app folder instead of electron-builder collecting `node_modules` from the pnpm
  workspace: deterministic contents, no hoisting surprises, nothing the bundles already inline.
- Hooks are inline functions in the `.cjs` config that import our ESM scripts.
- Output under `build/release/` (gitignored) — not `dist/`, which is the renderer's output.
- Icons drawn by our own script (a pure-Node PNG painter) from geometric shapes in our palette.
- Usage strings only for what the app can ask for (microphone, camera, screen recording, local
  network, Apple Events), in our words.

Tests we will write:
- stamp: fields and types, owner per platform, channel/version validation, env fallbacks, CLI writes
  the file.
- builder config: app id, product, targets per OS, signing off (`identity: null`, no sign hooks,
  `notarize: false`), `publish: null`, asar + unpack node-pty, afterPack wired to the fuse hook and
  asar check, stamp extra resource, icons exist and are ours (PNG size), linux executable name.
- staging: package.json has no deps and the right `main`; maps and sources not copied; node-pty
  copied with `.node`/prebuilds and without its sources; missing build → error.
- asar check: hand-built archive with a `.ts` file fails; node-pty `.node` packed → fails; unpacked
  → passes; release-dir discovery for mac/win/linux layouts; missing stamp fails.
- launcher: entry text (Exec quoting, `%U`, MimeType), written once, not rewritten when equal,
  skipped outside an AppImage / unpackaged.
- Electron-gated: offline `--dir` Linux build with the local Electron dist, then verify-fuses and
  check-asar (skips without the binary).

## Decisions for the lead
- Output folder is `apps/desktop/build/release` (the phase's "Run until green" names
  `apps/desktop/dist`, which is the renderer bundle); CI runs `verify-fuses` on `build/release`.
- New dev dependency `electron-builder` 26.17.0 (MIT, 2026-09-26) brings 246 lock entries, all
  passing `minimumReleaseAge`; build-tool only, nothing of it ships in the app. Its tree carries
  licences not on `licenses_allow.json`: BlueOak-1.0.0 (minimatch 10, tar 7, minipass, chownr,
  yallist, sax, isexe ×2 — permissive, OSI-approved; D-16.564 already met it), `WTFPL`
  (truncate-utf8-bytes), `WTFPL OR ISC` / `WTFPL OR MIT` (sanitize-filename, utf8-byte-length — the
  OR side is allowed). The licence guard only gates production dependencies, so it stays green;
  recommend adding BlueOak-1.0.0 and WTFPL to the allow list as dev-only build-tool licences, or an
  exception row for electron-builder's tree.
- CI keeps the unsigned artefacts as workflow artefacts for 7 days (`actions/upload-artifact`);
  nothing is published to a release or a feed. Drop that step if "no uploads" includes them.
- Default channel for a build without `--channel` is `canary` (a test build is never stable by
  accident); release jobs pass `stable` explicitly.
- Version comes from `INDUS_DESKTOP_VERSION`, else the root `pyproject.toml` version, so the app and
  the runtime it installs carry one version (P20-37's skew check compares them).
- `electron/linuxLauncher.ts` is an extra main-process file (the row asks for the behaviour, not
  the file); one install line for `main.ts` is in the report.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Targets per OS; the macOS ZIP is the update artefact beside the DMG (test: `scripts/builderConfig.test.ts::builds DMG + ZIP on macOS, NSIS + ZIP on Windows, AppImage on Linux`); the config passes electron-builder 26.17.0's own schema (`::passes electron-builder's own schema validation`).
- [x] asar unpack covers native bindings and prebuild folders — gap found: the reference also unpacks `**/prebuilds/**` generically; added beside `**/*.node` and node-pty (`builderConfig.test.ts::packs an asar…`, `check-asar.test.ts`).
- [x] Hooks order: fuses first in afterPack, then the archive check; no afterSign (P23 notarises) (`::never signs, notarises or publishes`).
- [x] Signing variables removed for local unsigned builds, as the reference's local build scripts do (`::strips every signing and publishing variable…`).
- [x] Info.plist usage strings: ours, five capabilities the app can request (`::carries our Info.plist usage strings…`).
- [x] Stamp fields version, commit, channel, update owner (+ platform, build time) (`scripts/write-build-stamp.test.ts`); the owner is declared per platform, never inferred.
- [x] Offline end-to-end probe (not committed): a `--dir` Linux build through electron-builder 26.17.0 with a stand-in Electron folder, node-pty staged → `linux-unpacked/indus-desktop`, `resources/desktop-build-stamp.json`, node-pty unpacked with `pty.node`, `check-asar` ok (510 entries). The committed `testing/electron/packaged.electron.test.ts` runs the real thing (fuses included) where an Electron binary exists.
- Deliberate differences: no `dist/**` unpack (OnlyLoadAppFromAsar + asar integrity want the app inside the archive); no store/MSIX/variant/feed/channel-build branches (D-16.15, D-16.48, D-16.82; feeds P20-37/P23); a staged app folder instead of the workspace's `node_modules`; the Linux binary is `indus-desktop`; Linux AppImage only (the reference also offers deb/rpm locally); our own icon painter instead of rendering reference artwork.
- Not taken: DMG background/volume icon and window layout (default DMG), Windows `.ico` extra resource (electron-builder derives it), macOS Icon Composer package, rcedit identity stamping (electron-builder's own resource edit is enough unsigned), signing caches and notarisation (P23).

## Integration (for the lead)

- `main.ts`: replace `windows.openMain();` with `installLinuxLauncher(windows.openMain(), { platform: process.platform, packaged: app.isPackaged, env: process.env, homeDir: os.homedir(), resourcesPath: process.resourcesPath, scheme: instance.scheme, log: logLine }); // P20-38` (+ import). Ready-made diff with the P20-39 lines: `$S/main-ts-wiring.patch` (d5 scratch), typechecked and biome-clean against this base.
- P20-37 reads `desktop-build-stamp.json` from `process.resourcesPath` (`readBuildStamp` in `electron/diagnostics.ts`; schema/validator `validateBuildStamp` in `scripts/write-build-stamp.mjs`). Note for P20-37: Squirrel.Mac refuses to install an update into an unsigned app, so the unsigned macOS build's "electron-updater" owner can check and download but the apply step needs P23's signature; a build can pass `--update-owner manual` until then.
- P20-31 declares `node-pty` in `apps/desktop/package.json` dependencies and keeps it external in `scripts/build-electron.mjs`; staging then copies it automatically (fails the build if its binding is missing).
- P20-40 adds the packaged smoke test to CI's `desktop-package` job.

## Status

Done (2026-10-21) for the unsigned scope: config, stamp, staging, archive check, icons, launcher entry,
`dist:unsigned` / `pack:unsigned`, CI matrix job (in the gate). Not run here: a real installer build
(no Electron binary and no network for electron-builder's AppImage/NSIS tools in this container) —
the CI job and `packaged.electron.test.ts` cover it. Shared files changed: `apps/desktop/package.json`,
`pnpm-workspace.yaml`, `pnpm-lock.yaml`, `.github/workflows/ci.yml`,
`docs/plan/phases/00-layout-and-conventions.md` (§4 row).

Integration (wave 35, D-16.715, D-16.725): the stamp is also staged inside the archive and copied to `app.asar.unpacked/` by afterPack; electron-updater and its run-time tree are staged when the stamp names it; `spawn-helper` made executable; staging refuses any package whose licence is not on the allow list; `check-asar` checks all of it. The packaged smoke test moved to `e2e/core/packaged.electron.test.ts` and now starts the packaged app (P20-40).
