# Task: CSP and Electron fuses (P20-04) Phase: 20 Owner: wave-33 builder D1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron-builder.config.cjs`
(hooks order only: `beforeBuild`, `beforePack`, `afterExtract`, `afterPack`, `afterSign`),
`apps/desktop/BUILDING.md` (build hooks section). Research 08 §3.28 (no CSP found, no fuse config in
source); D-16.19. Closed before writing.
Evidence: none.

What it does (own words): every renderer page gets one strict Content-Security-Policy, built by one
function and applied twice: as a `<meta http-equiv>` tag injected into the HTML at build time and
as a response header added by `session.webRequest.onHeadersReceived` for the app's own URLs (dev
origin and `file://`). The policy: `default-src 'self'`; `script-src 'self'` (no `unsafe-eval`, no
inline; a dev build adds only the Vite dev origin); `style-src 'self' 'unsafe-inline'`;
`img-src 'self' data: blob: indus-media:`; `media-src 'self' blob: indus-media:`;
`font-src 'self' data:`; `connect-src 'self'` plus the daemon origins computed at boot (and the
dev server's ws origin in dev); `frame-src 'none'` (the preview pane is a `<webview>` on its own
partition, not a frame); `object-src 'none'`; `base-uri 'none'`; `form-action 'none'`.

The packaged binary's Electron fuses are flipped in electron-builder's `afterPack` hook
(`scripts/after-pack-fuses.mjs`, using `@electron/fuses`): RunAsNode off,
EnableNodeOptionsEnvironmentVariable off, EnableNodeCliInspectArguments off,
EnableEmbeddedAsarIntegrityValidation on, OnlyLoadAppFromAsar on, EnableCookieEncryption on. The
wanted table lives in one module (`scripts/fuses.mjs`) read by both the hook and
`scripts/verify-fuses.mjs`, which finds each packaged binary under a dist folder (or takes binary
paths), reads its fuse wire, prints all six and exits 1 on any mismatch.

Inputs → outputs:
- `buildCsp({ dev, devServerOrigin?, daemonOrigins })` → policy string (origins validated:
  http/https/ws/wss only, no path, no wildcard).
- `injectCspMeta(html, csp)` → HTML with the meta tag as the first child of `<head>` (replacing an
  existing one).
- `installCspHeaders(session, { csp: () => string, appOrigin })` → header listener for app URLs
  only; other origins (a webview guest's sites) untouched.
- `after-pack-fuses.mjs` (electron-builder `afterPack(context)`) → fuses flipped on
  `<appOutDir>/<binary>`; ad-hoc signature reset on macOS arm64.
- `verify-fuses.mjs <dist-dir|binary>...` → table on stdout; exit 0 all match, 1 mismatch or no
  binary found, 2 usage.

Files: `apps/desktop/electron/security/csp.ts`, `apps/desktop/electron/security/csp.test.ts`,
`apps/desktop/scripts/{fuses,after-pack-fuses,verify-fuses}.mjs`,
`apps/desktop/scripts/fuses.test.ts`; real-window CSP probe in
`apps/desktop/electron/security/realWindow.electron.test.ts`.

Edge cases / failure modes:
- daemon origin with a path, query, credentials or `*` → refused (throws at boot: a bug);
- duplicated origins → listed once; order stable (snapshot);
- existing CSP header from a dev server → replaced by ours, not merged;
- fuse wire missing (not an Electron binary) → verify fails with "no fuse wire";
- stock Electron binary → verify fails (RunAsNode etc. still on);
- macOS: the fuse file is the framework binary inside the `.app`; Windows: `<name>.exe`;
  Linux: the executable named after the product in `linux-unpacked`.

Our design differences / decisions:
- The reference has no CSP and no fuses in source (research §3.28); both are new (D-16.19).
- `media-src`, `font-src`, `form-action 'none'` added beyond the row's list (narrow, needed for audio
  and our fonts; no loosening of the listed directives).
- Real-window CSP check runs on the unpackaged app under plain Electron (no Playwright) in the unit
  lane; the packaged-build Playwright lane is P20-38/P20-40.

Tests we will write:
- CSP string snapshot for packaged and dev; origin validation; meta injection.
- header listener only rewrites app URLs.
- fuses: wanted table has exactly the six; verify on a stock binary fails; flip a copy with the hook
  and verify passes (skipped without the Electron binary).
- Electron real window (skipped without binary/display): a page under our CSP tries `eval` and an
  inline script; both blocked, a `'self'` script runs.

## Decisions for the lead
- Dev React refresh preamble is an inline script; the dev CSP does not allow it. P20-15 should
  load the preamble from a file served by Vite or use the non-refresh React plugin in dev.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] CSP snapshot (packaged), dev adds only the dev server, no eval/inline anywhere, origin validation, meta injection, header replacement only on app URLs (test: `electron/security/csp.test.ts`).
- [x] Real window: `eval`, `new Function` and the inline script are blocked, the `'self'` script runs (test: `realWindow.electron.test.ts::CSP blocks…`) — unpackaged app under plain Electron 44.5.1 + xvfb.
- [x] Fuse table exactly six; afterPack hook picks the binary per platform; stock Electron 44.5.1 binary fails `verify`; a copy flipped by the hook passes with all six reported (test: `scripts/fuses.test.ts`, real binary).
- [x] Hooks order vs the reference's builder config: fuses in `afterPack`, before any signing (`afterSign` is P23's notarisation).
- Deliberate differences: the reference ships no CSP and no fuse config in source (research §3.28) — both new per D-16.19; `media-src`, `font-src`, `form-action 'none'` added.
- Not taken here: the packaged-build Playwright lane (P20-38/P20-40 wire `afterPack` into electron-builder and run the spec on the packaged app).

## Status

Done for the prework scope (2026-10-21): CSP module, header install (wired in `main.ts`), meta injection helper for the renderer build (P20-15 calls `injectCspMeta` from its Vite config), `after-pack-fuses.mjs` (P20-38 adds it to `electron-builder.config.cjs`), `verify-fuses.mjs`. Packaged-app verification waits for P20-38.
