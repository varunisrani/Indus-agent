# Task: WebSocket endpoint (P17-13) Phase: 17 Owner: wave-26 builder K5

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tui_gateway/ws.py` (all: connection
limits, write timeout and send deadline, dispatch queue, token coalescing, ready frame, teardown).
Research 05 §3.2 (mode B/C and the shared channel), research 09 R09.2. Our P17-10 `service/ws_auth.py`,
P17-12 `service/rpc/dispatch.py`, P17-08 `service/daemon_host.py`, P12 `tui_bridge/stdio_server.py`
(the stdio transport the WS loop mirrors). Closed before writing.
Evidence: none.

What it does (own words): the daemon listens for WebSocket clients on a loopback port it picks
itself (127.0.0.1, port 0; the real port is known once bound). A client (classic CLI, TUI, later the
desktop and ACP) gets a single-use ticket from the owner-only control socket and opens the socket
offering two subprotocols: `indus-gateway-v1` and `indus-gateway-ticket.<ticket>`. The endpoint asks
`service.ws_auth.authenticate_upgrade` and nothing else: a refusal is closed with the helper's code
(4401 for a bad, used or expired ticket, 4403 for a non-loopback peer or an Origin not on the
allow-list); a ticket in the URL is never used. On success the server echoes only
`indus-gateway-v1` and the first frame is the `gateway.ready` notification: replay epoch, heartbeat
on, profile id, instance id, capabilities, protocol version.

Then the connection speaks the same newline-free JSON-RPC 2.0 frames as stdio: each text message is
one frame; `decode_request` turns it into a request, an error answer or nothing; requests go to the
shared `Dispatcher` over the daemon's method table; every event or notification leaving the socket
is checked with `check_event` (strict under tests). `gateway.ping` is answered at once by the read
loop so the heartbeat measures the transport, not a busy handler.

Limits per connection: a frame may take at most 10 s on the socket (write timeout) and must leave
within 30 s of being queued (send deadline) — past either the connection is closed (1011) without
holding up any other connection, because each connection has its own writer task and its own
bounded outbox; at most 256 requests may be waiting or running (the 257th closes the socket with
1013, "try again later"); one inbound message is at most 1 MiB (1009) and binary messages are
refused (1003). Closing the socket only detaches its viewers: turns keep running. Close reasons are
fixed short texts (never an echo of client input, ≤ 123 bytes). Logs never carry params, tickets
or the request URL's query / user-info.

Inputs → outputs:
- `WsEndpoint(host, book, scope=, origins=(), limits=WsLimits(), theme="auto")`; `app` (Starlette,
  one WebSocket route at `/ws`); `await start(bind="127.0.0.1", port=0) -> ws_url`;
  `ws_url`, `status_row()` (for `StatusWriter.set_surface("ws", …)`), `await stop()`.
- Per connection: `gateway.ready` first; responses for requests; event notifications
  (`method` = event type, `params` = the numbered event with `seq` and `replay_epoch`);
  `server_request` notifications (`{request, replayed, generation}`).
- Served methods (the daemon's table, `gateway/ws/methods.py`): `gateway.ping`,
  `client.capabilities`, `session.create`, `session.resume`, `session.close`, `prompt.submit`,
  `session.interrupt`, `session.steer`, `server_request.answer`, `server_request.decline`, and
  P17-14's `session.subscribe`, `session.unsubscribe`, `sessions.watch`, `events.since`. Any other
  declared method answers `-32601` "not served here" until a later task adds a handler.

Files: `src/indus/gateway/ws/__init__.py`, `src/indus/gateway/ws/server.py`,
`src/indus/gateway/ws/methods.py`, `tests/contract/gateway/test_ws_endpoint.py`,
`tests/fakes/asgi_ws.py` (test-only in-loop ASGI WebSocket client),
`tests/integration/gateway/test_ws_listener.py` (real loopback listener). Shared:
`src/indus/gateway/http/serving.py` (`HttpServer(ws=…)` uvicorn WebSocket options),
`src/indus/protocol/bridge.py` (`BridgeReady` daemon fields), `src/indus/protocol/methods.py`
(attach methods, `generation` on steer / answer, `surface` on capabilities),
`packages/protocol/src/generated/*` (regenerated), `tests/unit/protocol/test_models.py`,
`tests/unit/tui_bridge/test_dispatch.py` (daemon-only names).

Edge cases / failure modes:
- the same ticket twice → the first upgrade succeeds, the second is closed 4401;
- ticket only in the query string → 4401 (and the query ticket is spent, never logged);
- `Origin: http://evil` → 4403; a non-loopback peer → 4403;
- a client that stops reading: its writer blocks; after 10 s on one frame (or 30 s queued) the
  socket is closed; other connections keep receiving events in order;
- 257 requests waiting → 1013 and the socket closes; everything it watched is detached;
- a handler that crashes → `-32603` answered, the loop goes on (the dispatcher's rule);
- a frame that is a response to a server request (`id` = `srq-…`) with an error → recorded as that
  viewer's refusal (`-32601` cannot render, `4404` not shown here) so the request goes to another
  viewer or is settled undeliverable at once; any other response from the client is dropped;
- daemon stop → every connection is closed 1001 ("going away") and detached.

Our design differences / decisions:
- Our names (`WsEndpoint`, `WsConnection`, `WsLimits`), the `/ws` path.
- Requests run as tasks (as on stdio) rather than one at a time: an interrupt or an approval answer
  is never stuck behind a long submit on the same socket; the 256 cap bounds them.
- No token-coalescing in the transport: the event log (P17-06) already joins deltas in 33 ms windows
  before numbering, so every viewer sees the same numbered events.
- A refused upgrade is accepted (echoing `indus-gateway-v1` only when it was offered) and closed
  at once with the code, so browser clients can read 4401/4403; nothing is read from it.
- Generation fences on the wire: `session.interrupt`, `session.steer` and `server_request.answer`
  must name `generation` on the daemon (missing → `INVALID_PARAMS` with the path); a stale one is
  answered with the method's "nothing happened" result (`interrupted: false`, `queued: false`,
  `accepted: false`), never applied to another turn. P17-25 may turn it into `stale_generation`.

Tests we will write (`tests/contract/gateway/test_ws_endpoint.py`):
- ticket in the subprotocol accepted once and `gateway.ready` first; reused → 4401; URL ticket → 4401;
  `Origin: http://evil` → 4403; non-loopback peer → 4403; echo is only `indus-gateway-v1`;
- slow reader closed at the write timeout while another viewer of the same session keeps receiving;
- 257 requests waiting → 1013; ping answered while the queue is full of slow work;
- a full turn over the socket: create, submit, deltas in order, `turn.done`; an approval answered
  over the socket with its generation; a stale-generation interrupt does nothing;
- per-connection authorisation: a second connection cannot submit to, interrupt or replay a session
  it did not attach (`SESSION_NOT_FOUND`);
- closing the socket mid-turn only detaches (the turn completes and is stored);
- logs: no ticket, no params, no query string;
- with a WebSocket implementation installed (`wsproto`): a real uvicorn listener on 127.0.0.1:0,
  `ws_url` / status row, ready frame over TCP (skipped otherwise).

## Decisions for the lead
- **Dependency**: uvicorn serves WebSockets only with `wsproto` or `websockets` installed; neither is
  a base dependency (wsproto 1.3.2, MIT, is locked through the httpx-ws extras). `WsEndpoint.start`
  refuses with a clear error when no implementation is importable, and the real-socket test skips.
  Proposed: add `wsproto==1.3.2` to the base dependencies (no new lock entry). Not done here (lead
  owns dependencies and the lock).
- `HttpServer` gained a `ws=` mapping (uvicorn WebSocket settings: implementation, `ws_max_size`,
  `ws_per_message_deflate=False`, protocol pings off — our own `gateway.ping` is the heartbeat).
- `gateway.ready` reuses `BridgeReady` with optional daemon fields (`replay_epoch`, `profile_id`,
  `instance_id`, `capabilities`); `theme` is `auto` from the daemon (the renderer's own config
  decides; P17-19 reads the skin through `runtime.describe`).
- `prompt.submit` on the daemon: the busy mode is the surface default (TUI queue, others interrupt)
  unless the client set `when_busy` explicitly: `queue` queues, `reject` answers `SESSION_BUSY`
  while a turn runs, `steer` / `redirect` are refused (`INVALID_PARAMS`: steer names its generation
  through `session.steer`). `turn_id` is the admission id; `generation` the started turn's, else the
  running turn's, else 0 (not yet assigned; `turn.started` carries it); `status` `streaming` or
  `queued`. Each submit is a fresh admission request id until P17-17 adds client request ids.
- The connection's surface kind comes from `client.capabilities {surface}` (default `cli`); the
  viewer's renderable request kinds from its features (`server_requests` = all kinds,
  `server_requests.<kind>` = one), `request.decline` turns on the 4404 decline.
- Limits: inbound message 1 MiB; queued outbound bytes per connection 16 MiB (past it the socket is
  closed 1013 — the client reconnects and replays); inbound waiting bytes 16 MiB.

- Protocol (P17-25 territory, done here because the dispatcher only serves declared names):
  `indus.protocol.attach` declares `session.subscribe`, `session.unsubscribe`, `sessions.watch`,
  `events.since` (`DAEMON_ONLY_METHODS`: the stdio bridge answers them `-32601`; its completeness
  test excludes them); optional `generation` on `SessionSteerParams` / `ServerRequestAnswerParams`,
  optional `surface` on `ClientCapabilitiesParams`, optional daemon fields on `BridgeReady`;
  `packages/protocol` regenerated. No new error code: stale generations answer the "nothing
  happened" result, admission refusals answer `SESSION_BUSY` with the ledger reason. P17-25 still
  owns `runtime.describe`, `admission.*`, `maintenance.run`, the event models (`session.replay_gap`,
  `session.updated`, `session.viewers`, `request.settled`) and the new error codes.
- Server requests on the WebSocket are `server_request` notifications whose params are P17-07's
  `Offer.to_wire()` (`{request, replayed, generation}`), so a viewer can name the generation in its
  answer. `server_request.decline` on the daemon is the viewer's refusal (P17-07: `4404` with the
  decline capability, else `-32601`), not a denial for everyone: the request goes to the other
  viewers and is settled undeliverable when none is left. A client may also answer a server request
  with a JSON-RPC error frame whose id is the `srq-…` id (same effect).
- `session.close` from a client only detaches that connection (other viewers and turns carry on);
  `session.resume` answers `last_seq` from the daemon's event log (the cursor for `events.since`).
- An idle limit was added in the compare step (below): no inbound frame for 120 s closes 1001.

Compare checklist (filled after the rewrite; reopened `tui_gateway/ws.py`):
- [x] write timeout / send deadline close a stalled peer without blocking others (test:
  `test_ws_endpoint.py::test_a_slow_reader_is_closed_without_stalling_other_viewers`)
- [x] dispatch queue bound and ping answered by the read loop (test:
  `test_ws_endpoint.py::test_the_257th_waiting_request_closes_the_socket_1013`)
- [x] ready frame first, carries the replay epoch and heartbeat (test:
  `test_ws_endpoint.py::test_a_ticket_in_the_subprotocol_is_accepted_once`; real socket:
  `tests/integration/gateway/test_ws_listener.py::test_a_real_listener_on_loopback_serves_an_attach`)
- [x] teardown detaches, never ends sessions (test:
  `test_ws_endpoint.py::test_closing_the_socket_mid_turn_only_detaches`)
- [x] lone surrogates in a frame do not kill the socket (test:
  `test_subscriptions.py::test_outbox_wait_and_lone_surrogates`)
- [x] gap found: the reference turns on TCP keepalive so a silently dropped peer does not stay
  half-open for ever — we cannot reach the raw socket through ASGI, so the endpoint closes a
  connection with no inbound frame for `idle_timeout` (120 s; clients ping every 15 s), pinned by
  `test_ws_endpoint.py::test_a_silent_client_is_closed_after_the_idle_limit`.
- Deliberate differences: the reference parks the reader when its dispatch queue is full; the plan
  says close 1013, which we do. The reference dispatches one request at a time; ours run as tasks
  (as stdio) under the 256 cap. The reference logs a preview of an unparsable frame; we never log
  frame contents. The reference's ready frame carries skin and change-event flags; ours carries
  the theme name, profile/instance ids and capabilities.
- Not taken: TCP_NODELAY (uvicorn's transport is not reachable from the app; frames are small and
  the event log already batches deltas), the dashboard-client liveness marker (P17-23 idle exit
  reads attached viewers instead), legacy-dispatch fallback, browser-controller and wake-word
  teardown (no such features here).

## Wiring owed (P17-22, with the daemon host)

The daemon cannot build its `DaemonSessionHost` yet (LEAD-NOTES, P17 wave 2), so nothing mounts
the endpoint in the daemon. When P17-22 builds the host:
1. In the daemon (a part after the control part, or `ControlPart.start`): `endpoint =
   WsEndpoint(daemon_host, bridge.attach_verbs.book, scope=daemon.scope, origins=<config allow-list,
   default empty>)`; `url = await endpoint.start()` (127.0.0.1, port 0); on `WsUnavailable` log it
   and leave `ws_url` null (`session-ticket` keeps answering `ws_unavailable`).
2. `AttachVerbs(home, book, host=daemon_host, ws_url=lambda: endpoint.ws_url)` — the same book the
   endpoint redeems from (today `DaemonBridge` builds `AttachVerbs` without a host or URL).
3. Runtime status: `daemon.status.set_surface("ws", endpoint.status_row())` after start and on
   stop (`set_surface("ws", None)`).
4. Stop order: `await endpoint.stop()` (closes sockets 1001, detaches viewers) before
   `daemon_host.aclose()`, and before the control part revokes the ticket book.
5. Dependency: add `wsproto==1.3.2` to the base dependencies (lead).

## Status

Done (2026-10-20), except the daemon mount (wiring owed above, needs P17-22's host). Shared edits:
`gateway/http/serving.py` (`HttpServer(ws=)`), `protocol/bridge.py`, `protocol/methods.py`,
`protocol/attach.py` (new), `packages/protocol/src/generated/*` (regenerated),
`tests/unit/protocol/test_models.py`, `tests/unit/tui_bridge/test_dispatch.py`,
`docs/internals/traps-ledger.md` (R09.2), `docs/internals/acceptance-map.md` (ACC-K51-4).

## Wave 47 (builder c, 2026-10-22): the TUI's pickers over the attach socket

Status: done. An attached TUI (the default) got `-32601` "… is not served here" for
`complete.slash`, `model.options` and `session.list`, so the slash menu, the Ctrl+O model picker and
the Ctrl+X sessions picker failed. The daemon's table (`gateway/ws/methods.py`) now serves all
three.

Read: no reference file (a bug in our own attach path; the bridge handlers are the spec). Our
`tui_bridge/methods_complete.py`, `methods_config.py`, `methods_session.py`, `methods_slash.py`,
`service/subscriptions.py::require`, threat model B-DAEMON-5 / B-MUX-3.

What changed (own words):
- One definition for both transports: `service/picker_reads.py` holds the slash candidates (the
  registry's names for the surface, quick commands, runnable skills and bundles), the provider rows
  (key stored in the secret store or the environment; a custom or local endpoint's live `/models`
  list) and the stored-session listing. The bridge's handlers (`--local`) and the daemon's now call
  it, so the answers are the same for the same profile; the bridge's private copies are gone
  (`methods_slash.quick_commands` / `skill_table` stay as thin wrappers for its other callers).
- Scope: a `session_id` on `complete.slash` or `model.options` must be a session this connection
  attached (`Subscriptions.require`; another connection's reads as `SESSION_NOT_FOUND`, B-DAEMON-5).
  `model.options` for a session reads that session's bound route (`service.current_id` follows a
  compaction's new id); without one, the profile's resolved route. `session.list` reads the
  daemon's own profile store (`DaemonSessionHost.store`, the served profile only, B-MUX-3) and
  lists what `--local` lists: every attached client is the profile's owner (its ticket comes from
  the owner-only control socket).
- The command surface follows the connection's kind: `cli` → `cli`, `gui` → `desktop`, anything
  else (the TUI) → `tui`. Blocking work (settings and skill catalogue reads, the store listing,
  route resolution) runs off the loop.

Tests (fail-first; all four failed on the base with `-32601` / not in `served()`):
`tests/contract/gateway/test_ws_pickers.py` — each method over the socket equals the in-process
bridge's answer for the same profile (`/he`, `/g` with a quick command, `/reasoning hig`, `/`;
`model.options` with and without a session; `session.list` with a limit and a query), a foreign
connection naming the session gets `SESSION_NOT_FOUND`, and `served()` lists the three.

Not done here: `config.get` / `config.set` stay unserved (`bridge.settings` allow-list entry kept,
its text updated); `complete.path`, `commands.catalog`, `slash.exec` and the other TUI-only methods
are still `-32601` when attached.

Decisions for the lead:
- W47-c-1 (D-16.1038): `session.list` over the daemon lists every stored conversation of the profile to any
  attached connection (as `--local` and `-c/-r` selection do); only session-addressed calls are
  limited to attached sessions. Proposed: keep; B-DAEMON-5's evidence now lists
  `tests/contract/gateway/test_ws_pickers.py`.
- W47-c-2 (D-16.1039): the shared reads live in `indus.service.picker_reads` (the gateway may not import
  `tui_bridge`). D-16.549's text in `16-what-we-change.md` should say "partly closed by W47-c:
  `model.options`, `complete.slash`, `session.list` served; `config.*` still -32601".
- W47-c-3 (D-16.1040): next candidates for the daemon table, same pattern: `complete.path` (needs the
  session's backend and cwd), `commands.catalog`, `config.get` (read-only; then `bridge.settings`
  narrows to `config.set`).
