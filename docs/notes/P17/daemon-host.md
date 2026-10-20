# Task: Daemon session host with many viewers (P17-08) Phase: 17 Owner: wave-25 builder K3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/session_authority.py`
(viewer attach / detach and ownership only: the live-session record, `attach`, `detach`, the
retire-on-overflow hook, the pending-close map), `tui_gateway/session_registry.py` (skim: how a
live session is looked up, built and torn down). Research 05 §3.2 mode C and the shared channel,
trap R05.18; research 08 §3.9 (skim). Closed before writing.
Evidence: none.

What it does (own words): inside the per-profile daemon one object hosts every session for every
surface. A surface (classic CLI, TUI, desktop, ACP client, web dashboard, a messaging chat) creates
or resumes a session and then *watches* it as a viewer: a connection id, the kind of surface and
what it can render. A session can have any number of viewers, including none. Closing a client only
removes its viewer; whatever turn is running keeps running, finishes and is stored, and the
admitted queue keeps draining. Every event of a session is numbered once by the daemon's event log
and handed to every viewer of that session in that order, so two viewers see the same stream. A
session opened by an ACP client is the exception that ends: when its last viewer leaves and nothing
is running or queued it is closed; if work is still running it is closed when that work drains,
unless a viewer came back meanwhile. Every session is closed when the daemon stops. When a
surface sends input while a turn runs and does not say what it wants, the TUI's input is queued
behind the turn and every other surface's input interrupts it (R05.18); either way the new input
is admitted durably first, so it is never lost.

The host is where the P17 wave-1 pieces meet (the wiring owed to P17-08):
- each input goes through the admission service (P17-02); the host is its turn runner: it binds
  the ticket's interrupt and steer hooks to the session, honours a stop that arrived before the
  hooks were bound (the turn is not started), reloads the session when the lease was only won
  after waiting for another process (`HeldLease.waited`), starts the agent's turn with the ledger's
  generation, feeds the liveness meter from the session's events and maps the turn's exit to the
  ledger outcome;
- a hard interrupt (and a fenced `stop`) also stops the session's background subagents;
- one `EventLog` per daemon: its flusher task runs while the host is open and its epoch is the
  `replay_epoch` a `gateway.ready` frame will carry;
- one `RequestBook` per session (`emit` into the event log, `generation` = the fence's running
  generation) handed to the session's broker (`ServerRequestBroker(book=)`), and
  `fences.bind_requests(session, book, broker.answer)`; `fences.forget` / `log.forget` on close;
- the book's `session_id` follows a rotating compaction to the child id.

Inputs → outputs:
- `DaemonSessionHost.open(service, store, instance_id=…, leases=, liveness=, gate=, on_blocked=,
  event_log=)` → an open host (owner epoch begun, flusher running). `aclose()` closes everything.
- `create(params, source=, policy=, viewer=)` → `SessionCreateResult`; `resume(session_id,
  policy=, viewer=)` → `SessionResumeResult`.
- `attach(session_id, viewer)` → `Attachment(session_id, replay_epoch, last_seq, reoffered)`;
  `detach(session_id, connection_id)`; `detach_connection(connection_id)` (a socket closed).
- `submit(session_id, text, principal=, request_id=, connection_id=, surface=, busy=)` → the
  admission `Receipt`; `receipt`, `wait`, `stop`, `discard_unknown`.
- `interrupt(session_id, generation, hard=)` / `steer(session_id, generation, text)` /
  `respond(session_id, request_id, generation, answer)` → the fence's result values
  (`ControlApplied` / `Responded` / `RequestNotOpen` / `StaleGeneration`); `refuse(session_id,
  connection_id, request_id, code)` for a viewer's error answer.
- `events_since(session_id, epoch, cursor, connection_id=)` → `Replay` or `ReplayGap` (and the
  still-open requests are offered again to that viewer).
- Viewers receive `send(wire_event_dict)` for every numbered event and `offer(Offer)` for server
  requests they can render.

Files: `src/indus/service/daemon_host.py`, `src/indus/service/viewers.py`,
`tests/unit/service/test_daemon_host.py`; shared: `src/indus/service/session_service.py` (seams:
broker factory, turn door, `start_admitted`, `reload`, `interrupt(by_system=)`, `events_since` /
`receipt` trivial in-process forms), `src/indus/service/admissions.py` (`TurnTicket.lease_waited`).

Edge cases / failure modes:
- viewer detaches mid-turn → the turn completes, its rows are stored, later viewers replay it;
- the same connection attaches twice → replaced, not duplicated; a viewer of another session
  never receives this session's events;
- a send that raises → logged, that viewer skipped, the others still get the event in order;
- stop before the runner bound its hooks → the turn never starts; settled as the stop reason says;
- a runner with no attached session (daemon restarted, queued input) → the session is resumed
  without a viewer first; a session that cannot be found settles `failed` with a clean detail;
- ACP last detach while running or queued → closed only after the queue drains; a viewer that
  attaches again in between cancels the close;
- a rotating compaction moves the session id: the host keeps its key (ledger, fences, log, viewers
  stay addressed by the first id), the book and broker carry the new id on their events;
- steer text a turn never consumed, background-subagent results and host injections (goals, loops)
  are admitted through the ledger as new inputs instead of starting turns behind its back.

Our design differences / decisions:
- Our names (`DaemonSessionHost`, `ViewerRegistry`, `Viewer`, `Attachment`). The host composes the
  P04 `SessionService` (agent building, transcript, titles, compaction aliases, plugins, MCP) and
  adds the daemon's door, fences, log and books; it exposes the service's call shapes with the
  daemon's controls (generation-checked) instead of subclassing it.
- Busy modes are `queue` and `interrupt`; an explicit mode wins; otherwise the submitting viewer's
  surface decides (TUI queue, all others interrupt).
- The session's per-session file lock (P02) is kept while the daemon has a session attached, beside
  the durable lease, until P17-18 moves `--local` onto the lease (then it can be retired).
- Reload after a contended lease rebuilds the session's agent from the store (and follows the
  lineage tip if the other process compacted it).

Tests we will write:
- detach mid-turn: the turn completes and is persisted; a later replay shows it;
- two viewers receive every event of the turn in the same seq order; a third viewer of another
  session receives none; a raising viewer does not stop the others;
- ACP session: ends after the last detach when idle; stays while a turn runs and ends when it
  drains; a reattach cancels the pending close; a CLI session is never ended by detach;
- busy defaults: TUI queues behind a running turn, CLI interrupts it (both inputs admitted);
- fenced controls: interrupt with an old generation is stale; hard interrupt stops subagents;
- approval offered to both viewers, answered by one, `request.settled` reaches both;
- stop before bind never starts the agent; lease waited → reload called;
- steer leftovers and injected turns go through the admission ledger.

## Decisions for the lead
- The host's key for a session is the id it was first attached under; ledger rows, fences, the
  event log and viewers stay keyed by it across rotating compactions (cursors stay valid). The
  events themselves carry the current id. Change by re-keying on `on_session_moved`.
- A session's frozen creation policy (P17-09) is checked on create/resume; applying fields the
  P04 service does not apply yet (provider, reasoning, max turns, skills, safety switches) is the
  agent factory's job: the record rides `session_fields["model_config"]["launch"]`.
- Server-request kinds an attached viewer can render are its capabilities that name request kinds;
  `request.decline` advertises the 4404 decline.
- With no viewer attached a new server request is unsupported at once (P17-07's book rule).
- Reload after a contended lease (`HeldLease.waited`) rebuilds the agent with the same options
  and closes the old one, which runs the session's task cleanups (background processes of that
  session end); plugins' `session_started` is not run again. Alternative: a lighter agent-level
  refresh (prompt, context meter, todo) once the core offers one.
- The P02 per-session file lock stays held while the daemon has a session attached (beside the
  durable lease) until P17-18 moves `--local` onto the lease.
- `create` writes the session row at once only when a policy is given (to freeze it); otherwise
  the first turn writes it, as in-process. The session's source is kept in memory.
- The host owns the service once opened: `aclose` also closes the service.
- A busy-mode interrupt is soft and only targets the turn running when the input was admitted.

## Published interface (for P17-10..14 and later surfaces)

`indus.service.daemon_host.DaemonSessionHost` (service layer; gateway and cli import it, never the
reverse). Every method runs on the daemon loop.

```python
host = await DaemonSessionHost.open(service, store, instance_id=..., leases=None, liveness=None,
                                    gate=None, on_blocked=None, event_log=None, offload=None)
host.replay_epoch -> str                     # gateway.ready.replay_epoch
host.event_log / host.fences / host.viewers / host.admissions / host.service
await host.create(params=None, *, source="cli", policy=None, viewer=None) -> SessionCreateResult
await host.resume(session_id, *, policy=None, source=None, viewer=None) -> SessionResumeResult
host.attach(session_id, viewer) -> Attachment(session_id, key, replay_epoch, last_seq, reoffered)
await host.detach(session_id, connection_id) -> bool
await host.detach_connection(connection_id) -> list[str]           # socket closed
await host.submit(session_id, text, *, principal, request_id, connection_id=None, surface=None,
                  busy=None) -> Receipt                             # LedgerRefusal on refusal
await host.receipt(admission_id) / host.wait(admission_id) -> Receipt
await host.discard_unknown(admission_id, expected_generation) -> Receipt
host.interrupt(session_id, generation, *, hard=False) -> ControlApplied | StaleGeneration
host.steer(session_id, generation, text) -> ControlApplied | StaleGeneration
host.respond(session_id, request_id, generation, answer) -> Responded | RequestNotOpen | StaleGeneration
host.refuse(session_id, connection_id, request_id, code) -> bool    # -32601 / 4404 answers
await host.stop(session_id, admission_id) -> StopResult
host.events_since(session_id, epoch, cursor, *, connection_id=None) -> Replay | ReplayGap
host.state(session_id) -> SessionState(key, session_id, source, replay_epoch, last_seq, running,
                                        viewers, open_requests, close_pending)
host.key_of(session_id) -> str | None; host.hosted() -> list[str]
host.creatable_sources() -> tuple[str, ...]   # describe (P17-11): cli tui gui acp web
host.capabilities() -> tuple[str, ...]        # describe: events.since, server_requests(.<kind>), request.decline
await host.close_session(session_id); await host.aclose()          # aclose also closes the service
```

`indus.service.viewers.Viewer.of(connection_id, surface, capabilities=(), *, send, offer=None)`:
`surface` ∈ `cli tui gui acp web messaging`; capabilities naming request kinds (`approval`,
`clarify`, `sudo`, `secret`) are what it renders (only with an `offer` hook); `request.decline`
allows 4404. `send(dict)` gets each numbered event's wire form (`seq`, `replay_epoch` included);
`offer(Offer)` gets server requests (`Offer.to_wire()`). Unknown session ids raise
`ProtocolError(SESSION_NOT_FOUND)`; a resume that changes a frozen value raises
`creation_policy.PolicyConflict` (`.to_data()` is the error data); a bad launch raises
`PolicyRefused`.

Compare checklist (filled after the rewrite; reopened `gateway/session_authority.py` attach /
detach and the ACP close it calls, `tui_gateway/session_registry.py`):
- [x] viewer attach is idempotent per connection (test: `test_daemon_host.py::test_reattach_same_connection_is_one_viewer`)
- [x] attach re-offers open requests to the viewer (test: `test_daemon_host.py::test_an_approval_reaches_both_viewers_and_settles_once`)
- [x] detach of the last viewer of an ACP session ends it only when idle (test: `test_daemon_host.py::test_acp_session_ends_after_last_detach_only_when_idle`)
- [x] a reattach cancels a pending close (test: `test_daemon_host.py::test_reattach_cancels_pending_acp_close`)
- [x] detach never stops a turn (test: `test_daemon_host.py::test_detach_mid_turn_completes_and_persists`)
- [x] gap found: the reference ends the ACP session's stored row, not only its live entry — fixed:
  the row is ended `acp_close`, pinned by the ACP test above.
- Deliberate differences: subscriptions are keyed by connection id (one per connection and
  session), not by a minted subscription id; see the decisions above.
- Not taken: per-actor authorisation (`authorize(actor, ref, capability)`; P17-13/14 check the
  actor before any host call), a viewer-supplied end reason on the last detach (CLI/TUI close
  reasons; P17-18/19), overflow retirement of a slow viewer (P17-14), editor MCP release on last
  detach (P19/P20), ending unviewed ACP sessions found only in the store at shutdown (they are
  unloaded, not ended).

## Integration (P17 wave 2)

- `creatable_sources()` / `capabilities()` added (plain reads) so the host serves the attach
  verbs' `AttachHost` (D-16.433); `test_attach_verbs.py` runs against the real host.
- `events_since(..., connection_id=)` from a connection that is not a viewer of the session
  answers `SESSION_NOT_FOUND` (D-16.435; `test_a_connection_replays_only_sessions_it_attached`).

## Status

Done (2026-10-20). Shared edits: `service/session_service.py` (daemon seams, in-process
`events_since` / `receipt`), `service/admissions.py` (`TurnTicket.lease_waited`),
`docs/internals/traps-ledger.md` (R05.18 rows). Handed on: P17-13/14 build viewers from WS
connections and call the host; P17-22 routes messaging through `submit`; P17-25 declares the new
events and error codes.
