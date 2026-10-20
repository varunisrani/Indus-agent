# Task: Chat WebSocket routes — `/api/pty`, events fan-out, RPC sidecar (P18-09) Phase: 18 Owner: wave-29 builder W2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/web_routers/chat_ws.py`
(all: pre-accept gates, `/api/pty`, `/api/ws`, `/api/pub`, `/api/events`),
`web/src/lib/eventsFeedClient.ts` (all: what the subscriber does), `hermes_cli/web_server_chat.py`
(chat argv resolution, sidecar URL building). Research 09 §3.3, §3.5. Our P17
`service/ws_auth.py`, `service/remote/channel.py` (`home_dialer`, `Dial`), `gateway/ws/server.py`
(how the daemon endpoint is reached), `cli/tui_launch.py` (what `indus --tui` builds),
`safety/child_env.py`. Closed before writing.
Evidence: none.

What it does (own words): three WebSocket families back the Chat tab.

`/api/pty` (purpose `web-pty`): the socket passes the shared WebSocket gate (P18-06: feature,
credential, Host/Origin, peer) and is accepted; then the route resolves the profile scope
(P18-10), the channel and its active-session file, the resume target (explicit, else the
channel's last session unless `fresh`), the working directory (fresh chats only) and the child:
`python -m indus --tui [-r <id>]` for that profile — exactly what `indus --tui` would run — with
the env from the single child-env builder (R02.22) plus the event publish URL
(`INDUS_TUI_SIDECAR_URL`, loopback, channel in the query, no credential) and the internal
credential in its own variable (`INDUS_TUI_SIDECAR_CREDENTIAL`), the channel's active-session
file (`INDUS_TUI_DASHBOARD_SESSION_FILE`, which the launcher hands to the renderer), `TERM` (when
unset), `COLORTERM=truecolor`, `INDUS_TUI_MOUSE=off` and `INDUS_PTY_HOST=dashboard`. Missing Node, an unknown profile, a
bad `cwd` or a full registry → one red line in the terminal and close 1011. Then 1:1 or keep-alive
(P18-08).

`/api/pub` + `/api/events` (purpose `web-events`): the TUI child publishes every event it sees as
a text frame to `/api/pub?channel=…`; the backend sends each frame to every `/api/events?channel=…`
subscriber of that channel. Subscribers never speak (anything they send is ignored); a channel
without subscribers is dropped. Only the server-spawned child may publish: `/api/pub` refuses any
credential but the internal one (4401), so a browser cannot inject events into another tab's
feed.

`/api/ws` (purpose `web-rpc`): the JSON-RPC sidecar the SPA uses for connection state and
credential warnings. After the gate it dials the profile's daemon through P17 (a fresh
single-use ticket per dial, `service.remote.channel.home_dialer`; an absent daemon is started
by the recovery path) and relays text frames both ways unchanged — the same declared contract as
any attached client. Identity comes from the WebSocket credential only (the gate's result), never
from a frame: privileged methods (`maintenance.run`) are refused for the server-internal
credential with a JSON-RPC error; frames over 1 MiB close 1009, binary frames 1003; a daemon that
cannot be reached closes 1011 with a short reason; either side closing closes the other.

Inputs → outputs: query `{channel, attach?, resume?, profile?, cwd?, fresh?}` + the credential the
gate reads → terminal bytes / close codes; `/api/pub` frames → `/api/events` frames; `/api/ws`
frames ↔ daemon frames.

Files: `src/indus/web/ws/pty_route.py`, `src/indus/web/ws/events_fanout.py`,
`src/indus/web/ws/rpc_sidecar.py`; tests `tests/contract/web/test_pty_route.py`,
`tests/contract/web/test_events_fanout.py`, `tests/contract/web/test_rpc_sidecar.py`; stand-in
`tests/contract/web/_stubs_w2.py` (W1's gate); fake child `tests/fakes/pty/fake_tui.py`; one
shared line in `src/indus/cli/tui_launch.py` (below).

Edge cases / failure modes:
- channel id invalid → no channel (no marker, no sidecar URL), as if none was sent.
- implicit resume announced before any byte; an explicit `resume` never announces.
- `cwd` given with a resume → ignored (the session keeps its folder).
- a slow subscriber: each subscriber has its own bounded queue (256 frames); one that falls
  behind is closed 1013 and the others never wait for it.
- a publisher frame over 1 MiB → publisher closed 1009; binary → 1003.
- the sidecar's upstream drops → browser socket closed 1011 ("gateway connection lost").

Our design differences / decisions:
- The child is the real `indus --tui` entry, not a second resolution of the renderer argv/env; the
  only addition to the launcher is `INDUS_TUI_DASHBOARD_SESSION_FILE`: when set to a regular file we
  own, the renderer writes its live session id there and the launcher does not delete it.
- The internal credential travels in its own env variable, never in the publish URL (no
  credential in a URL, argv or log).
- `/api/pub` accepts only the internal credential (the reference accepts any dashboard credential).
- Per-subscriber queues instead of a sequential broadcast loop.

Tests we will write: fake TUI child (python script) publishes three events over a real loopback
server → two subscribers each get three, in order; a subscriber's send ignored; channels evicted;
a browser credential on `/api/pub` → 4401; unknown profile → red text + 1011; missing Node → 1011;
implicit resume frame; explicit resume not announced; resize never reaches the child; fresh drops
the marker; 1:1 marker dropped on disconnect; sidecar: relays both ways, refuses privileged method
for internal credential, loopback token rejected in gated mode (stand-in gate), upstream refusal →
1011.

## Decisions for the lead
- Interface assumed from W1 (stand-in in `tests/contract/web/_stubs_w2.py`): `gate(websocket,
  purpose) -> identity | None` — accepts on success, closes with 4404/4401/4403/4408 otherwise;
  `identity.credential` is `"token"`, `"ticket"` or `"internal"`; purposes `web-pty`,
  `web-events`, `web-rpc`; plus `internal_credential() -> str` and the bound loopback base URL
  (`ws://127.0.0.1:<port>`) for the publish URL.
- The TUI renderer (TS) does not publish to the sidecar yet: it must read `INDUS_TUI_SIDECAR_URL`
  and `INDUS_TUI_SIDECAR_CREDENTIAL` and offer the credential the way the gate expects (follow-up
  for the TUI owner once W1's credential form is final).
- `src/indus/web/ws/__init__.py` is W1's (P18-06); this slice does not create it (the modules import
  as a namespace portion until it lands). The gate protocol, purposes and `SidecarLink` live in a
  new `src/indus/web/ws/chat_gate.py`; at integration point it at W1's helper (or move the
  Protocols into W1's module) and mount `PtyRoute.endpoint`, `EventsRoutes.publish_endpoint` /
  `events_endpoint` and `RpcSidecar.endpoint` at `/api/pty`, `/api/pub`, `/api/events`, `/api/ws`
  (each endpoint is tagged with its purpose: `chat_gate.purpose_of`).
- App wiring owed (P18-01): one `PtyRegistry(clock=…)` per backend with `run_reaper()` started and
  `close_all()` at stop; one `ChannelFiles()` (closed at stop); one `EventsFanout`; `install(app,
  ProfileScopes(base_scope))` (P18-10).
- The 1:1 path closes 4410 when the child exits (the reference closes 1000).

Compare checklist (filled after the rewrite; reopened `hermes_cli/web_routers/chat_ws.py`,
`web/src/lib/eventsFeedClient.ts`, `hermes_cli/web_server_chat.py`):
- [x] every chat socket passes the gate first, purpose per route
  (test: `tests/contract/web/test_pty_route.py::test_the_gate_decides_first`,
  `test_events_fanout.py::test_routes_carry_their_gate_purpose`, `test_rpc_sidecar.py::test_frames_are_relayed_both_ways`)
- [x] no PTY support / unknown profile / missing Node / bad cwd → red text + 1011
  (test: `test_pty_route.py::test_failures_show_red_text_and_close_1011`)
- [x] invalid channel = no channel; implicit resume announced only when it differs from the asked
  one and not fresh (test: `test_pty_route.py::test_implicit_resume_is_announced_explicit_is_not`)
- [x] child argv/env (test: `test_pty_route.py::test_child_env_comes_from_the_builder`)
- [x] profile switch on the same token ends the old PTY (test: `test_pty_route.py::test_a_profile_switch_ends_the_token_s_old_pty`)
- [x] fan-out: every publisher frame to every subscriber of the channel; subscribers never speak;
  empty channels dropped; bad channel 4400
  (test: `test_events_fanout.py::test_a_child_s_three_events_reach_both_subscribers`,
  `test_events_fanout.py::test_subscribers_never_speak`, `test_events_fanout.py::test_only_the_internal_credential_may_publish`)
- [x] sidecar: same contract relayed; identity from the credential only; loopback token refused in
  gated mode (test: `test_rpc_sidecar.py::test_privileged_calls_need_a_person_s_credential`,
  `test_rpc_sidecar.py::test_gated_mode_refuses_the_loopback_token`)
- [x] gap found: the reference's PTY child gets mouse tracking off — added (`INDUS_TUI_MOUSE=off`)
- Deliberate differences: the publish URL carries no credential (env variable instead); `/api/pub`
  accepts only the internal credential; per-subscriber queues (slow tab closed 1013) instead of a
  sequential broadcast; the 1:1 path closes 4410 when the child exits (the reference closes 1000) so
  the client shows "start a new session" either way; the sidecar relays to the daemon over P17
  instead of serving an in-process dispatcher, so there is no deferred MCP discovery to kick.
- Not taken: `/api/console` (P18 "later" purpose `web-console`); the desktop's native one-use
  grant header (D-16.89: the desktop uses the daemon WS and `indus serve`).

Integration (P18 integrator, 2026-10-20):
- The stand-in gate is gone: each route's endpoint is `guarded_ws(purpose, allow_internal=…)` over
  its handler `(websocket, grant)` — `/api/pty` `web-pty`, `/api/events` `web-events`, `/api/pub`
  `web-pub` (internal only), `/api/ws` `web-rpc` (internal allowed) (D-16.581). The contract tests
  run against the real app (`tests/contract/web/chat_rig.py`); `tests/integration/web/
  test_chat_routes_gate.py` dials every route allowed and refused.
- The credential is a subprotocol, never the URL (D-16.580); the fake TUI offers
  `indus-dashboard-internal.<value>`; the real TUI must do the same (follow-up).
- Routes serve the grant's profile (`chat_gate.scope_of`, D-16.582); an unknown named profile still
  ends in red text + 1011.
- Wiring per this note's list in `web.ws.chat_routes` (D-16.583).

## Status

Done (2026-10-20). Shared: `src/indus/cli/tui_launch.py` (`INDUS_TUI_DASHBOARD_SESSION_FILE`,
~20 lines) and two tests in `tests/unit/cli/test_tui_launch.py`. W1's gate and app factory are
stood in by `tests/contract/web/_stubs_w2.py`.
