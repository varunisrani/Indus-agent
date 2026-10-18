# Task: messages (P02-10) Phase: 02 Owner: wave-4 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_messages.py`
(`_coerce_timestamp`, tool-call parsing/counting, `_encode_content`/`_decode_content`,
`_check_transcript_write_guards` for which checks an append runs, `_message_row_params`,
`_bump_session_counters`, `append_message`, `append_messages_batch`, `_insert_message_rows`,
`replace_messages`/`replace_messages_in_transaction`/`_stamp_kept_live_prefix`,
`_clone_message_rows`, `get_messages`, `_fetch_conversation_rows`, `get_messages_as_conversation`
and the start of `_rows_to_conversation`, `_resume_lineage_ids`, `assert_resume_safe`),
`agent/message_sanitization.py` (`_sanitize_surrogates` only), `hermes_state.py` (the content
prefix constant only), `hermes_cli/timefmt.py` (the epoch window constants only), test names in
`tests/hermes_state/test_message_uid.py`. Closed before writing.
Evidence: none.

What it does (own words): the transcript. Every message is a row; the row id (AUTOINCREMENT, never
reused) is the order — timestamps can go backwards (clock steps, imports) and are display data
only (R03.14). An append is the most important write in a turn, so it runs under the long
`TRANSCRIPT` budget; it bumps the session's message and tool-call counters in the same
transaction. Values SQLite cannot bind or that would let an import forge structure are made safe
on the way in:
- list/dict content (multimodal parts) is stored as text: our reserved prefix
  `"\x00indus.parts:"` + JSON. A plain string that happens to start with the prefix is stored as
  prefix + a JSON *string*, so reading it back yields the same plain string, never structure.
- lone UTF-16 surrogates (`\ud800`) make the sqlite3 module raise and the whole write fail; they
  are replaced by U+FFFD in content, reasoning, `api_content` and tool names (R03.19).
- timestamps outside [2020-01-01, now + 1 day], non-numbers, NaN → now.
- reasoning sidecars (`reasoning`, `reasoning_details`) are stored as given (JSON for
  structures), `api_content` as sent.
Each message carries a durable `message_uid` (32 hex): kept when the caller supplies one (a copy of
a stored message), minted otherwise; every copy (replace, clone, branch, import) carries it. An
assistant row's tool calls get `tool_call_uids` = {provider call id → uid}.

A batch append is all-or-nothing: every row is validated before the transaction starts, then the
rows are inserted in groups of 500 per statement in one transaction. Reads come back in id order;
the default read is the live rows, `include_inactive` adds rewound/soft-replaced rows (audit).
`as_conversation` is the provider-facing view of the live rows (no uids, no store columns).
`replace` swaps a transcript atomically: destructive (delete then insert), or soft (rows that
still match the leading live rows keep their ids and uids, the divergent live tail gets
`active = 0`, only the new tail is inserted). The pinned variant takes the live id list the caller
computed from and refuses when it moved. Ordinary appends never look at the compression lock
(R03.10): the rotation clones anything appended after its watermark, so blocking appends would
only kill turns. An append to a session already ended by compression is refused — its transcript
continues in the child.

Resume guard: counting the live rows of the lineage up to the session (root … this id), more than
`sessions.resume_message_limit` (default 20,000; 0 disables) raises `ResumeTooLarge` numeric 4130
with the exact count. The export guard (numeric 4131) counts every row of one session against a
limit (helper for P02-17).

Inputs → outputs: `encode_content`, `decode_content`, `append(store, sid, msg) -> id`,
`append_in(conn, sid, msg, now=)`, `append_batch(store, sid, msgs) -> [ids]`,
`append_batch_in`, `get(store, sid, include_inactive=False) -> [dict]`, `as_conversation`,
`replace(store, sid, rows, soft=False, expect_active_ids=None) -> [live ids]`, `replace_in`,
`clone_rows_in(conn, to_sid, row_ids) -> [new ids]`, `live_ids_in`, `resume_limit(scope)`,
`check_resume_size(store, sid, limit=None) -> count`, `check_resume_size_in`,
`check_export_size(store, sid, limit=None)`, `check_export_size_in`.
On `StoreReplaced` an append/batch writes the pending rows to `<home>/sessions/<sid>.jsonl`
(P02-07 divert) and re-raises.

Files: `src/indus/state/messages.py`, `tests/unit/state/test_messages.py`;
shared: `src/indus/foundation/errors.py` (codes).

Edge cases / failure modes:
- content None / empty string / number / nested parts / prefixed literal / non-JSON object
  (stored as its text).
- `tool_calls` as a list, a JSON string, garbage text (invalid row), a dict (one call).
- a batch with one row lacking a role → `ValueError`, nothing written; empty batch → `[]`.
- an append for a missing session → the foreign-key error (classifier: session_row_missing).
- decreasing timestamps → id order wins; a year-2099 timestamp → now.
- clone keeps every payload column byte for byte (including columns added later by reconcile),
  fresh ids, active, not compacted, in id order, target counters bumped.
- soft replace with nothing in common archives the whole live set; with an identical list is a
  no-op for ids.

Our design differences / decisions:
- our prefix and our column names; one validated row shape shared by append, batch, replace.
- no display projections, reactions, checkpoints, turn leases or transcript repair here (later
  phases); destructive replace deletes inactive rows too (the reference's default).
- the resume count is exact (not capped at limit + 1) so the error can say how many.
- clone bumps the target's counters (the child's counters match its rows).

Tests we will write (`test_messages.py`):
- multimodal round trip; prefixed literal stays a string; surrogates → U+FFFD in content,
  reasoning, api_content, tool name; codec table.
- decreasing timestamps read in id order; out-of-window timestamps clamped.
- counters bumped; uid minted (32 hex) or kept; tool_call_uids map.
- batch all-or-nothing; batch over 500 rows; ids ascending.
- replace destructive / soft (archives tail, keeps prefix ids and uids) / pinned with moved set;
  uid stable across replace.
- as_conversation strips uids and inactive rows; clone_rows_in byte-exact.
- resume guard: 20,001 rows → 4130 with the count; 0 disables; config read; lineage counted.
- export guard 4131; replaced store diverts pending rows.
- generated-text table (seeded random, `hypothesis` not installed): arbitrary text round-trips
  except surrogates, which come back as U+FFFD.

## Decisions for the lead
- `CompressionClosed` (code `store.compression_closed`, already listed) lives in `messages.py`;
  append/replace refuse a session ended with `compression`.
- `TranscriptMoved` (new code `store.transcript_moved`) for the pinned replace.
- export limit: `sessions.export_message_limit` when set, else the resume limit.
- `clone_rows_in` bumps the target session's counters.

Compare checklist (filled after the rewrite; reopened the message file above):
- [x] content codec: parts behind the prefix, prefixed literal wrapped as a JSON string, damaged
  payload returned raw, non-JSON value stored as text (tests: `test_messages.py::test_codec_round_trip`,
  `::test_prefixed_literal_is_wrapped_as_a_json_string`, `::test_damaged_parts_payload_reads_back_raw`)
- [x] surrogate scrub in content (nested parts too), reasoning, `api_content`, tool names
  (`::test_surrogates_written_and_read_back_as_replacement`, `::test_surrogates_scrubbed_inside_parts`)
- [x] timestamp window with the phase bounds (`::test_timestamp_window`)
- [x] counters, uid keep/mint, tool-call uid map (`::test_counters_uids_and_tool_call_uids`,
  `::test_supplied_tool_call_uids_are_kept`)
- [x] batch all-or-nothing, validated before the write (`::test_batch_with_one_invalid_row_writes_nothing`,
  `::test_batch_inside_a_transaction_is_all_or_nothing`, `::test_large_batch_spans_statement_groups`)
- [x] id order, never timestamp; include_inactive (`::test_decreasing_timestamps_still_read_in_id_order`)
- [x] replace destructive / soft kept prefix / pinned (`::test_destructive_replace_keeps_uids_of_rows_given_back`,
  `::test_soft_replace_archives_the_divergent_tail`, `::test_pinned_replace_refuses_a_moved_live_set`)
- [x] clone byte-exact via SQL column copy (`::test_clone_is_byte_exact_with_fresh_ids`)
- [x] resume 4130 over the lineage / export 4131 per segment, 0 disables
  (`::test_resume_guard_counts_past_the_limit`, `::test_resume_guard_counts_live_rows_across_the_lineage`,
  `::test_export_guard_counts_every_row_of_one_session`, `::test_limits_come_from_config`)
- [x] appends ignore the compression lock; a compression-closed session refuses appends and replace
  (`::test_appends_ignore_the_compression_lock`, `::test_compressed_parent_refuses_appends`)
- [x] replaced store diverts pending rows (`::test_replaced_store_diverts_pending_messages`)
- Deliberate differences: numbers as content are stored as parts JSON (the reference binds them
  raw); the resume count is exact rather than capped at limit + 1 and counts live rows (we have no
  in-place compaction yet); clone bumps the target's counters; reasoning kept on every role.
- Not taken: display projections/order/metadata, reactions, checkpoint pruning, turn-lease checks,
  transcript repair and replay dedupe, `message_id` alias for `platform_message_id`, per-row
  timestamp nudging inside a batch (order is the id anyway), absorbed-uid witnesses, tool-result
  uid pairing (our schema keeps the map on the assistant row only).
- `hypothesis` is not installed: the "arbitrary text round-trips" property is a seeded table of 40
  generated cases × 5 strings (`::test_generated_text_round_trips_except_surrogates`).

## Status

Done (2026-10-18). `messages.py`, 103 unit tests. Shared: two codes in `foundation/errors.py`;
traps ledger rows R03.10 (append half), R03.14, R03.19 point at these tests.
