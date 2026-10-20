# Task: MCP serve — event bridge and long-poll (P19-15) Phase: 19 Owner: wave-30 builder M1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `mcp_serve.py` (the store version
gate, the queue event shape, the bridge class: baseline, poll loop, one poll, enqueue, poll and wait,
and the `events_poll` / `events_wait` handlers), `tests/test_mcp_serve.py` (bridge unit cases and the
poll end-to-end cases: baseline, new conversation, WAL commit without checkpoint, commit in the
baseline's mtime tick, one commit costs one full poll, URI-special home names). Research 09 §3.12.
Evidence: none.

What it does (own words): while `indus mcp serve` runs, a background task watches the session store
for new user and assistant messages in gateway conversations and turns each into a `message` event
in an in-memory queue. A client either polls (`events_poll`) with the cursor it last saw or
long-polls (`events_wait`), which returns as soon as a matching event exists or after its timeout.
Messages that existed when the server started are history, not events. Watching is cheap: every
200 ms the task reads the store's change counter (`PRAGMA data_version` on our read-only
connection), which moves only when another connection commits; when it has not moved nothing else
is read.

Inputs → outputs:
- start: baseline = the store's newest message id; every conversation, including one the index
  only names later, starts after it.
- tick (200 ms, injected clock): counter unchanged → nothing; changed → reload the routing index,
  read rows with id > last seen for each conversation (user/assistant, text only, live rows),
  enqueue `{cursor, type: "message", session_key, role, content ≤ 500, timestamp, message_id}` in
  row-id order (R03.14), move each conversation's last seen id.
- `events_poll {after_cursor=0, session_key?, limit=20 (1..200)}` → `{events, next_cursor}` and
  `dropped: n` when events after the cursor were pushed out of the queue.
- `events_wait {after_cursor=0, session_key?, timeout_ms=30000 (0..300000)}` → `{event}` or
  `{event: null, reason: "timeout"}`; it re-checks at most every 200 ms (and at once when the bridge
  adds an event) until its deadline.

Files: `src/indus/mcp_serve/event_bridge.py`, `tests/contract/mcp_serve/test_event_bridge.py`.

Edge cases / failure modes:
- No store at start: the baseline is empty (id 0), every tick tries to open it; once it exists all
  of its messages are new (they arrived after the start).
- The counter cannot be read (file replaced, I/O error): close, reopen next tick, poll fully once.
- A commit that only reaches the WAL, or lands in the same mtime tick as the baseline: the counter
  still moves (we never look at file times).
- One commit → exactly one index reload, however many ticks follow.
- Timestamps going backwards between rows: order and cursoring are by row id only.
- Queue cap 1,000: the oldest event is dropped; cursors keep growing (never reused), so a client
  behind the oldest kept cursor learns how many it missed (`dropped`).
- `events_wait` with timeout 0 answers at once; with a session filter only that conversation's
  events count; cursors are clamped to ≥ 0, non-numbers become the defaults.
- A route rotated by `/new` points at a new session: its rows have higher ids than anything seen,
  so they are delivered; the old session's history is not replayed.
- Stop: the task is cancelled and the connection stays with the reader (closed by the server).

Our design differences / decisions:
- Last seen is a row id per conversation, not a timestamp (R03.14; the reference compared
  timestamps). A conversation first seen after the start begins at the store's newest id at start
  (the reference began at zero, which replays the whole history of an old session the index only
  now names).
- The bridge is an asyncio task on the server's loop with an injected clock (tests use FakeClock);
  store reads run on the blocking pool. Waiters wake on an asyncio event, no threads.
- Event kinds: `message`, and from P19-17 `approval_requested` / `approval_resolved`.

Tests we will write (`tests/contract/mcp_serve/test_event_bridge.py`):
- baseline excludes history; one new message → one event (through the MCP in-memory client).
- unchanged counter → no index reload (spy on the reload), one commit → one reload over 10 ticks.
- a WAL commit from another connection is seen.
- new conversation after start is delivered; a rotated route does not replay old history.
- 1,001 events → oldest dropped and `dropped: 1` reported; cursors monotonic.
- `events_wait` returns at the timeout exactly (fake clock), wakes early on a new event,
  timeout 0 answers at once, session filter; coercion of bad values.
- rows whose timestamps regress are delivered in id order.

## Decisions for the lead
- The baseline is the store's newest message id at start for every conversation (equivalent to "each conversation's newest id" for the ones that exist, and also correct for keys that appear later).
- Compaction copies and handoff rows are not events (message-uid check against the session and its parent).
- `dropped` is computed per call from the caller's cursor (stateless): `first kept cursor − 1 −
  after_cursor` when positive. It appears in `events_poll` and `events_wait` answers only then.
- The tick interval and queue cap are constants (`POLL_SECONDS = 0.2`, `QUEUE_LIMIT = 1000`), not
  config rows.

Compare checklist (filled after the rewrite; reopened `mcp_serve.py`, `tests/test_mcp_serve.py`):
- [x] baseline before polling; history not replayed (test: `tests/contract/mcp_serve/test_event_bridge.py::test_history_is_not_replayed_and_one_new_message_is_one_event`)
- [x] later conversations delivered; a key newly pointed at an old session does not replay it (test: `tests/contract/mcp_serve/test_event_bridge.py::test_new_conversations_are_delivered_but_an_old_session_is_not_replayed`)
- [x] version gate: unchanged → no index reload; one commit → one reload over 10 ticks (test: `tests/contract/mcp_serve/test_event_bridge.py::test_an_unchanged_store_is_not_read_and_one_commit_is_one_reload`)
- [x] a commit from another connection with the file times pinned back (same-tick case; WAL where the store runs WAL) (test: `tests/contract/mcp_serve/test_event_bridge.py::test_a_commit_from_another_connection_is_seen_whatever_the_file_times`)
- [x] URI-special home names `#`, `%23`, `?` (test: `tests/contract/mcp_serve/test_event_bridge.py::test_the_change_counter_watches_the_real_store_under_a_uri_special_home`)
- [x] queue cap and `dropped`, cursor monotonic, poll limit and coercion (test: `tests/contract/mcp_serve/test_event_bridge.py::test_the_queue_keeps_the_latest_1000_and_reports_what_was_dropped`)
- [x] wait: timeout exactly, timeout 0, early wake, session filter (test: `tests/contract/mcp_serve/test_event_bridge.py::test_events_wait_answers_at_its_timeout_exactly`, `tests/contract/mcp_serve/test_event_bridge.py::test_events_wait_wakes_on_a_new_event_and_honours_the_filter`)
- [x] gap found: rows copied by a compaction (rotation clone / in-place carry) get new ids and would be replayed as news — fixed by skipping rows whose message uid already exists at a lower id in the session or its parent, and handoff rows; pinned by `tests/contract/mcp_serve/test_event_bridge.py::test_rows_copied_by_a_compaction_are_not_news`
- [x] id order across conversations when timestamps regress (test: `tests/contract/mcp_serve/test_event_bridge.py::test_order_is_the_row_id_when_timestamps_go_backwards`)
- [x] a store that appears after the start; the background task ticks on the clock (test: `tests/contract/mcp_serve/test_event_bridge.py::test_a_store_that_appears_after_the_start_is_watched`, `tests/contract/mcp_serve/test_event_bridge.py::test_the_background_task_ticks_on_the_clock`)
- Deliberate differences: last seen is a row id, not a timestamp; the baseline is one number (the store's newest message id at start) which every conversation starts after, so a conversation the index names later never replays its old session (the reference started such a key at zero); the bridge is an asyncio task on an injected clock instead of a thread; the change-counter connection is the read tools' one (one descriptor per process).
- Not taken: the reference's thread-safety locks (everything but the store read runs on one loop).

## Status

Done (2026-10-20). Event bridge, `events_poll`, `events_wait` built and tested (14 tests). No shared files beyond the P19-14 list; traps R03.14 and ACC-M10-2.
