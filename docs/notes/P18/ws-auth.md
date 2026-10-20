# Task: one WebSocket auth helper and tickets (P18-06) Phase: 18 Owner: builder W1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/dashboard_auth/ws_tickets.py`,
`hermes_cli/web_routers/chat_ws.py` (pre-accept checks).
Integrator Compare also read: `hermes_cli/web_server_chat.py` (WS auth and host/origin/peer reasons), `hermes_cli/web_server_boundary.py`.
Evidence: none.

What it does (own words): every dashboard WebSocket (PTY, events, RPC sidecar, publisher, console
later, plugin sockets) decides an upgrade through one helper. `service/ws_auth.py` (P17-10) gains the
dashboard purposes (`web-pty`, `web-events`, `web-rpc`, `web-pub`, `web-console`), per-ticket profile
binding, a process-lifetime internal credential for server-spawned clients, the 123-byte close-reason
clamp and `authenticate_dashboard_upgrade`, which checks in a fixed order: feature enabled (else
4404), credential (else 4401), Host/Origin (else 4403), peer allowed (else 4408). Credentials:
loopback mode → `?token=<session token>`; gated mode → `?ticket=<t>` minted by a cookie-authenticated
`POST /api/auth/ws-ticket {purpose, profile?}`, single use, 30 s, bound to purpose and profile; the
loopback token is refused in gated mode. The internal credential (never sent to a browser) is
accepted only by routes that allow it. `web/ws/__init__.py` holds `ws_accept(websocket, purpose)`
and the `guarded_ws` decorator every WS route uses; a refused upgrade is accepted and closed at once
with the code so a browser can read it, and nothing else happens on that socket.

Inputs → outputs: ASGI scope (query, headers, peer) + purpose + dashboard state → accepted grant
(purpose, profile id, credential kind) or a close code with a clamped reason; the ticket route
answers `{ticket, expires_in}` with `Cache-Control: no-store` and an audit line (no value).

Files: `src/indus/web/ws/__init__.py`, `src/indus/web/routes/ws_ticket.py`,
`tests/unit/web/ws/test_ws_auth.py`, `tests/unit/web/test_ws_ticket_route.py`; shared:
`src/indus/service/ws_auth.py` (+ its unit test).

Edge cases / failure modes:
- ticket reused → 4401; at 30.001 s → 4401 (injected clock); wrong purpose → 4401; wrong profile →
  4401; two tickets/tokens offered → 4401.
- origin `http://evil` → 4403 with a reason ≤ 123 bytes (UTF-8 safe cut); Host mismatch → 4403.
- loopback bind: a non-loopback peer → 4408; gated mode: any peer (the gate is the credential).
- feature disabled → 4404 before anything is read.
- a failed upgrade spends the ticket it carried.

Our design differences / decisions:
- Tickets come from the same `TicketBook` type as the daemon's (one book per dashboard process,
  purposes limited to the dashboard's), not a second store; `mint` takes an optional profile id.
- Accept-then-close for refusals (as the daemon endpoint does) so the close code reaches browsers.
- The credential rode in the query as the row said; at integration the lead moved it to the
  subprotocol list (`indus-dashboard-v1` + `indus-dashboard-<kind>.<value>`, D-16.580) and a query
  credential is refused. The server never logs URLs (access log off) either way.

Tests we will write:
- order of checks; each code; clamp; ticket reuse/expiry/purpose/profile; loopback token refused in
  gated mode; internal credential only where allowed.
- an enumeration test: every WebSocket route in the app is wrapped by `guarded_ws` and refuses an
  upgrade without a credential with 4401.

## Decisions for the lead
- `web-pub` purpose added for the publisher route (P18-09) — internal credential only.
- Ticket minting is refused (409 `not_gated`) in loopback mode: the SPA uses the token there.

Compare checklist (filled after the rewrite):
- [x] single-use 30 s tickets, constant-time compare, no value in logs
- [x] internal credential: process lifetime, multi-use, never in HTML/REST
- [x] pre-accept order 4404 → 4401 → 4403 → 4408 and the reason clamp

Compare (integrator, 2026-10-20; reference reopened read-only, then closed): `hermes_cli/dashboard_auth/ws_tickets.py`, `hermes_cli/web_routers/chat_ws.py` (`_ws_gate`, `_close_unless_sidecar_allowed`), `hermes_cli/web_server_chat.py` (`_ws_auth_reason`, host/origin/peer reasons), `hermes_cli/web_server_boundary.py` (ticket subprotocol, `ws_client_reason`, `ws_host_origin_reason`).
- [x] 30 s single-use tickets, 32 random bytes, constant-time compare, nothing logged in full
- [x] internal credential: process lifetime, multi-use, only via the child's env
- [x] order 4404 → 4401 → 4403 → 4408 and the 123-byte clamp; ours uses it for the sidecar routes too (D-16.581)
- [x] credentials now in subprotocols only, query credentials refused (lead decision 2, D-16.580)
- [x] non-web origins only on a loopback bind; refusal accepted-then-closed; tickets bound to purpose + profile, no user identity (D-16.591)
- [x] the grant's profile is the route's profile; an unknown named profile reaches the route (D-16.582)

## Status

Done (2026-10-20), builder W1. Compare: done by the P18 integrator with the reference reopened (see the integrator Compare above).
