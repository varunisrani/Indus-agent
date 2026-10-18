# Task: Pruning and boundaries (P08-03) Phase: 08 Owner: wave-10 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/context_compressor.py`
(the `compress` method's prune and boundary steps: `_prune_boundary`, `_walk_tail_budget`,
`_tail_soft_ceiling`, `tail_token_budget`, `_find_tail_cut_by_tokens`, the align-backward/forward
helpers, the skill-prune marker constants), `agent/conversation_compression.py` (skimmed the
contract docstring). Spec: research `01` §3.19 algorithm steps 1–2. Closed before writing.
Evidence: none.

What it does (own words): before anything is summarised, cheap work is done without a model.
Step 1 (prune): every tool result longer than 200 characters that sits before the protected tail
becomes a one-line placeholder naming the tool and the size it had; the newest tool round that
the model has not answered yet is never touched; a pruned skill load leaves a
`[skill elided: <name>]` marker, and the markers (at most 20, newest first) are listed under one
heading so the summary can tell the model which skills to reload. Step 2 (boundaries): the head
is the first three rows (the system row when present plus the first exchange); the tail is found
by walking backwards adding row estimates until a token budget is met. In lean mode (default) the
budget is 2.5 % of the window clamped to [10,000, 25,000] and to at most 20 % of the window; rows
may overrun it by 1.5× (still ≤ 20 % of the window) so whole rows are kept; the tail keeps at
least 8 rows (when there are that many to spare) and at least `min_tail_user_messages` real user
rows. Legacy mode uses `threshold × 0.20`. A boundary never falls between an assistant tool call
and its results: it walks back to the parent assistant row.

Inputs → outputs: messages + `TailPolicy(window, threshold, mode, min_user)` →
`PruneResult{messages, pruned, skills}` and `Window{head_end, tail_start}` (middle =
`messages[head_end:tail_start]`, empty when nothing can be compressed).

Files: `src/indus/core/compression/boundaries.py`, `tests/unit/core/compression/test_boundaries.py`.

Edge cases / failure modes:
- tiny transcripts (head + tail cover everything) → empty middle (a structural no-op, no strike);
- the newest row alone over budget → it stays in the tail with its tool group;
- a tool group longer than the budget → kept whole (may overrun the ceiling);
- multimodal tool results (lists) → image parts dropped with a text note, not counted as 200 chars;
- already-pruned placeholders are not pruned again;
- skill name read from the call's JSON arguments (`name`), tool named `skill_view` or `skill_load`.

Our design differences / decisions:
- Placeholder text and skill marker are ours (`texts.py`).
- No dedupe pass, no "pressure demotion" inside the tail, no stale-tail demotion stubs (lean
  recovery stubs): the tail stays verbatim; the summary's session-search pointer is the recovery
  path. Recorded as a follow-up if the soak (P08-18) shows tails too large.
- Real user rows exclude steer-less scaffolding, summary rows and our own handoff rows.

Tests we will write:
- seeded property over random transcripts: boundary never between a call and its results; tail
  meets the row and user minimums whenever the transcript allows; pruned placeholders only
  before the tail; the newest unanswered round untouched;
- skill markers capped at 20, newest first;
- budget table for lean/legacy at several windows.

Compare checklist (filled after the rewrite; reopened the compressor's tail walk,
`_tail_soft_ceiling`, `_prune_boundary`, `_find_tail_cut_by_tokens`, the skill-marker constants):
- [x] Tail budget formula, 20 % hard share, 1.5× soft overrun (test: `tests/unit/core/compression/test_boundaries.py::test_tail_budget_table`)
- [x] Boundary never splits a tool group; head never ends inside one (test: `test_boundaries.py::test_property_boundaries`)
- [x] Tail row floor (counted like the reference: rows after the head minus one) and user
  minimum (test: `test_boundaries.py::test_property_boundaries`)
- [x] Gap found: when every row fits the budget the reference still keeps only the floor so a
  middle exists — added to `find_tail_start`
- [x] Pending tool round spared (test: `test_boundaries.py::test_pending_round_is_never_pruned`)
- [x] Placeholders only before the tail, input untouched (test: `test_boundaries.py::test_property_boundaries`)
- [x] Skill markers ≤ 20, newest first (test: `test_boundaries.py::test_skill_markers_capped_at_twenty`)
- Deliberate differences: no dedupe pass, no pressure demotion inside the tail, no lean
  stale-tail recovery stubs, no assistant-reply/oversized-turn anchors (the user-row minimum is
  the anchor). Follow-up if the P08-18 soak shows tails too large.
- Not taken: `protect_last_n` beyond the 8-row floor.

## Status

Done (2026-10-18).
