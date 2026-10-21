# Task: E2E and the M6 demo (P18-30) Phase: 18 Owner: integrator (wave 32, P18c)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `web/src/pages/ChatPage.test.tsx` (test names
only), `web/src/lib/dashboard-auth-reload.test.ts` and `web/src/lib/pty-reconnect.test.ts` (test names only, as
the row says). Our side: `docs/plan/phases/P18-web-dashboard.md` (row P18-30, "Done when", M6), the P17-27 demo
`tests/e2e/service/test_shared_backend.py`, `tests/fakes/daemon.py`, `tests/fakes/model/http_serve.py`,
`src/indus/web/{command,spa,app}.py`, `src/indus/web/ws/{chat_routes,pty_route}.py`, `src/indus/cli/tui_launch.py`,
`apps/web/src/chat/*`, `apps/web/src/lib/{api,wsUrl,auth}.ts`, `docs/notes/P18/{chat-host,chat-rail,login,
build-serve}.md`, the three P18 landing sections in LEAD-NOTES.
Evidence: none (no capture of the running reference; the suite runs our own product only).

What it does (own words): the first run of the whole dashboard as a person meets it — a real browser
(Playwright, Chromium) against a real `indus dashboard` and a real `indus gateway run` daemon in a temp
home whose model is the scripted fake on a loopback port. It proves the pieces built against mocks
(pages, xterm host, login) work against the real server, and it runs the browser half of the M6
demo: the Chat tab is the real TUI attached to the same daemon session as a classic CLI, and each
sees the other's turns live; an approval raised by a turn is answered from the browser.

Inputs → outputs: a Python stack harness (`tests/fakes/web_stack.py`, JSON lines over stdin/stdout:
start model + profile + daemon + dashboard, restart the dashboard, run the CLI once or attached in a
pseudo-terminal, read the store, stop everything) driven by Playwright fixtures (`apps/web/e2e/
fixtures.ts`); the specs (`dashboard.spec.ts`, `chat.spec.ts`, `auth.spec.ts`) → pass/fail, a trace
on failure, and a request log in which no URL may carry the token or a ticket. Beside the browser
suite, `tests/integration/web/test_dashboard_daemon.py` runs the multi-surface check without a
browser (the `/api/pty` socket dialled the way the SPA dials it), so the Python gate covers it.

Files: `apps/web/e2e/{fixtures.ts,dashboard.spec.ts,chat.spec.ts,auth.spec.ts}`,
`apps/web/playwright.config.ts`, `apps/web/tsconfig.e2e.json`, `tests/fakes/web_stack.py`, `tests/integration/web/test_dashboard_daemon.py`;
shared: `apps/web/package.json` (`@playwright/test`, `e2e` script), `apps/web/vitest.config.ts` (exclude
`e2e/`), `apps/web/tsconfig.json`, ledgers.

Edge cases / failure modes:
- the bundle or the TUI build missing → the suite builds them first (`e2e` script builds the SPA;
  the fixtures build the TUI dist when absent), never silently passes;
- no browser for this Playwright version → the run fails naming `PLAYWRIGHT_BROWSERS_PATH`
  (we never download one here);
- leftover processes: the harness stops every process naming the temp home (`ProfileHome.stop_all`) and
  reports strays; a spec fails when any is left;
- timing: every wait polls a condition with a bound (no fixed sleeps as assertions);
- credentials in URLs: every request the browser makes (HTTP and WebSocket) is logged; none may name the
  session token, a ticket, `token=` or `ticket=`;
- the gated run binds `127.0.0.2` (outside the loopback set the gate trusts, still on `lo`), so nothing is
  exposed beyond the machine.

Our design differences / decisions:
- Playwright pinned at the version whose Chromium revision is preinstalled here (1.56.1 → chromium-1194);
  the plan's reference pin 1.63.0 needs a browser this machine does not have (decision for the lead).
- the CLI side of the multi-surface demo is driven from Python (pexpect) through the harness, as in
  P17-27, instead of node-pty (its native build needs node-gyp, missing here).
- the browser suite is an opt-in lane (`pnpm --filter @indus/web e2e`), like the TUI's Demo 2 lane; the
  Python integration test runs in the normal gate.

Tests we will write:
- auth: loopback token flow (token only in a header; JSON calls succeed), stale token after a dashboard
  restart reloads once and works; gated bind without a provider refuses to start; gated with basic auth:
  login page, wrong password, login, loopback token rejected, WS ticket single use, logout.
- dashboard: Sessions search + delete; Config save round trip.
- chat: prompt typed in xterm streams; approval card answered in the terminal; Logs and back keeps the
  same PTY (no new socket); reload reattaches (same session, transcript redrawn); killing the PTY child
  shows "Start new session"; multi-surface with a CLI attached to the same session (both directions where
  the protocol allows), an approval raised by a CLI prompt answered from the dashboard; one user row.
- Python: the same multi-surface check over `/api/pty` without a browser.

## Decisions for the lead
- Playwright pin 1.56.1 instead of the table's reference 1.63.0 (D-16.658): the only browser here is Chromium
  revision 1194, which 1.56.1 drives; CI installs the same revision. Moving up means a newer preinstalled browser.
- The browser suite is an opt-in local lane (`pnpm web:e2e`) plus a CI job `web-e2e`, added to the gate's `needs`
  (R10.18: the gate needs every job). Not run in CI from here.
- Fixes in other tasks' files, found by this run (D-16.653, D-16.656, D-16.657): the resume sanitiser (P18-26),
  the login page's referrer policy (P18-28), the TUI input filter (P12).

Compare checklist (filled after the rewrite; reopened the test names of `web/src/pages/ChatPage.test.tsx`,
`web/src/lib/dashboard-auth-reload.test.ts`, `web/src/lib/pty-reconnect.test.ts`, names only):
- [x] stale token → one reload, latched, cleared on success (`auth.spec.ts::a stale token after a dashboard restart
  reloads the page once and goes on`; the WS 4401 half stays in `ptySocket.test.ts`)
- [x] an ended agent process offers "Start new session" (`chat.spec.ts::chat: …` kills the PTY child)
- [x] the reconnect ladder, hidden-tab parking, keep-alive, WebGL gating: unit level (`ptySocket.test.ts`,
  `terminal.ts`); in the browser run headless Chromium has a software GL, so the DOM renderer is used (the suite
  reads it)
- [x] the Chat tab keeps its socket across pages and reattaches after a reload (`chat.spec.ts::chat: …`)
- Deliberate differences: the reference's suite is unit-level around the chat page; ours adds a real dashboard,
  daemon, TUI child and CLI (M6).
- Not taken: a 1012 service-restart redial against a real server (a dashboard restart rotates the token and ends
  its PTY children, so the browser reloads instead; the 1012 path stays covered by `ptySocket.test.ts`).

## M6 demo (browser half) — what holds, what not yet

Holds (`chat.spec.ts`, `test_dashboard_daemon.py`, `auth.spec.ts`): the dashboard's Chat tab (the real TUI) and a
classic CLI attached to one daemon session; a prompt typed in the browser is answered there and its reply streams
live in the CLI; a prompt typed in the CLI streams in the browser, the approval it raises is answered from the
dashboard and the CLI's card closes ("question answered in another window"); one user row per prompt, all in
the one session; the Chat tab survives a switch to Logs (same socket, same PTY child) and a full reload (same
PTY child through the attach token, transcript redrawn); a gated bind without a provider refuses to start; with
username/password the loopback token opens nothing (header 401, WS 4401) and login works; no token or ticket in
any URL the browser asked for.

Not yet: the person's text typed on one surface is not shown on the other, only the turn (no event carries it,
P17 follow-up); a resumed session opens on an empty transcript (`session.resume` over the daemon returns no
stored messages, P17 follow-up); the rail's live feed (the TUI does not publish to `/api/pub` yet); CLI, a
standalone TUI and the browser at once in one test (P17-27 runs CLI + TUI; this suite runs CLI + browser TUI);
on a resumed chat the TUI's OSC 11 probe runs while the PTY is still cooked, so xterm's answer is echoed once
at the top of the screen (TUI owner; harmless since D-16.657 drops it from the input).

## Status

Done (2026-10-21). 9 browser specs green (`pnpm web:e2e`), `tests/integration/web/test_dashboard_daemon.py` green.
Shared edits: `apps/web/{package.json,vitest.config.ts}`, `package.json` (`web:e2e`), `pnpm-lock.yaml`,
`.github/workflows/ci.yml` (`web-e2e`), `.gitignore`, `tests/fakes/web_stack.py` (new harness), the three fixes
above, ledgers (R09.1/2/3/5/6/8, ACC-L1-1/L3-1/L4-1/L5-1/L6-1).
