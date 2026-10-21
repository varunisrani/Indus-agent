# Task: Relay connector transport (P24-08) Phase: 24 Owner: wave-37 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/relay/ws_transport.py`
(framing, dial URL, handshake, outbound request/answer, read loop, reconnect supervisor, 4401
handling, going-idle / dormant, inbound acks), `gateway/relay/descriptor.py` (whole),
`gateway/relay/auth.py` (whole), `gateway/relay/transport.py` (the protocol surface, skimmed),
`website/docs/developer-guide/relay-connector-contract.md` (behaviour only: §1 handshake, §2
descriptor, §3 inbound transport, §3.2 going-idle, §4 op table, §5 interrupts, §6.1 channel auth).
Research 06 §3.25 (relay), §3.22 (scale-to-zero quiesces the relay), §3.17 (transport resolution).
Evidence: none.

Protocol pin: our own contract, `contract_version` 1 (D-16.1: not wire-compatible with the
reference's connector). Written up for connector authors in `docs/user/relay-contract.md`.
WebSocket client: httpx-ws 0.9.0 (MIT, already locked for other adapters), imported lazily only
when the production opener dials; tests use an opener seam and the fake connector
(`tests/fakes/platform/relay_wire.py`), so no WebSocket library and no socket is needed.

What it does (own words): when the gateway is configured with a relay (`gateway.relay.url`, a
gateway id, and the shared secret `INDUS_RELAY_SECRET` in the profile's secret store), it dials
OUT to the connector over one WebSocket, so a gateway with no public address can still be reached
from chat platforms the connector fronts. The first frame proves who we are: an HMAC-SHA256 (stdlib
`hmac`) over the gateway id, a Unix timestamp and a fresh random nonce, keyed by the shared secret.
The connector answers with one capability descriptor per platform it fronts for us and then
`ready`; after that, inbound chat events and interrupt frames arrive on the same socket, and
outbound operations go up it as numbered requests answered by `result` frames. A dropped socket is
re-dialled after 1 s, doubling each failure to a 60 s ceiling, and the delay goes back to 1 s after
a successful handshake. When the host is told it is idle, the transport asks the connector to go
"dormant": the connector acknowledges, the socket is closed, and the connector buffers events for
us; on the next dial it replays them, each with a buffer id, in order, and we acknowledge each one
only after it has been handed to the gateway, so nothing is lost or delivered twice.

Inputs → outputs: settings (url, gateway id, secret, timeouts) + clock + opener → `start()` (dial,
authenticate, collect descriptors), `request(platform, action)` → result mapping (never raises),
`interrupt(platform, session_key)`, `go_dormant()` → acknowledged?, `wake()`, `stop()`; inbound
`event` / `interrupt` frames → the registered handlers, `fatal` → the registered fatal listener.

Files: `src/indus/gateway/relay/__init__.py`, `src/indus/gateway/relay/descriptor.py`,
`src/indus/gateway/relay/transport.py`, `tests/unit/gateway/relay/test_transport.py`,
`docs/user/relay-contract.md`; shared: `src/indus/foundation/config/schema.py` (`gateway.relay`
rows) and its test.

Edge cases / failure modes:
- descriptor `max_len` 0, negative or not a number → 4096; unknown `len_unit` → `utf16` (the
  stricter count: a chunk measured in UTF-16 units never overflows a code-point limit); `ops`
  absent or empty → the base set `send`, `edit`, `typing`, `follow_up` (new ops only when
  advertised); a `contract_version` other than 1 or a bad platform name → the descriptor is refused
  (logged, that platform is not fronted), never half-read.
- the connector refuses our hello (`refused` with reason `auth`): at start this is a
  configuration problem (not retryable); after an earlier success it means the secret was revoked:
  the fatal listener is told and no re-dial happens. A fresh timestamp and nonce go into every
  dial, so an "expired token" case cannot arise.
- no `ready` within the connect timeout, or the socket closes during the handshake → a retryable
  connect failure.
- a request when the socket is down → a definite "not connected" failure (nothing sent, safe to
  retry); a request whose answer does not come within the request timeout, or whose socket drops
  after the frame left → "timed out" (the connector may have applied it; never re-sent).
- a malformed frame is skipped with a warning; unknown frame types are ignored (forward
  compatibility).
- inbound handler failure → the frame is not acknowledged (the connector replays it later); a
  replayed buffer id we already acknowledged is acknowledged again but not delivered twice
  (bounded seen-set).
- `go_dormant` without an `idle_ack` in time → stays connected and returns False (the caller must
  not treat the relay as quiesced).
- the secret never appears in a frame, log line, URL or exception text (only the HMAC does).

Our design differences / decisions:
- Names: `RelayTransport`, `Descriptor`, frames `hello`/`descriptor`/`ready`/`refused`/`op`/
  `result`/`event`/`interrupt`/`idle`/`idle_ack`/`ack` (our own vocabulary).
- Auth is a signed first frame, not an upgrade header; the connector checks the timestamp window
  (±300 s) and remembers nonces inside it, so a captured hello cannot be replayed.
- One JSON object per WebSocket text message (no newline framing inside messages).
- The reference's passthrough-plane forwards (Discord interactions, Twilio), the Slack slash
  parent-command rewrite, channel `context`, auto-thread markers, command manifests, relevance
  policy POSTs, wake-URL registration and brokered-suspend holds are not built (recorded "later":
  they serve the reference's hosted connector).
- Dormant: the transport does not re-dial on its own while dormant; `wake()` (or the next gateway
  start) re-dials and the connector replays. The decision to go idle stays with the daemon's
  idle-exit path (P17-23); wiring is the lead's.

Tests we will write:
- descriptor parsing: `max_len` 0 → 4096, garbage values, unknown unit, missing ops, version and
  platform checks, capabilities projection.
- HMAC: signature shape, the fake rejects a wrong secret, a stale timestamp and a replayed nonce.
- handshake: descriptors collected, `ready` ends start, refused auth is not retryable, missing
  `ready` times out on the fake clock.
- requests: answered result, timeout is ambiguous, not-connected is definite.
- reconnect: drop → delays 1, 2, 4, 8, 16, 32, 60, 60 (fake clock) while the connector refuses
  sockets, reset to 1 after a successful handshake; revoked auth after success → fatal, no re-dial.
- dormant: idle → ack → socket closed; events pushed while dormant buffer; wake → replay in order,
  each acked once; a failing handler leaves its frame unacked.

## Decisions for the lead
- Config rows `gateway.relay.url`, `gateway.relay.gateway_id`, `gateway.relay.connect_timeout_seconds`,
  `gateway.relay.request_timeout_seconds`; the secret is `INDUS_RELAY_SECRET` (secret store, env
  fallback through `read_secret`).
- The production socket needs httpx-ws: suggest a `relay = ["httpx-ws==0.9.0"]` extra (already
  locked; no new package). I did not edit `pyproject.toml`, so `uv.lock` stays untouched.
- Intended difference (needs a D-id): our relay wire contract is our own (frames, signed hello,
  JSON per WebSocket message), not the reference connector's; connector authors follow
  `docs/user/relay-contract.md`.

Compare checklist (filled after the rewrite; reopened `gateway/relay/ws_transport.py`,
`gateway/relay/descriptor.py`, `gateway/relay/auth.py`, the contract doc):
- [x] signed first frame, refusal handling (test: `tests/unit/gateway/relay/test_transport.py::test_bad_hmac_is_rejected_by_the_fake_and_start_is_not_retryable`, `::test_verify_refuses_bad_signature_stale_time_and_replayed_nonce`)
- [x] descriptor normalisation: max_len 0/garbage → 4096, ops absent → base set, unknown keys ignored (test: `::test_a_fake_connector_with_max_len_zero_gives_4096`, `::test_missing_ops_mean_the_base_set_and_new_ops_need_advertising`)
- [x] request/answer by id; timeout and mid-flight drop are "maybe delivered", not-connected is definite (test: `::test_an_unanswered_request_times_out_as_maybe_delivered`, `::test_a_dropped_socket_fails_waiting_requests_as_maybe_delivered`, `::test_a_request_while_disconnected_is_definitely_not_sent`)
- [x] reconnect with doubling backoff, reset after success (test: `::test_reconnect_backoff_doubles_from_one_to_sixty_and_resets_after_success`)
- [x] revocation after a successful handshake is terminal (test: `::test_revoked_credentials_after_success_are_fatal_and_never_retried`)
- [x] going idle waits for the connector's ack; no ack → stay connected (test: `::test_go_dormant_without_an_ack_stays_connected`)
- [x] replay acked only after the handler took it; handler failure leaves it unacked; replays not delivered twice (test: `::test_dormant_buffering_replays_in_order_and_acks_each_once`, `::test_a_failing_handler_leaves_the_event_unacked_for_replay`, `::test_a_replay_already_handled_is_acked_but_not_delivered_twice`)
- [x] gap found: the reference lets in-flight operations finish before closing — fixed by a 2 s drain in `stop`, pinned by `::test_stop_lets_an_operation_already_sent_be_answered` and `::test_stop_gives_up_on_an_unanswered_operation_after_two_seconds`
- [x] interrupts both ways (test: `::test_interrupt_frames_reach_the_handler_and_ours_reach_the_connector`)
- Deliberate differences: the reference signs an expiring upgrade token (5 min TTL) and so needs a "first 4401 after success is provisional" re-dial; we sign a fresh timestamp + nonce per dial, so an auth refusal is always final. The reference's dormant mode keeps re-dialling on a slow cadence (a suspended machine wakes with a frozen loop); we wait for `wake()` or the next start (Indus has no machine suspend; idle exit ends the process). One hello per connection instead of one per fronted identity: the connector answers with every platform it fronts for us.
- Not taken: passthrough-plane forwards (Discord interactions, Twilio), channel `context` rendering, Slack `/parent` command rewrite, Discord command manifest on hello, relevance-policy POST, wake-URL registration, brokered-suspend redial holds, keepalive tuning (httpx-ws defaults) — they serve the reference's hosted connector; recorded "later".

## Status

Done (2026-10-21). Transport, descriptor and contract document written and tested
(`tests/unit/gateway/relay/test_transport.py`, 49 tests). Shared files changed:
`src/indus/foundation/config/schema.py` (`gateway.relay.*`) and its test. Daemon wiring (start the
front, idle → `go_dormant`) is the lead's at integration.
