# Task: Renderer entry, routes and shell (P20-15) Phase: 20 Owner: wave-34 builder R

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/main.tsx` (all),
`apps/desktop/src/app/routes.ts` (all), `apps/desktop/DESIGN.md` (principles, information
architecture, surfaces, z-index ladder, motion, loader rule). Closed before writing.
Evidence: none.

What it does (own words): the renderer is a Vite + React 19 page loaded by every window. The
window kind comes from the `?win=` query (also reported by main as `facts.windowKind`): main,
secondary (one session, optional watch mode) and instance windows get the full shell; HUD and quick
entry get a small "coming later" root until P20-35. Routing is a HashRouter with transitions off
(route changes commit at normal priority even while deltas stream). Routes: chat home `/` (a new
chat) and `/<sessionId>`; pages `/capabilities`, `/messaging`, `/artifacts` (empty states "coming
later" in 1.0); overlays `/settings`, `/cron`, `/profiles`, `/agents` (reserved; each shows a
"coming later" card over the current route). Anything else goes to a new chat. Overlays keep the
route they were opened from as the background and return to it on close (opened cold → home).
Pickers and dialogs are not routes. Shell: left sidebar (new chat, pages, overlay entries; the
session list is P20-25), the central pane area (chat or page), bottom status bar (slot; slice C's
`StatusBar` at merge). Every visible string goes through `useT()` (English only, D-16.51). Theme:
light / dark / system from `@indus/ui` tokens, the choice kept per window in local storage and told
to main (`windows.setTheme`) so the title bar matches. Busy states use an animated `Loader`
element, never a literal "Loading…".

Build: `vite.config.ts` builds `index.html` into `dist/` (what main's `file://` origin loads) and
injects the Content-Security-Policy meta tag with P20-04's `injectCspMeta` for both dev and build.
No React-refresh plugin: JSX goes through Vite's own transform, so no inline preamble script is
needed and the dev CSP (no inline script) holds; dev edits reload the page (HMR without state).

Inputs → outputs: `location.search` (`win`, `watch`, `profile`) + hash route → rendered root;
theme choice → `data-theme` on `<html>` + `windows.setTheme`.

Files: `apps/desktop/index.html`, `vite.config.ts`, `src/main.tsx`, `src/styles.css`,
`src/app/{routes.ts,Shell.tsx,App.tsx,Loader.tsx,theme.tsx,pages.tsx,windowKind.ts,bridge.ts}`,
`src/i18n/{index.tsx,en.ts}`, `src/app/routes.test.ts`, `src/app/Shell.test.tsx`,
`src/i18n/i18n.test.ts`, `scripts/viteConfig.test.ts`. Shared (minimal): `apps/desktop/package.json`
(deps), `tsconfig.json` (jsx, vite types, root config files), `vitest.config.ts` (src tests).

Edge cases / failure modes:
- `/settings` opened cold → closing goes home; `/settings` from `/abc` → back to `/abc`;
- reserved names never read as a session id; `/a/b`, `/%zz` → home;
- an encoded session id (`/a%2Fb`) decodes once;
- no bridge (plain browser preview) → facts default to a main window, theme still works;
- a missing translation key → typecheck error (keys are typed), parity test fails.

Our design differences / decisions:
- Route table + classifier is a pure module (`routes.ts`), no store library; no plugin routes in
  1.0 (P24).
- Radix only where a primitive is needed (Dialog for the overlay card); Lucide icons.
- Fonts: family names with system fallbacks (no OFL font vendored yet, as on web).

Tests we will write: route classifier and overlay return; i18n key parity (every `t("…")` key in
`src/` exists in `en.ts`, every `en.ts` key is used); axe on the rendered shell (light and dark);
CSP meta in dev and build (no `unsafe-inline` script, no inline `<script>` in `index.html`).

## Decisions for the lead
- The build-time CSP meta cannot know the daemon/serve ports or a remote host, and a `file://`
  page is governed by the meta (the header listener does not see `file:` responses). The meta's
  `connect-src` therefore adds loopback (`ws://127.0.0.1:*`, `http://127.0.0.1:*`,
  `ws://localhost:*`, `http://localhost:*`) and `wss:`/`https:` for remote connections; where the
  header applies (dev server) the browser enforces both policies, so the header's exact origins
  win. Script, frame, object and base rules are unchanged.
- `@indus/ui` React primitives (SegmentedControl, ListRow) not added: `packages/ui` has no React
  yet; the desktop keeps them local until P20-22/web need them shared.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Window kind from `?win=`; HUD/quick get a small root (later P20-35); route table and classifier (test: `src/app/routes.test.ts`).
- [x] HashRouter with transitions off (`useTransitions={false}`), as the reference does for the same starvation reason.
- [x] Unknown → new chat; overlay over its background and back on close; cold overlay → home (tests: `src/app/Shell.test.tsx`).
- [x] i18n key parity: every `t("…")` key exists, every English key is used (test: `src/i18n/i18n.test.ts`).
- [x] Axe on the shell, light and dark (test: `Shell.test.tsx::has no axe violations`).
- [x] CSP meta first in `<head>` in dev and build, no inline script (test: `scripts/viteConfig.test.ts`); dev server checked by hand (`/@vite/client` is an external script); built app renders in Electron from `file://`, `eval` blocked.
- [x] Gap found: the reference wraps the root in an error boundary that logs to main — added `RootErrorBoundary` (reports through `logs.rendererError`; test `Shell.test.tsx::a render error…`).
- [x] z-index ladder (overlay < toast < boot), reduced motion respected, animated Loader (DESIGN principles).
- Deliberate differences: no plugin-contributed routes, no command center / session import / webhooks / starmap routes (not in the row); no React-refresh in dev (CSP).
- Not taken: transparent-window pre-paint styles (HUD/quick are P20-35), clipboard and selection-colour shims (P20-39).

## Status

Done (2026-10-21). Dev: run `pnpm --filter @indus/desktop dev` and start main with
`INDUS_DESKTOP_DEV_SERVER=http://127.0.0.1:5174`. Shared: `apps/desktop/{package.json,tsconfig.json,vitest.config.ts}`,
root `biome.json` (Tailwind CSS directives for `apps/desktop/src/**/*.css`).

Integration (wave-34 merge, D-16.670): Electron 44 does apply main's CSP header to the `file://`
page, so the header's `connect-src 'self'` blocked the first dial to the daemon. Main's header
(`CspState`) now carries the same `RUNTIME_CONNECT_SOURCES` (moved to `electron/security/csp.ts`,
re-exported by `vite.config.ts`) beside the exact origins. Slots filled by `src/app/chatSlots.tsx`
(composer, cards, status bar, Retry), mounted by `main.tsx`.
