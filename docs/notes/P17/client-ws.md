# Task: `@indus/client` WebSocket transport (P17-20) Phase: 17 Owner: wave-27 builder K8

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none beyond the research specs —
the row names research 05 §3.2 (shared channel) and research 08 §3.9 (renderer ↔ agent transport:
numbers, server→client requests, decline handshake, replay cursor cap, prepared-submission
journal). Our P12 `packages/client` (channel, client, heartbeat, backoff, event buffer), P17-13
`gateway/ws/server.py` and `gateway/ws/methods.py` (ready frame, `server_request` offers
`{request, replayed, generation}`, `generation` on answers, `-32601` / `4404` refusals by id),
P17-14 `service/subscriptions.py` (`session.subscribe` with a cursor replays then attaches in one
step; `session.replay_gap`), P17-07 `request_redelivery.py` (`request.settled`). Closed before
writing.
Evidence: none.

What it does (own words): the shared TypeScript connection layer learns to talk to the daemon over
a WebSocket. `WsTransport` opens `ws://127.0.0.1:<port>/ws` offering two subprotocols — our
protocol name and the single-use ticket — and refuses to run if the server picked anything but our
protocol name; it never puts a ticket in a URL and never logs subprotocols. Each text message is
one JSON-RPC frame; a binary message or a non-JSON text is a protocol error, not a crash. Close
codes from the daemon become readable reasons (4401 ticket refused, 4403 forbidden, 1013 try
again, 1011 too slow, 1001 going away).

The channel learns the daemon's wire: a `server_request` notification there is
`{request, replayed, generation}`; the answer names that generation. The same request id
delivered twice (live, then re-offered after a reconnect or a resume) reaches the handlers once;
the first respond / fail wins on the wire. An answer that never reached the wire (the link was
down) does not count, so the re-offer is shown again. Kinds nobody here can render are answered at
once with a `-32601` error frame under the request's id (R05.3), so the daemon offers them to
other viewers or settles them undeliverable; `decline` ("not shown here", 4404) is sent only when
the daemon advertised `request.decline`. `request.settled` (like `server_request.cancelled`)
closes a card.

Replay: `ReplayCursors` keeps, per session, the newest `seq` applied and its `replay_epoch` (at most
256 sessions, least recently touched dropped). Events at or below the cursor of the same epoch are
duplicates and dropped before listeners see them. With `replay.resubscribe` on, a reconnect (every
ready after the first) re-sends the remembered `client.capabilities` and then
`session.subscribe {session_id, epoch, cursor}` for every tracked session: the daemon replays what
was missed (once, in order) and re-offers open requests; a `session.replay_gap` resets the cursor
to the gap's `last_seq` in the new epoch and is passed to listeners so the surface reloads.
`eventsSince(session)` pulls the missed events explicitly (the same dedup applies).

Kept from P12 and pinned for the WebSocket: any inbound frame is liveness (R05.9); replacing a
transport rejects every in-flight call; close events of a discarded socket are ignored; the
"subscribed" flag of the event buffer survives reconnects.

Inputs → outputs: `new WsTransport({url, protocols, webSocket?, logs?, onEvent?})` →
`Transport` (exported from `@indus/client`'s browser-safe entry); `IndusClient({..., replay:
{resubscribe}})`; `client.replay.cursor(sid)`, `client.subscribeSession(sid)`,
`client.eventsSince(sid)`; `RpcChannel.decline(id)`, `RpcChannel.setServerCapabilities(list)`,
`RpcChannel.generationOf(id)`; events `transport.closed {code, reason, terminal}` and
`transport.attach_failed` (used by P17-19).

Files: `packages/client/src/ws-transport.ts`, `packages/client/src/replay.ts`,
`packages/client/src/ws-transport.test.ts`, `packages/client/src/replay.test.ts`,
`tests/contract/client/test_ts_client_against_daemon.py` (+ its node script
`tests/contract/client/ts_client_script.ts`, `tests/contract/client/__init__.py`). Shared:
`packages/client/src/channel.ts`, `packages/client/src/client.ts`,
`packages/client/src/localEvents.ts`, `packages/client/src/index.ts`,
`docs/internals/traps-ledger.md` (R05.3, R05.9), `docs/internals/acceptance-map.md` (ACC-K51-4).

Edge cases / failure modes:
- server echoes no subprotocol or another one → closed at once, reason `subprotocol`;
- URL with user-info, query or fragment, or not `ws:`/`wss:` → refused before dialling;
- a frame larger than the transport cap (64 MiB) or not JSON → `transport.protocol_error`;
- same `srq-` id delivered live and in a resume's `pending_requests` → one card;
- a replayed id whose earlier answer failed to send → shown again;
- reconnect while a call waits → that call rejects (`TransportReplaced`/`TransportClosed`), never
  hangs; a close of the old socket arriving after the new one opened → ignored;
- 257th tracked session → the least recently touched cursor is dropped (that session reloads by
  snapshot if it reconnects);
- events without `seq` / `session_id` (transport events, ready) pass untouched.

Our design differences / decisions:
- Reconnect uses `session.subscribe` with the cursor (the daemon does "since + attach" in one step
  on its loop, so nothing is lost or doubled between the two); `events.since` is offered for an
  explicit pull. Research describes "events since cursor then resubscribe".
- `WsTransport` takes a `WebSocket` constructor (the global one in browsers and Node ≥ 22), so
  tests use a fake.
- Decline needs the server's `request.decline` capability; without it a refusal is the `-32601`
  error frame.

Tests we will write:
- ws-transport (fake WebSocket): subprotocols offered and echoed; wrong echo refused; URL rules;
  text frames parsed, binary / junk reported; close codes mapped; ticket absent from logs; send
  before open fails; close idempotent.
- channel over WS wire: wrapped offer unwrapped and answered with its generation; duplicate id
  (live + re-offer + resume) handled once; failed answer lets a re-offer through; unanswerable
  kind → `-32601` at once; decline only with the capability; `request.settled` withdraws;
  transport swap rejects in-flight; old socket's close ignored; subscribed flag kept.
- replay: cursor per session; duplicates dropped; epoch change; gap resets; 256 cap; reconnect
  re-subscribes with cursors after capabilities.
- contract (Python + node): a real `WsEndpoint` over a `DaemonSessionHost` on the fake model;
  the TS client attaches, subscribes, receives deltas, answers an approval, loses its socket,
  reconnects with a fresh ticket and resumes from its cursor without duplicates.

## Decisions for the lead
- `request.decline` is the capability name the daemon advertises (`DECLINE_CAPABILITY`); the client
  declares it only when the surface asks (`features`).
- Auto re-subscribe is opt-in (`replay.resubscribe`); the TUI keeps its P12 resume path
  (`session.resume` of the parked session on ready) and uses the cursor only to drop duplicates.
- A ready without `replay_epoch` (the stdio bridge numbers each process from 1) drops every
  cursor, so a restarted child's events are never mistaken for duplicates.
- A crashing handler on the daemon's wire answers `-32603` under the request's id (the daemon
  counts it as this viewer's refusal); on the bridge's wire it stays the kind's decline answer.
- An answer whose call failed without a server error (no link, link lost while waiting) is
  forgotten, so the daemon's re-offer after a reconnect is shown again; an error frame from the
  server (or `accepted: false`) still counts as answered.

Compare checklist (filled after the rewrite; research 05 §3.2 and 08 §3.9 re-read):
- [x] numbers: 120 s default call timeout, 15 s / 45 s / 8 pings, 1 → 30 s backoff (P12, kept),
  15 s connect deadline, cursor cap 256 (test: `replay.test.ts::keeps at most 256 sessions, dropping the least recently touched`)
- [x] first respond / fail wins; re-delivery keeps the id (test: `ws-transport.test.ts::delivers one id once across live, re-offer and resume copies; first answer wins`)
- [x] decline only when advertised (test: `ws-transport.test.ts::declines with server_request.decline only when the daemon advertised it`)
- [x] unanswerable kinds `-32601` at once (test: `ws-transport.test.ts::answers a kind nobody here renders with -32601 at once, under the request id`)
- [x] transport swap rejects in-flight; old close ignored; subscribed flag kept (test: `ws-transport.test.ts::a transport swap rejects in-flight calls; the old socket's close is ignored`)
- [x] any frame is liveness (test: `ws-transport.test.ts::counts any inbound frame as liveness on a WebSocket (R05.9)`)
- [x] replay after reconnect without duplicates (tests: `replay.test.ts::resumes every tracked session from its cursor after a reconnect, without duplicates`, `test_ts_client_against_daemon.py::test_ts_client_attaches_answers_and_resumes_from_its_cursor`)
- [x] gap found while comparing with research 08 §3.9 ("a second answer from an outdated card has
  no effect on the wire"): an answer refused for lack of a link used to block the re-offer for
  good — fixed by forgetting undelivered answers, pinned by
  `ws-transport.test.ts::shows a re-offer again when the earlier answer never reached the daemon`.
- Deliberate differences: re-subscribe with the cursor instead of "since, then subscribe" (one
  atomic step on the daemon); the replay layer sits in the client, not in each surface.
- Not taken: the renderer's in-flight turn journal and the prepared-submission journal of
  research 08 §3.9 (desktop main process, P20; the Python journal is P17-17); the REST helper
  (desktop, P20).

## Status

Done (2026-10-20). The cross-language contract runs the TS client against a real
`WsEndpoint` + `DaemonSessionHost` on loopback (skipped without Node ≥ 22.18, pnpm install or
wsproto).
