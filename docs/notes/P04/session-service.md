# Task: SessionService + in-process host (P04-21) Phase: 04 Owner: wave-6a builder Z (lock + broker); wave 6b (service + host)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`):
`website/docs/developer-guide/gateway-session-lifecycle.md` (skimmed for what a host owns:
restart recovery markers, busy-session queueing, interrupt fan-out; no names or text taken).
Research `01-agent-core.md` §3.23 (admission, lease, liveness — design only) and R01.4;
`05-tui.md` R05.3. Plan: `16-what-we-change.md` D-16.69, `P17-daemon-shared-backend.md` rows P17-03 and
P17-05 (what the lease and the fences must later do). Ours: `docs/notes/P04/protocol-models.md`,
`docs/notes/P02/deletion.md`, `src/indus/state/holders.py`, `src/indus/state/fts.py` (rebuild lock).
Closed before writing.
Evidence: none.

Scope of this note: slice Z of wave 6a builds the two parts with no loop dependency — the
per-session advisory lock (`state/session_lock.py`) and the server-request broker
(`service/broker.py`, with its port in `protocol/requests_port.py`). `SessionService` and
`InProcessHost` are wave 6b; their section below is a plan, marked "not built".

## Part 1 — per-session advisory lock (built)

What it does (own words): until P17 brings the durable turn lease, the only thing that stops two
surfaces (a REPL and a TUI started from one shell, or two REPLs) from running turns on the same
session is this lock. It is a plain file under the store's folder that a process holds an
exclusive, non-blocking OS lock on while it runs that session. Asking for a session that someone
else holds does not wait: it comes back at once as "busy", which the service turns into the
protocol's `SESSION_BUSY` (3001). The kernel drops the OS lock when the holding process dies, so
a crash never strands the session; the file itself also names its holder so a contended lock whose
recorded holder is provably dead (an inherited descriptor kept by a forked child) can be broken.

Inputs → outputs:
- `lock_dir_for(store_path) -> Path` → `<store folder>/session-locks`.
- `try_lock_session(directory, session_id, *, owner="") -> SessionLock | LockBusy` (never waits).
- `lock_session(...) -> SessionLock` (raises `SessionBusy`, code `session.busy`).
- `SessionLock`: `session_id`, `path`, `recovered_from` (stamp of a dead previous holder, if
  any), `held`, `release()` (idempotent), context manager.
- `LockBusy`: `session_id`, `holder` (stamp, may be None), `owner`, `in_process`, `error()` →
  `SessionBusy`.

File: one per session, name = the id itself when it is lowercase `[a-z0-9._-]{1,128}`, else
`h-<sha256 prefix>` (case-insensitive file systems, odd characters). Content: line 1 the holder
stamp from `state.holders.holder_stamp()`, line 2 an optional owner label (for "busy: held by
the TUI in pid 123").

Rules:
1. In-process table first: a session held by this process (any thread, any lock object) is busy
   without touching the file, and the answer is `in_process=True`. This also covers file systems
   where OS locks do not work.
2. Open (create, mode 0600, close-on-exec) and take the OS lock without blocking
   (`fcntl.flock(LOCK_EX|LOCK_NB)`; Windows: `msvcrt.locking(LK_NBLCK)` on byte 0 — sketched,
   not exercised here).
3. Got it → check that the open file is still the one at the path (a releaser or a breaker may
   have unlinked it meanwhile); if not, reopen and retry (bounded). Then read the old content: a
   stamp whose holder is provably dead is reported as `recovered_from` (a crash), and write ours.
4. Contended → read the stamp. Holder provably dead in our PID namespace
   (`holders.holder_process_is_dead`) → unlink the file and retry once with a fresh file (the old
   descriptor lives on in some orphan, but it no longer guards anything). Otherwise busy.
5. OS lock unsupported (`ENOLCK`, `EOPNOTSUPP`, …) → warn once, hold by the in-process table only.
6. Release: unlink the path (POSIX, only if it is still our file) then unlock and close; the
   in-process entry goes last. Release is idempotent and also runs from `__exit__`; process exit
   needs no hook because the kernel drops the lock.

Edge cases / failure modes:
- the same thread asks twice → busy (`in_process=True`); this is a session lock, not re-entrant.
- a stamp that does not parse, from another host or another PID namespace (container on a shared
  volume) → never treated as dead (R03.20 via `holders`).
- release racing a new acquirer that opened the old file → the acquirer sees a different inode
  after locking and reopens (rule 3).
- unreadable lock folder → the `OSError` propagates (a real fault, not "busy").
- Windows cannot unlink an open file: the file stays, the byte lock is what counts.

## Part 2 — server-request broker (built)

What it does (own words): the agent sometimes has to ask the person something (approve a command,
answer a clarifying question, give a sudo password or a secret). The broker turns each question
into a server request with its own id, sends it to the client, and parks the asking code on a
future until the matching answer arrives. It refuses kinds the client never said it can answer
at once with `SERVER_REQUEST_UNSUPPORTED` (R05.3: otherwise the agent waits out a long deadline
with nothing on screen). When the turn is interrupted or ends, every open request is withdrawn
with exactly one `server_request.cancelled` event each, so cards close. It also keeps a stopwatch
of how long the agent sat waiting on a person, which the tool executor (P04-14) subtracts from its
batch deadlines.

Port (protocol layer, so `core` and `tools` can depend on it — they sit below `service`):
`indus.protocol.requests_port`:
- `ServerRequestPort` (typing `Protocol`): `async ask(kind, payload, *, timeout=None,
  turn_id=None) -> AnswerModel`; `cancel_all(reason) -> int`; `human_wait_seconds() -> float`.
- Errors, all `RequestNotAnswered`: `RequestWithdrawn(request_id, reason: CancelReason)`;
  `RequestUnsupported(kind, error: ErrorObject)`.
- `AnswerModel` = union of the four answer models.

Broker (`indus.service.broker.ServerRequestBroker`), one per session:
- `__init__(*, session_id, deliver, emit, next_seq, clock, answerable_kinds=(), new_id=...)`:
  `deliver(request)` sends the request notification; `emit(event)` is the session's event sink;
  `next_seq()` gives the session's next event seq; `clock.monotonic()` drives the timers and the
  stopwatch, `clock.sleep()` the timeout.
- `set_answerable_kinds(kinds)` — what the client declared; empty by default (R05.3: never send a
  request a client did not declare).
- `ask(...)`: unknown kind → `ValueError` (programming error); payload checked by building the
  request model (pydantic error propagates); kind not answerable → `RequestUnsupported` at once,
  nothing sent; closed broker → `RequestWithdrawn(SESSION_CLOSED)`. Otherwise registers the
  pending entry, calls `deliver` (a failing deliver is logged; the request stays open for
  re-delivery on resume), and waits for the answer, the timeout (`deadline_s` = timeout) or a
  withdrawal. Timeout → one cancelled event (`timed_out`) and `RequestWithdrawn`. The asking task
  being cancelled (hard interrupt) → the request is withdrawn (`interrupted`) and the
  `CancelledError` propagates.
- `answer(request_id, answer)` (mapping or model) → `ServerRequestAnswerResult(accepted=True)`.
  Unknown / already answered / withdrawn → `ProtocolError(SERVER_REQUEST_NOT_FOUND)`; malformed →
  `INVALID_PARAMS`; other kind → `ANSWER_KIND_MISMATCH`; approval choice not offered, clarify key
  that is not a question id, several values for a single-select question → `INVALID_PARAMS`.
  Clarify questions the answer leaves out are filled as skipped (`[]`).
- `cancel_all(reason)` → count withdrawn; events in the order the requests were made.
- `close()` = `cancel_all(SESSION_CLOSED)` and refuse new asks.
- `pending_requests()` → open request models, oldest first (for `session.resume`).
- `human_wait_seconds()` → total time with at least one request open (overlaps counted once),
  including the open stretch; `waiting` → any request open.

Edge cases: the single gate is "pop from the pending table": whoever pops (answer, timeout,
cancel, asker cancelled) owns the outcome, so an answer racing a cancel either lands or is
NOT_FOUND and the event is sent exactly once. A raising sink is logged, never fatal. Loop-bound:
every method runs on the session's event loop (the in-process host marshals calls onto it).

## Part 3 — SessionService and InProcessHost (wave 6b, not built)

Plan for 6b: `SessionService` holds per session a `SessionLock` for the life of an attachment
(taken at `create`/`resume`, `SESSION_BUSY` when another surface has it; `submit` while a turn
runs → `SESSION_BUSY` too), a broker wired to the session's event sink and seq counter, the
turn task and its generation; `answer_server_request` routes to the broker; `interrupt` checks
the generation, cancels the task and calls `broker.cancel_all(INTERRUPTED)`; turn end calls
`cancel_all(TURN_ENDED)`. `IndusError` codes map through `error_for_indus_code`
(`session.busy` → `SESSION_BUSY`). `InProcessHost` owns a loop thread and marshals every call
with `run_coroutine_threadsafe`.

## Design for P17 — turn lease and generation fences (design only)

Turn lease (replaces this file lock for daemon and `--local`, D-16.69):
- A row per session in the store (`turn_leases` already exists in schema 1:
  `conversation_id, holder, acquired_at, expires_at`; P17 adds `generation` and a holder id).
  The holder value is the same stamp format as the file lock, so `holders.holder_is_stale`
  decides takeovers in both.
- Acquire in one write transaction: delete a stale row (expired, or holder provably dead), then
  insert-or-ignore, then read back and compare holder; bump `generation` on every new holder.
- Refresh on a timer well inside the TTL; a refresh that finds another holder, or cannot write
  until past expiry, raises `store.turn_lease_lost` into the turn (interrupt reason `lease_lost`).
- Waiting: a second holder polls with backoff and shows a "waiting for another run" status; input
  that arrives while waiting is kept for the turn.
- Migration: `session_lock` stays only for stores without the lease columns; the service takes the
  lease where it exists and the file lock otherwise, never both.

Generation fences:
- Every turn gets `generation = previous + 1` (per session, from the lease row once it exists;
  in-process counter until then). `turn.started` and `prompt.submit` already carry it.
- Every control names a generation: interrupt, steer, server-request answers (via the request's
  turn), settle/release. A control for a generation that is not the running one returns
  `stale_generation` with the current one and does nothing; it never falls through to "the latest
  turn" (R01.4). P04's `session.interrupt` treats a stale generation as a no-op already.
- Releases are conditional on (holder, generation), so a displaced turn's late release cannot free
  its successor's lease (`DELETE … WHERE conversation_id=? AND holder=? AND generation=?`).
- The broker withdraws by turn: `cancel_all` is called with the ending turn; P17 may scope it with
  `turn_id` so a late cancel of turn N never withdraws turn N+1's requests.

Files: `src/indus/state/session_lock.py`, `src/indus/service/broker.py`,
`src/indus/protocol/requests_port.py` (new), `tests/unit/state/test_session_lock.py`,
`tests/integration/state/test_session_lock_process.py`, `tests/unit/service/__init__.py`,
`tests/unit/service/test_broker.py`. Shared, one line each: `src/indus/foundation/errors.py`
(`session.busy` row), `src/indus/protocol/errors.py` (`session.busy` → `SESSION_BUSY`).

Our design differences / decisions:
- The lock is an OS advisory lock plus a holder stamp; the stamp is only used to explain "busy"
  and to break a lock kept by an orphaned descriptor, never to decide liveness on its own.
- Unanswerable kinds raise at the asker instead of sending a request and waiting; the caller picks
  the fallback (an approval tool denies, clarify proceeds without answers).
- Human-wait accounting is a union stopwatch on the broker, not per request, so overlapping asks
  in a parallel batch are not double-counted.
- No "answered elsewhere" fan-out yet (multi-viewer is P17).

Tests we will write:
- lock: acquire/release/reacquire; busy in process (same and other thread) with holder and owner;
  `lock_session` raises with code `session.busy` mapped to `SESSION_BUSY`; file name rules;
  stale stamp of a dead pid reported as recovered; contended lock with a dead recorded holder is
  broken; foreign namespace or unparsable stamp is not broken; unsupported OS lock degrades;
  release idempotent and removes the file.
- lock, second process: child holds → busy naming the child pid; child SIGKILLed → acquire
  succeeds and reports the dead holder; child exits cleanly → free.
- broker: answer resolves ask; unsupported kind raises at once with 4002 and sends nothing;
  timeout withdraws with one `timed_out` event; `cancel_all` emits one event per pending, in
  order, and is idempotent; asker task cancelled → one `interrupted` event; answer validation
  table (unknown id, double answer, kind mismatch, malformed, choice not offered, unknown question,
  multi value on single select); missing clarify answers filled; human-wait stopwatch with
  overlapping requests on a fake clock; failing sink and failing deliver are not fatal; pending
  list for resume; close refuses new asks; the broker satisfies the port.

## Decisions for the lead
- The port lives in the protocol layer (`indus.protocol.requests_port`), not in
  `indus.service.broker`: `core` and `tools` sit below `service` in the layer contract, so the
  loop (and P05's approval tool) cannot import `indus.service.broker`. `service.broker`
  re-exports the port names. Change: move the port to any layer at or below `tools`.
- New product code `session.busy` (`SessionBusy`, defined in `state/session_lock.py`) mapped to
  `SESSION_BUSY` in `protocol.errors._INDUS_EXACT`. No locale copy yet: the CLI text for it lands
  with the surface that shows it (6b / P06).
- Lock files live in `<store folder>/session-locks/`, one per session, removed on release.
- The broker's default answerable set is empty (a client must declare the kinds it answers); the
  in-process REPL host will declare all four.

Compare checklist (filled after the rewrite; reopened
`website/docs/developer-guide/gateway-session-lifecycle.md` §7, §8, §10, §11):
- [x] One running turn per session at a time, whichever surface asks (test:
  `tests/unit/state/test_session_lock.py::test_second_take_in_this_process_is_busy`,
  `::test_racing_threads_get_exactly_one_lock`,
  `tests/integration/state/test_session_lock_process.py::test_the_child_finds_our_session_busy`).
- [x] A crashed holder never strands the session (test:
  `test_session_lock_process.py::test_busy_while_the_child_holds_then_free_after_it_is_killed`,
  `test_session_lock.py::test_a_crashed_holder_is_reported_as_recovered`).
- [x] Liveness judged only inside our PID namespace and host (test:
  `test_session_lock.py::test_holders_we_cannot_judge_keep_the_lock`).
- [x] Interrupting a busy session also stops what waits on a person: pending requests withdrawn,
  one event each (test: `tests/unit/service/test_broker.py::test_cancel_all_sends_one_event_per_open_request_in_order`,
  `::test_a_cancelled_asker_withdraws_its_request_once`).
- [x] R05.3: no request for a kind the client cannot answer; deadlines withdraw instead of
  stalling (test: `test_broker.py::test_kinds_the_client_cannot_answer_fail_at_once`,
  `::test_a_client_that_declared_nothing_gets_no_requests`, `::test_a_timeout_withdraws_the_request_once`).
- [x] Open requests survive a reconnect (test: `test_broker.py::test_open_requests_ride_along_on_resume`).
- Deliberate differences: the reference's host keeps per-session follow-up slots and a FIFO
  queue for messages that arrive while a turn runs; across surfaces we answer "busy" instead,
  and inside one surface follow-ups are steer (P04-19); a durable queue is P17's admission
  ledger. The reference recovers crashed turns from durable turn markers and a clean-shutdown
  marker; our lock only reports the dead holder (`recovered_from`) — turn-level recovery is P17.
- Not taken: agent cache, idle eviction, memory-pressure shedding, session-key rules (gateway,
  P15); background-unit stop fan-out (P10).

## Status

Partial (2026-10-18). Slice Z done: per-session lock, broker and its port, tests. Shared one-line
changes: `src/indus/foundation/errors.py` (`session.busy`), `src/indus/protocol/errors.py`
(`session.busy` → `SESSION_BUSY`). Handed on to wave 6b: `SessionService`, `InProcessHost`,
`tests/unit/service/test_session_service.py`, `tests/integration/service/test_inprocess_host.py`
(the row's "sync caller drives and interrupts a turn" and "two services on one session" verify
items, using the lock and the broker built here).
