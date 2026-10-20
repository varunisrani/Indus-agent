# Task: Control socket attach verbs (P17-11) Phase: 17 Owner: wave-25 builder K4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/control_socket.py` (verb
table, `handle_request_line`, `_session_ticket`, `_posix_peer_subject`). Research 06 §3.21
(control socket wire and verbs), 05 §3.2 mode C steps 1–5 (`runtime.describe`, grant). Closed
before writing.
Evidence: none.

What it does (own words): the daemon's control socket (P15-27) learns two verbs a client needs to
attach to the daemon:

- `session-ticket {purpose, profile_id?, instance_id?}` mints one single-use WebSocket ticket
  (P17-10) and answers `{ticket, ws_url, profile_id, instance_id, expires_in}`. Only a peer the OS
  confirmed as this user gets one; where the OS cannot tell, the answer is `forbidden`. The
  daemon also refuses when its home has become writable by others (`unsafe_home`), when the
  caller expects another profile (`profile_mismatch`) or another daemon start (`stale_instance`),
  for an unknown purpose (`bad_purpose`), unknown params (`bad_params`), when the WebSocket
  endpoint is not up (`ws_unavailable`) or when too many tickets are waiting (`capacity`).
- `describe` answers `{profile_id, instance_id, code_version, protocol_version, ws_url,
  creatable_sources[], capabilities[]}`: what a client checks before it dials. It only copies
  values the daemon holds, so it answers at once even while turns run.

Both use the P15 one-exchange wire and limits unchanged. The client half lives in
`service/gateway_control.py` (`session_ticket`, `describe`), so the CLI, the TUI grant helper and
the Python attach channel can call it without importing the gateway; like every client call it
refuses a home other people can write to before connecting.

Inputs → outputs: control request lines → answer lines; `AttachVerbs(home, book, host=, ws_url=)`
→ `handlers()` (`{verb: (handler, seconds)}`, the shape `DaemonPorts.control_handlers` returns)
or `install(control)`; `close()` revokes every ticket.

Files: `src/indus/gateway/daemon/attach_verbs.py`, `tests/unit/gateway/daemon/test_attach_verbs.py`
(the stand-in host was removed at integration: the tests use `DaemonSessionHost`); shared: `src/indus/gateway/control_socket.py`
(owner-only verbs, `VerbRefused`, the two names in `KNOWN_VERBS`, public `code_version`),
`src/indus/service/gateway_control.py` (client helpers).

Edge cases / failure modes:
- peer uid unknown (an OS without peer credentials, or a test double) → `forbidden` for
  `session-ticket`, other verbs unchanged.
- another user's peer → `forbidden` for every verb (P15 rule, unchanged).
- home turned group/world-writable after start → the server refuses to mint and the client
  refuses to ask.
- WebSocket endpoint not mounted yet (P17-13) → `ws_unavailable`, no ticket minted.
- a reply with a ticket of the wrong shape, a non-loopback or credential-carrying `ws_url`, or
  other ids than expected → the client answers `bad_response` / `profile_mismatch` /
  `stale_instance` and drops the ticket.
- the ticket never reaches a log line, an exception text or a `repr` on either side.

Our design differences / decisions:
- The reference requires the caller to name `profile_id` and `instance_id`; we make both optional
  (`describe` first is the normal flow, then the client passes them and checks the answer).
- No forwarded `subject` and no `scope: host` (multiplexed profiles are P24).
- The owner-only rule is enforced by the control server for the verb name, so a registration
  path that forgets the flag still cannot hand tickets to an unverified peer.
- The server also refuses to mint from an unsafe home; the reference checks a symlinked home
  too, we do not (a symlinked `~/.indus` is common and the target directory is what we check).

Tests we will write:
- round trip over a real socket: describe, then session-ticket with the described ids, then the
  ticket opens one upgrade (P17-10 helper) and only one.
- describe answers while a turn runs (a real host turn parked in a tool; stand-in at build time).
- group-writable home refused by the client (and by the server).
- unverified peer → forbidden; other verbs still answer.
- each refusal code; ws endpoint missing; capacity.
- the ticket is absent from logs and from `repr` of the client reply.

## Decisions for the lead
- `describe` also carries `protocol_version` (indus.protocol `PROTOCOL_VERSION`) so a client can
  refuse an incompatible daemon before dialling; drop it if P17-25 puts it in `gateway.ready` only.
- Refusal codes are plain strings on the control wire (`forbidden`, `unsafe_home`, `bad_params`,
  `bad_purpose`, `profile_mismatch`, `stale_instance`, `ws_unavailable`, `capacity`); P17-25 owns
  the JSON-RPC codes (`ticket_invalid`, `profile_mismatch`).

Compare checklist (filled after the rewrite; reopened `gateway/control_socket.py`):
- [x] ticket only for a verified same-user peer (test: `test_attach_verbs.py::test_an_unverified_peer_gets_no_ticket`)
- [x] envelope keys and protocol checked (P15 `_parse`, unchanged; test: `test_attach_verbs.py::test_refusals`)
- [x] unknown params refused (test: `test_attach_verbs.py::test_refusals`)
- [x] stale instance refused (same test)
- [x] 30 s expiry reported (`expires_in`; test: `test_attach_verbs.py::test_round_trip_describe_ticket_upgrade`)
- [x] tickets revoked on stop (test: `test_attach_verbs.py::test_close_revokes_tickets`)
- [x] the client refuses an unsafe home (test: `test_attach_verbs.py::test_group_writable_home_refused_by_the_client`)
- Deliberate differences: optional ids, no forwarded subject, no symlink rule (above).
- Not taken: Windows named pipe transport (D-16.55: the control socket is POSIX only).

## Status

Done (2026-10-20). Verbs, client helpers and tests landed. Integration (P17 wave 2): the verbs
are registered by the daemon's control part (`DaemonBridge.attach_verbs`, a `TicketBook` per
daemon start, revoked at stop; `tests/integration/gateway/test_control_part.py`). Still owed to
P17-13: the `ws_url` of the mounted endpoint and the daemon's `DaemonSessionHost` as `host=`
(until then `session-ticket` answers `ws_unavailable` and `describe` lists no sources).
