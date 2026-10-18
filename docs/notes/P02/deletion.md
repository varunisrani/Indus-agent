# Task: delete and tombstones (P02-18) Phase: 02 Owner: wave-4 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_sessions.py`
(`delete_session` 1701–1790, `delete_session_if_empty` 1790, `delete_sessions` 1834, the
delegate-child collector and `_remove_session_files`), `hermes_state.py`
(`_delete_unreferenced_system_prompts` only), `hermes_state_maintenance.py` (`_guarded_ids` only),
`hermes_state_common.py` (`emit_session_deleted`, `_id_chunks` only),
`hermes_state_mutation_retirement.py` (tombstone idea and the 7-day horizon only),
`tests/hermes_state/test_delete_compression_chain.py`. Closed before writing.
Evidence: none.

What it does (own words): deleting a session removes its row, its messages, metadata, usage and
lock rows in one write transaction. Sub-agent sessions it delegated to go with it (recursively);
a branch, a reset or a compression continuation is its own conversation and stays, only losing
its parent link. A session under a live turn lease or compression lock is refused — something is
writing to it right now — but a lease whose holder expired, or whose PID is provably dead in our
PID namespace, is reaped first and does not protect anything. Every deleted id leaves a
tombstone in `meta` (`tombstone:<id>` → `{"deleted_at", "by"}`) so the id can never be created
again or re-imported (R03.22): user deletes are permanent; housekeeping sweeps (empty sessions,
prune) leave tombstones that age out after 7 days. After the commit, side files
`<home>/sessions/<id>.json` / `.jsonl` are removed best-effort and the `on_session_delete` hook
point is called (P13 wires plugins there); a failing hook is logged and swallowed.

Inputs → outputs:
- `delete_session(store, session_id, *, expect_message_ids=None, whole_chain=False) -> bool`
  (False: unknown id, or the live message ids differ from `expect_message_ids`);
  raises `SessionGuarded` (`store.session_guarded`) when it or a delegate child is protected.
- `delete_many(store, ids, *, whole_chain=False) -> DeleteReport(deleted, removed, unknown,
  guarded)`: one transaction, unknown ids and guarded roots skipped and reported.
- `delete_if_empty(store, session_id) -> bool`: only ended, untitled, childless, message-less,
  unguarded rows; sweep tombstone.
- `is_tombstoned(store, session_id)`, `tombstone_in(conn, id) -> Tombstone | None`,
  `write_tombstone_in(conn, id, *, by, now)`, `reap_tombstones_in(conn, *, now) -> int`.
- in-transaction core shared with prune: `delete_rows_in(conn, ids, *, by, now,
  cascade_delegates=True) -> list[str]`, `guarded_ids_in(conn, ids, *, now, reap=True)`,
  `delegate_descendants_in(conn, ids)`, `chain_ids_in(conn, id)`; `after_delete(store, ids,
  reason)` for the post-commit work.
- hook registry: `register_on_session_delete(fn)`, `unregister_on_session_delete(fn)`; hooks
  get a `SessionDeleted(session_ids, reason, profile_id, home)`.

Files: `src/indus/state/deletion.py`, `tests/unit/state/test_deletion.py`,
`src/indus/foundation/errors.py` (shared: one code row and the class lives in deletion.py).

Edge cases / failure modes:
- every `IN (…)` list chunked at 900 variables (R03.18): 5,000 ids delete in one call.
- delegate cascade only for children whose kind (`sessions.child_kind`) is `delegate`; a
  delegation marker loop never collects a parent as its own descendant.
- `whole_chain`: from the root of the compression chain forward through every continuation,
  stale sibling continuations included; branches/resets of chain members are orphaned.
- a guard on any delegate child (or chain member with `whole_chain`) protects the root.
- a sweep tombstone never downgrades a user tombstone; a user delete upgrades a sweep one.
- `system_prompts` rows no session references are removed in the same transaction.
- side files: only plain-file-name ids (the divert-path rule); `OSError` swallowed.
- hooks run after the commit, never inside the transaction; a raising hook is logged.

Our design differences / decisions:
- tombstones live in our `meta` table (no separate retirement ledgers: no gateway/receipts yet).
- expiry is enforced by removing aged sweep tombstones (`reap_tombstones_in`, run by every prune
  pass), so "present" means "tombstoned" for every reader (`sessions.is_tombstoned_in`).
- `expect_message_ids` compares the live (`active = 1`) message id set (the transcript the
  caller showed); the reference's delegate-id and display-snapshot fences are folded into this.
- an orphaned delegate whose parent link was already NULL is not collected (kinds count only
  when the marker names the row's parent, R03.13).

Tests we will write:
- parent with a delegate child (and its own delegate grandchild) and a branch child → delegates
  gone, branch orphaned; whole-chain variant from the tip removes root/mid/tip and a stale
  sibling, orphans a branch.
- live lease / lock → `SessionGuarded`; expired → allowed and reaped; dead-PID holder reaped;
  guarded delegate child protects the parent.
- re-create a deleted id → `SessionTombstoned`; tombstone JSON shape; sweep tombstone reaped
  after 7 days, user tombstone kept; user over sweep upgrades.
- `delete_many` of 5,000 ids (statements under the variable cap) skipping unknowns; guarded
  root skipped and reported.
- `delete_if_empty` table; prompt row collected with its last user only; side files removed;
  hook called after commit (sees the row gone) and its exception swallowed + logged;
  `expect_message_ids` mismatch deletes nothing.

## Decisions for the lead
- New code `store.session_guarded` (class `SessionGuarded` in `deletion.py`); not yet mapped in
  `state.errors.TYPED_BUCKETS` (shared file; suggest bucket `turn_lease`).
- Tombstone `by` values: `user` (permanent) and `sweep` (7 days); `delete_if_empty` and prune
  are sweeps.
- `whole_chain` added (reference tests' chain delete); default off.

Compare checklist (filled after the rewrite; reopened `hermes_state_sessions.py` 1690–1980 and
the helpers listed above, `hermes_state_mutation_retirement.py` marker/expiry part,
`test_delete_compression_chain.py`):
- [x] delegate cascade recursive, branch/reset/compression children orphaned; an inherited marker
  naming another id is not a delegate (tests: `tests/unit/state/test_deletion.py::test_delegates_go_and_branches_are_orphaned`,
  `::test_inherited_delegate_marker_naming_another_id_is_not_cascaded`)
- [x] whole-chain delete incl. stale siblings, count is the requested rows, default chain-blind
  (tests: `::test_whole_chain_from_the_tip`, `::test_default_delete_is_chain_blind`)
- [x] guards refuse, stale ones reaped first; a guard anywhere in the covered set protects the
  root (tests: `::test_live_guard_refuses_then_expiry_allows`, `::test_dead_holder_is_reaped`,
  `::test_guarded_delegate_protects_its_parent`, `::test_whole_chain_guard_keeps_the_chain`)
- [x] tombstone in the same transaction; deleted id cannot be recreated; sweep markers age out
  after 7 days and never weaken a user one (tests: `::test_deleted_id_cannot_be_recreated`,
  `::test_tombstone_shape_and_expiry`, `::test_sweep_never_weakens_a_user_tombstone`,
  `::test_unreadable_tombstone_still_fences`)
- [x] bulk delete in one transaction, 900-id chunks, unknown skipped, guarded reported (tests:
  `::test_delete_many_five_thousand_ids`, `::test_delete_many_skips_guarded_roots`)
- [x] empty-only delete predicate incl. guard (test: `::test_delete_if_empty_rules`)
- [x] prompt GC; child rows removed (tests: `::test_prompt_row_collected_with_its_last_user`,
  `::test_child_rows_go_with_the_session`)
- [x] side files + hook after commit, fail-open (tests: `::test_side_files_removed`,
  `::test_hook_runs_after_commit_and_failures_are_swallowed`)
- [x] transcript fence (test: `::test_expect_message_ids_fences_a_moved_transcript`)
- Deliberate differences: guards always refuse (the reference only with an opt-in flag; the
  phase row makes it the rule); tombstones in `meta`; one transcript fence
  (`expect_message_ids`); `delete_if_empty` also requires an ended row (phase row); no
  local-reset "owned lineage" expansion and no request-dump / legacy snapshot files.
- Not taken: retirement of gateway receipts and media (P15/P17), `discard_unadmitted_session`.

## Status

Done (2026-10-18). `deletion.py` with 22 unit tests (one skips until slice A's `sessions.create` lands). Shared: `foundation/errors.py` (code row), `docs/internals/traps-ledger.md` (R03.18, R03.22).
