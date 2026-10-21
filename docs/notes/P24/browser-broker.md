# Task: browser-control broker (P24-47) Phase: 24 Owner: wave-39 builder B4 (design by wave-38 W1)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): research 06 §3.24 (behaviour list);
`gateway/browser_control_broker.py` (ticket and selection rules: ticket mint/consume, identity
fields, capability allow-lists, developer gate, select, complete-once, detach vs disconnect,
deferred cancel bound) — read again by B4 (wave 39) before writing. Closed before writing;
reopened for Compare. Evidence: none.

What it does (own words): an external browser controller (for example a browser extension driving
the user's own browser) registers with the gateway's API server and is then reachable by callers
on any transport under exactly one identity. Registration (`POST /v1/browser-control/register`,
behind the API key) returns a ticket minted through `service/ws_auth.py` for the
`browser-controller` purpose: 30 s, single use, bound to the full identity (the broker keeps the
ticket's SHA-256 digest → identity). The controller opens a
WebSocket offering `indus-browser-control-v1` and `indus-browser-control-ticket.<ticket>`; a
ticket anywhere else (the query) is spent and refused; the server echoes only the base protocol.
The first frame is a hello with protocol version 1 and its capabilities (within 10 s); the
broker keeps the allowed subset: no-op, back, click, navigate, press, screenshot, scroll, snapshot, tab activate,
tabs, type, plus artefact download/upload (ids only); raw CDP and evaluate only when developer mode
is on at the time of the call and the controller negotiated them.

A call names the caller's identity (transport family, principal, browser profile, controller id,
session, profile) and a capability. It goes only to a connected controller whose identity equals
the caller's on every attribute and whose negotiated set holds the capability; anything else is
refused, never re-routed. Each command times out after 30 s and completes at most once (a second
answer is ignored). Detaching the controller fails its outstanding commands; a dropped socket only
marks it offline (commands wait for a reconnect of the same identity or time out). A cancel for an
offline controller is kept for its reconnect, at most 512.

Artefacts move only by server-minted 32-hex ids: 300 s lifetime, 10 MiB cap, MIME allow-list
(PNG, JPEG, WebP, GIF, PDF, plain text, JSON, CSV), written to a temp file then renamed, SHA-256
checked again on read, and consumed once, atomically, only with the exact scope key it was
stored under.

Inputs → outputs: register body `{controller_id, browser_profile, session, protocol: 1}` →
`{ticket, expires_in, subprotocol, ticket_prefix, protocol, ws_path}` (`Cache-Control: no-store`);
WebSocket frames `browser.controller.hello | result | detach | artifact.put (+ one binary frame) |
artifact.fetch` in, `browser.controller.ready | command | cancel | artifact.stored |
artifact.data (+ bytes) | artifact.refused` out;
`BrowserBroker.call(identity, capability, arguments)` → the controller's result or a typed error.

Files: `src/indus/gateway/browser_broker/{__init__,broker,artifacts,routes}.py` (`routes` is new:
the register route and the controller socket), tests
`tests/unit/gateway/browser_broker/test_broker.py`, `test_artifacts.py`, `test_routes.py`; shared:
`service/ws_auth.py` (`BROWSER_CONTROLLER` purpose in `DEFAULT_PURPOSES`),
`authenticate_upgrade` / `offered_tickets` take the endpoint's subprotocol and ticket prefix
(defaults unchanged), `gateway/http/api_server.py` (`ApiContext.browser_broker`; routes mounted
and WebSockets enabled in uvicorn only when set; the broker closed with the server), schema rows
`browser.control_enabled`, `browser.control_developer_mode` (W1, read by
`browser_broker.control_enabled` / `developer_mode`).

Edge cases / failure modes:
- ticket reused, expired (> 30 s on the book's clock), unknown, for another purpose → 4401 close;
- hello with protocol ≠ 1 (a bool is not an int) → 4400 close; non-JSON or oversized frame → 1003/1009;
- a caller differing in one attribute → `ControllerUnavailable`; developer capability outside
  developer mode → refused even if negotiated; developer mode switched off later → refused at once;
- a command answered twice keeps the first; an answer from another identity is ignored;
- artefact over 10 MiB, MIME not allowed, bytes changed on disk, expired, wrong scope key, consumed
  twice → refused; the temp file never stays behind.

Our design differences / decisions:
- asyncio instead of threads; the ticket store is the shared `TicketBook` (the broker keeps only a
  digest → identity map, never the ticket).
- The register route sets `principal` from the API key id and `transport_family` to `api`; the
  caller-side transports (chat, CLI) build their identities the same way — owed at wiring.

Tests we will write:
- ticket reuse and expiry; WebSocket upgrade with/without the ticket subprotocol and with a query
  ticket; identity mismatch per attribute; complete-once; detach vs disconnect; evaluate outside
  developer mode; deferred cancel bound; artefact SHA-256, size, MIME, TTL, scope, single consume.

## Decisions for the lead
- Feature off by default (`browser.control_enabled: false`); the API server mounts the routes only
  when the daemon passes a broker. WIRING OWED (daemon): when `control_enabled(scope)`, build
  `BrowserBroker(book, clock=scope.clock, developer_mode=lambda: developer_mode(scope),
  enabled=lambda: control_enabled(scope), artifacts=ArtifactStore(<home>/cache/browser-control))`
  over the daemon's `TicketBook` (it already mints `browser-controller` via `DEFAULT_PURPOSES`)
  and set `ApiContext.browser_broker`. Callers (browser tools, the `broker` handler extra W1's
  vault tool takes) find their controller with `broker.lane(session=…, principal=…,
  transport_family="api")` and call `broker.call(identity, capability, args, call_id=…)`.
- Loopback only: the register route answers only a loopback peer (403) and the socket upgrade
  goes through the one helper, which refuses a non-loopback peer (4403) — the controller runs on
  the user's machine. An Origin must be on `gateway.api_server.cors_origins` (a browser
  extension's origin has to be listed there, as for its HTTP calls).
- Capability names are ours (`navigate`, `tab_activate`, `artifact_upload`, `cdp`, `evaluate`, …).
- A cancel for an offline controller (timeout or explicit) is kept for its reconnect (≤ 512,
  newest kept); a reconnect that cannot flush them closes 1013 and keeps the rest.
- Artefact bytes move over the controller socket (`artifact.put` + one binary frame,
  `artifact.fetch` → one binary frame); commands carry only the id. One store per daemon, scoped
  by the identity's `scope_key` (SHA-256 of the six attributes).

Compare checklist (reopened `gateway/browser_control_broker.py` and research 06 §3.24):
- [x] ticket mint / consume: single use, TTL 30 s, identity-bound; unknown / used / expired
  refused (we refuse at exactly 30 s; the reference only after).
- [x] select: exact match on all six identity fields + the controller's current negotiated set;
  developer capabilities gated on live developer mode at every call.
- [x] attach: same-identity reconnect keeps pending work and refreshes send + capabilities; a
  different controller in the same lane (principal, profile, session, transport) hard-replaces;
  deferred cancels flushed first; a failed flush keeps the rest (added after Compare).
- [x] disconnect only by the owning socket, marks offline; detach fails pending (cancelled) and
  sends cancel frames.
- [x] dispatch: revalidated under the controller's send lock; send failure unreserves the id;
  timeout 30 s → cancel sent, or deferred when offline (≤ 512).
- [x] complete once, only from the same identity; `ok` must be literally true.
- [x] artefact actions need a store-approved id; only the id travels.
- [x] protocol version exactly int 1 (bool refused).
- Deliberate differences: asyncio instead of threads + RLock; tickets in the shared
  `TicketBook`; explicit cancel also works while the controller is offline (kept for reconnect);
  one artefact store per daemon instead of per profile slot.
- Not taken: `disconnect_owner` (one owner per socket here), `lane_registered` (an offline
  controller stays in the table, so `lane` already answers "bound but unavailable").

## Status

Done (2026-10-21, wave 39 B4): broker, artefact store, register route + controller socket,
API-server mount, ws_auth purpose; tests `tests/unit/gateway/browser_broker/` (broker, artifacts,
routes). Daemon construction and caller-side tool routing owed (see Decisions).

## Integration (P24 integrator, 2026-10-21)

Wired: the daemon's API server builds the broker over the daemon's ticket book only when
`browser.control_enabled` (`gateway/http/__init__.py::_browser_broker`, artefacts in
`<home>/cache/browser-control`). Owed: callers (`broker.lane` + `call`) from the browser tools and
the vault's `broker` extra.
