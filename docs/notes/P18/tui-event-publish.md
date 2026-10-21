# Task: The TUI publishes its events to the dashboard (`/api/pub`) when the dashboard starts it (P18-09 / P18-26 follow-up) Phase: 18 Owner: wave-33 builder F1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/gatewayClient.ts` (the
sidecar mirror: where the URL is read, that each frame is copied best-effort), `tui_gateway/entry.py`
(`_install_sidecar_publisher`: the Python side tees its frames instead). Our side:
`src/indus/web/ws/{__init__,events_fanout,chat_gate,pty_route}.py`, `service/ws_auth.py`
(`dashboard_offer`, internal credential), `tests/fakes/pty/fake_tui.py`,
`tests/contract/web/{chat_rig,test_events_fanout}.py`, the incoming SPA feed
`apps/web/src/chat/eventsFeed.ts` (in `.agents/wip/w32/w32-p2.patch`: `parseFeedFrame` accepts
`{type, …}` or `{method, params}`), `apps/tui/src/entry.tsx`, `packages/client/src/{client,
localEvents,ws-transport}.ts`. Reference closed before writing.
Evidence: none.

What it does (own words): when the dashboard's Chat tab starts the TUI (`/api/pty`), the child gets
`INDUS_TUI_SIDECAR_URL` (`ws://…/api/pub?channel=<id>`, no credential) and
`INDUS_TUI_SIDECAR_CREDENTIAL`. The TUI owns the event stream in both of its modes (attached to the
daemon over WebSocket, or the local bridge child over stdio): every event reaches `IndusClient`'s
subscribers. A new subscriber, the dashboard feed, opens one WebSocket to that URL offering
`indus-dashboard-v1` and `indus-dashboard-internal.<credential>` as subprotocols and sends each
agent event as one JSON text frame (`{"type": "...", ...}`, the shape the rail parses). The
dashboard fans it out to the channel's `/api/events` subscribers.

Rules:
- Only protocol events are published: the client's own `transport.*` events (stderr lines, exit
  codes, attach failures) and `gateway.ready` stay inside the TUI.
- The credential is read once at start and removed from `process.env`, so the bridge child and
  every tool process it starts never inherit it; it is never put in the URL, a log line, an event
  or an error text. A URL that carries user-info or a fragment, or is not `ws:`/`wss:`, is refused
  (no feed).
- Best-effort: frames sent before the socket opens wait in a queue of at most 256 (the oldest are
  dropped); a frame larger than 1 MiB is skipped (the server would close the socket); after a drop
  the feed dials again with 1 s, 2 s, 4 s … 30 s waits, at most 8 tries per outage; close codes
  4400/4401/4403 (bad channel, refused credential, forbidden) stop it for good. The terminal never
  shows any of this.
- It closes with the TUI (the lifecycle's bridge stopper).

Inputs → outputs: env (`INDUS_TUI_SIDECAR_URL`, `INDUS_TUI_SIDECAR_CREDENTIAL`) + client events →
text frames on `/api/pub`.

Files: new `apps/tui/src/transport/dashboardFeed.ts` (+ `dashboardFeed.test.ts`), shared
`apps/tui/src/entry.tsx` (wiring), new `tests/contract/web/test_tui_publish.py` (the real route, the
TS module run by Node with a small driver `tests/fakes/pty/feed_driver.ts`).

Edge cases / failure modes:
- no URL, or a URL without a credential: no feed (the server would refuse it anyway);
- the dashboard went away: retries then stops; the TUI keeps working;
- a slow dashboard: frames queue in the socket's buffer; above 4 MiB buffered the feed drops
  frames rather than growing.

Our design differences / decisions:
- The TS TUI publishes (it sees the events of both transports), not the Python bridge; the
  reference tees in both places.
- Parsed events are re-serialised (`JSON.stringify(event)`), not the raw wire frames.

Tests we will write:
- vitest: env plan (no URL / no credential / a URL with user-info → none; protocols in order; the
  credential leaves `process.env`); transport events filtered; queue before open, cap and oldest
  dropped; oversize skipped; retry with backoff; stop codes; the credential in no error text;
- Python contract: Node runs the module against the real dashboard app's `/api/pub`; a subscriber
  of the channel receives the events in order, `transport.*` not among them; a subscriber of
  another channel nothing.

## Decisions for the lead
- At most 8 reconnect tries per outage (the child dies with its PTY anyway).

Compare checklist (filled after the rewrite; reopened `ui-tui/src/gatewayClient.ts` sidecar part):
- [x] the URL comes from the env, the mirror is best-effort (test: `dashboardFeed.test.ts`)
- [x] nothing reaches the terminal on failure (test: `dashboardFeed.test.ts` "never throws")
- Deliberate differences: the reference passes the credential in the URL's query and redacts it
  from logs; ours is a subprotocol and never in the URL (D-16.580). The reference mirrors raw
  frames; we publish parsed protocol events only.
- Not taken: the Python-side tee.

## Status

Done (2026-10-21).
