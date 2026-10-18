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

## Part 3 — SessionService and InProcessHost (built, wave 6b builder S)

Read for this part: nothing new from the reference beyond the skim above (sections 7, 8, 11
again: what a host owns, busy-session queueing, agent cache). Ours: `docs/notes/P04/agent-object.md`,
`interrupts.md`, `route-and-d04-1.md` (criterion e), `protocol-models.md` (method catalogue).

What it does (own words): `SessionService` is the API every surface drives a session through.
Per attached session it keeps the agent (P04-20), the session's `EventSink`, the broker (wired
to that sink: `deliver` → `sink.request`, `emit` → `sink.emit`, the shared seq counter) and the
session lock, held for the life of the attachment. `InProcessHost` runs the service on an
asyncio loop in a daemon thread so a synchronous caller can call it.

Inputs → outputs (every failure is a `ProtocolError` from the table):
- `create(SessionCreateParams?) -> SessionCreateResult`: new id (`new_session_id`), attach;
  `cwd`/`model` go into the agent options and the session row fields, `toolsets` into the
  selection; a repeated `idempotency_key` returns the first session (`created=False`), with other
  params `IDEMPOTENCY_CONFLICT`. The session row itself is written by the first turn's prologue
  (it must carry the prompt), so a created session that never ran a turn is only known to this
  service.
- `resume(id) -> SessionResumeResult` (`message_count`, `last_seq`, `pending_requests`): an id
  already attached here is a reconnect (described again, nothing taken); otherwise the session
  must exist (P02 row, memory store entry, or created here) — else `SESSION_NOT_FOUND` — and the
  lock is taken: held elsewhere → `SESSION_BUSY`.
- `submit(id, text) -> PromptSubmitResult(turn_id, generation)`: blank → `TEXT_EMPTY`; a stored
  session that is not attached is attached first (so a second surface's submit is `SESSION_BUSY`);
  a running turn → `SESSION_BUSY`. Turn end (done callback, or `wait`, whichever first, once):
  `broker.cancel_all(TURN_ENDED)`, the result kept (last 20) for `wait`, and steer text the turn
  never delivered (ticketed steers withdrawn back, plus the loop's ticketless leftover) starts the
  next turn — unless the turn was interrupted, then it is dropped (logged).
- `interrupt(id, generation?, hard?)` → `interrupted`; a stale generation is a no-op (False).
- `steer(id, text)` → `queued` + ticket (`st_…`), False with no live turn; `redirect(id, text)` →
  `redirected` / `steered` (during tools) / `rejected`.
- `answer_server_request(id, request_id, answer)` → the broker's answer result.
- `events(id, since?)`, `subscribe(id, fn)`, `on_request(id, fn)`, `wait(id, turn_id?)`,
  `close(id)` (agent closed, broker closed, lock released), `aclose()`.
- Errors: `protocol_error_for(exc)`: `ProtocolError` as is, `IndusError` through
  `error_for_indus_code`, anything else `INTERNAL_ERROR` (logged). The busy message is the
  `errors.session.busy` copy (`BUSY_COPY`, mirrored in `locales/en.yaml`, a test keeps them
  equal); the holder's description goes to the log only.
- Host: `InProcessHost(make_service, scope=, call_timeout=60)`; `start()`/`stop()`/context
  manager; blocking `create`, `resume`, `submit`, `wait`, `interrupt`, `steer`, `redirect`,
  `answer_server_request`, `events`, `close_session`, `feed(id)` (a `queue.Queue` the loop fills
  with events and server requests, plus its stop callable); `call(coro)` / `run(fn)` for anything
  else. It declares all four answerable kinds. Calls run under the host's scope (the task would
  otherwise copy the calling thread's context, R01.16); a blocking call from the loop thread
  itself is refused (`RuntimeError`) instead of hanging.

Edge cases: interrupt before the turn's first model call → the loop sees the flag at its first
effect point (no request is sent); a turn task that died without a result is logged and leaves
no result; closing the service while a turn runs interrupts it (by system) through the agent;
`stop()` closes every session before the loop stops.

Tests: `tests/unit/service/test_session_service.py` (16), `tests/unit/service/test_host_inprocess.py`
(6), `tests/integration/service/test_inprocess_host.py` (end-to-end smoke on the real P02 store,
resume in a second host), `tests/integration/core/test_d04_1_sync_caller.py` (criterion e).

Decisions for the lead (Part 3):
- The lock is held per attachment (create/resume/implicit attach), not per turn; a reconnect
  through the same service is not busy.
- Undelivered steer text after a completed turn starts the next turn automatically; after an
  interrupt it is dropped. Change in `SessionService._turn_ended`.
- `session.create`'s `title` is accepted but not stored yet: the P02 create fields have no title
  (titles land with P02's title API / P06).
- `tests/integration/service/test_inprocess_host.py` holds the end-to-end smoke (M1 shape); the
  host's own unit tests are `tests/unit/service/test_host_inprocess.py`.

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

Done (2026-10-18). Slice Z (wave 6a): per-session lock, broker and its port, tests; shared one-line
changes `src/indus/foundation/errors.py` (`session.busy`), `src/indus/protocol/errors.py`
(`session.busy` → `SESSION_BUSY`). Wave 6b (builder S): `src/indus/service/session_service.py`,
`src/indus/service/host_inprocess.py`, their tests, the end-to-end smoke and D-04.1 criterion (e)
(`tests/integration/core/test_d04_1_sync_caller.py`); `errors.session.busy` copy added to
`locales/en.yaml`.

Compare checklist, Part 3 (reopened `website/docs/developer-guide/gateway-session-lifecycle.md`
§7, §8, §11):
- [x] one running turn per session; a second submit is busy, in this service and from another
  (tests: `test_session_service.py::test_a_second_submit_while_busy_is_refused`,
  `::test_two_services_on_one_session`)
- [x] interrupt reaches the running turn by generation and withdraws its open requests (tests:
  `::test_interrupt_checks_the_generation`, `::test_interrupt_withdraws_a_pending_approval`)
- [x] a sync caller drives and interrupts a turn from another thread (tests:
  `test_host_inprocess.py::test_a_sync_caller_interrupts_mid_stream_from_another_thread`,
  `tests/integration/core/test_d04_1_sync_caller.py::test_sync_caller_drives_and_interrupts_a_streaming_turn`)
- [x] R05.3 through the whole stack: an undeclared kind fails at once, nothing is sent (test:
  `::test_a_kind_the_client_cannot_answer_fails_at_once`); the in-process host declares all four
  (test: `test_host_inprocess.py::test_the_host_runs_the_service_on_its_own_thread`)
- [x] resume in a new process finds the history and reuses the stored prompt bytes (test:
  `tests/integration/service/test_inprocess_host.py::test_create_submit_tool_events_and_resume_in_a_new_service`)
- Deliberate differences: the reference queues messages that arrive while a turn runs (FIFO per
  session) and promotes them afterwards; we answer `SESSION_BUSY` to a second submit and carry
  only steer text over into a follow-up turn. No agent cache or idle eviction: one agent per
  attachment, closed with it.
- Not taken: restart recovery markers and stuck-loop suspension (P17), queue depth reporting,
  session-key rules (gateway, P15).
