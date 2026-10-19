# Task: `memory` tool (P09-02) Phase: 09 Owner: P09 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/memory_tool.py` (all),
`tools/threat_patterns.py` (scope names only). Closed before writing.
Evidence: none.

What it does (own words): the one model tool that writes the two memory stores. Every call names
one store at the top level (`target: memory|user`). Single shape: `{action: add|patch|remove,
target, content?, old_text?}` (`new_text` is accepted for `content`). Batch shape: `{target,
operations: [{action, content?, old_text?}, ...]}` against that one store, applied all-or-nothing
and checked against the limit only on the final list. There is no read action (the entries are in
the prompt, and every error that needs them carries them). `replace` is refused with a pointer to
`patch`. `add` appends; an exact duplicate is a success that says it was not added again. `patch`
picks one entry with `old_text` (an entry equal to it wins over partial hits), the span must occur
once inside that entry, and only that span changes. `remove` drops the whole entry. Errors are
recoverable and say what to send next: missing `old_text` returns the current entries and usage;
no match returns the three closest entries; several matches ask for a longer substring; over the
limit returns usage, the new entry's size, the overflow and the live entries, asking for one batch
that trims and adds. Per turn, after three failed attempts the tool answers with a terminal "stop
retrying, answer the user" result, and every later memory call in that turn gets the same answer
without touching the files. New or patched text passes the strict threat scan (scope `memory`); a
hit is refused naming only the categories. With `memory.notes_enabled` / `memory.profile_enabled`
both off the tool is unavailable (probe) and its guidance is gone; with one off the schema offers
only the live target and writes to the other are refused. Writes land on disk at once and show in
tool results; the prompt keeps its frozen block (P09-04). A foreground success calls the review
counter reset hooks (P09-14 attaches). When the write origin is `background_review` and the run is
unattended, `patch`/`remove` (alone or in a batch) are staged through P09-03 instead; if staging
fails the call is denied. Gate on (`memory.write_approval`) → interactive CLI foreground asks
inline, everything else stages.

Inputs → outputs: tool arguments + session state (turn id, origin context, platform, run context,
broker) → JSON text result (`success`, `message`, `usage`, `entry_count`, and per case
`current_entries`, `closest_entries`, `matches`, `pending_id`, `done`).

Files: `src/indus/learning/memory/tool.py`, `tests/unit/learning/memory/test_tool.py`. Shared:
`src/indus/safety/threat_scan.py` (scopes `memory` and `skill` = the strict set),
`src/indus/tools/toolsets.py` (`memory` and `session_search` toolsets, both in the core list),
`tests/unit/tools/test_toolsets.py`, `src/indus/core/prompt/builder.py` (guidance override slot).

Edge cases / failure modes:
- missing / unknown target, or an op naming another target → refused, nothing written;
- `operations` not a list, an op not an object, an unknown action → invalid, nothing written;
- `content` and `new_text` both given → `content` wins;
- target disabled → refused naming the switch;
- lock refused / unreadable file / drift → the store's message, counted as a failure;
- the per-turn counter keys on the turn id; a new turn starts at zero; a success resets it.

Our design differences / decisions:
- result wording and schema text are ours;
- once the cap is reached, further calls in the same turn are refused without a write (the
  reference resets on a later success; ours stops the loop for good in that turn);
- `ambiguous` and `invalid` are not counted as consolidation failures (they are argument mistakes
  that the model fixes in one go); `no_match`, `over_limit`, `stale`, `span_repeated`, `no_op`,
  `drift`, `unreadable` are.

Tests we will write:
- table tests per action (add, duplicate add, patch span, exact beats substring, remove, replace
  refused, no read action, missing old_text with entries, no match with closest, several matches);
- batch remove+add at capacity succeeds while add alone fails, over-budget error carries entries,
  batch abort does not;
- 4th failed attempt → terminal result, later call in the same turn writes nothing; a new turn works;
- threat-scan hit refused with category only;
- one store disabled → schema shows one target, writes to the other refused; both off → probe false;
- unattended review `remove` lands in pending, not on disk; staging failure denies;
- gate on: CLI foreground asks (approve writes, reject refuses), other surfaces stage;
- golden prompt bytes unchanged after a successful add in the same session (with P09-04).

## Decisions for the lead
- The write-origin context variable lives in `learning/pending.py` (P09-03) so the memory and skill
  paths share one; P09-12's provenance helpers should re-export it rather than define a second one.
- Inline asking uses the broker's `approval` kind (choices `once` / `deny`), `command` = the change
  summary; a client that cannot answer approvals → staged.
- Host wiring: `learning.memory.tool.memory_wiring(...)` builds the inline executor; the host adds
  it to `AgentOptions.inline` and registers `register_tools` into its registry (not done in the CLI
  host by this slice; see the slice report).

Compare checklist (filled after the rewrite; reopened `tools/memory_tool.py`):
- [x] target required at top level, one store per call (test: `test_tool.py::test_target_is_required`, `test_batch_op_with_other_target_refused`)
- [x] replace refused with a patch pointer (test: `test_tool.py::test_replace_refused`)
- [x] missing old_text returns entries + usage (test: `test_tool.py::test_missing_old_text_is_recoverable`)
- [x] batch at capacity (test: `test_tool.py::test_batch_remove_and_add_at_capacity`)
- [x] retry cap 3 → terminal (test: `test_tool.py::test_fourth_failure_is_terminal`)
- [x] unattended review patch/remove staged with the pinned entry (test: `test_tool.py::test_unattended_review_remove_is_staged`)
- [x] one store disabled → narrowed schema (test: `test_tool.py::test_one_store_disabled_narrows_schema`)
- Deliberate differences: terminal result sticks for the rest of the turn; ambiguous/invalid calls do
  not count toward the cap; no per-call metrics.
- Not taken: external memory providers (P13); a `read` action.

## Status

Done (2026-10-19). Shared: threat_scan scopes, toolsets, builder guidance slot (see report).

## Integration (lead, P09 wave 1 landing, 2026-10-19)

Compare redone at integration (lead reopened `tools/memory_tool.py`, scope names of
`tools/threat_patterns.py`, then closed them): retry cap 3 with a terminal fourth answer, duplicate add
as a quiet success, `new_text` alias, `replace` refused towards `patch`, unattended review
patch/remove (alone or in a batch) staged with the pinned entry and denied when staging fails, strict
threat scope: all match. Host wiring done (`learning.host`, `cli/runtime.with_learning`): the registry
holds the schema, the inline executor exists only when `memory` is selected, tested by
`tests/integration/learning/test_chat_wiring.py`. Differences recorded as D-16.136.
