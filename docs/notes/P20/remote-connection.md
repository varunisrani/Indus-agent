# Task: Remote connection (one saved remote) (P20-11) Phase: 20 Owner: wave-34 builder M

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/connection-config.ts`
(probe/test/apply rules: URL normalisation, auth-mode classification, test WS URL, reauth codes),
`apps/desktop/electron/remote-liveness.ts` (all: constants, tracker, coordinator). Research 08
§3.5, §3.7, §3.10, traps R08.4, R08.5, R08.23. Our `docs/notes/P18/ws-auth.md`, D-16.580 (dashboard
WS credentials ride in `Sec-WebSocket-Protocol`), `web/routes/ws_ticket.py`, the `/api/status`
fields `auth_required` / `auth_providers`. Closed before writing.
Evidence: none.

What it does (own words): first run (or Settings) can point the desktop at an Indus server that
already runs elsewhere: the person enters a URL and a token. The URL is normalised (a scheme-less
`host:port` gets `http://`, only http/https, no user-info, query or fragment, trailing slashes
dropped). The test first reads the server's public `GET /api/status` to learn its auth mode — a
server that wants a sign-in (`auth_required: true`) is reported as needing sign-in, which 1.0 does
not do yet (OAuth/native sign-in is N17). Then both legs must pass with the real credential
(R08.5): an HTTP call (`GET /api/config` with `X-Indus-Session-Token`) and a WebSocket to
`/api/ws` offering `indus-dashboard-v1` + `indus-dashboard-token.<token>`. Only when both pass is
the remote saved: the URL in `remote-connection.json` (userData), the token through the secrets
store (P20-07, keychain by default). A saved remote skips the first-run chooser next launch.

Failures are classified: only a confirmed 401/403 (HTTP) or 4401/4403 (WS close) means
"unauthorized" — re-authenticate; a timeout, refused connection or 5xx is connectivity
(`unreachable` / `error`) and never asks for a new token.

In remote mode the desktop's REST proxy adds the saved token itself (P20-14); for a WebSocket the
window gets a reservation id instead of the token: it offers `indus-dashboard-v1` and
`indus-dashboard-ticket.<id>`, and main replaces that entry with the token subprotocol on that one
upgrade, so the token never crosses the bridge.

Liveness: each probe has 10,000 ms; 3 failures within 60,000 ms mark the route dead (the
orchestrator then reconnects); a success clears the count. After a power resume, revalidation is
held for 15,000 ms and the requests of every window during the hold coalesce into one run per
window. In remote mode tools, terminals and files run on the remote host; client-only files are
shipped as bytes (R08.23, enforced where uploads are built: P20-19, P20-29).

Inputs → outputs: `RemoteStore.get()` → `{url, hasToken}`; `test({url, token})` →
`{ok, http, ws, message?}`; `save(...)` → the same plus `saved`; `forget()`;
`RemoteLiveness.recordFailure/recordSuccess`, `ResumeGate.noteResume()/request(windowId, run)`.

Files: `apps/desktop/electron/backend/remote.ts`, `apps/desktop/electron/backend/remoteLiveness.ts`,
`apps/desktop/electron/backend/remote.test.ts`, `apps/desktop/electron/backend/remoteLiveness.test.ts`.

Edge cases / failure modes:
- HTTP ok + WS closed 4401 → not saved, `ws: "unauthorized"`, message says the token was refused.
- HTTP 503 → `http: "error"`, not unauthorized; no reauth latch.
- server needs sign-in → not saved, message names it.
- URL `ftp://…`, `http://user:pw@host`, empty → `bad-request`.
- the token never appears in a log line, an error message or the descriptor.

Our design differences / decisions:
- One saved remote only (the reference keeps a registry of connections, cloud and SSH: N17).
- Token mode only in 1.0; a gated server is reported, not signed into.
- WS credential by main-side subprotocol rewrite of a reserved dial (no token in the renderer).

Tests we will write:
- `remote.test.ts` (local HTTP + WS test servers): both legs ok → saved, token in the secrets
  store, not in `remote-connection.json`; HTTP ok + WS 4401 → not saved; 503 → error, not
  unauthorized; sign-in server → refused; URL normalisation table; forget clears both.
- `remoteLiveness.test.ts` (fake clock): 3rd failure in 60 s marks dead; failures spread over more
  than 60 s do not; resume holds revalidation 15 s and coalesces per window.

## Decisions for the lead
- Remote HTTP leg uses `GET /api/config` (behind the gate on P18 servers); a 404 also counts as
  authenticated (the gate answers 401 before routing).
- `remote-connection.json` holds the URL only; the token is the `remote-connection-token` secret.

Compare checklist (filled after the rewrite; reopened the two files in `Read:`):
- [x] URL normalisation: bare `host:port`, http/https only, no user-info/query/fragment (test: `remote.test.ts::normalises URLs`)
- [x] both legs with the real credential before saving (tests: `::HTTP ok + WS 4401 → not saved, error shown`, `::both legs pass → saved; …`)
- [x] only 401/403 (4401/4403) is reauth; 5xx and unreachable are connectivity (tests: `::503 is connectivity, not reauth`, `::an unreachable server is unreachable, not unauthorized`)
- [x] token kept out of files, logs and the bridge (test: `::both legs pass → saved; the token goes to the secrets store, not the file`; `tickets.test.ts::a remote dial swaps the reservation id for the token on the wire only`)
- [x] constants 10,000 / 3 / 60,000 / 15,000 (test: `remoteLiveness.test.ts::constants`); 3rd failure in 60 s marks dead (test: `::the 3rd failure within 60 s marks the route dead (fake clock)`)
- [x] resume hold and per-window coalescing (test: `remoteLiveness.test.ts::a resume holds revalidation 15 s and coalesces each window's requests`)
- [x] against a real `indus serve`: both legs pass with the token, both unauthorized without (test: `realBackend.test.ts`)
- Deliberate differences: one saved remote, token mode only; a gated server is reported (no embedded login window, no native OAuth); the WS credential is swapped in by main on the reserved upgrade.
- Not taken: the connection registry, cloud and SSH remotes, access-proxy headers, pooled-secondary windows (N17).

## Status

Done (2026-10-21). Follow-up: the renderer's WS dial in remote mode relies on main rewriting
`Sec-WebSocket-Protocol` in `onBeforeSendHeaders`; P20-40's Electron E2E must cover it against a
real serve (unit-tested only here).

Integration (wave-34 merge, D-16.685, D-16.691): checked in Electron 44.5.1 with a probe page and a
local server — `Origin` can be removed in `onBeforeSendHeaders`, but a rewritten
`Sec-WebSocket-Protocol` is ignored (the server still received the reservation id). The remote WS
swap therefore cannot work inside Electron; remote chat is owed until the owner picks the
replacement (recommended: a single-use ticket the serve mints for a token-mode client, minted by
main per dial).
