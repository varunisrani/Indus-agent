# Task: rewind, clear, branch (P02-11) Phase: 02 Owner: P02 wave-4 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_rewind.py` (all),
`hermes_state_mutation_branch.py` (skim: boundary rule, child row, title), `hermes_state_messages.py`
(`rewind_to_message` 1835, `clear_messages` 1933), `hermes_state_mutation_transcript.py`
(`rewind_in_transaction`), `hermes_state_search.py` (`list_recent_user_messages` 778),
`tests/hermes_state/test_rewind_surfaces_invariant.py` (all). Closed before writing.
Evidence: none.

What it does (own words): the store half of `/undo`, `/retry`, `/clear` and `/branch`.
`rewind` takes back one or more user turns: every live row from the chosen user message onward is
soft-deleted (`active = 0`, so audit reads and `include_inactive` still see it), and the chosen
message's text comes back so the surface can offer it again. Because the running process keeps its
own copy of the conversation, the store refuses to rewind unless that warm copy agrees with the
durable transcript (same number of user turns, same target text); otherwise a stale UI could cut
the wrong turn. `clear` empties a transcript. `branch` makes an independent copy of a conversation
up to a chosen live message, with its own title, in one transaction.

Inputs → outputs:
- `rewind(store, session_id, user_ordinal, warm_history) -> RewindResult(removed, target_text,
  new_head_id, target_id)`; ordinal 0 = oldest user turn, negative counts back from the newest and
  clamps at the oldest; ordinal past the newest or no user turns → `RewindUnavailable`; warm history
  disagreeing → `RewindMismatch`; live compression lock → `CompressionBusy`. In all refusals nothing
  changes.
- `recent_user_messages(store, session_id, n=20) -> list[UserTurn(id, timestamp, preview)]`, newest
  first, live rows only, handoff rows skipped, whitespace collapsed, over 80 chars → 77 + "...".
- `clear(store, session_id) -> int` deletes every message row and zeroes `message_count` and
  `tool_call_count`.
- `branch(store, session_id, *, upto_message_id=None, title=None) -> str` (new id).

Rules:
- user turns = live `role = 'user'` rows that are not compression handoff rows, in id order. The
  warm history's user turns use the same rule; a warm message counts as a handoff when it carries the
  handoff finish reason or its text equals a durable handoff row's text (so a warm list built
  without that field still lines up).
- rewind effect, in one write transaction: soft-delete live rows with id ≥ target, increment
  `rewind_count`, recompute `message_count` / `tool_call_count` from the live rows, `new_head_id` =
  highest live id left (None when empty).
- `target_text` = the target's content as text (string as-is; list parts → their text parts
  joined with no separator, so retry re-sends the exact bytes).
- branch: parent must exist (`BranchRefused`); boundary = `upto_message_id` (must be a live row of
  that session, else `BranchRefused`) or the newest live row; an assistant boundary also takes the
  tool rows right after it (they answer its calls). Child: fresh id, `parent_session_id` = parent,
  `model_config` = `{"_branched_from": parent}` only, same source,
  profile, user, route (model, billing provider/base URL), prompt hash, workspace; live rows up to
  the boundary cloned (fresh ids, same uids) through `messages.clone_rows_in`; counters recomputed;
  title = `title` with user provenance, else `titles.next_lineage_title_in(parent title or
  "branch")` with derived provenance; branch-safe session metadata copied
  (`session_meta.copy_branch_safe_in`). Parent untouched; child starts unpinned, unarchived,
  visible.

Files: `src/indus/state/rewind.py`, `tests/unit/state/test_rewind.py`.

Edge cases / failure modes:
- empty transcript → `RewindUnavailable`; ordinal −5 with 3 turns → oldest turn.
- warm history with an extra or missing user turn, or different target text → `RewindMismatch`.
- target text in a multimodal user row compares structurally (JSON of the parts).
- rewound rows hidden from `messages.as_conversation` and search (they are `active = 0`), still in
  `get(include_inactive=True)`.
- branch boundary on an inactive row or on another session's row → `BranchRefused`, nothing
  written; a user title already taken → `TitleTaken`, nothing written.
- branch callback may be retried by the writer: the child id is minted outside the callback.

Our design differences / decisions:
- no composite "summary + live ask" carriers (our handoff rows are separate rows), so no scaffold
  re-insert on rewind; no `require_retryable` / `adopt_row_ids` modes (surfaces do that).
- no gateway routing receipt or editor-MCP policy on branch (P15/P17 own those).
- turn-lease refusal waits for P17 (leases unused until then); the compression lock is checked.
- new error codes `store.rewind_unavailable`, `store.rewind_mismatch`, `store.branch_refused`.

Tests we will write:
- rewind −1 removes the last exchange and returns its text; ordinal 0; clamp; counters and
  `rewind_count`; new head id.
- mismatched warm history (count, text) changes nothing; no-user-turn transcript refused.
- rewound rows hidden from live reads, visible to audit reads.
- live compression lock refuses.
- `recent_user_messages` previews, order, handoff skip, inactive skip.
- clear zeroes counters.
- branch: copies up to the boundary, parent untouched, title `name #2`, explicit title user
  provenance, assistant boundary takes its tool results, inactive boundary refused, child kind
  marker, metadata copy called.

## Decisions for the lead
- Message counters after rewind/branch/publish are recomputed as: `message_count` = live rows,
  `tool_call_count` = total length of the live rows' `tool_calls` JSON arrays. If P02-10 counts
  tool calls differently, `rewind._recount_in` is the one place to align.
- Rewind refuses while a live compression lock exists (a summary in flight would otherwise
  summarise rows the user just took back).

Compare checklist (filled after the rewrite; reopened the files named in `Read:`):
- [x] ordinal resolution: 0 oldest, negative from the newest clamped, past the newest refused
  (tests: `tests/unit/state/test_rewind.py::test_rewind_by_ordinal_and_clamping`, `::test_nothing_to_rewind`)
- [x] warm-history validation before any write, nothing changes on mismatch
  (`::test_mismatched_warm_history_changes_nothing`)
- [x] soft delete with the target included, `rewind_count` always +1, counters from live rows,
  head id (`::test_rewind_last_exchange_returns_its_text`, `::test_tool_exchange_goes_with_its_turn`,
  `::test_rewound_rows_stay_for_audit`)
- [x] refused while a live compression lock exists (`::test_live_compression_lock_refuses`)
- [x] recent user messages: newest first, handoffs skipped, whitespace folded, 77 + "..."
  (`::test_recent_user_messages`, `::test_handoff_rows_are_not_user_turns`)
- [x] clear resets the message counters (`::test_clear_zeroes_counters`)
- [x] branch boundary must be live; assistant boundary takes its tool results; title rule
  (`::test_branch_copies_up_to_the_boundary`, `::test_assistant_boundary_takes_its_tool_results`,
  `::test_branch_refusals_write_nothing`, `::test_branch_whole_transcript_with_user_title`)
- [x] gap found: the branch child must not inherit the parent's per-session `model_config` — it now
  gets only `{"_branched_from": parent}`, pinned by `::test_branch_copies_up_to_the_boundary`
- Deliberate differences: validation and write share one transaction, so no separate pin of the
  active id set is needed; merged `user;user` turns (an unanswered ask) count as two turns — the
  warm history from `as_conversation` does not merge them either.
- Not taken: composite summary carriers, `require_retryable` / `adopt_row_ids` modes,
  `include_inactive` on recent user turns, gateway routing receipts and editor policies on branch,
  turn-lease refusal (P17).

## Status

Done (2026-10-18). `rewind.py` with 17 unit tests. Uses slice A (`sessions.create_in/get_in`,
`messages.decode_content/clone_rows_in`, `titles.set_title_in/next_lineage_title_in`) and slice D
(`session_meta.copy_branch_safe_in`). "Rewound rows hidden from `as_conversation` and search" holds
because they are `active = 0`; the `as_conversation` / search assertions belong to slices A and C.
