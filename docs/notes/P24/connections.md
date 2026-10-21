# Task: Connections registry and remote auth (P24-29) Phase: 24 Owner: wave-39 builder U2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/connection-registry.ts`
(registry shape, labels, normalise/quarantine, remove/primary/last-used, agent handle),
`apps/desktop/electron/connection-config.ts` (header name rules, forbidden header list, base-URL
match only), `apps/desktop/electron/native-oauth.ts` (PKCE, state, authorize/token/refresh URLs,
callback parse, refresh skew). Research 08 §3.10; traps R08.4, R08.5. Our P20 notes
`remote-connection.md`, `local-attach.md`, `rest-proxy.md`, `secrets.md`; LEAD-NOTES P20 wave 34
(the remote WS swap finding) and D-16.691. Closed before writing.
Evidence: none.

What it does (own words): the desktop can know several Indus servers at once. A registry file in
userData lists them: always exactly one "This computer" (local) entry, plus remote entries (a URL
with a token or a native sign-in) and SSH entries (P24-30). One entry is the primary (what a new
window opens); with launch mode "last used" the app reopens the last one that worked instead.
Labels are unique ignoring case; a remote is the same remote when its normalised URL matches, an
SSH entry when `user@host:port` and the remote profile match, so adding one twice updates the
existing entry. An entry the app cannot read is never dropped: it is kept verbatim in
`quarantined[]` and written back on every save, so a hand edit or a newer app's field cannot lose a
connection. Removing the primary makes the local entry primary. Credentials never live in the file:
tokens, OAuth token sets and access-proxy headers go to the secrets store (keychain), keyed by the
entry id.

Remote auth: token mode (the serve's session token) or native sign-in (RFC 8252 + RFC 7636): main
opens a loopback listener on a random 127.0.0.1 port, opens the system browser at the server's
`/auth/native/authorize` with a PKCE S256 challenge and a random state, accepts exactly one callback
whose state matches, exchanges the code at `/auth/native/token`, stores access + refresh token in the
keychain, refreshes 60 s before expiry (one refresh at a time; a rotated refresh token replaces the
old one), and clears the set on a terminal refresh failure (400/401). Re-authentication is asked for
only on an explicit 401 or 403 from the server — a timeout, refused connection or 5xx is
connectivity.

The WebSocket dial (D-16.691, still open on main): Electron 44 ignores a rewritten
`Sec-WebSocket-Protocol`, so main cannot swap a reservation id for the token. We take the
recommended fix: for every dial main asks the server for a single-use ticket
(`POST /api/auth/ws-ticket {purpose: "web-rpc", profile}` with the connection's credential), and the
renderer offers `indus-dashboard-v1` + `indus-dashboard-ticket.<ticket>`. Only the single-use ticket
crosses the bridge (D-16.18–20 hold). The serve side changes so this works in token mode too: the
route mints for a request that passed the loopback token gate (it answered 409 `not_gated` before),
and the upgrade check accepts a ticket in token mode (it accepted only the token before). Main still
holds a reservation for the dial so `Origin` is removed on exactly that upgrade.

Access-proxy headers (Cloudflare Access style): per connection, attached only to requests whose
origin and path prefix match the connection's base URL; `Authorization`, `Cookie`, `Host`, `Origin`,
`Referer` and our `X-Indus-Session-Token` are always dropped, plus framing headers we never let a
user set (`Connection`, `Content-Length`, `Content-Type`, `Transfer-Encoding`, `Upgrade`,
`Sec-WebSocket-*`). Values lose CR/LF.

Connection test (R08.5): both legs with the real credential — HTTP `GET /api/config` and a
WebSocket that mints its own ticket and opens `/api/ws`. HTTP passing with WS failing reports the
connection as failing.

Same-named profiles on two gateways show as `@<profile>-<label-slug>`; a name unique across the
roster keeps its bare name. Cloud connections are not built (D-16.12, N18).

Inputs → outputs: `connections.list` → `{connections: [{id, kind, label, url?, authMode?, hasToken,
signedIn, headerNames[], ssh?, primary, lastUsed}], primaryId, launchMode, quarantined: n}`;
`connections.save({...entry, token?, headers?})` → test result + saved entry id;
`connections.test`, `connections.remove({id})`, `connections.setPrimary({id})`,
`connections.setLaunchMode({mode})`, `connections.signIn({id})` → `{ok}`.
Main: `RegistryStore.load/save/launchTarget()`, `mintRemoteTicket(...)`, `testRemoteConnection(...)`,
`proxyHeadersFor(url, entry)`, `NativeSignIn.start/refresh`.

Files: `apps/desktop/electron/connections/{registry,nativeOauth,proxyHeaders,remoteDial,install}.ts`
+ tests; shared: `electron/backend/remote.ts` (reads the registry), `electron/backend/install.ts`
(per-dial ticket), `electron/backend/tickets.ts` (remote ticket reservation), `electron/api/proxy.ts`
(extra headers on the target), `electron/ipc/{channels,schemas}.ts`, `electron/main.ts` (install step),
`src/global.d.ts` (generated), `src/settings/` (Connections page);
`src/indus/web/routes/ws_ticket.py`, `src/indus/service/ws_auth.py` + their tests.

Edge cases / failure modes:
- registry missing / not JSON / an entry with an unknown kind / a remote without URL → local-only or
  that entry quarantined (cap 20), others load; the quarantine is written back unchanged.
- two `local` entries → the first wins; duplicate ids get fresh ones; duplicate labels get " 2".
- adding a remote whose URL normalises to an existing one updates that entry (id and label kept).
- removing the local entry is refused; removing an unknown id is a no-op.
- OAuth: callback with a mismatched or missing state → rejected, the code never redeemed; an
  `error=` callback → its text shown; listener times out after 5 min; only one callback accepted.
- refresh: two callers at once share one request; 400/401 clears the stored set and marks
  "sign in again"; 5xx/network keeps it.
- ticket mint 401/403 → reauth; 409 `not_gated` (an older serve) → "update the server" message.
- header named `Cookie` or `authorization` in the configured set → dropped (and reported by name).

Our design differences / decisions:
- Our registry fields: `primaryId`, `lastUsedId`, `launchMode`, `quarantined[]` as the row says; no
  cloud kind, no org/name fields; tokens never in the file even encrypted (keychain names
  `connection-<id>-token|oauth|headers`), the reference stores encrypted envelopes in the file.
- The WS credential is a server-minted single-use ticket per dial for token mode and OAuth alike
  (the reference ships the token in the upgrade).
- The migration takes our P20 single remote (`remote-connection.json` + `remote-connection-token`)
  into the registry once; the old file is left in place.

Tests we will write:
- registry: malformed entry survives a save; one local always; removing primary retargets local;
  dedupe by URL and by ssh target + profile; label uniqueness; last-used launch; agent handles.
- proxyHeaders: `Cookie` (any case) dropped; origin/prefix match only; CR/LF stripped.
- nativeOauth: PKCE pair verifies; state mismatch rejected; one callback; refresh single-flight,
  rotation stored, terminal failure clears.
- remoteDial: ticket minted per dial (two dials, two tickets); HTTP ok + WS fail → failing; 401 →
  reauth, 503 → connectivity; no credential in logs.
- Python: token-mode client gets a ticket; a ticket opens a token-mode socket once.

## Decisions for the lead
- Serve change (shared files): `ws_ticket.py` mints in token mode, `ws_auth` accepts a ticket in token
  mode. The token stays accepted in the upgrade (the web SPA uses it). Recorded as the fix for
  D-16.691 — the lead flips that row.
- The server-side RFC 8252 broker (`/auth/native/{authorize,token,refresh}` + `auth_flows:
  ["native_pkce"]` in `/api/status`) does not exist on our serve (D-16.589); the desktop side is built
  against that interface and falls back to token mode when the status does not advertise it.
- Registry file `connections.json` (0600, atomic); launch-mode default "primary".

Compare checklist (filled after the rewrite; reopened `connection-registry.ts`, `connection-config.ts`, `native-oauth.ts`):
- [x] one local entry always, first `local` wins (test: `registry.test.ts::exactly one local entry exists, whatever the file says`)
- [x] malformed entries quarantined verbatim, carried through saves, capped at 20 (test: `registry.test.ts::a malformed entry survives a save verbatim`)
- [x] label unique case-insensitively with a numeric suffix (test: `registry.test.ts::labels stay unique ignoring case`)
- [x] removing the primary retargets local; local cannot be removed (test: `registry.test.ts::removing the primary retargets the local entry`)
- [x] last-used launch mode (test: `registry.test.ts::launch mode last-used reopens the last entry that worked`)
- [x] duplicate profile handle `@<profile>-<slug>` (test: `registry.test.ts::same-named profiles on two gateways get the label suffix`)
- [x] forbidden header list + header-name charset + CR/LF strip (test: `proxyHeaders.test.ts::Cookie in a configured header set is dropped`)
- [x] headers only for matching origin and path prefix, ws↔http mapped (test: `proxyHeaders.test.ts::headers go only to the connection's own gateway`)
- [x] PKCE S256 verifier/challenge (test: `nativeOauth.test.ts::the PKCE pair verifies (RFC 7636 S256)`)
- [x] state mismatch rejected, error callback reported (test: `nativeOauth.test.ts::an OAuth state mismatch is rejected and the code never redeemed`)
- [x] refresh 60 s before expiry; unknown expiry refreshes (test: `nativeOauth.test.ts::refreshes near expiry, single flight, keeps the rotated refresh token`)
- [x] HTTP + WS legs (test: `remoteDial.test.ts::HTTP-only success with a WS failure reports the connection as failing`)
- [x] one ticket per dial (test: `remoteDial.test.ts::each dial mints its own single-use ticket`)
- Deliberate differences: the reference keeps encrypted secret envelopes in the registry file and
  migrates its own v1 file; we keep secrets only in the keychain and migrate our P20 single remote.
  The reference sends the remote token in the WS upgrade; we mint a server ticket per dial.
- Not taken: cloud kind, org/name, pooled per-connection backends in one window (one connection per
  window via the boot orchestrator; switching = setPrimary + reconnect), embedded-webview login.

## Status

Done (2026-10-21) except the server-side native broker (owed, D-16.589).

What D-16.691 now is: built the recommended fix. Serve: `POST /api/auth/ws-ticket` mints for a
token-mode client (the loopback gate already checked its `X-Indus-Session-Token`), and
`authenticate_dashboard_upgrade` accepts a ticket in token mode (single use, purpose- and
profile-bound, as in gated mode; the token itself is still accepted for the web SPA). Desktop: every
remote dial (`connection.ticket`) mints a ticket with main's credential (`mintRemoteTicket`),
reserves the dial (`DialReservations.reserveTicketDial`, which only removes `Origin` on that upgrade
— matched by window + URL inside Electron) and answers `[indus-dashboard-v1,
indus-dashboard-ticket.<t>]`. The old reservation-id swap (`reserveRemote`) is no longer used by
main (kept, marked superseded). Tests: `install.test.ts::a registry remote dials with a
server-minted ticket per dial, never the token (D-16.691)`, `test_ws_auth.py::test_a_token_mode_ticket_opens_one_socket_through_the_app`,
`test_ws_ticket_route.py::test_a_token_mode_client_gets_a_ticket`. Not yet run inside a real Electron against a real serve (the E2E lane's remote spec is
follow-up).

Shared files: `electron/backend/{remote,install,tickets}.ts`, `electron/boot/orchestrator.ts`
(`Connection.rest.headers`), `electron/api/proxy.ts` (`ProxyTarget.headers`), `electron/secrets.ts`
(per-connection secret names, re-written on a store switch), IPC table (8 `connections.*`
channels), `src/settings/{pages.ts,customPages.tsx}` (Gateways › Connections),
`src/indus/{service/ws_auth.py,web/routes/ws_ticket.py}` + their tests. The P20 chooser's single
remote is now the registry's primary entry (`adoptRemote` / `forgetPrimaryRemote`). Integration: the boot
orchestrator reads the registry primary through `RemoteStore.saved()`; P20-40 should add a remote
E2E spec now that the dial no longer needs the header rewrite.
