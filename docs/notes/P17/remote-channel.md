# Task: Python attach channel (P17-16) Phase: 17 Owner: wave-27 builder K6

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/gateway_client.py` (all:
ticket dial, request ids, per-call budgets, reader loop, error shaping), `hermes_cli/gateway_connection.py`
(all: one connection per CLI process, loop thread, epochs, redial and resubscribe, "not sent" vs
"outcome unknown"). Research 05 §3.2 "Shared channel behaviour" and §3.30, research 08 §3.9
(numbers), R05.9, R08.4. Our P17-10 `ws_auth`, P17-11 `gateway_control.describe/session_ticket`,
P17-13 `gateway/ws/server.py` (ready frame, ping, server-request notifications). Closed before
writing.
Evidence: none.

What it does (own words): the Python surfaces (classic CLI, one-shots, later ACP) talk to the
daemon over one WebSocket carrying JSON-RPC 2.0. The channel owns that socket:

- Dial: every attempt gets a fresh grant — `describe` (profile and instance ids), then a new
  single-use ticket from the owner-only control socket (`session-ticket`, purpose
  `ws-interactive`, the ids passed along and checked). It dials only the loopback `ws_url` the
  ticket came with (credential-free, checked again), offering `indus-gateway-v1` and
  `indus-gateway-ticket.<t>`; the server must echo exactly `indus-gateway-v1`. The ticket never
  reaches a URL, an argv, a log line or a `repr`. A reused ticket would fail (R08.4), so a
  reconnect never reuses one.
- Ready: the first frame must be `gateway.ready` within 15 s; its `profile_id` must be the one the
  grant named. Then `client.capabilities {surface, features}` is sent.
- Requests: ids are client integers (server-request ids are `srq-…` strings, so they never
  collide). Budgets: 120 s by default, `prompt.submit` 1,800 s, `server_request.answer` 330 s,
  `session.compress` 660 s; a caller may pass its own. A request made while the socket is down
  waits for the next ready within its budget; if none comes it fails "not sent" (nothing reached the
  daemon, safe to try again).
- Heartbeat: when ready says `heartbeat`, `gateway.ping` every 15 s; any inbound frame counts as
  liveness (a turn streaming deltas never looks dead, R05.9); no inbound frame for 45 s closes the
  socket so the reconnect runs; at most 8 pings may be outstanding.
- Loss and reconnect: when the socket goes, every request in flight on it fails at once with
  "transport lost" (it was sent: the outcome is unknown); a replaced socket's late close or frames
  are ignored (each socket has an epoch). Redial after 1 s, doubling to 30 s, no jitter; each
  scheduled attempt is reported (`reconnecting {attempt, delay}`); reaching ready resets the
  counter and calls the `on_ready` hooks (the remote host re-subscribes there). `close()` ends it
  for good.
- Inbound notifications (events, `server_request` offers, `session.replay_gap`, …) go to the
  `on_notification` listener in arrival order. A JSON-RPC *request* from the server (a string id
  with a method) that no handler takes is answered `-32601` at once (R05.3).

Inputs → outputs: `Channel(dial, surface=, features=, clock=, limits=)`; `await start()`;
`await request(method, params, timeout=)` → result dict or `RpcError` / `TransportLost` /
`NotSent`; `notify(method, params)`; `respond(id, result | error)`; `on_notification(cb)`,
`on_ready(cb)`, `on_state(cb)`; `await close()`. Dialers: `home_dialer(home, ...)` (describe →
ticket → WebSocket) and any `async () -> Transport` in tests.

Files: `src/indus/service/remote/__init__.py`, `src/indus/service/remote/channel.py`,
`src/indus/service/remote/ws_client.py` (minimal loopback WebSocket client over `wsproto`, already
a base dependency), `tests/unit/service/remote/__init__.py`, `tests/unit/service/remote/test_channel.py`.

Edge cases / failure modes:
- server streams deltas and never answers pings → stays connected; silent server → closed at 45 s;
- `gateway.ready` missing or for another profile → the dial fails (`profile_mismatch`), backoff;
- control socket gone (daemon restarting) → the dial fails, backoff; a home that other users can
  write to → `unsafe_home`, terminal;
- frames that are not JSON objects are dropped (logged without content);
- a response for an id we do not know (late answer to a timed-out call) is dropped;
- `close()` during a dial → the dial result is closed, nothing is left running.

Our design differences / decisions:
- asyncio only, on the caller's loop (the reference runs a loop thread per CLI process; the remote
  host's sync facade does that once for the REPL, P17-17).
- Our own small WebSocket client over `wsproto` (no `websockets` dependency); it refuses anything
  that is not `ws://` on a loopback address, never routes through a proxy, masks every frame.
- No pre-mount event buffer or log ring here (renderer concerns, P17-20).

Tests we will write (`tests/unit/service/remote/test_channel.py`, the real `WsEndpoint` over a
`DaemonSessionHost` through an in-loop ASGI transport, plus scripted fake transports, fake clock):
- a server streaming frames without pongs stays connected past 45 s; a silent one is closed at 45 s;
- at most 8 pings outstanding;
- an in-flight call on a swapped transport is rejected; a close from the old transport is ignored;
- each reconnect mints a new ticket (the real endpoint refuses a reused one); backoff 1, 2, 4 … 30 s
  and reset on ready;
- an unanswerable server request gets `-32601` at once;
- per-method budgets; "not sent" when no socket comes back;
- the ticket appears in no log record and no repr; the real-socket client against a uvicorn
  listener (`wsproto`), loopback only.

## Decisions for the lead
- Ready timeout 15 s and the surface/features handshake are the channel's (not the caller's).
- `RpcError` carries the code, a bounded message and scalar-only `data` (no remote diagnostics
  forwarded, as the reference's client does).

Compare checklist (filled after the rewrite; reopened `gateway_client.py`, `gateway_connection.py`):
- [x] fresh ticket per dial, subprotocol echo checked (test: `test_channel.py::test_each_reconnect_mints_a_new_ticket`, `::test_a_server_that_does_not_echo_the_protocol_is_refused`)
- [x] any frame is liveness (test: `test_channel.py::test_a_server_streaming_without_pongs_stays_connected`)
- [x] silent server closed at 45 s (test: `test_channel.py::test_a_silent_server_is_closed_at_45_s`)
- [x] in-flight requests of a swapped transport rejected; old close ignored (test: `test_channel.py::test_a_swapped_transport_rejects_its_in_flight_calls`)
- [x] "not sent" vs "outcome unknown" kept apart (test: `test_channel.py::test_a_request_while_disconnected_waits_for_the_next_ready_or_is_not_sent`)
- [x] error data bounded to scalars (test: `test_channel.py::test_method_budgets_and_rpc_errors`)
- [x] no proxy, 8 MiB message cap, 10 s open timeout, loopback only (`ws_client.connect`; test: `test_channel.py::test_the_wsproto_client_attaches_to_a_real_loopback_listener`)
- Deliberate differences: one asyncio channel on the caller's loop instead of a loop thread per
  process (the REPL's thread lives in `RemoteHost`); requests made while down wait for the next
  ready within their budget (the reference fails at once with "not sent"); a long-running call
  does not get a socket of its own (the daemon runs each request as its own task, so a slow
  handler never blocks reads).
- Not taken: side-task answer queues (`/bg`, `/btw` over the daemon: later), session state
  caches for the status bar (the REPL keeps its own from events).

## Interface for other slices (K7 CLI attach)

```python
from indus.service.remote.channel import (
    Channel,
    ChannelLimits,
    home_dialer,
    Dialed,
    ChannelError,
    NotSent,
    TransportLost,
    RequestTimedOut,
    RpcError,
    DialRefused,
)

dial = home_dialer(home, env=scope.env.to_dict(), budget=RecoverBudget())  # fresh ticket per dial
channel = Channel(dial, surface="cli", features=("server_requests",))
await channel.start()  # DialRefused on a failed first dial (.reason, .terminal)
await channel.request(method, params, timeout=None)  # dict | RpcError | TransportLost | NotSent
channel.on_notification(cb)
channel.on_state(cb)
channel.on_ready(async_hook)
await channel.close()
```

## Status

Done (2026-10-20). Shared: `docs/internals/traps-ledger.md` (R05.9, R08.4 refs).
