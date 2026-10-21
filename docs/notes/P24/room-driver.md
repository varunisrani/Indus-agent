# Task: Group room driver (P24-06) Phase: 24 Owner: wave-37 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `website/docs/user-guide/bot-mode.md`
("Groups and group chats": turn timing, stop directives; "Transferring hosted room authority"),
`gateway/hosted_room_driver.py` (constants and state transitions only), `tui_gateway/hosted_room_service.py`
(skim: task status groups, `status`, method names), `tui_gateway/methods_groups.py` (method names
only). Research 06 §3.20; research 03 R03.21.
Evidence: none.

What it does (own words): the driver decides whose turn it is in a group room and keeps the record
of each member turn ("task") durable, without calling a model itself (the runner that actually runs
a member's turn is handed in). One gateway process at a time holds a room's *lease* (a row with a
lease generation that grows on every take-over, the gateway id and a per-process generation); work
is only started or settled under the current lease. A task moves `queued → running → settled |
failed`, can be `cancelled` while queued, goes `running → stopping → cancelled` when a stop is
acknowledged (or `stopping → settled` when the result won the race), and becomes `indeterminate`
when the driver finds it `running` under a lease that is no longer the current one (the process
died or lost the lease). Indeterminate work is never run again on its own (R03.21): an operator
resolves it with a verified result, cancels it, defers it, or explicitly requeues it accepting the
risk of running twice. Every transition is fenced: the settling attempt carries the task's execution
generation, its cancel generation and the run fence `(gateway id, process generation, lease
generation)`; if any of them moved, the write touches no row and the caller gets `StaleTask`.

Turn timing: a member that keeps reporting work keeps its turn for up to 3 hours; a member that goes
silent for 3 minutes loses it. `expire_turns()` fails such running tasks with reason
`member_silent` or `turn_limit` so the room moves on.

Stop directives: a stop word (`stop`, `halt`, `pause`) in a message holds a member only when it sits
next to that member's @mention (`stop @bot`, `@bot stop`, `@bot please pause`); the same word
anywhere else is ordinary prose. Words inside fenced code blocks, inline code, quotes or blockquote
lines never count. `@all` / `@everyone` next to a stop word holds every member. A room may turn
detection off.

Authority transfer (`groups.promote` / `groups.demote`) is an operator recovery procedure and is
documented as non-atomic: promotion needs `confirm: true` (the operator asserts the old authority
cannot write any more) and moves the epoch by one with an `authority.claimed` event; demotion needs
the observed newer gateway id and epoch and records `authority.lost`. Neither fences the other host
remotely and neither merges histories.

Limits: prompt ≤ 128 KiB; result JSON ≤ 256 KiB; ids ≤ 128 characters; terminal tasks kept 30 days
and at most 2,048 per room, pruned at most 1,000 per call.

Inputs → outputs: `RoomDriver(store, clock=)`: `acquire_lease(room_id, process_generation, ttl)` →
`DriverLease`; `renew_lease`, `release_lease`; `admit(TaskKey, payload)` → task;
`start(key, lease, expected_cancel_generation)` → `TaskAttempt`; `heartbeat(attempt)`;
`settle(attempt, settlement_id, status, result)`; `cancel_queued`, `begin_stop`, `complete_stop`,
`settle_stopping`; `recover(lease)` → `{queued, indeterminate}`; `resolve_indeterminate`,
`cancel_indeterminate`, `requeue_indeterminate`, `defer_indeterminate`, `requeue_deferred`;
`expire_turns(lease)`; `prune_terminal(room_id)`; `list_tasks`, `get_task`.
`stop_directives(text, handles)` → held member handles. `GroupsRpc(store, driver)` answers the
`groups.*` methods (`capabilities`, `list`, `create`, `state`, `log`, `send`, `rename`, `disband`,
`stop`, `retry`, `promote`, `demote`, `status`) with plain JSON params and results.

Files: `src/indus/gateway/rooms/__init__.py`, `src/indus/gateway/rooms/driver.py`,
`src/indus/gateway/rooms/rpc.py`, `tests/unit/gateway/rooms/test_driver.py`,
`tests/unit/gateway/rooms/test_rpc.py`.

Edge cases / failure modes:
- two processes: the second acquire while the lease is live → `LeaseHeld`; after expiry or release
  it takes over with the next lease generation; the first one's settle then fails `StaleTask` and
  `recover` by the new holder marks its running task indeterminate.
- a task whose lease generation changed settles indeterminate, never re-runs (`requeue` only on an
  explicit operator call).
- admit is idempotent per task id (same payload digest) and unique per (room, thread, turn).
- start only the head of the queue (event order), and never while anything is running, stopping or
  indeterminate in that room.
- a stop on queued work cancels at once; on running work it is two-phase (stop intent, then the
  runner's acknowledgement).
- release refuses while a task is running.
- clock values must be finite; TTL positive.
- RPC: unknown params refused, `promote` without `confirm: true` refused with a code, `demote`
  without an observed newer lineage refused.

Our design differences / decisions:
- Tables `room_leases` and `room_tasks` live in the same `rooms.db` as the room store (one writer).
- Turn timing is in the driver (`heartbeat` + `expire_turns`) rather than in a desktop client, so a
  room keeps moving when no client is open.
- `groups.*` methods are a plain handler table with their own pydantic param models; declaring them
  in `indus.protocol` (and generated TS types) is wiring for the lead.
- Left out: cross-gateway (peer) room members and their grants, task input context and
  attachments, the work-record capture hooks, replicas (`groups.replicate`, `groups.replica_state`),
  approvals inside rooms (`groups.approve`). Recorded "later".

Tests we will write:
- silent member expires at 3 min, a working member (heartbeats) is kept to 3 h then expires (fake
  clock).
- stop inside a code block / inline code / quote / blockquote ignored; next to the mention it holds;
  far from it, it does not; `@all stop` holds everyone.
- a task whose lease generation changed settles indeterminate and is not requeued by recovery;
  explicit requeue works.
- lease acquire / held / take-over / renew / release rules; fenced settle replays idempotently;
  start ordering; two-phase stop; cancel of queued; prune terminal (30 days, 2,048, 1,000 per batch).
- RPC: create → send → log round trip; promote needs confirm; demote needs a newer lineage.

## Decisions for the lead
- `groups.*` are not yet declared in `indus.protocol.methods` (shared file with codegen); `rpc.py`
  exposes `GroupsRpc.handlers()` (name → callable taking a params dict) for the WS/stdio dispatcher.
- Member turn runner (the code that runs a member's turn in its room session) is not in this row:
  the driver is the state machine; the daemon wiring that loops `recover → start → run → settle`
  and calls `expire_turns` is owed to whoever mounts rooms in the daemon (with P24-31's views).
- Idle exit (P17-23) should treat a room with live tasks as busy (follow-up from P17's notes).

Compare checklist (filled after the rewrite; reopened `gateway/hosted_room_driver.py` (every
transition, the not-admitted requeue, prune) and the test names of
`tests/gateway/test_hosted_room_driver.py`):
- [x] constants: prompt 128 KiB, result 256 KiB, 30 days, 2,048, 1,000; 3 h / 3 min (test: `tests/unit/gateway/rooms/test_driver.py::test_limits_match_the_spec`)
- [x] statuses and transitions (tests: `::test_two_phase_stop`, `::test_cancel_queued_task`, `::test_settle_is_idempotent_and_conflicts_on_other_results`, `::test_a_result_that_wins_the_stop_race_is_kept`)
- [x] lease rules, one winner, never shortened (tests: `::test_second_process_cannot_take_a_live_lease`, `::test_takeover_after_expiry_bumps_the_generation`, `::test_two_contenders_have_one_winner`, `::test_renewal_never_shortens_a_lease`, `::test_release_is_exact_and_refused_while_running`)
- [x] stale fence → indeterminate, never re-run; operator resolve / cancel / defer / requeue (tests: `::test_a_task_whose_lease_generation_changed_settles_indeterminate`, `::test_resolve_indeterminate_with_a_verified_result`, `::test_cancel_indeterminate`)
- [x] start order and recovery barrier (test: `::test_start_takes_only_the_head_and_waits_for_recovery`)
- [x] authority transfer and disband fence lease operations (tests: `::test_authority_transfer_fences_the_lease_and_a_late_settlement`, `::test_disbanding_fences_lease_operations`)
- [x] turn timing (tests: `::test_silent_member_expires_at_three_minutes`, `::test_working_member_is_kept_to_three_hours`); stop directives (test: `::test_stop_directives`)
- [x] gap found: proven non-admission returns a running task to the queue under its exact fence —
  added `requeue_not_admitted` (test: `::test_proven_not_admitted_attempt_returns_to_the_queue`).
- [x] gap found: the payload digest is checked on read — added in `_load` (test: `::test_a_tampered_task_row_is_not_used`).
- Deliberate differences: turn timing (`heartbeat` / `expire_turns`) and stop-directive parsing
  live in the driver module (the reference keeps them in its desktop client); the lease takes the
  room's current epoch itself; terminal tasks are pruned without waiting for a room-log
  "publication" record (we have no publication table); task payload is target, prompt,
  source_seq and an optional member id.
- Not taken: peer (cross-gateway) members and their grants, task input context and attachments,
  work-record capture, replicas (`groups.replicate`, `groups.replica_state`), approvals inside
  rooms (`groups.approve`), schema migrations of older drafts.

## Status

Done (2026-10-21). Driver, stop-directive parser and the `groups.*` handler table finished;
declaring `groups.*` in the protocol and mounting the driver loop in the daemon are wiring for the
lead.

## Integration (P24 integrator, 2026-10-21)

Wired: `groups.*` declared in `indus.protocol.groups` and served by the daemon's WebSocket over
`<root>/rooms.db` and its driver (opened on the first call). The room loop (lease → recover → start
→ member turn → settle) is not built: rooms take messages and stops, members do not answer yet
(D-16.799).
