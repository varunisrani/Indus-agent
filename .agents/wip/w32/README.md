# Wave 32 parked patches (P18-23..29; lead: Agent 2) — base 0aac95d

## w32-p2.patch: P18-26 chat host (xterm over /api/pty) + P18-27 chat right rail — done (P18-27's projects/repos list needs a backend route)
- Builder checks: chat tests 68 passed; apps/web vitest 164 passed (18 files); pnpm lint / fmt:check / -r typecheck clean;
  tooling.checks ok; a scratch vite build ok (xterm chunk 474 kB < 600 kB). No Python.
- New npm (exact, MIT, published 2025-12-22, no deps): @xterm/xterm 6.0.0, @xterm/addon-fit 0.11.0, addon-unicode11 0.9.0,
  addon-web-links 0.12.0, addon-webgl 0.19.0 — patch INCLUDES pnpm-lock.yaml (pnpm also added an apps/photon-sidecar: {}
  importer entry; integrator re-locks / verifies `pnpm install --frozen-lockfile`).
- New: apps/web/src/chat/{ChatHost.tsx, ptySocket.ts, attachToken.ts, resumeSanitizer.ts, reconnect.ts, terminal.ts,
  ChatRail.tsx, eventsFeed.ts} + tests, src/testing/fakePtyServer.ts, notes docs/notes/P18/chat-{host,rail}.md.
- Shared: apps/web/src/main.tsx (lazy ChatHost into <App chatHost>), package.json, pnpm-lock.yaml, traps R09.1/6/8, ACC-L4-1.
- Decisions (number at integration): 4409 → one muted line + "Use it here", no auto-reclaim; clean 1000 = session ended;
  keep-alive only after 20 s silence; one 8 s budget for ticket + handshake; sanitiser bounded to the 30 s window, strips
  erase codes before folding; feed stops on 4400; rail drives the TUI's slash commands (not the /api/ws sidecar); chat image
  uploads under <files root>/indus-chat-uploads/ (no cleanup yet).
- Follow-ups: TUI must publish to /api/pub (INDUS_TUI_SIDECAR_URL / _CREDENTIAL, internal subprotocol) — until then the rail
  shows the configured model only; a projects + discovered-repos route for the workspace picker; parity items for P18-30.
- Env slip (lead's brief bug: `export S=… HOME=$S/…` in one statement): wrote /w32-p2-home, /uv-cache, /pnpm-store at the
  filesystem root before fixing its env.

## w32-p3.patch: P18-28 login page + auth widget, P18-29 build and serve integration — done
- Builder checks: unit/contract/integration web 615 passed; ruff, pyright 0, lint-imports; apps/web vitest 120 passed (15
  files); pnpm lint / fmt:check / typecheck ok; tooling.checks ok (one similarity hit restructured); `pnpm --filter
  @indus/web build` ok (react 257 kB); a real `indus dashboard --port 0` served apps/web/dist. Lock unchanged.
- New: web/auth/login_page.py (no script, CSP default-src 'none' + style hash, nosniff, COOP, XFO DENY, no-referrer,
  no-store; fixed error codes), web/bundle.py (locate / publish stage-and-swap; `python -m indus.web.bundle publish`),
  apps/web shell/AuthWidget.tsx (+test), lib/auth.ts, vite.dev-bootstrap.ts (+test), vite.chunks.ts (600 kB budget, fails
  only under CI), tests unit/web/auth/test_login_page.py, unit/web/test_bundle_locate.py, notes docs/notes/P18/{login,
  build-serve}.md.
- Shared: web/routes/auth.py (render_login replaced; form-post rate_limited / unavailable redirect to the login page),
  web/app.py, web/command.py, web/spa.py, pyproject.toml (wheel artifacts src/indus/web/static/), .gitignore, apps/web
  lib/api.ts (assignLocation), main.tsx (AuthWidget in sidebarFooter), vite.config.ts, tsconfig.json, test/setup.ts,
  ACC-L1-1, R09.23.
- Decisions: dev backend override INDUS_WEB_DEV_BACKEND; P23 runs `python -m indus.web.bundle publish` after the SPA build
  and before `uv build`; no env override for the bundle dir; checkout dist wins over package static.
- Follow-ups: P18-25's status strip next to the widget in main.tsx (conflict with w32-p1); P23 wheel test that the static
  files are inside; gate could add error=expired for an expired HTML session.
- Note: AF_UNIX path too long when TMPDIR is the long scratchpad path — run web tests with the default tmp dir.

## Owner item: stray paths at the filesystem root
The lead's prompts used `export S=… HOME=$S/…` in one statement (S empty), so agents wrote at `/`: /fix-m2-home,
/w30-a2-home, /w30-r3-home, /w31-d1-home, /w32-p1-home, /w32-p2-home, /w32-t1-home (caches only), /uv-cache (125 MB),
/pnpm-store (110 MB), /x (a file list), /backends_conftest.orig, /dbg_msgraph.py. A safety check blocks the agent from
removing paths at `/`; remove them by hand once no agent is running. Briefs now use two statements.
