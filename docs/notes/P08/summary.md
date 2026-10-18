# Task: Summary call + deterministic fallback (P08-04) Phase: 08 Owner: wave-10 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/context_compressor.py`
(summary budget `_compute_summary_budget`, input bounding `_bound_summary_input` /
`_sample_summary_records`, the prompt builder's structure (section list, update-vs-fresh forms,
focus topic placement), `_build_static_fallback_summary`, the lean sections: anchor index
patterns, verbatim user messages, recovery footer, session log budget). Spec: research `01`
§3.19 step 3, Open question 6, D-16.23, D-16.24. Closed before writing.
Evidence: none.

What it does (own words): the middle of the conversation is turned into one handoff summary with
**one** auxiliary-model call (task `compression`). The prompt (ours) asks for fixed sections:
goal, constraints and preferences, progress (done / in progress / blocked), decisions, relevant
files, next steps, critical context; in lean mode also a dense session log (~4,000 tokens). The
turns are serialised as records (role, text, tool calls with arguments cut to 1,500 chars, tool
results cut head+tail) and, when they exceed 160,000 characters, sampled evenly in 8 slices with
elision markers (the newest record always kept). The output budget is `content tokens × 0.20`
clamped to [2,000, min(window × 0.05, 10,000)] — the code constant 10,000, until REA answers
01 Q6 (D-16.24). A second compression updates the previous summary instead of starting over. A
focus topic (manual `/compress <topic>`) goes last in the prompt. Then three deterministic
sections are appended by us, never by the model: an anchor index of exact identifiers harvested
by regex (PR/issue numbers, commit SHAs, branches, file paths, error lines, URLs; ≤ 7,000 chars),
every real user message verbatim newest-first (24,000-char budget, ≤ 4,000 each), and a pointer
that older detail can be found with session search.

The **deterministic fallback** needs no model: the same structure filled from the user messages,
the file paths and the anchor index (plus the previous summary, trimmed), committed through the
same pipeline when the summary call keeps failing.

Inputs → outputs: middle messages + window/focus/previous/mode → `SummaryPlan{prompt,
max_tokens, sampled}`; a `Summarizer` (the aux client seam) → `SummaryReply{text,
finish_reason}` or a typed `SummaryFailure(kind)`; fallback: middle messages + previous summary →
summary text.

Files: `src/indus/core/compression/summarize.py`, `src/indus/core/compression/fallback_summary.py`,
`src/indus/core/compression/texts.py`, `tests/unit/core/compression/test_summarize.py`,
`tests/fakes/aux_model.py` (fake auxiliary client, shared with P08-14 later).

Edge cases / failure modes:
- empty or whitespace reply → failure kind `empty`; `finish_reason == "length"` → `truncated`;
- timeout / stall → `stall`; 429/503/"overloaded" → `overload`; 401/402/403 → `access`;
  connection / closed stream / bad JSON → `decode`; no auxiliary client at all → `unavailable`;
- secrets in turns are redacted (`foundation.redact.redact_text`) before they enter the prompt or
  the fallback;
- a reply that is only a refusal is treated as `empty`;
- the previous summary is bounded by the same input cap.

Our design differences / decisions:
- All prompt and fallback text written fresh in `texts.py` (D-16.23); headings are ours.
- The summariser is a small protocol (`Summarizer`) so the engine does not depend on P08-17 at
  import time; `AuxSummarizer` takes slice B's `AuxClient` (`call("compression", messages)`,
  `window_for("compression")`) and falls back to the main route caller (`auto` = main route and
  model) when no client is wired.
- The summary sits in our own handoff row, recognised on reload by its opening marker text.

Tests we will write:
- budget clamp table (small middle → 2,000; big middle on 1M window → 10,000; 64K window → 3,200);
- sampled input ≤ 160,000 with markers and the newest record kept;
- second pass: prompt carries the previous summary (update, not restart);
- fallback contains every user message (within budget), extracted SHAs and paths;
- failure kinds from the fake aux model (stall, overload, truncation, empty, decode).

## Decisions for the lead
- Summary ceiling: 10,000 tokens (code constant) until REA answers 01 Q6 — recorded per
  D-16.24; one constant `SUMMARY_CEILING` in `summarize.py`.
- `AuxClient.call(...)` is assumed to return a string or an object with `.text` (or `.content`)
  and `.finish_reason`; `AuxSummarizer` is the only place to adjust at integration with slice B.
  The Agent reads `Agent.aux` (set by the host) — wire it to B's client at integration.

Compare checklist (filled after the rewrite; reopened the summary sections of
`agent/context_compressor.py`):
- [x] Budget clamp and ceiling 10,000 (test: `tests/unit/core/compression/test_summarize.py::test_budget_clamp_table`)
- [x] Input ≤ 160,000 with 8 evenly spaced slices, markers, newest record kept (test:
  `test_summarize.py::test_sampled_input_is_bounded`); per-record cuts and redaction
  (`test_summarize.py::test_records_cut_and_redact`)
- [x] Update-not-restart, focus last (test: `test_summarize.py::test_second_pass_updates_the_previous_summary`,
  `test_engine.py::test_second_compaction_updates_the_notes`)
- [x] Fallback keeps user messages, anchors, files, previous notes (test:
  `test_summarize.py::test_fallback_keeps_user_messages_and_anchors`)
- [x] Anchor index ranked and bounded; user messages budgeted (tests:
  `test_summarize.py::test_anchor_index_ranks_and_bounds`, `test_summarize.py::test_user_messages_budget`)
- [x] Failure classification (test: `test_summarize.py::test_failure_kinds`, `test_summarize.py::test_stall_hits_the_deadline_on_the_clock`)
- [x] The budget is prompt guidance, not a wire cap (the reference never caps the summary call
  on the wire); a provider cut shows as `truncated`
- Deliberate differences: no aux→main retry inside one attempt (the cooldown ladder retries);
  no temporal-anchoring date line; no memory-provider block (P09); our headings and prompt.
- Not taken: long refusal-prefix list; historical task snapshot heading.

## Status

Done (2026-10-18). Integration with slice B: `AuxSummarizer` takes B's `AuxClient` (duck-typed
`call("compression", messages)` + `window_for("compression")`); the Agent passes `Agent.aux` when
the host sets it, else the main route caller.

## Integration (lead)

Merged in the P08 wave 1 integration (2026-10-18). `AuxSummarizer` adapted to slice B's client:
`call()` returns an `AuxResult` (text + `response.finish_reason`), `window_for(task, main_window)`
is async and is asked once when the session starts (`BuiltinEngine.on_session_start`). The Agent
passes its bound `Agent.aux`; without one the main route is used and recorded under the
`compression` task tag. The test-only aux stand-in in `test_summarize.py` was replaced by a test
over the real `AuxClient` with a mocked HTTP layer.
