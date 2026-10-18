# Task: Anthropic Messages transport (P03-10) Phase: 03 Owner: P03-B1 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/transports/anthropic.py` (all),
`agent/anthropic_adapter.py` (thinking parameters only: `THINKING_BUDGET`, `_thinking_kwargs`; the
OAuth / identity paths skipped on purpose, R01.12), `agent/anthropic_message_convert.py` (block
conversion), `tests/fakes/providers/anthropic_messages.py` (which events a fake emits). Closed before
writing. Wire format checked against Anthropic's streaming guide
(https://platform.claude.com/docs/en/build-with-claude/streaming, read 2026-10-18): event flow
`message_start` → per block `content_block_start` / `content_block_delta` / `content_block_stop` →
`message_delta` (usage cumulative) → `message_stop`; `ping` anywhere; `error` events such as
`overloaded_error`; `signature_delta` just before a thinking block's stop; unknown events must be
tolerated.
Evidence: none.

What it does (own words): converts OpenAI-shaped history into a Messages request
(`POST {base}/v1/messages`) and the SSE reply into normalized events. System messages become the
top-level `system`; assistant `tool_calls` become `tool_use` blocks; runs of `tool` messages become
one user turn of `tool_result` blocks in call order; images become `image` blocks; blank text is
dropped. Thinking: the effort ladder maps to `budget_tokens` (low 4,000 · medium 8,000 · high
16,000 · xhigh/max/ultra 32,000, other values 8,000), forcing `temperature: 1` and
`max_tokens ≥ budget + 4,096`; models the profile lists as adaptive get
`thinking: {type: adaptive}` + `output_config.effort`. Signed thinking blocks kept in
`reasoning_details` are replayed byte for byte at the start of the assistant turn.

Inputs → outputs: route + `ChatRequest` → `httpx.Request`; SSE → events; JSON message → response.

Files: `src/indus/providers/transports/anthropic.py`,
`tests/unit/providers/transports/test_anthropic.py`.

Edge cases / failure modes:
- base URL ending in `/v1` is not doubled.
- official host (`api.anthropic.com`, exact hostname) → `x-api-key`; compatible hosts whose
  profile says so → `Authorization: Bearer`; `anthropic-version: 2023-06-01` on every request.
- tool arguments that are not a JSON object → `{"_raw": "<text>"}` plus a warning log (never the
  argument text in the log).
- tool ids outside `[A-Za-z0-9_-]` are rewritten the same way on `tool_use` and `tool_result`.
- empty tool output → `tool_result` without `content`.
- a message left with no blocks is dropped; then same-role neighbours are merged.
- tool_use with no input fragments assembles as `{}`.
- EOF without `message_stop`: clean finish if a stop reason arrived, else `ProviderStreamError`
  (`incomplete_stream`).
- `error` event → `ProviderStreamError` keeping the vendor `type` (e.g. `overloaded_error`).
- stop reasons: `end_turn`/`stop_sequence` → stop, `tool_use` → tool_calls, `max_tokens` and
  `model_context_window_exceeded` → length, `refusal` → content_filter, anything else → stop.

Our design differences / decisions:
- No OAuth identity line, no foreign CLI headers, no tool renames (R01.12); a test asserts no
  request header names another product.
- Usage: `prompt_tokens = input + cache_read + cache_creation` so it means "whole prompt" as on the
  Chat Completions wire.
- Prompt-cache markers are P08.
- Unparseable tool JSON is wrapped (`_raw`) rather than replaced by `{}`, so the model sees what it
  sent.

Tests we will write: conversion goldens (two tool results merged in order, system join, empty text
dropped, invalid JSON wrapped, image data URL, signature replay byte-exact); thinking budgets
(`high` → 16,000, max_tokens 20,096, temperature 1) and adaptive; URL not doubled; auth header per
host; stream golden with thinking + text + tool_use; `error` event; ping → KeepAlive; JSON parse;
no foreign identity headers.

## Decisions for the lead
- Profile extras read with `getattr` and defaults: `adaptive_thinking_models` (model-name prefixes
  that take the adaptive form) and `bearer_auth` (Anthropic-wire host that takes
  `Authorization: Bearer`). Add them to `ProviderProfile` or rename here.
- Default `max_tokens` when neither request nor profile sets one: 8,192 (the API requires it).

Compare checklist (filled after the rewrite; reopened the transport, `_thinking_kwargs` and the
converter):
- [x] consecutive tool results merged into one user turn in call order (test:
  `tests/unit/providers/transports/test_anthropic.py::test_two_tool_results_merged_into_one_user_turn_in_order`)
- [x] budgets low/medium/high/xhigh 4,000/8,000/16,000/32,000, default 8,000, temperature 1,
  `max_tokens ≥ budget + 4,096` (tests: `test_thinking_budgets`, `test_high_effort_golden_numbers`)
- [x] adaptive models: `thinking.type=adaptive` + `output_config.effort` (test:
  `test_adaptive_models_use_effort_field`)
- [x] signed thinking replayed verbatim first in the assistant turn (test:
  `test_signature_replayed_byte_exact`); merged assistant turns drop the later turn's thinking
  (gap found; test `test_merged_assistant_turns_drop_the_later_thinking`)
- [x] stop-reason map incl. `model_context_window_exceeded` → length (test: `test_stop_reason_map`)
- [x] event types, ping, error event (tests: `test_stream_event_sequence_golden`,
  `test_error_event_keeps_type`, `test_unknown_events_tolerated_and_eof_rules`)
- [x] nothing from the OAuth identity path: no identity line, no foreign headers or betas, no tool
  renames (test: `test_no_header_names_another_product`)
- Deliberate differences: bad tool JSON → `{"_raw": text}` (reference: `{}`); empty tool output →
  `tool_result` without content (reference: a placeholder text); several system messages are
  joined (reference keeps the last one); `xhigh`→`max` downgrade for some adaptive models, display
  `summarized`, and the "think by default" disable are not taken (model-specific, P08 can add them
  through the profile).
- Not taken: prompt-cache markers, orphan tool-block stripping, leading-user-turn insertion,
  screenshot eviction, third-party signature stripping, Kimi `reasoning_content` thinking blocks
  (send-time sanitising is P04; vendor policy P08), fast mode, Bedrock guardrail mapping.

## Status

Done (2026-10-18).

## Integration (lead)

- Tests now use the real `RuntimeRoute` / `ProviderProfile` (helpers `make_route`, `make_profile`,
  `FAKE_KEY` in `tests/unit/providers/_support.py`); `tests/unit/providers/_stubs_p03a.py` deleted.
- Traps ledger R01.12: both "Review:" lines (B1's and P03-A's) were dropped; the row now lists the
  profile, http, bundled and both transport header tests.
