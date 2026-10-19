# Task: `pre_llm_call` in the turn prologue (P13-17 deferred seam) Phase: 13 Owner: builder N3

Read: none (product-only wiring of the landed P13-17 bus into P04's prologue; the behaviour rules are
the P13-17 row and `docs/notes/P13/hooks-bus.md`).
Evidence: none.

What it does (own words): once per user turn, before the user row is stored, the prologue asks the
session's `llm_context` source (a new `SessionRuntime` / `AgentOptions` seam, `None` by default) for
extra context. The plugin side's source is `plugins.session.llm_context_source(bus)`, which runs the
bus's `pre_llm_call` (plugin callbacks and shell hooks alike, each bounded by the hook deadline,
answers joined by blank lines in registration order). The text is appended to the user row's
`api_content` sidecar (what is sent), never to `content` (what the person typed) and never to the
system prompt, so the provider's prompt cache stays valid (R07.18, R01.1). Because `api_content` is
stored with the row, later turns replay the same bytes.

Payload: `session_id`, `turn_id`, `user_message` (text of the message), `conversation_history`
(messages before this turn), `is_first_turn`, `model`, `provider`, `platform`, `cwd`.

Order: after the history is loaded, repaired and the prompt is restored/built; before the one-shot
notes are attached (context first, then notes) and before the user row is stored. The turn-start
preflight compaction runs after; the user row (with its context) is in the protected tail, so the
context is never summarised away.

Failure: a source that raises is logged and the turn goes on without context. A blank answer adds
nothing.

Files: `src/indus/core/prologue.py`, `src/indus/core/turn_state.py`, `src/indus/core/agent.py`
(all shared, P04), `src/indus/plugins/session.py` (new), test
`tests/unit/plugins/test_pre_llm_call_turn.py` (a real `Agent` turn against the fake model server).

## Decisions for the lead
- Runs before the preflight compaction, not after (the P13-17 row says "after compaction"): the user
  row must be stored before the gate runs (R01.3), and adding the context after it would need a
  second write of a stored row. The hook sees the repaired history of this turn; the effect for the
  model is the same (context rides this turn's user message, survives compaction in the tail).
- Subagents do not get the source (children are built without it).

Compare checklist: n/a (no reference read for this wiring).

## Status

Done (2026-10-19).
