# Task: Protocol additions and codegen (P17-25) Phase: 17 Owner: wave-28 builder L2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tui_gateway/contracts/canonical.py`
(the method names it registers only: receipts, cancel, resolve-unknown, mutate, prune, detach,
describe, clarify respond/lock, cron/kanban/worker/group verbs). Research 05 §3.3 (wire contract
discipline, overloaded per-method codes, renderer-side synthetic events), §3.4 (canonical names
the TUI calls), §3.5 (`session.replay_gap`, `session.updated`). Closed before writing.
Evidence: none.

What it does (own words): everything the attach transport puts on the wire becomes a declared,
typed part of `indus.protocol`, so the generated TypeScript (`packages/protocol`) and the JSON
Schema describe it and the stale check guards it:

- methods: `runtime.describe` (the daemon's attach descriptor over the WebSocket),
  `admission.receipt` (what became of an input, for a client that lost its ack),
  `maintenance.run` (bulk store maintenance inside the daemon), next to the ones P17-13/14/22
  already declared (`session.subscribe`, `session.unsubscribe`, `sessions.watch`, `events.since`,
  `admission.discard`);
- events (numbered, in a session's stream): `session.viewers` (who else watches),
  `request.settled` (a server request ended: answered, withdrawn, undeliverable),
  `admission.state` (an input moved: queued, started, terminal, unknown);
- notifications (not numbered): `gateway.ready` (already), `session.replay_gap` (this viewer
  cannot be caught up: reload a snapshot), `session.updated` (list views), `sessions.resync`
  (list updates were dropped: reload the list);
- error codes, one meaning each: `STALE_GENERATION`, `ADMISSION_CONFLICT`, `QUEUE_FULL`,
  `OUTCOME_UNKNOWN`, `POLICY_CONFLICT`, `LEASE_LOST` (conflict band), `TICKET_INVALID` (4401),
  `PROFILE_MISMATCH`, `NOT_SHOWN` (4404) (refused band). Product codes `runtime.*` and
  `store.turn_lease_lost` map onto them.

Codes that never travel as a method's error answer are listed apart (`OUT_OF_BAND_ERRORS`, with
where each one travels): the ticket code is the WebSocket close code of a refused upgrade, the
not-shown code is a viewer's answer to a server request, a profile mismatch is the client's own
refusal (and its close code), a stale generation is a control's "nothing changed" answer and
`admission.discard`'s reason.

Inputs → outputs: Python models → `python -m indus.protocol.codegen` → `schema.json`, `types.ts`,
`catalog.ts`; `--check` exits 1 with a diff when a model changed without regeneration.

Files: `tests/unit/protocol/test_attach_contract.py`; shared (P04/P12/P17 files):
`src/indus/protocol/attach.py`, `src/indus/protocol/events.py`, `src/indus/protocol/errors.py`,
`src/indus/protocol/bridge.py` (notifications table), `src/indus/protocol/methods.py` (method
error lists), `packages/protocol/src/generated/*` (regenerated, never hand-edited),
`src/indus/gateway/ws/methods.py` (handlers for the three new methods; submit refusal codes),
`src/indus/service/remote/host.py` (an `OUTCOME_UNKNOWN` answer keeps the journal entry),
`src/indus/service/request_redelivery.py` / `service/ws_auth.py` (the numbers come from the
table), `tests/unit/protocol/{test_error_table,test_models,_samples,_bridge_samples}.py`.

Edge cases / failure modes:
- an event declared in `EVENTS` is checked against its model by every emitter in strict mode, so
  each new model matches exactly what the daemon emits today (`session.viewers`, `request.settled`
  are appended to the event log and carry `seq`; `session.replay_gap` and `session.updated` are
  sent outside the log and carry none — hence notifications);
- one meaning per code: `store.turn_lease_lost` moves from `SESSION_BUSY` to `LEASE_LOST` (same
  409 group, so the HTTP-like status is unchanged);
- an exact retry of an input whose turn was lost in a restart used to answer `streaming` with the
  lost turn's generation (the client then waited for a turn that never comes); it now answers
  `OUTCOME_UNKNOWN`, and the Python attach host keeps the journal entry as unknown;
- `admission.receipt` for another session's admission reads as not found (as `admission.discard`);
- `maintenance.run` answers a refusal (`bad_op`, `bad_params`, `guarded`, `store_error`) inside the
  result, like the control verb; it runs on the daemon's own store handle.

Our design differences / decisions:
- Our names: `admission.receipt` / `admission.discard` (not prompt receipt / resolve-unknown),
  `runtime.describe` result = the control socket's `describe` plus the replay epoch.
- The reference overloads numbers per method; ours are one table, and four codes reuse the number
  of the wire value they stand for (4401 ticket close, 4404 not shown).
- Unnumbered frames are notifications, not events (the event union stays "things with a seq").
- `admission.state` is declared; no emitter yet (follow-up: the host's admission transitions).

Tests we will write:
- the schema / catalogue names every new method, event, notification and code; each code appears
  once and every code is declared by a method or listed out of band;
- product `runtime.*` codes map onto the new rows; lease lost maps to `LEASE_LOST`;
- codegen `--check` fails when one of the new models changes without regeneration;
- the daemon's real emitters pass the new models in strict mode (viewers, settled, gap, updated);
- the three new WebSocket methods answer over a real `WsEndpoint` (describe ids, receipt of our
  own admission / another session's → not found, maintenance on the daemon store); submit
  refusal codes.

## Decisions for the lead
- Code numbers: 3005 `STALE_GENERATION`, 3006 `ADMISSION_CONFLICT`, 3007 `QUEUE_FULL`, 3008
  `OUTCOME_UNKNOWN`, 3009 `POLICY_CONFLICT`, 3010 `LEASE_LOST`, 4005 `PROFILE_MISMATCH`, 4401
  `TICKET_INVALID`, 4404 `NOT_SHOWN`. Queue full sits in the conflict group (409: the session's
  state refuses it now), not refused.
- `ExitReason` keeps its closed set: `stalled` stays a failure reason beside
  `interrupted_by_system` (D-16.364 unchanged); `lease_lost` is likewise a failure reason, the
  `LEASE_LOST` code is for RPC answers.
- `server_request.cancelled` is kept beside `request.settled` (TS renderer still reads it).
- One code beyond the row's list: `ADMISSION_NOT_FOUND` (2005, not-found band) for
  `admission.receipt` of an unknown admission or another session's (product `runtime.not_found`
  maps to it). `admission.discard` keeps its `reason: not_found` result (D-16.496).
- Codes that never answer a method are `OUT_OF_BAND_ERRORS` (`STALE_GENERATION`,
  `PROFILE_MISMATCH`, `TICKET_INVALID`, `NOT_SHOWN`), each with where it travels; the error-table
  test now reads "declared by a method or listed out of band, never both". The daemon's controls
  keep answering a stale generation with "nothing changed" (D-16.365): raising
  `STALE_GENERATION` there would change the TS and Python clients and the dual-host matrix.
- `POLICY_CONFLICT` is declared on `session.create` / `session.resume` (the in-process service
  maps `runtime.policy_conflict` through `error_for_indus_code`); the policy itself is still not on
  the WebSocket (D-16.490 stays until that follow-up).
- `LEASE_LOST` is declared on `prompt.submit` only; `store.turn_lease_lost` moved from
  `SESSION_BUSY` to it (same 409 status).
- Unnumbered frames (`session.replay_gap`, `session.updated`, `sessions.resync`) are declared as
  notifications (`methods.NOTIFICATIONS` = bridge + attach), numbered ones (`session.viewers`,
  `request.settled`, `admission.state`) as events in the `Event` union.
- Submit refusals now answer their own codes (`ADMISSION_CONFLICT`, `QUEUE_FULL`,
  `SESSION_REFUSED` for a deleted session) instead of `SESSION_BUSY` for everything; an exact
  retry of a lost input answers `OUTCOME_UNKNOWN` (was `streaming` with the lost generation), and
  `RemoteSessionService` keeps that journal entry as unknown (`OutcomeUnknown`).
- `RemoteSessionService` does not hand `session.viewers` / `admission.state` to the session
  renderer (they parse as events now; before they were dropped as unknown) — keeps the dual-host
  matrix unchanged.
- `maintenance.run` over the WebSocket runs `MaintenanceVerb` on the host's own store handle; it is
  served to any connection holding a ticket (tickets are minted only to the owner).

Compare checklist (filled after the rewrite; reopened `tui_gateway/contracts/canonical.py`, names only):
- [x] describe over the attach transport (`runtime.describe`) (test:
  `tests/unit/protocol/test_attach_contract.py::test_runtime_describe_over_the_websocket`)
- [x] receipt for a lost ack (`admission.receipt`) (test: `::test_admission_receipt_is_per_session`)
- [x] resolve-unknown (`admission.discard`, P17-17 integrator) — unchanged
- [x] prune / maintenance through the owner (`maintenance.run`) (test:
  `::test_maintenance_run_inside_the_daemon`)
- Deliberate differences: no `session.mutate`, `session.detach`, `command.observe`, cron / kanban /
  worker / groups verbs here (their phases: P17 follow-up, P14, P19/P24); `session.close` already
  only detaches.
- Not taken: `prompt.cancel` (our `session.interrupt` with a generation and `stop` cover it).

## Status

Done (2026-10-20). See the wave-28 L2 report.

Integration (2026-10-20): D-16.544 (codes), D-16.545 (additions), D-16.546 (submit refusals,
lost inputs). Lead decisions applied: `admission.state` now has its emitter
(`AdmissionService(on_state=)` → `DaemonSessionHost`, D-16.553); `prompt.submit` gained
`unattended` (D-16.554) and `PromptSubmitResult.generation` became optional — the daemon waits
for the claim of an input that starts at once and never answers a 0 placeholder (D-16.555).
Kept: D-16.365 (no `STALE_GENERATION` raise from daemon controls), D-16.490 (no creation policy
on the wire).
