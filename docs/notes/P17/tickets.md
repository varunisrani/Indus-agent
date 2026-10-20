# Task: Single-use tickets and the shared WS auth helper (P17-10) Phase: 17 Owner: wave-25 builder K4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/control_socket.py` (the
`session-ticket` branch of the request handler, `_session_ticket`, the peer-subject check),
`gateway/browser_control_broker.py` (ticket TTL and single-use shape only: `mint_ticket`,
`consume_ticket`, `reset`), `gateway/runtime_bootstrap.py` (the ticket store the control socket
mints from: purposes, hashing, cap, redeem, revoke). Research 05 §3.2 mode C step 4, 06 §3.21
(verbs), 08 R08.4, 09 R09.2. Closed before writing.
Evidence: none.

What it does (own words): a client that wants to open the daemon's loopback WebSocket first asks
the owner-only control socket for a ticket. A ticket is a 32-byte random secret, URL-safe base64
(43 characters), that lives 30 seconds, opens exactly one connection and only for the purpose it
was minted for (`ws-interactive` for the CLI/TUI channel, `native-http` for other native
clients). The daemon keeps only a SHA-256 digest of each ticket together with its binding
(purpose, profile id, daemon instance id, expiry), so the secret itself is never stored, logged or
put in a URL. The ticket travels as a WebSocket subprotocol `indus-gateway-ticket.<ticket>` beside
`indus-gateway-v1`; the server echoes only `indus-gateway-v1`.

`authenticate_upgrade(scope, purpose, book=, origins=)` is the one helper every WS endpoint uses
(the gateway endpoint in P17-13 and the P18 web server): it checks that the peer is a loopback
address, that `Origin` is absent or on the allow-list, that exactly one ticket is offered and that
the ticket redeems for this purpose. It answers either "accept, echo `indus-gateway-v1`" with the
grant, or "close with 4401" (bad, used, expired, missing or wrong-purpose ticket) / "close with
4403" (bad Origin or non-loopback peer). A ticket in the query string is never used: it is spent
and the request is logged without the value.

`profile_id` is a stable hash of the profile home (same home → same id across restarts);
`instance_id` is random per daemon start, so a ticket minted by an earlier daemon is useless after
a restart (R08.4).

Inputs → outputs:
- `TicketBook(profile_id=, instance_id=None, monotonic=, ttl=30, limit=1024, purposes=)`:
  `mint(purpose) -> MintedTicket{ticket (hidden from repr), purpose, profile_id, instance_id,
  expires_at, expires_in}`; `redeem(ticket, purpose=, profile_id=None) -> TicketGrant` or
  `TicketRefused{reason}`; `spend(ticket)` (burn without using); `outstanding() -> int`;
  `revoke_all()`; `TicketBook.for_home(home)`.
- `stable_profile_id(home) -> str` (16 hex), `new_instance_id() -> str`.
- `authenticate_upgrade(scope, purpose, *, book, origins=()) -> UpgradeAccepted{subprotocol,
  grant} | UpgradeRefused{close_code, reason}`.
- `offered_ticket(subprotocols)`, constants `SUBPROTOCOL`, `TICKET_PROTOCOL_PREFIX`,
  `CLOSE_BAD_TICKET = 4401`, `CLOSE_FORBIDDEN = 4403`.
- `foundation/redact.py`: a shape pattern masks `indus-gateway-ticket.<t>` and `ticket=` query
  values anywhere they reach a redacting log handler (defence in depth; our code never logs one).
  Lead ruling at integration: both are masked whole (`[redacted]`, no ends kept; D-16.429).

Files: `src/indus/service/ws_auth.py`, `tests/unit/service/test_ws_auth.py`;
shared: `src/indus/foundation/redact.py` (two patterns) and its test file.

Edge cases / failure modes:
- second use within the TTL → `used` (4401); use at exactly `expires_at` → `expired`.
- wrong purpose → refused and the ticket is spent (a ticket shown to the wrong endpoint counts as
  leaked).
- malformed ticket (wrong length, wrong alphabet, not a string) → `malformed` before any hashing.
- two ticket subprotocols in one upgrade → 4401, both spent; no `indus-gateway-v1` offered → 4401
  (the ticket is still spent).
- Origin present and not allow-listed (including `null`) → 4403, ticket spent.
- peer not loopback, or no peer address (Unix socket, test client name) → 4403, ticket spent.
- more than 1,024 live tickets → mint refused (`capacity`); expired entries are pruned on every
  mint/redeem.
- book shared between the control-socket thread pool and the event loop → one lock.
- daemon stop → `revoke_all()`.

Our design differences / decisions:
- Names: `TicketBook`, purposes `ws-interactive` / `native-http`, subprotocols `indus-gateway-v1` /
  `indus-gateway-ticket.<t>`.
- Every failed redemption of a known ticket spends it, and every ticket offered in a refused
  upgrade (bad Origin, non-loopback peer, query string) is spent too; the reference keeps a ticket
  alive after a wrong-purpose attempt.
- The digest lookup is followed by a `hmac.compare_digest` of the stored and computed digests; the
  dict lookup is keyed on SHA-256 of a 256-bit secret, which leaks nothing useful about the ticket.
- Spent tickets leave a digest-only marker until their expiry so a reuse answers `used` (clearer
  diagnostics), never anything that helps an attacker.
- No subject / scope (`host`) fields: one profile per daemon (multiplexing is P24).
- `profile_id` is SHA-256 of the resolved home path (16 hex); the P15 `profile_id_of` (named
  profile label) stays for display.

Tests we will write:
- mint shape (43 chars URL-safe, digest stored, not the ticket), repr hides the ticket.
- reuse within TTL fails `used`; expiry at 30 s with a fake clock (29.9 ok, 30.0 refused).
- wrong purpose refused and spent; unknown purpose refused at mint; capacity cap; revoke.
- a ticket from another book (another daemon start) is unknown.
- upgrade: accepted echoes only `indus-gateway-v1`; reuse → 4401; missing / doubled ticket → 4401;
  bad Origin → 4403; allow-listed Origin accepted; non-loopback peer → 4403; query-string ticket
  ignored, spent and logged without its value.
- the ticket string is absent from captured logs across all of the above.
- stable profile id: same home twice equal, different homes differ.
- redaction pattern masks the subprotocol and the query value.

## Decisions for the lead
- Spend-on-any-attempt (see above). To relax: drop the `spend` calls in `authenticate_upgrade`
  and the burn in `redeem`'s purpose branch.
- Close codes: non-loopback peer answers 4403 (the row names 4403 only for Origin).
- `TicketRefused.reason` codes: `malformed`, `unknown`, `used`, `expired`, `wrong_purpose`,
  `wrong_profile`; P17-25 maps them onto `ticket_invalid` on the wire if a JSON-RPC answer is
  ever needed (today only close codes leave the daemon).

Compare checklist (filled after the rewrite; reopened `gateway/control_socket.py`,
`gateway/browser_control_broker.py`, `gateway/runtime_bootstrap.py`):
- [x] 32 random bytes from `secrets`, URL-safe (test: `test_ws_auth.py::test_minted_ticket_shape_and_hidden_repr`)
- [x] digest-only storage (same test)
- [x] TTL 30 s, expiry boundary (test: `test_ws_auth.py::test_expiry_at_thirty_seconds`)
- [x] single use (test: `test_ws_auth.py::test_reuse_within_ttl_fails`)
- [x] purpose-bound (test: `test_ws_auth.py::test_wrong_purpose_fails_and_spends_the_ticket`)
- [x] capacity cap of 1,024 live tickets (test: `test_ws_auth.py::test_capacity_and_revoke`)
- [x] revoke on stop (same test; the attach verbs call it on stop)
- [x] instance binding: a ticket of another daemon start is useless (test: `test_ws_auth.py::test_a_ticket_from_another_daemon_start_is_unknown`)
- [x] bounded ticket length before hashing (test: `test_ws_auth.py::test_malformed_tickets_are_refused_before_lookup`)
- [x] loopback peer and absent Origin (tests: `test_ws_auth.py::test_upgrade_refuses_bad_origin`, `::test_upgrade_refuses_a_remote_peer`)
- [x] never in URL or log (tests: `test_ws_auth.py::test_query_string_ticket_is_ignored_and_not_logged`, `::test_ticket_absent_from_logs`)
- Deliberate differences: the reference keeps a ticket after a wrong-purpose redeem and has no
  digest compare beyond the dict lookup; we spend it and compare digests in constant time. The
  reference carries a forwarded subject and a `host` scope for multiplexed profiles; we have one
  profile per daemon until P24.
- Not taken: `native-profile-create`, `exposure`, `worker-adoption` purposes (no feature here
  needs them yet; P18/P24 extend `purposes=`).

## Status

Done (2026-10-20). `service/ws_auth.py` with tests; redaction patterns added to
`foundation/redact.py` (shared). P17-13 consumes `authenticate_upgrade`; P18-06 passes its own
purposes to `TicketBook(purposes=)`.
