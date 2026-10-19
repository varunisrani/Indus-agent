# Task: Event log and replay (P17-06) Phase: 17 Owner: wave-23 builder K2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tui_gateway/event_replay.py` (all:
stamping, ring, byte caps, eviction, truncation watermark, epoch), `tui_gateway/ws.py` (lines
95–240 only: which event types are coalesced and the 33 ms window). Research 05 §3.13 (replay
ring numbers) and §3.6 (attach hold). Closed before writing.
Evidence: none.

What it does (own words): the daemon gives every event of a session a sequence number that only
grows (`seq`, starting at 1 per session) and stamps it with the daemon's replay epoch (a random
token made once per daemon start). It keeps the newest events of each session in memory so a
client that lost its socket can ask "what happened after seq c in epoch e?" and get exactly the
missing events, in order. When it cannot answer truthfully — the epoch is another daemon's, or
some events after the cursor were already dropped from memory — it says so (`replay_gap`) and the
client reloads a snapshot (transcript, open requests, running state) instead of trusting a replay
with holes.

Memory is bounded: per session at most 512 events and 4 MiB of serialized payload; at most 64
sessions; at most 64 MiB in the whole process. When the process total is over, events are dropped
from the session that has been quiet the longest first; when there are too many sessions, the
quietest session's buffer goes entirely. Dropping events moves the session's "evicted through"
watermark, which is how a gap is detected. A session's counter and watermark outlive its buffer,
so a session that comes back keeps counting up instead of restarting at 1 under a cursor a client
still holds. An event bigger than the per-session cap is never kept; it moves the watermark.

Streaming text (assistant and reasoning deltas) is coalesced before it is buffered: deltas of the
same kind and turn that arrive within 33 ms of the first one held are joined into one event; any
other event (or a different kind/turn) flushes the held delta first, so order is kept and the
joined text equals the concatenation of the pieces. A timer (`flush_due` / `run_flusher`) flushes a
held delta once its window is over.

Inputs → outputs:
- `EventLog(clock=…, epoch=None, limits=ReplayLimits(), coalesce_window=0.033)`:
  - `append(session_id, event)` (protocol event model or mapping with a `type`) → list of
    `LoggedEvent` made durable in the buffer by this call (zero when the delta is being held);
  - `flush(session_id=None)`, `flush_due()`, `next_flush_at()`, `async run_flusher()`;
  - `since(session_id, epoch, cursor)` → `Replay{events, last_seq}` or
    `ReplayGap{reason: epoch_changed | evicted | ahead, last_seq}`;
  - `last_seq(session_id)`, `stats()`, `add_listener(fn) -> remove` (each flushed event, in seq
    order, outside the lock), `forget(session_id)` drops the buffer but keeps the counters.
- `LoggedEvent{session_id, seq, size}` with `to_wire()` → the event dict with `seq` and
  `replay_epoch` set by the log.

Files: `src/indus/service/event_log.py`, `tests/unit/service/test_event_log.py`.

Edge cases / failure modes:
- cursor 0 on a fresh session → every kept event; cursor == last → empty replay.
- cursor below the watermark (evicted) → gap; cursor above last seq (client from a broken
  timeline) → gap `ahead`; epoch different or missing → gap `epoch_changed`.
- the incoming event's own `seq` is ignored: the log owns numbering (the per-attachment
  `EventSink` counter restarts on every attach).
- payload frozen to JSON bytes when appended, so later changes to the caller's object cannot
  change a buffered event or its byte count.
- a listener that raises is logged and skipped.
- appends from a worker thread are safe (one lock around counters and buffers; JSON encoding
  outside it); `run_flusher` must run on the loop that owns the log's timer.

Our design differences / decisions:
- Names: `EventLog`, `ReplayLimits`, `Replay`, `ReplayGap`, `replay_gap` reason strings.
- One log object per daemon (not module globals); the epoch can be passed in so the daemon shares
  it with `gateway.ready` and `describe`.
- Coalescing happens before numbering (one seq per joined delta), so replay and live delivery
  carry the same joined events; the reference coalesces only on the socket writer.
- Eviction under the process cap takes from the least recently active session (the reference
  takes from the oldest-inserted one); spec wording "oldest sessions evicted first".
- `since` returns a gap value instead of a truncated flag next to a partial list.

Tests we will write:
- seeded random interleavings (hypothesis is not installed; same approach as P15-14): appends of
  deltas and other events across several sessions, a reader that alternates `since` and live
  delivery → strictly increasing seqs with no gaps or duplicates, joined text preserved.
- epoch change → gap; per-session count and byte caps evict oldest and produce a gap for an old
  cursor; process byte cap evicts from the quietest session; 65th session drops the quietest
  buffer but keeps its counter; oversized event not kept.
- coalescing: same turn within the window joined; window over → new event; other event flushes
  first; different turn / kind not joined; `flush_due` with a fake clock.

## Decisions for the lead
- `Replay`/`ReplayGap` are service values; P17-25 declares `session.replay_gap` and
  `events.since` on the wire.
- Counters for evicted sessions are kept for the life of the daemon (one int pair per session id
  seen), as the reference does; a cap can be added if long-lived daemons see unbounded session ids.

Compare checklist (filled after the rewrite; reopened `tui_gateway/event_replay.py`,
`tui_gateway/ws.py` lines 95–240):
- [x] Per-session monotonic seq starting at 1; the server owns it (incoming seq ignored)
  (`test_event_log.py::test_seq_starts_at_one_per_session_and_ignores_incoming_seq`).
- [x] Frozen payload detached from the caller's object (`test_buffer_is_frozen_against_later_mutation`).
- [x] Ring bounds 512 events / 4 MiB per session, 64 sessions, 64 MiB per process
  (`ReplayLimits` defaults; caps pinned by `test_event_cap_*`, `test_byte_cap_*`,
  `test_process_byte_cap_*`, `test_session_cap_*`).
- [x] Evicting a session keeps its counter and raises its watermark to the latest seq
  (`test_session_cap_drops_quietest_buffer_but_keeps_its_counter`).
- [x] Oversized event never kept and leaves a watermark (`test_oversized_event_is_not_kept`).
- [x] Truncation = cursor below the watermark → gap; epoch per process
  (`test_epoch_change_is_a_gap`, random tables).
- [x] Streaming kinds coalesced 33 ms; a non-streaming event goes out behind held text, never
  ahead of it (`test_other_event_flushes_held_text_first`).
- Deliberate differences: we join the held pieces into one event before numbering (the reference
  batches frames on the socket only); the process cap takes from the least recently active
  session (the reference: oldest inserted); `since` with no epoch is a gap (the reference skips the
  epoch check when the client sends none); a cursor past the newest seq is a gap (`ahead`) rather
  than an empty replay; `forget` lets the host drop a closed session's buffer early.
- Not taken: `thinking.delta` (no such event in our protocol); the debug `replay_stats` keys are
  ours (`stats()`); module-level globals and a test reset hook (one `EventLog` object per daemon).

## Status

Done (2026-10-19). New files: `src/indus/service/event_log.py`,
`tests/unit/service/test_event_log.py` (62 tests: 22 cases + 40 seeded random tables). Wiring for
the lead: the daemon (P17-08/13) creates one `EventLog(clock=scope.clock, epoch=<instance replay
epoch>)`, feeds every session event through `append(session_id, event)` (an `EventSink` listener),
runs `run_flusher()` on its loop, serves `events.since` from `since(...)` and fans out to viewers
via `add_listener` (P17-14). `gateway.ready.replay_epoch` = `EventLog.epoch`.
