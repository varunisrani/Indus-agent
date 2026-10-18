# Task: Wire copy + send-time sanitizer (P04-04) Phase: 04 Owner: wave-5 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/agent_runtime_helpers.py`
(`sanitize_api_messages` and its steps `_drop_invalid_roles`, `repair_empty_non_final_messages`,
the heal log/notice state, `_drop_empty_tool_calls_arrays`, `_repair_invalid_tool_call_names`,
`_drop_results_without_ids`, `_pair_tool_calls_positionally`, `_dedupe_tool_call_ids`,
`_realign_tool_result_names`, `_msg_has_payload`, ~2640–3170; `drop_thinking_only_and_merge_users`
and `_merge_user_content`, ~1230–1300), `agent/turn_request_assembly.py` (`assemble_api_request`,
order of steps, 106–215), `agent/turn_context.py` (`substitute_api_content`,
`drop_stale_api_content`, `extract_api_content_sidecar`, 135–153), `agent/conversation_loop.py`
(`_canonicalize_tool_call_arguments` memo, `_clone_message_for_send`,
`_canonicalize_api_tool_calls`, 995–1060), `agent/message_sanitization.py` (`coerce_tool_name`,
`_repair_tool_call_arguments` and helpers, `uniquify_tool_call_ids` for the `_d<n>` rule),
`run_agent.py` (`_VALID_API_ROLES`, `_is_thinking_only_assistant`). Test names only of
`tests/agent/test_message_sequence_repair.py`. Spec: `01-agent-core.md` §3.7, traps R01.1,
R01.2. Closed before writing.
Evidence: none.

What it does (own words): each model call sends a copy of the history, never the history itself.
`build_wire_copy(rows, route)` turns transcript rows into fresh provider-neutral message dicts:
the `api_content` sidecar (the exact bytes sent before) replaces `content` on user and assistant
rows, and fields that only matter for storage (uids, row ids, display kind, scaffold flag, finish
reason, timestamps) are left out. `sanitize_wire(messages, route)` then makes the copy safe for
the route without touching its input: drops roles the route does not take, heals empty non-final
user/assistant messages with a fixed placeholder (counted per session; a one-time notice when it
keeps happening), removes empty `tool_calls`, coerces tool names to `^[A-Za-z0-9_-]{1,64}$`, drops
results without an id, renames a repeated call id to `<id>_d<n>` (smallest free n from 1, the
matching result in the run after it renamed with it), pairs calls and results positionally
(results outside their call's run are dropped; an unanswered call gets a fixed stub result),
realigns each result's `name` with its call, drops thinking-only assistant rows and merges the
user rows that leaves adjacent, trims string content, and canonicalises tool-call argument JSON
(sorted keys, compact separators) through a bounded memo. Everything is deterministic, so the
same history always produces the same bytes (prompt-cache stability, R01.1).

Inputs → outputs: rows + route → `list[dict]`; messages + route (+ optional `HealLedger`) →
new `list[dict]`. `prepare_wire(rows, route, heals=None)` = both. `canonical_arguments(text)` →
canonical JSON text; `ArgsMemo(max_entries=4096, max_bytes=32 MiB)`. `WireRules` (roles,
positional pairing, thinking-only drop) from `rules_for(route)`.

Files: `src/indus/core/wire.py`, `tests/unit/core/test_wire.py`.

Edge cases / failure modes:
- `api_content` that is empty or not a string is ignored; tool rows never take it.
- the last message is never healed (it is the request's prompt or the open assistant prefill).
- a tool name that is all invalid characters → fixed fallback name `unnamed_tool`.
- one id used twice in one assistant row with two results → `_d1` on the second call and second
  result; the same id reused in a later turn (servers that number every call `call_0`) → that
  turn's call and result get the next free suffix; earlier ids are never rewritten, so a growing
  history keeps its prefix bytes.
- a suffix that collides with a real later id is skipped (`x_d1` taken → `x_d2`).
- results before their call, after a user row, or a second result for one call → dropped.
- malformed argument JSON → small repair (lenient parse, trailing commas, missing closers), else
  `{}`; repairs are not memoised. Memo evicts oldest-first past 4,096 entries or 32 MiB.
- input dicts and lists are never mutated (structural clone first).
- a thinking-only assistant row at the very end is dropped too (strict wires reject a trailing
  thinking block).

Our design differences / decisions:
- Duplicate ids are renamed here on the copy (theirs renames on intake and *drops* duplicates at
  send time); the phase row asks for the `_d<n>` rename at send time, counting from 1.
- Rename runs before pairing so two calls sharing one id both keep their results.
- Rules come from `rules_for(route)`: the set of roles per API mode; positional pairing and the
  thinking-only drop default on for every route (theirs runs both unconditionally); a caller can
  pass its own `WireRules`.
- Heal counting is an explicit `HealLedger` object the session owns (theirs keeps module globals
  keyed by a logging context); notice after 3 heal events, once per ledger.
- Placeholder and stub texts are ours.
- Lone surrogates in string content are scrubbed on the copy with the state codec's scrubber.
- Not taken: mixture-of-agents context, prefill injection, context-engine hook, image eviction,
  cache-marker plan, request pressure (other tasks or later phases); Responses alias ids;
  SDK-object tool calls (ours are always dicts).

Tests we will write: no mutation (identity + deep-equal on rows and input messages); twice →
identical JSON bytes; `_d1`/`_d2` cases incl. collision and constant id across turns, prefix
stability when a turn is appended; each step's table; heal count + one-time notice; memo
eviction by count and by bytes; canonical args; thinking-only drop + user merge; seeded-random
property test (no hypothesis): sanitized output passes a strict-alternation validator
(results right after their call run, one result per call, no same-role neighbours other than
tool/tool, unique call ids).

## Decisions for the lead
- Route quirks (rejected roles, strict pairing) are not on `ProviderProfile` yet; `rules_for`
  derives them from `api_mode` and honours an optional `wire_rules` attribute on the profile
  (none set today). P08 profiles can declare it.

Cross-slice names used: none.

Compare checklist (filled after the rewrite; reopened `agent/agent_runtime_helpers.py`
2640–3170, `agent/turn_request_assembly.py` 106–215, `agent/conversation_loop.py` 995–1060):
- [x] step order matches theirs (roles → heal → empty arrays → names → id-less → pairing →
  duplicate ids → names realigned), then thinking-only drop + merge, trim/surrogates, canonical
  args; our id rename moved before pairing (see below)
- [x] heal leaves the last message alone, counts heals, one notice per session
  (tests: `tests/unit/core/test_wire.py::test_empty_non_final_messages_are_healed_and_counted`,
  `test_heal_notice_is_asked_for_once`)
- [x] empty / non-list `tool_calls` removed (test: `test_empty_tool_calls_arrays_are_removed`)
- [x] name coercion: valid names unchanged, invalid runs → `_`, cut at 64, fallback name
  (test: `test_tool_names_are_coerced`)
- [x] positional pairing: results before their call or past a user row dropped, unanswered calls
  stubbed at the end of their run (test:
  `test_positional_pairing_drops_displaced_results_and_stubs_missing_ones`)
- [x] constant ids reused by a server across turns keep every result (test:
  `test_constant_id_across_turns_is_renamed_stably`); `_d<n>` deterministic and prefix-stable
  (tests: `test_repeated_ids_in_one_row_get_d1_d2`, `test_appending_a_turn_keeps_the_prefix_bytes`,
  `test_suffix_collision_takes_the_next_free_number`)
- [x] result names realigned only when present and different (tests:
  `test_result_names_are_realigned_with_their_call`, `test_result_without_a_name_stays_without_one`)
- [x] thinking-only drop + user merge (blank line, parts concatenated) on the copy only (tests:
  `test_thinking_only_rows_are_dropped_and_users_merged`,
  `test_steer_and_prompt_rows_merge_on_the_wire_only`)
- [x] canonical memo bounds 4,096 / 32 MiB; malformed text repaired, not memoised (tests:
  `test_memo_evicts_oldest_past_the_entry_cap`, `test_memo_evicts_past_the_byte_cap`,
  `test_broken_arguments_are_repaired`, `test_default_memo_caps_match_the_spec`)
- [x] sidecar substitution only for user/assistant string sidecars, tool rows never (test:
  `test_wire_copy_substitutes_sidecar_and_strips_storage_fields`)
- [x] no mutation, identical bytes twice (tests: `test_durable_rows_are_never_mutated`,
  `test_sanitize_does_not_mutate_its_input`, `test_same_input_twice_gives_identical_bytes`)
- [x] property: 500 random payloads sanitized + 300 repaired-then-sanitized pass the strict
  validator (seeded-random; hypothesis not installed)
- Deliberate differences: theirs drops duplicate ids at send time and renames only on intake from
  `_d2`; ours renames on the copy from `_d1` (the phase row). Their stubs are added in sorted-id
  order; ours in call order. Their memo is first-in-first-out; ours refreshes an entry on a hit.
  Ours also merges adjacent assistant messages on the copy (theirs relies on repair having run).
  A call with no id gets the positional id `call_<message>_<position>` (theirs leaves it unpaired).
- Not taken: Responses alias variants, SDK-object tool calls, image eviction, MoA, prefill
  messages, context-engine hook, cache-marker plan, request pressure.

## Status

Done (2026-10-18). Files: `src/indus/core/wire.py`, `tests/unit/core/test_wire.py` (47 tests).
