# Task: Admission service (P17-02) Phase: 17 Owner: wave-23 builder K1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/session_admission.py`
(all), `gateway/session_retry_admission.py` (receipt kinds, rewrite inside the admission write).
Research 03 §3.18, 09 §3.11 steps 3–5; R06.19.
Evidence: none.

What it does (own words): the one door every surface (CLI, TUI, messaging, API) uses to hand the
daemon an input for a session. `admit` records it in the runtime ledger and answers with a
`Receipt` (admission id, state queued / started / terminal / unknown, generation once started,
FIFO position while queued). One worker task per session claims the head of its FIFO, takes the
durable turn lease for that generation, runs the turn through the host's runner, and settles the
admission with the outcome and the final text. A client that lost its ack asks `receipt(id)`. An
exact retry of an input whose turn already finished gets the terminal receipt with the stored final
text, so the client shows it once instead of running the model again. `cancel` drops a queued input
or stops the running turn; `steer(id, text)` adds text to the running turn; `steer_queued(id)`
hands a queued input to the running turn; `discard_unknown(id, generation)` is the operator's way
to resolve a turn lost in a restart (a stale generation is refused). While a session waits behind
an unknown turn, messaging users are told once per pause.

Inputs → outputs: principal, session id, request id, input dict (text and attachments) →
`Receipt`; runner callback `(TurnTicket) -> TurnOutcome`; blocked-session callback.

Files: `src/indus/service/admissions.py`, `tests/unit/service/test_admissions.py`.

Interface (for K2 and P17-08/22):
- `AdmissionService(store, *, epoch, runner, clock, leases=None, liveness=None, waiting=None,
  on_blocked=None, offload=None, gate=None)`;
  `await AdmissionService.open(store, *, instance_id, runner, clock, ...)` begins the owner epoch
  (caller holds the profile ownership lock) and restarts workers for sessions with queued input.
- `await admit(principal, session_id, request_id, input) -> Receipt`, `await receipt(id)`,
  `await cancel(id)`, `await steer(id, text) -> bool`, `await steer_queued(id) -> Receipt`,
  `await discard_unknown(id, expected_generation) -> Receipt`, `await wait(id) -> Receipt`,
  `running(session_id) -> TurnTicket | None`, `await aclose()`.
- `TurnTicket`: `admission`, `generation`, `stop_reason`, `stopped` (asyncio.Event),
  `interrupt(reason)`, `bind(interrupt=, steer=)`, `touch(note)` (liveness), `transfers`.
- `TurnOutcome(outcome, final_text=None, detail=None)`; `Receipt` fields as above plus `outcome`,
  `final_text` (only on a replayed terminal receipt), `replayed`, `blocked_by`.

Edge cases / failure modes:
- two concurrent admits of one request id → one row, both receipts name it, one turn runs.
- the runner raises → settled `failed` with a cleaned detail; the worker goes on to the next input.
- settling fails (store down) → the row stays `started`; after a restart it becomes `unknown`.
- the lease cannot be taken (timeout) → settled `failed` (`lease_timeout`), never run unserialised.
- lease lost mid-turn → the ticket is interrupted with `lease_lost`; outcome interrupted.
- a session blocked by an unknown turn: admits still queue (up to 32); the worker stops; the
  blocked callback runs once per unknown admission; `discard_unknown` restarts the worker.
- `aclose` interrupts running turns (`shutdown`) and waits for workers; rows they leave `started`
  become `unknown` at the next start (never replayed).

Our design differences / decisions:
- the runner is injected (P17-08 binds it to the session host; P17-22 to messaging); the service
  holds no agent itself.
- receipts carry the stored final text only on an exact retry of a terminal admission
  (`replayed=True`); a fresh admit never carries text.
- the per-pause notice is a callback with the unknown admission row; wording belongs to the surface.

Tests we will write:
- admit → run → terminal receipt with outcome and text; FIFO order across three inputs.
- two concurrent admits of one request id → one row, one runner call.
- exact retry after completion replays the final text with zero runner calls.
- discard with an old generation refused (`stale_generation`), right generation unblocks the FIFO.
- restart with a started row → unknown receipt, later input waits, blocked callback once.
- cancel queued / cancel running; steer to the running turn; steer_queued transfer settles steered.
- runner failure → failed; lease taken per generation and released; lease lost → interrupted.

## Decisions for the lead
- A lease timeout settles the admission `failed` with detail `lease_timeout` (the person may send
  again); the reference answered a "session busy, send again" result.
- Discard settles outcome `discarded`.

Compare checklist (filled after the rewrite; reopened the two files above):
- [x] the digest is computed from the canonical payload after the surface has captured media (the surface passes the final input; test: `test_admissions.py::test_exact_retry_after_completion_replays_text_without_running`)
- [x] admission refusals keep their bounded reason (test: `::test_refusals_keep_their_reason`)
- [x] a refused admission writes nothing (ledger transaction; test: `test_runtime_ledger.py::test_the_33rd_queued_input_is_refused`)
- [x] the owner's gate runs inside the admission (and claim) transaction; exact retries bypass it — gap found at compare, fixed by `gate=` (ledger `authorize` / `guard`), pinned by `::test_the_owner_gate_refuses_new_work` and `test_runtime_ledger.py::test_owner_gates_run_inside_the_transaction`
- Deliberate differences: no `/retry` transcript rewrite of our own here (messaging `/retry` is
  P17-22's to route through `admit`; its rewrite runs as the `gate`).
- Not taken: the context-variable binding of a retry rewrite.

## Status

Done (2026-10-19). No shared files.

## Integration (P17 wave 1, integrator)

- `Receipt.session_id` added (third field) so generation-fenced controls can check the session.
- `fences=` (P17-05): `begin` at claim, `adopt` with the ticket, `release` when the turn ends;
  `INTERRUPTED` / `SYSTEM_INTERRUPT` reasons beside `CANCELLED` (D-16.366).
- Failure and settle details pass the P05 redactor (force, the store's scope) and the ledger's
  `clean_text` (credential shapes masked, absolute paths cut to their last part; D-16.369).

