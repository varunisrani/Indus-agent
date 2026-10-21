# Task: Local daemon attach, serve ensure and tickets (P20-10) Phase: 20 Owner: wave-34 builder M

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/local-gateway.ts`
(all: ensure runner, output parsing, stale restart, dial reservations, control-path checks),
`apps/desktop/docs/canonical-gateway-protocol.md` (all), `apps/desktop/electron/main.ts` (gateway
file save and ticket mint, 7,752–7,950). Research 08 §3.4, traps R08.4; D-16.89. Our contracts:
`docs/notes/P17/ensure.md`, `tickets.md`, `attach-verbs.md`, `src/indus/service/gateway_control.py`
(paths, pointer file, wire), `src/indus/web/serve_runtime.py` + `web/command.py` (`serve ensure`),
`docs/notes/P18/token-gate.md`. Closed before writing.
Evidence: none.

What it does (own words): the desktop never owns the agent. For a local profile main runs
`<indus> [-p P] gateway ensure --json --timeout 60` (60 s + 15 s client grace): the CLI attaches to
or starts the profile daemon (service if installed, else detached; it never installs one) and prints
the credential-free descriptor `{ws_url, profile_id, instance_id, code_version}`. A descriptor whose
`ws_url` is not a plain loopback `ws://127.0.0.1:PORT/…` (no user-info, query or fragment) is
refused. REST goes to the profile's headless `indus serve`: main mints a random 32-byte session
token once per launch, passes it only in `INDUS_DASHBOARD_SESSION_TOKEN` to `<indus> [-p P] serve
ensure --json`, and reads `{url, port, pid, profile_id, code_version}` back. Because the real
`serve ensure` reuses a live serve whatever token it was started with, main checks its token with
one authenticated call; a 401 means a serve from an earlier launch holds another token, so main
stops that pid (SIGTERM, guarded by the lock) and ensures once more. The token never leaves main.

Every WebSocket dial gets a fresh ticket: the window calls `connection.ticket`; main asks the
daemon's owner-only control socket (`<profile home>/gateway.sock`, or the private pointer file's
target when the path is too long) for `session-ticket {purpose: "ws-interactive", profile_id,
instance_id}`, keeps a reservation `{ticket, webContentsId, expires 25,000 ms, origin, pathname}`
and answers `{ticket, wsUrl, expiresInMs, protocols}`. The renderer offers `indus-gateway-v1` and
`indus-gateway-ticket.<ticket>` as subprotocols (the URL carries nothing). Main's
`onBeforeSendHeaders` matches the upgrade by the ticket subprotocol, sender, origin and path, removes
the `Origin` header for that one dial (the daemon refuses browser origins) and consumes the
reservation; any other request is untouched. A ticket mint failing with the stale-socket error
(`stale_socket`), or an ensure answering another code version than the resolved CLI, restarts the
stale local daemon once (`gateway restart`, then `gateway ensure`) and redials; a daemon that went
away or restarted (`stale_instance`, `no_socket`, `unreachable`) is only ensured again (ensure
starts a dead one), so a healthy daemon serving another surface is never bounced. Closing a window or switching only detaches. The desktop creates GUI sessions
with `surface: "gui"` (renderer, P20-17).

Inputs → outputs: `LocalAttach.attach()` → `LocalConnection { descriptor, serve, restHeaders }`;
`TicketBroker.issue(webContentsId)` → `{ticket, wsUrl, expiresInMs, protocols}`;
`DialReservations.reserve(...)`, `.rewrite(details)` → headers or null.

Files: `apps/desktop/electron/backend/localAttach.ts`, `apps/desktop/electron/backend/tickets.ts`,
`apps/desktop/electron/backend/localAttach.test.ts`, `apps/desktop/electron/backend/tickets.test.ts`,
`apps/desktop/electron/backend/control.ts` (control-socket client; new).

Edge cases / failure modes:
- ensure exits 1 with `{state, reason, detail}` → error naming the reason; empty or non-JSON stdout
  → "the CLI printed no result" with the stderr tail (redacted).
- ensure hangs → killed at 75 s.
- two dials → two tickets; a reservation is single use and expires at 25 s (fake clock); more than
  100 pending reservations → refused.
- an upgrade for another window, another origin/path, or without the reserved ticket → headers
  untouched (Origin stays, the daemon refuses it).
- control socket: home writable by others → refused before connecting; pointer file not ours or
  with group/other bits, or its directory not 0700 → refused.
- stale-socket error → one restart, then the mint is retried; a second failure is reported.
- remote mode never calls any of this (P20-12 keeps the routes apart).

Our design differences / decisions:
- No per-request HTTP grant to the daemon (D-16.89): REST goes to `indus serve` with the launch
  token.
- The reservation is keyed by the ticket subprotocol, not by a nonce query parameter (nothing is
  added to the URL).
- `connection.ticket` answers `protocols` (the exact list to offer) beside `ticket` so the remote
  mode (P20-11) can use the same call.

Tests we will write:
- `localAttach.test.ts` (fake backend): ensure → descriptor; serve ensure with the token in env
  only; a serve from an earlier launch (other token) is replaced; ensure failure names the reason;
  stale-socket error → one restart then success; version skew → one restart.
- `tickets.test.ts`: two dials → two tickets; the fake rejects a reused ticket; reservation
  expires at 25 s; Origin stripped only for the reserved URL; cap.
- one integration test against a real `indus` daemon + `indus serve` in a temp home (python via
  `uv run`), skipped when the toolchain is missing.

## Decisions for the lead
- Serve token: one random token per app launch held in main; a serve started by an earlier launch
  (401 with our token) is stopped by pid and re-ensured once.
- Stale handling: `stale_socket` → restart once; `stale_instance` / `no_socket` / `unreachable` →
  ensure again (no restart); then one more mint, whose answer is final.
- `protocols` added to the `indus:connection:ticket` answer.

Compare checklist (filled after the rewrite; reopened the three files in `Read:`):
- [x] ensure deadline 60,000 + 15,000 ms (test: `localAttach.test.ts::the ensure deadline is 60 s plus the 15 s client grace`)
- [x] output with no JSON diagnosed with the stderr tail (test: `::a CLI that prints no JSON is diagnosed with its stderr`); a verdict names its reason (test: `::ensure failure names the verdict's reason`)
- [x] stale restart once — stale ticket path (test: `::stale-socket on a ticket mint: one restart, then the mint succeeds`), version skew (test: `::version skew: the stale daemon is restarted once, then the descriptor matches`); a gone daemon is re-ensured, not restarted (test: `::a gone daemon is re-ensured on the next mint (not stopped)`)
- [x] two dials → two tickets, reuse refused by the daemon (test: `tickets.test.ts::two dials get two distinct tickets; the fake refuses a reused one`)
- [x] reservation 25 s, single use, Origin removed only for that dial (tests: `tickets.test.ts::strips Origin only for the reserved dial, once`, `::a reservation expires at 25 s (fake clock)`, `::at most 100 dials wait at once`)
- [x] control path safety: unsafe home, pointer ownership/mode, private socket dir (test: `tickets.test.ts::control socket path checks: unsafe home, pointer ownership and mode`)
- [x] serve token only in the environment; a serve with another token replaced once (tests: `localAttach.test.ts::serve ensure: the token goes in the environment only and opens the REST API`, `::a serve from an earlier launch holding another token is replaced once`)
- [x] real runtime: ensure, serve ensure, pointer-file control socket, ticket opens the WS once (test: `realBackend.test.ts::resolve → ensure → ticket → dial once → REST → remote test`)
- Deliberate differences: restart is `indus gateway restart` then `gateway ensure` (the CLI handles service vs detached); the reservation is keyed by the ticket subprotocol, not a nonce query parameter; no per-request HTTP grant (D-16.89); no multiplexed-home routing (P24).
- Not taken: the listener-uid check on the control socket (Node cannot read SO_PEERCRED; the 0600 socket and private pointer directory remain; follow-up shared with P17's own note), plugin WebSocket routes (P24).

## Status

Done (2026-10-21). Shared: `testing/fakeBackend/server.mjs` (scripted mint failures counted per
home, so a restart does not re-arm them), `testing/fakeBackend/ensure.mjs` (`gateway restart`).

Integration (wave-34 merge, D-16.684): inside Electron 44.5.1 `onBeforeSendHeaders` does not show a
WebSocket upgrade's `Sec-WebSocket-Protocol` (headers seen: User-Agent, Upgrade, Connection, Origin,
Sec-WebSocket-Version, Accept-*), so the marker lookup never matched and the daemon refused the
desktop's `file://` Origin (4403). `DialReservations.rewrite` now matches a local dial by window and
exact URL when the header is absent (oldest live reservation, single use); test
`tickets.test.ts::inside Electron (no subprotocol header) a local dial is matched by window and exact URL`.
