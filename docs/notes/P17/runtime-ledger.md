# Task: Runtime ledger (store) (P17-01) Phase: 17 Owner: wave-23 builder K1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_runtime.py` (owner
epoch, admit, find/list, claim head, settle, cancel, steer transfer / revert / settle, replace
binding, rehome, recover-on-start, resolve unknown; worker and mutation receipts skimmed),
`hermes_state_runtime_workers.py` (skim), `hermes_state_terminal.py` (terminal admission lookup and
exact-retry of a retired target only). Research 03 §3.18, §3.21; R03.8, R03.21, R03.22.
Evidence: none.

What it does (own words): the durable record of every input a surface hands the daemon for a
session. Each input is an *admission* keyed by (principal, target session, request id) and
fingerprinted by a digest of its payload. Retrying the same key with the same payload gives back the
row that already exists (whatever state it is in); the same key with another payload is refused as
`admission_conflict`. A session holds at most 32 queued inputs (`queue_full`). The owner (the
daemon) claims the FIFO head: the row moves `queued → started`, stamped with the owner epoch and a
fresh per-session generation; the turn's end settles it `terminal` with an outcome. A queued input
can be handed into the running claim (steer transfer) and handed back (revert); replacing a queued
input cancels it and admits its successor in one transaction. Each daemon start bumps the owner
epoch; in the same write every row still `started` from an older epoch becomes `unknown` and stays
so: never replayed, and the session's later inputs wait until an operator discards it with the
right generation. Rows that leave the table (retention, or their session is deleted) leave a small
retirement marker, so an exact retry of a retired input answers with the retired receipt instead of
admitting it again, and a deleted session never takes fresh input.

Inputs → outputs: store handle, epoch, (principal, session, request id, payload dict) → frozen
`AdmissionRow` values; refusals as `LedgerRefusal` (code `runtime.<reason>`, bounded reason,
optional field, a short cleaned detail).

Files: `src/indus/state/runtime_ledger.py`, `src/indus/state/schema.py` (shared: four tables,
three indexes), `src/indus/state/deletion.py` (shared: retire a deleted session's admissions in the
delete transaction), `src/indus/foundation/errors.py` (shared: `runtime.*` codes),
`tests/unit/state/test_runtime_ledger.py`.

Interface (for K2 and the later P17 tasks):
- every write takes the caller's `epoch`; owner hooks run inside the transaction: `admit(...,
  authorize=)` (new rows only), `claim_head(..., guard=)`, `resolve_unknown(..., terminal_write=)`.
- `begin_owner_epoch(store, instance_id) -> OwnerStart(epoch, unknown, interrupted_steers)`;
  caller holds the profile ownership lock. `current_epoch(store)`.
- `admit(store, *, epoch, principal_id, session_id, request_id, payload, replaces=None) ->
  Admission(row, created)`.
- `get(store, admission_id)`, `find(store, principal_id, session_id, request_id)`,
  `pending(store, session_id)` (queued/started/unknown, FIFO order), `queue_position(rows, id)`,
  `sessions_with_queued(store)`.
- `claim_head(store, *, epoch, session_id, expect_head=ANY) -> Claim(kind, row)` with kind
  `claimed` / `idle` / `running` / `blocked` / `moved`.
- `settle(store, *, epoch, admission_id, generation, outcome, final_text=None, detail=None,
  transfers={})`, `cancel(store, *, epoch, admission_id)`, `steer_into(...) -> (row, committed)`,
  `revert_steer(...) -> bool`, `resolve_unknown(store, *, epoch, admission_id, generation)`.
- `generation_of(store, session_id)`, `next_generation_in(conn, session_id, now)` (one counter per
  session shared with the turn lease of P17-03), `retire(store, ids)`, `retire_sessions_in(conn,
  ids, now)`, `prune(store, older_than=...)`.

Edge cases / failure modes:
- two concurrent admits of one key: the second sees the first's row inside its own
  `BEGIN IMMEDIATE` (UNIQUE key index as the backstop) → one row.
- payload not a JSON object, NaN, over 1 MiB; ids empty or over 1,024 chars → `invalid_params`.
- stale caller epoch (an old daemon still running) → `stale_epoch` for every write.
- settle with a generation that is not the claim's → `stale_generation`; a repeated settle of the
  same generation returns the settled row (idempotent).
- cancel of a started row → `admission_started` (the running turn is stopped through its control
  and settles itself); of an unknown row → `unknown_execution`; of a terminal row → returned as is.
- steer into an idle session → `session_idle`; of a started row → `admission_started`.
- a steer transfer handed to a claim that was lost in a crash → `interrupted` at owner start
  (it may have reached the agent); undelivered transfers go back to their FIFO place.
- detail text from a client: whitespace collapsed, non-printables dropped, 160 chars.
- every write goes through the P02 writer (time-budgeted retry, idempotent callbacks).

Our design differences / decisions:
- our table names: `runtime_owner`, `runtime_generations`, `runtime_admissions`,
  `runtime_retired`; refusals are one `LedgerRefusal` class with a `reason` field.
- the session row need not exist (daemon sessions can take input before their first row); a
  tombstoned session id refuses fresh input (`retired`).
- `claim_head` answers with a kind instead of raising for a blocked FIFO (the worker loop reads
  it); `resolve_unknown` settles with outcome `discarded` (not `interrupted`) so receipts can tell
  an operator's discard from a stopped turn.
- the final text is kept on the terminal row (and its marker) for exact-retry replay.
- left out: worker executions and their receipts, metadata-mutation receipts, legacy import,
  rehome on automatic reset (no P17 task needs them yet).

Tests we will write:
- exact retry same row; changed payload conflict; 33rd queued `queue_full`; concurrent admits.
- claim/settle walk, generations increase, stale generation, idempotent settle.
- restart: started → unknown, head blocked, discard with wrong/right generation, then the next row.
- steer transfer: committed once, revert keeps FIFO place, settle with transfers, crash → interrupted.
- replace queued is atomic (old cancelled + new tail) and its exact retry.
- retirement: retire → exact retry returns the retired receipt; different payload conflict;
  deleted session → `retired` for new ids, exact retry still answers.
- detail cleaning; stale epoch.

## Decisions for the lead
- Outcome `discarded` for an operator's resolution of an unknown turn (reference: interrupted).
- Retirement markers are never pruned (small rows); if they need a bound, add an age cap later.
- A deleted session's admissions are retired inside `deletion.delete_rows_in` (one-line call).
- Product error codes `runtime.*` added to `ERROR_CODES`; P17-25 maps them to the protocol table.

Compare checklist (filled after the rewrite; reopened `hermes_state_runtime.py`,
`hermes_state_terminal.py`):
- [x] epoch bump on owner start with the instance id (test: `test_runtime_ledger.py::test_owner_epoch_bumps_and_stale_epochs_are_refused`)
- [x] exact retry returns the existing row in any state, conflict on a new digest (test: `::test_exact_retry_returns_the_same_row`, `::test_changed_payload_is_a_conflict`)
- [x] queue cap counts queued rows only and holds while paused (test: `::test_the_33rd_queued_input_is_refused`, `::test_queue_cap_holds_while_the_fifo_is_paused`)
- [x] claim refuses behind unknown, waits behind started, `expect_head` mismatch (test: `::test_claim_kinds`)
- [x] settle fenced by epoch + generation + started (test: `::test_settle_is_fenced_by_generation`)
- [x] cancel rules: started/steering not cancellable, unknown refused (test: `::test_cancel_rules`)
- [x] steer transfer bound to the running claim; replay of a committed transfer; settle outcomes steered / interrupted / requeued (test: `::test_steer_transfer_*`)
- [x] recovery: started → unknown, in-flight steering of an old epoch → interrupted (test: `::test_restart_flips_started_to_unknown_and_blocks_the_next_row`)
- [x] resolve unknown: generation checked, idempotent repeat (test: `::test_discard_unknown_needs_the_generation`)
- [x] replacement: old queued row cancelled in the same write; exact replay bound to the row it replaced (test: `::test_replace_queued_is_atomic`)
- [x] retired targets answer only the exact digest (test: `::test_retired_id_stays_retired_after_an_exact_retry`, `::test_deleted_session_refuses_new_input`)
- [x] refusal detail one line, printable, bounded (test: `::test_refusal_detail_is_cleaned`)
- [x] owner gate inside the admission / claim transaction, discard's same-transaction owner write (gap found at compare; test: `::test_owner_gates_run_inside_the_transaction`, `::test_discard_runs_the_owner_write_in_its_transaction`)
- Deliberate differences: no session-row requirement; claim answers with a kind; `discarded`
  outcome; final text on the row rather than a separate stored-result envelope.
- Not taken: worker executions and receipts, mutation receipts, legacy import, rehome on reset.

## Status

Done (2026-10-19). Shared edits: `state/schema.py` (tables + indexes), `state/deletion.py` (retire
on delete), `foundation/errors.py` (`runtime.*` codes).
