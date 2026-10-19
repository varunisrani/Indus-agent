# Task: Generation fences on controls (P17-05) Phase: 17 Owner: wave-23 builder K2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/session_controls.py` (all:
control routing, `interrupt` / `respond` / `respond_clarify` / `cancel` / `resolve_unknown`
handlers, refusal data), `tui_gateway/methods_session_interrupt.py` (all: interrupt, steer and
redirect handlers), `gateway/session_authority.py` (skim of `interrupt`, `steer_agent`,
`adopt_agent`, `check_approval_generation`, `respond`, `cancel_or_claimed`: where the generation
checks actually sit). Research 05 §3.7 (interrupt flow), 06 §3.9 (generation-keyed release), traps
R01.4, R06.2, R09.11. Closed before writing.
Evidence: none.

What it does (own words): in the shared daemon several surfaces watch one session, and each
turn of that session has a generation number. Every control a surface sends names the generation
it means: interrupt, steer, an approval or clarify answer, and stop (which names an admission).
If the named generation is not the one running now, the control is refused with
`stale_generation` and the reply carries the generation that is running (or none), so the client
can decide again. A control is never re-aimed at "whatever runs now": the turn running now may
have been started by another surface, and stopping it because an old card was clicked would be
wrong (R01.4). Turn bookkeeping is keyed by generation as well: only the generation that is
running can release the session, so a displaced turn finishing late can never clear the state of
the turn that replaced it (R06.2).

Between a claim (the admission worker has picked the head and given it a generation) and the
moment the turn's agent is ready, a control for that generation is accepted and latched; it is
delivered when the turn adopts its controls, and dropped if that generation is released first.
It never reaches a later generation.

Inputs → outputs:
- `GenerationFences` (one per daemon, all sessions):
  - lifecycle (called by the admission worker / daemon host): `begin(session, generation)`,
    `adopt(session, generation, controls) -> bool`, `release(session, generation) -> bool`,
    `running(session) -> int | None`, `latest(session) -> int | None`;
  - wiring: `bind_requests(session, requests, answer)` (the session's open-request book from
    P17-07 and the broker's `answer`), `forget(session)`, `bind_admissions(service)`;
  - controls: `interrupt(session, generation, *, hard=False, by_system=False)`,
    `steer(session, generation, text)` → `ControlApplied{status: delivered/latched/rejected}` or
    `StaleGeneration{requested, current}`;
    `respond(session, request_id, generation, answer)` → `Responded{accepted}` /
    `RequestNotOpen` / `StaleGeneration`;
    `async stop(session, admission_id)` → `StopResult{outcome}` (cancelled, interrupted,
    already_settled, unknown, not_found) or `StaleGeneration`.
- `TurnControls` protocol: what a running turn exposes (`interrupt(generation, *, hard,
  by_system) -> bool`, `steer(text) -> bool`); the agent fits with a thin adapter.
- `AdmissionPort` protocol (K1's P17-02 service): `receipt(admission_id)`, `cancel(admission_id)`
  returning a receipt (sync or awaitable).

Files: `src/indus/service/fences.py`, `tests/unit/service/test_fences.py`,
`tests/unit/service/_stubs_k2.py` (stand-in for K1's admission service; removed at integration —
the stop tests now run against `AdmissionService` itself, plus a small double of its real
`receipt`/`cancel` interface for the races a real store cannot be paused in).

Edge cases / failure modes:
- interrupt with N−1 while N runs → stale, N untouched; interrupt when nothing runs → stale with
  `current: None`; a generation from the future → stale.
- `begin` with a generation not above the last one seen → `ValueError` (generations only grow).
- `begin(N+1)` while N still runs (lease lost and re-claimed) → N is displaced; `release(N)` later
  is a no-op and N+1 keeps running.
- latched interrupt then `release` of that generation → dropped; latched steers and a latched
  interrupt are delivered at `adopt`, steers first, then the stop (the stop wins anyway, and the
  steer text is handled like any steer the turn never used).
- `adopt` for a generation that is not running → refused (False), nothing delivered.
- approval answered for a request that was withdrawn, timed out or answered elsewhere → no-op
  `RequestNotOpen`, never an error and never another request's answer.
- request open but answered with another generation, or for a turn that is no longer running →
  stale.
- stop of a queued admission → cancelled through the admission service; if the worker claimed it
  between our read and the cancel, the receipt comes back `started` and we interrupt exactly that
  generation (never the session's current one). Stop of a started admission whose generation has
  already been released → `already_settled`. Stop of an `unknown` admission → `unknown` (only the
  operator's `discard_unknown` resolves it).
- controls run under one lock for the decision; the target's own methods run outside it and get
  the generation again, so a turn that ended in between ignores the call.

Our design differences / decisions:
- Names: `GenerationFences`, `StaleGeneration`, `ControlApplied`, `stale_generation` code string.
- The fence keeps the running generation in memory, fed by `begin`/`release` from whoever claims
  turns (the admission worker). The durable copy is K1's ledger; after a daemon restart the fence
  starts empty and every running row is `unknown` anyway (R03.21).
- Results are values, not exceptions; the RPC layer (P17-12/13) maps `StaleGeneration` to the
  protocol error added by P17-25 with `data.generation`.
- Stop takes the session id as well as the admission id (every canonical verb carries the
  session), because the admission receipt shape of P17-02 does not name the session.

Tests we will write:
- interrupt / steer stale cases; N unaffected; idle session; future generation.
- displaced turn release does not clear its successor; non-growing `begin` refused.
- latch before adopt delivered on adopt; dropped on release; never reaches N+1.
- respond: open request with right generation accepted; withdrawn → no-op; wrong generation →
  stale; second answer → no-op.
- stop: queued → cancelled; queued-but-claimed race → interrupts that exact generation; started
  → interrupted; released → already_settled; unknown; not found.

## Decisions for the lead
- Stale is a returned value with the running generation; P17-25 adds the protocol code
  (`stale_generation`) and the RPC handlers turn the value into that error.
- `stop` signature is `stop(session_id, admission_id)` (see above).
- `steer` with no adopted controls is latched for that generation, not refused.

Compare checklist (filled after the rewrite; reopened `gateway/session_controls.py`,
`tui_gateway/methods_session_interrupt.py`, `gateway/session_authority.py` (interrupt, steer,
adopt, respond, cancel paths)):
- [x] Interrupt names a generation; a mismatch is refused with stale and never falls through
  (test: `test_fences.py::test_interrupt_with_older_generation_is_stale_and_leaves_turn_running`).
- [x] A stop accepted for a claimed turn before its agent exists is latched and fires on adopt,
  never against a later generation (`test_latched_interrupt_delivered_on_adopt`,
  `test_latched_control_dropped_when_generation_released`).
- [x] Steers latched for the claimed generation reach the agent it adopts, in order
  (`test_latched_steers_delivered_in_order_on_adopt`).
- [x] Approval/clarify answers check the running generation before settling
  (`test_respond_with_stale_generation_is_refused`); an answer for a withdrawn request is a no-op
  (`test_approval_answered_for_withdrawn_request_is_a_no_op`).
- [x] Cancel of a queued admission that the drain claimed meanwhile stops that exact claim
  (`test_stop_queued_claimed_meanwhile_stops_that_claim`, `test_stop_when_the_ledger_refuses_the_cancel_as_started`); a stop for a turn that has
  already settled never reaches its successor
  (`test_stop_of_a_finished_admission_never_hits_the_next_turn`, `test_stop_of_a_generation_released_after_the_read_is_already_settled`).
- [x] Release keyed by generation: a displaced turn cannot clear its successor (R06.2;
  `test_displaced_turn_cannot_release_its_successor`).
- [x] Gap found in compare: the reference keeps a stop that arrives before the claim is visible to
  the controls; added the early-stop latch kept for that generation's `begin`
  (`test_stop_before_the_claim_reaches_the_fence_is_kept_for_its_begin`).
- Deliberate differences: the reference answers an interrupt whose generation equals the
  session's last generation with success even when that turn already ended; we answer stale with
  `current: None` (the phase row: "not the running one returns stale_generation"). The reference
  checks the generation before looking the prompt up; we look the request up first, so a withdrawn
  request is a quiet no-op even when its turn has also ended. A hard interrupt in the reference
  also stops the session's background delegations; here that stays with the host's
  `TurnControls` adapter (P17-08 wires it to `SessionService`'s hard path).
- Not taken: hosted/compute-host task ids (`expected_hosted_task_id`), wake/voice resume after an
  interrupt, compression-in-flight demotion of a steer to a queued prompt (P17-08 decides busy
  behaviour), redirect (no generation is named for it in the phase row; it keeps P04's in-process
  path), per-actor capability checks (`session:control`/`session:approve`; P17-13/14 attach
  viewers and authorise them before calling the fence).

## Status

Done (2026-10-19). New files: `src/indus/service/fences.py`, `tests/unit/service/test_fences.py`;
stand-in `tests/unit/service/_stubs_k2.py` (removed at integration). Wiring for the lead: the
daemon host (P17-08) / admission worker (K1's P17-02) call `begin(session, generation)` at claim,
`adopt(session, generation, controls)` once the turn's agent exists and `release(session,
generation)` at settle; `bind_requests(session, book, broker.answer)` per attached session; the RPC
handlers (P17-12/13) route `session.interrupt`, `session.steer`, `approval.respond`,
`clarify.respond` and the stop verb here and map `StaleGeneration` to P17-25's `stale_generation`
error with `to_data()`. K1's admission service replaces `FakeAdmissions` as `admissions=`.

## Integration (P17 wave 1, integrator)

- K1's real interface differs from the stand-in: `receipt` raises `LedgerRefusal("not_found")`
  instead of answering None; `cancel` of an admission the worker claimed meanwhile either
  interrupts that ticket itself (and answers `started`) or, in the ledger's own race, raises
  `admission_started`; an unknown one raises `unknown_execution`. `stop` handles all three (it
  reads the receipt again after a refusal) and counts `cancelled` only when the receipt's outcome
  is `cancelled` (a row steered or settled meanwhile is `already_settled`).
- `Receipt` gained `session_id`; `stop` answers `not_found` for an admission of another session, so
  a stop can never cancel or interrupt work in a session it does not name (D-16.365).
- Wired: `AdmissionService(fences=)` calls `begin` at claim, `adopt` at once with the ticket
  (`_FencedTurn`), `release` when the turn ends (D-16.366). `GenerationFences.bind_admissions`.
- `StaleGeneration.error_code` = `runtime.stale_generation` (the ledger's code).

