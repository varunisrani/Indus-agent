# Task: Subscriptions and cross-surface events (P17-14) Phase: 17 Owner: wave-26 builder K5

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/local_viewer_events.py` (all:
which turn callbacks become viewer events), `gateway/session_events.py` (all: per-session ordering,
overflow retirement and the gap frame, `since`). Research 05 §3.2 (shared channel: subscribed flag,
replay cursors) and §3.3. Our P17-06 `service/event_log.py`, P17-07 `request_redelivery.py`,
P17-08 `daemon_host.py` / `viewers.py`. Closed before writing.
Evidence: none.

What it does (own words): one client connection may watch several sessions. `session.subscribe
{session_id, epoch?, cursor?}` makes the connection a viewer of the session (resuming it in the host
if needed): the events after the client's cursor are sent first, in seq order, as ordinary event
notifications (or one `session.replay_gap` when they cannot be — another daemon run, evicted, ahead
— and the client reloads a snapshot), every open server request it can render is offered again with
`replayed: true`, and from then on every new event of the session reaches it in the event log's
order. `session.unsubscribe` stops that. `events.since {session_id, epoch, cursor}` is the pull form
(events in the result). `sessions.watch` turns on `session.updated` notifications for list views:
title, running state and last activity of every hosted session (and archived / pinned when a later
task reports them), whatever surface caused the change. Presence: whenever the set of viewers of a
session changes, a `session.viewers` event goes through the session's log with the surface kinds
and counts, so a surface can show "also open in the TUI".

A connection only sees, controls and replays sessions it attached (created, resumed or
subscribed); any other session id reads as unknown (`SESSION_NOT_FOUND`). Watching the list is
open to every attached connection (the same rows `session.list` shows; no transcript content).

A slow viewer never blocks the others: each connection has its own outbox, and each of its
viewers may hold at most 1,024 queued events (and 4 MiB). When a viewer would exceed that, its
queued events are dropped, it is detached from that session and it receives one
`session.replay_gap {reason: "overflow", last_seq}`; it recovers by subscribing again (replay from
its cursor if the log still has it, else a snapshot). List updates are merged per session while
queued (the newest values win), so they cannot pile up.

Inputs → outputs:
- `Outbox(limits=…)`: `put_event(session_key, frame)`, `put(frame)`, `get()`; overflow → the gap
  frame and the `on_overflow(session_key)` hook; `oldest_wait()` for the send deadline.
- `Subscriptions(host, connection_id, outbox, watch_hub, surface=, capabilities=)`:
  `subscribe(session_id, epoch, cursor)`, `unsubscribe(session_id)`, `since(session_id, epoch,
  cursor)`, `adopt(session_id)` (after create/resume), `require(session_id)` (authorisation),
  `watch(on)`, `close()` (socket closed: detach everything).
- `SessionWatch(host)`: listens to the host's event log; `session.updated {session_id, title?,
  running, last_activity, archived?, pinned?}`; `report(session_id, **fields)` for changes that do
  not travel as events.
- `presence(host, key)`: `session.viewers {viewers: [{surface, count}], count}` into the log.

Files: `src/indus/service/subscriptions.py`, `tests/unit/service/test_subscriptions.py`.

Edge cases / failure modes:
- subscribe twice from one connection → one viewer (replaced), the replay starts from the new cursor;
- subscribe with no cursor → no replay, live from now on (`last_seq` in the result is the cursor);
- a stale epoch / evicted cursor → `session.replay_gap` first, live events after it;
- events numbered between attach and replay cannot be lost or doubled: attach, the replay read and
  the queueing happen in one step on the daemon loop;
- overflow of one viewer leaves the connection's other sessions and the other viewers untouched;
- a session closed by the host (ACP end, daemon stop) drops its viewers; later calls answer unknown;
- unsubscribe of the last viewer of an ACP session follows the host's close rule.

Our design differences / decisions:
- Our names; subscriptions keyed by connection id (one per connection and session, as P17-08).
- The overflow gap reuses `session.replay_gap` with reason `overflow` (the log's own reasons are
  `epoch_changed`, `evicted`, `ahead`) and the viewer is detached, not left looking subscribed.
- Presence travels as a numbered event of the session (ordered with its turns, replayable).
- `session.updated` is sent for running-state and title changes (not for every delta).

Tests we will write (`tests/unit/service/test_subscriptions.py`):
- viewer A submits, viewer B (subscribed) receives every delta in seq order, same as A;
- subscribe with a cursor replays the missing events once, then live, no duplicates;
- epoch change / evicted cursor → `session.replay_gap` then live;
- overflowing viewer gets `replay_gap{overflow}`, is detached, the other viewer keeps everything;
  resubscribing recovers (snapshot via history / replay);
- a title change and a turn's running state reach a list watcher (`session.updated`), merged;
- presence: `session.viewers` counts surfaces as viewers come and go;
- per-connection authorisation: unknown to a connection that did not attach;
- an open approval is re-offered on subscribe with `replayed: true` and the same id.

## Decisions for the lead
- `session.updated` / `session.viewers` / `session.replay_gap` stay plain notifications until
  P17-25 declares their models (shapes above). `sessions.watch {enabled}` turns the list feed on
  and off; nothing replays for it (list views reload with `session.list` on reconnect).
- Per-viewer caps: 1,024 events and 4 MiB queued; per connection 16 MiB queued in all and at most
  64 subscribed sessions (the 65th subscribe is refused `INVALID_PARAMS`).

- List updates that pile past 1,024 distinct sessions are dropped and one `sessions.resync
  {reason: "overflow"}` notification is sent instead (reload with `session.list`).
- Presence is announced by subscriptions (subscribe, unsubscribe, overflow, socket close); a viewer
  attached through `DaemonSessionHost.create/resume(viewer=)` directly (not through a connection's
  subscriptions) is counted but does not trigger an announcement.
- Recovery after an overflow: the replay itself can be large; with the default caps (log keeps at
  most 512 events per session, a lane holds 1,024) a full replay always fits a fresh lane.

Compare checklist (filled after the rewrite; reopened `gateway/session_events.py`,
`gateway/local_viewer_events.py`):
- [x] overflow retires the viewer's subscription and sends a gap with the latest seq (test:
  `test_subscriptions.py::test_an_overflowing_viewer_drops_to_a_gap_and_recovers_by_subscribing_again`)
- [x] since: epoch mismatch, cursor ahead, truncated → snapshot required (test:
  `test_subscriptions.py::test_another_epoch_or_an_evicted_cursor_gives_a_gap_then_live_events`)
- [x] ordering: events reach every viewer in publication order (test:
  `test_subscriptions.py::test_a_subscriber_sees_every_delta_of_another_surfaces_turn_in_order`)
- [x] replay then live without loss or duplicates (test:
  `test_subscriptions.py::test_subscribe_from_a_cursor_replays_once_then_continues_live`)
- Deliberate differences: the reference keeps a private lifetime key per session event stream and
  rotates the epoch when its shared cache evicts a session; ours has one daemon epoch and the
  event log's "evicted through" watermark gives the gap instead (P17-06). The reference's gap
  payload says `latest_seq`; ours `last_seq` with a reason.
- Not taken: `local_viewer_events.py` projects agent callbacks (tool-generating lines, printed
  status, kawaii thinking text, reactions, MoA progress, desktop UI bridges, edit diffs) into
  viewer events; our viewers receive the core's own event stream (P04/P12 events), and those
  extra projections belong to the surfaces that render them (P18/P20), not to the subscription
  layer.

## Status

Done (2026-10-20). New: `src/indus/service/subscriptions.py`, `tests/unit/service/test_subscriptions.py`,
`tests/fakes/session_host.py`. Served over the WebSocket by P17-13 (`gateway/ws/methods.py`).
