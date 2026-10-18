# Task: sessions and the lineage walker (P02-08) Phase: 02 Owner: wave-4 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_sessions.py`
(`classify_session_status`, the upsert/inherit SQL and `_insert_session_row`, `create_session`,
`end_session`/`reopen_session`, `touch_session_activity`, the model-config merge helpers and the
yolo accessor, `resolve_session_id`, the lineage flag setters, `session_lifecycle_statuses`,
`assert_export_safe`), `hermes_state_common.py` (end-reason tuples, child-kind predicates,
`_non_continuation_child_sql`), `hermes_state_compression.py` (`_CHAIN_STEP_SQL`,
`_walk_compression_chain`, `get_compression_tip`, `get_compression_lineage` — for the walker),
`hermes_state_worker_lifecycle.py` (`insert_session_row_in_transaction`: the keep-existing upsert),
`tests/hermes_state/test_pinned_lineage.py`. Closed before writing.
Evidence: none.

What it does (own words): one row per session. Creating is an upsert that only fills gaps: a value
an earlier writer stored is never replaced by a later create (the gateway may create a bare row
before the agent knows the model). The only exception is the placeholder source `unknown`, which a
real source replaces. A child (row with a parent) copies cwd, git root/branch and profile from the
parent when its own are empty. Ending is "first reason wins": once ended, a later end is a no-op,
so a stale `user_exit` landing after a compression rotation cannot turn the parent back into an
ordinary closed session (R03.12). `reopen` clears both columns so the next end works again.

Children come in kinds. `parent_session_id` plus a marker in `model_config` (`_branched_from`,
`_delegate_from`, `_reset_from`) says why. A continuation copies its parent's `model_config`
verbatim, so it may carry a marker that names *some other* session; a marker counts only when its
value equals this row's parent id (R03.13). Without a matching marker a `tool`-source child is a
tool run, a child of a reset-ended parent is a reset, and everything else is a continuation.

A compression lineage is root → … → tip, where each hop is a continuation whose parent ended with
a compression reason. The walker (`state/lineage.py`) finds the root by walking parent links back
over such hops and the tip by walking forward over the one live continuation; two live
continuations of one parent is a fork nobody can resolve, so it raises `AmbiguousChain` instead
of guessing (fail closed). Listing projects a root onto its tip, so per-chat flags (archive, pin,
hide, read/unread) are written to every member of the lineage (R03.11), otherwise the root
would resurrect a flag cleared on the tip.

Inputs → outputs (module functions, handle first; `_in` forms take a connection inside a write):
- `create(store, id, source, **fields) -> bool` (True = inserted), `create_in(conn, …, now=)`;
  raises `SessionTombstoned` when the row is absent and meta holds `tombstone:<id>`.
- `get(store, id) -> dict | None`, `end(store, id, reason) -> bool`, `reopen(store, id) -> bool`.
- `child_kind(row, parent=None)` → `root|continuation|branch|reset|delegate|tool`.
- `patch_model_config(store, id, patch) -> bool`; `set_yolo(store, id, on)`; `yolo_enabled(row)`.
- `set_archived/set_pinned/set_hidden(store, id, flag)`, `mark_read/mark_unread(store, id)`,
  `is_unread(row)`.
- `classify_status(role, has_tool_calls, finish_reason)`, `status(store, id)`,
  `statuses(store, ids) -> {id: status}`.
- `resolve_id(store, text) -> str`: exact id, else the unique prefix; none →
  `SessionNotFound`; several → `AmbiguousSessionId(candidates=[…])`.
- `touch_activity(store, id, note=None, provenance=None, *, at=None) -> bool` (OBSERVATION budget).
- lineage: `root_in`, `tip_in`, `lineage_ids_in`, `ancestors_in`, `members_in`,
  `is_continuation(child, parent)`, `AmbiguousChain`.

Files: `src/indus/state/sessions.py`, `src/indus/state/lineage.py`,
`tests/unit/state/test_sessions.py`, `tests/unit/state/test_lineage.py`;
shared: `src/indus/foundation/errors.py` (new codes only).

Edge cases / failure modes:
- create twice: second create passing no model keeps the first model; passing a different model
  keeps the first too; `user_id` set once; `created_source` = first source.
- create of a tombstoned id → `SessionTombstoned`; re-create of an existing row is never refused.
- `model_config` given as dict or JSON text; a garbled stored value parses as `{}` (never raises).
- patch: `None` deletes a key; lineage markers in a patch are ignored (they always survive);
  empty result stored as NULL; missing row → False.
- yolo flag true only for a real JSON `true` under our key; corrupt JSON, a non-object, a string
  `"yes"` → false (resume must never switch the bypass on by accident).
- end on a missing row → False; end after end → False and the first reason stays.
- activity: an earlier timestamp than the stored one changes nothing; an observation that cannot
  get the write lock within 0.5 s is dropped (returns False), the next heartbeat retries.
- flags: unknown id → False; a lineage with an ambiguous fork still gets every member flagged
  (falls back to all members instead of failing the user's archive).
- read watermark: NULL = never tracked = read; 0 = explicitly unread; a time = read up to then.
- prefix lookup escapes `%`/`_`; empty text → not found.
- walker: cycles in parent links stop at the first repeat; no step cap (a 1,200-segment chain
  resolves); a missing id resolves to itself.

Our design differences / decisions:
- module functions taking the handle (contract), not mixin methods; our column names (`git_root`,
  `profile_id`, `last_activity_note`).
- end-reason families exactly as the phase lists them; the reference's legacy routing-key reset
  heuristic is not needed (no legacy stores): a reset child is marked or has a reset-ended parent.
- `status` reads the last *live* message (a rewound tail does not decide the status).
- no gateway routing columns, no profile namespace fence, no conversation-generation bump, no
  queued-prompt retirement (those features arrive with P15/P17).
- reopen also lifts an *automatic* archive across the lineage (never a deliberate one).

Tests we will write (`test_sessions.py`, `test_lineage.py`):
- upsert keeps model/user_id/created_source; `unknown` source upgraded; child inherits cwd/git/
  profile, explicit child value kept; tombstoned id refused.
- first end wins; reopen then end; end on missing row.
- child kinds: each marker naming the parent; inherited marker naming another id → continuation;
  tool source; reset-ended parent; root.
- patch merge/delete/marker survival; yolo table (true, false, corrupt JSON, list, string).
- archive on the tip archives the root and back; pin clears hidden; deliberate archive clears
  `auto_archived`; read/unread watermark; listing-style read agrees on root and tip.
- status table (error finish, pending tool call, trailing user/tool, complete, empty).
- prefix lookup unique / ambiguous (candidates listed) / none; `%` in text.
- activity never regresses; observation budget used.
- walker: root/tip/lineage over a 3-segment chain, branch child not part of it, two live
  continuations → `AmbiguousChain`, live sibling preferred over a closed one, 1,200-segment chain,
  cycle guard.

## Decisions for the lead
- New error codes: `store.session_not_found`, `store.ambiguous_id`, `store.ambiguous_chain`.
- Tip rule when a compression parent has several continuations: the ones still open or themselves
  compression-ended count as live; exactly one live → follow it; more than one → `AmbiguousChain`;
  none live and exactly one closed → follow it; several closed → `AmbiguousChain`.
- Lineage flags fall back to `members_in` (root plus every continuation descendant) when
  `lineage_ids_in` raises `AmbiguousChain`, so archiving a forked chat still works.
- `AUTOMATIC_REASONS` is the three automatic reasons only (the recoverable set is separate);
  `is_automatic_end(reason)` checks both, matching the reference's superset idea.
- `create` stamps `profile_id` with the store's profile when the caller gives none.

Compare checklist (filled after the rewrite; reopened the session, common and compression files
listed above):
- [x] keep-existing upsert, `unknown` source upgrade, `created_source`/`user_id` set once, child
  NULL-fill of cwd/git/profile (tests: `test_sessions.py::test_later_create_never_overwrites_an_earlier_value`,
  `::test_placeholder_source_is_replaced_once`, `::test_child_inherits_workspace_and_profile`)
- [x] tombstoned id refused only when the row is absent (`::test_tombstoned_id_cannot_be_created`)
- [x] first end wins; reopen clears both and lifts only an automatic archive
  (`::test_first_end_reason_wins`, `::test_reopen_then_end_again`,
  `::test_reopen_lifts_only_an_automatic_archive`)
- [x] marker-names-parent rule and child kinds (`::test_child_kind`,
  `test_lineage.py::test_inherited_marker_naming_another_id_still_continues`)
- [x] merge-patch keeps markers, `None` deletes, empty → NULL; yolo false on any parse problem
  (`::test_patch_merges_deletes_and_keeps_markers`, `::test_yolo_flag_reads_false_on_any_parse_problem`)
- [x] lineage-wide flags; deliberate archive clears the automatic stamp; the sweep keeps a
  deliberate archive deliberate; pin clears hidden; NULL/0/time watermark
  (`::test_archive_on_the_tip_archives_the_root_and_back`, `::test_deliberate_archive_clears_the_automatic_stamp`,
  `::test_auto_archive_keeps_a_deliberate_archive_deliberate`, `::test_pin_hide_and_unknown_ids`,
  `::test_read_watermark`)
- [x] status from the last message, error finish reasons, pending tool call (`::test_classify_status`,
  `::test_status_from_the_last_live_message`, `::test_statuses_over_more_ids_than_one_chunk`)
- [x] prefix lookup with LIKE escaping (`::test_prefix_lookup`, `::test_prefix_lookup_treats_wildcards_literally`)
- [x] activity never moves back; short budget; contention drops the heartbeat
  (`::test_activity_never_moves_backwards`, `::test_activity_uses_the_observation_budget_and_skips_when_busy`)
- [x] chain walkers: continuation filter (branch/delegate/reset/tool excluded), live sibling preferred
  over a closed one, cycle guard, no cap (`test_lineage.py`, 12 tests)
- Deliberate differences: the reference's tip walk always picks one child by an ordering; we fail
  closed with `AmbiguousChain` on two live continuations (phase P02-12). Its lineage flag CTE takes
  every child of a compression parent; ours takes continuations only (a branch child keeps its own
  flags). `resolve_id` raises typed errors instead of returning None. Status ignores inactive rows.
- Not taken: gateway routing columns and their inheritance, profile namespace fence, conversation
  generation bump, queued-prompt retirement and legacy reset-marker freezing on reopen, canonical
  bot-chat exemption from pin-unhide, activity label clearing (no callers before P04/P15).

## Status

Done (2026-10-18). `sessions.py` + `lineage.py`, 62 + 12 unit tests. Shared: three new codes in
`foundation/errors.py`; traps ledger rows R03.11–R03.13 point at these tests.
