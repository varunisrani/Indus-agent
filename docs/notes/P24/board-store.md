# Task: Task board store (P24-11) Phase: 24 Owner: wave-37 builder B1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/kanban_db.py` (module
docstring and constants block, the dataclasses and `SCHEMA_SQL`, `create_task`,
`recompute_ready`, `claim_task` / `claim_review_task`, `heartbeat_claim`, `release_stale_claims`,
`reclaim_task`, `complete_task`, `block_task` / `_route_block`, `request_review`,
`request_changes`, `unblock_task`, `archive_task`, `schedule_task`, the attachment helpers),
`hermes_cli/kanban_db_graph.py` (`initial_task_state` only); research 02 §3.28 (kanban) and the
tool table row. Closed before writing.

What it does (own words): one SQLite file per board, shared by every profile under the same Indus
root, holds the work profiles hand to each other. A task has a title, an optional body, an
assignee (a profile name), a status and a workspace. Tasks link to parents; a task whose parents
are not all done waits in `todo` and moves to `ready` once they are. A dispatcher (P24-12) claims a
`ready` task for one worker run; the claim lasts 15 minutes unless the worker heartbeats; a run
ends by completing (with a handoff summary), blocking (with a reason and a kind), asking for
review, or by the dispatcher reclaiming it. Every change appends an event row so watchers and the
CLI can show history; comments, attachments (one 25 MiB cap for every surface) and run rows
(one per attempt) are kept beside the task.

Inputs → outputs: `BoardStore.open(scope, slug)` → store; `create(...)` → `Task`; `claim(id,
claimer)` → `Task | None`; `heartbeat`, `complete`, `block`, `schedule`, `request_review`,
`request_changes`, `reopen_review`, `unblock`, `archive`, `assign`, `link`, `comment`, `attach_bytes`
→ the new state or a `BoardError` naming what is wrong; `release_stale_claims(alive, terminate)` →
ids reclaimed; `recompute_ready()` → count promoted; `events_after(cursor)` for watchers.

Files: `src/indus/state/board.py`, `tests/unit/state/test_board.py`, `src/indus/foundation/errors.py`
(shared: `board.*` codes).

Rules (numbers from the row):
- statuses `todo`, `ready`, `running`, `blocked`, `review`, `done`, `archived`;
- a new task is `ready` when every parent is done or archived, else `todo`; an initial `running`
  (the creating worker claims it in the same transaction) or `blocked` is allowed only when a
  worker creates the task, anything else is refused;
- block kinds `dependency`, `needs_input`, `capability`, `transient`: `dependency` with an open
  parent waits in `todo` (no recurrence counted); `dependency` with no open parent becomes
  `needs_input`; the others count recurrences of the same kind (kept across unblock, cleared only
  by a completion); the third block of the same kind in a row escalates (limit 2 recurrences);
- workspace kinds `scratch`, `worktree`, `dir`;
- claim TTL 15 min; a live worker whose heartbeat is older than 60 min is reclaimed anyway; a
  worker that survives termination keeps its claim for a 120 s grace; crash detection ignores a
  run younger than 30 s;
- unblock: `ready` when every parent is done, else `todo`; resets the dispatcher failure count;
- attachments ≤ 25 MiB each, safe base names, collision-free file names under the board's
  attachment folder; a blob whose row cannot be written is removed;
- every write goes through one `Writer` (R03.8): `BEGIN IMMEDIATE`, retries with a deadline; the
  file is never the profile's session store.

Edge cases / failure modes:
- claim race: two claimers, one wins (compare-and-set on status and lock), the other gets `None`;
- a `ready` task whose parent was reopened is demoted to `todo` at claim time;
- complete while another worker holds a live claim without proof of the run → refused;
- complete from any status but `review` without a summary or result → refused (empty handoff);
- link that would form a cycle, a self link, or an unknown id → refused;
- attachment 25 MiB + 1 byte → refused before any file is written;
- a schedule parks the task (not dispatchable) until a time or an explicit unblock.

Our design differences / decisions:
- our names: `BoardStore`, `Task`, `Run`, `TaskEvent`; ids `t_<8 hex>`; times are epoch seconds
  from the scope clock;
- no `triage` or `scheduled` status (the row's status list has neither): escalation keeps the task
  `blocked` with `escalated = 1` and a `block_loop` event; scheduling keeps it `blocked` with
  `scheduled = 1` and an optional `wake_at` the dispatcher honours;
- the board file lives at `<root>/boards/<slug>.db` (the row's `<home>/..` only holds for named
  profiles; the root is what every profile shares); attachments, logs and scratch workspaces under
  `<root>/boards/<slug>/`;
- process probing and termination are injected (`alive`, `terminate`) so the store stays testable;
- left out: goal mode, swarms, completion contracts, per-task model overrides, tenants, workflow
  templates, board metadata (default workdir, project binding beyond `project_id`).

Tests we will write: claim expiry and reclaim with a fake clock (live worker extended, stale
heartbeat reclaimed, survivor deferred 120 s); block recurrence: third same-kind block escalates,
dependency with an open parent waits in todo; unblock with one unfinished parent → todo, all done →
ready; 25 MiB + 1 attachment refused, exactly 25 MiB kept; complete without a handoff refused;
initial status only for workers; link cycle refused; review → changes → back to implementer.

## Decisions for the lead
- Escalation happens on the third consecutive block of the same kind (the row's test); the
  reference escalates on the second. Change `BLOCK_RECURRENCE_LIMIT` handling in `_route_block`.
- Board root is `scope.root / "boards"` (see above).

Compare checklist (reopened `hermes_cli/kanban_db.py` the functions listed above):
- [x] claim CAS and parent demotion (`tests/unit/state/test_board.py::test_claim_race_and_parent_demotion`)
- [x] TTL reclaim, live-worker extension, stale heartbeat, survivor grace (`::test_claim_expiry_and_reclaim_with_fake_clock`)
- [x] reclaim counts a failure; the breaker blocks at the limit (`::test_reclaim_counts_toward_the_failure_limit`)
- [x] block routing and recurrence escalation (`::test_third_recurrence_of_the_same_kind_escalates`, `::test_dependency_block_waits_in_todo_or_becomes_needs_input`)
- [x] unblock re-gates on parents (`::test_unblock_with_one_unfinished_parent_goes_to_todo`)
- [x] completion gates: parents, handoff, live claim (`::test_complete_needs_a_handoff_and_finished_parents`, `::test_complete_refuses_a_live_claim_without_its_run`)
- [x] review hand-off and changes back to the implementer (`::test_review_then_changes_returns_to_the_implementer`)
- [x] attachment cap and safe names (`::test_attachment_cap_is_25_mib`)
- [x] initial status only for workers (`::test_initial_status_only_for_workers`)
- [x] link cycle, self link (`::test_links_refuse_cycles`)
- [x] schedule parks, wake time releases (`::test_schedule_parks_until_its_time`)
- Deliberate differences: escalation on the third block (row) instead of the second; no triage /
  scheduled statuses (flags instead); single per-root boards folder; a task the failure breaker
  blocked waits for an explicit unblock (the reference re-promotes it when the limit is raised or
  it is reassigned); a heartbeat writes an event only when it carries a note (the claim and
  heartbeat time are always updated).
- Not taken: lifecycle plugin hooks for board transitions (no `on_board_*` hook names exist in our
  plugin table yet), phantom card / prose scans, artifact staging from the scratch workspace,
  operator owner-recovery pause.

## Status

Done (2026-10-21). Store and tests in place; the dispatcher, tools and CLI build on it.

## Integration (P24 integrator, 2026-10-21)

`indus/board/__init__.py` is a one-line docstring now (layer packages, `tests/unit/test_layers.py`);
import `indus.board.dispatcher` / `indus.board.watchers` directly.
