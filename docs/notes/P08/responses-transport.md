# Task: Responses transport (P08-09) Phase: 08 Owner: P08 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/transports/codex.py`
(`build_kwargs`, `normalize_response`, reasoning fields), `agent/codex_responses_adapter.py`
(id helpers, input conversion, reasoning replay, output scan and incomplete status). Closed before
writing. Wire format checked against OpenAI's API reference for Responses
(https://platform.openai.com/docs/api-reference/responses and the streaming-events page, read
2026-10-18): request `model`, `instructions`, `input` items (`message` with `input_text` /
`input_image` / `output_text` parts, `function_call {call_id, name, arguments}`,
`function_call_output {call_id, output}`, `reasoning {id, summary, encrypted_content}`), function
tools `{type: function, name, description, parameters, strict}`, `tool_choice`,
`max_output_tokens`, `reasoning {effort, summary}`, `include: ["reasoning.encrypted_content"]`,
`store: false`, `stream`; response `status` `completed | incomplete | failed | cancelled |
in_progress | queued`, `incomplete_details.reason` (`max_output_tokens`, `content_filter`),
`error {code, message}`, `usage {input_tokens, input_tokens_details.cached_tokens, output_tokens,
output_tokens_details.reasoning_tokens}`; stream events `response.created`,
`response.output_item.added/done`, `response.output_text.delta`, `response.refusal.delta`,
`response.reasoning_summary_text.delta`, `response.reasoning_text.delta`,
`response.function_call_arguments.delta/done`, `response.completed`, `response.incomplete`,
`response.failed`, `error`.
Evidence: none.

What it does (own words): speaks OpenAI's Responses API for the `responses` API mode. The system
text becomes `instructions`; user turns become `message` items; an assistant turn becomes, in
order, its stored reasoning items (replayed exactly as they arrived), its text as an assistant
`message`, then one `function_call` per tool call; each tool row becomes a
`function_call_output`. Tools become flat function tools. The reply stream is folded into our
normalized events; reasoning items (with `encrypted_content`) are kept as opaque
`reasoning_details` records so the next request can send them back byte for byte.

Inputs → outputs: route + `ChatRequest` → `POST {base}/responses`; SSE → events; JSON → response.

Files: `src/indus/providers/transports/responses.py`,
`tests/unit/providers/transports/test_responses.py`,
`tests/fixtures/captures/responses/{text,tool_call}.json`, `tests/fakes/model/wire_responses.py`;
shared: registry entry in `providers/transports/__init__.py`, the API mode turned on in
`providers/profile.py`, `providers/route.py` (no more "Responses not available" downgrade),
`providers/registry.py` (custom providers on OpenAI hosts detect `responses`).

Edge cases / failure modes:
- `status: incomplete` with `max_output_tokens` → finish `length` (a normal length stop, the loop's
  continuation handles it); with `content_filter` → `content_filter`.
- `status: failed` (stream event or JSON) → `ProviderStreamError` carrying the embedded
  `error.code`/`message`, so the P03 classifier sees e.g. `rate_limit_exceeded`,
  `context_length_exceeded`, `server_error`.
- `status: cancelled` → `ResponseCancelled` (a `ProviderStreamError` subclass, `interrupted`
  flag), never a reply.
- an `error` event mid-stream → `ProviderStreamError` with its code.
- stream ends without a terminal event → `incomplete_stream` (as the other transports).
- reasoning-only incomplete output three times in a row on one route → `incomplete_stream`
  error (P03 classifier reason `incomplete_response`, the P08-13 fallback trigger); any reply with
  text or tool calls resets the count.
- reasoning items are tagged with the model that produced them; they are replayed only to the
  same model (sealed blobs fail elsewhere). A 400 naming `encrypted_content` switches replay off
  for that route and the call is sent once more (the P03 compatibility hook).
- item ids: replayed reasoning items keep their `id` (stable); a rejection naming an item id that
  is not found / not persisted switches to "no item ids" for that route (compat retry); call ids
  longer than 64 characters map to a stable `call_<sha256 prefix>` on both the call and its output.
- `refusal` content counts as visible text.

Our design differences / decisions:
- Stored reasoning items live in `reasoning_details` as `{"type": "responses.reasoning", "model":
  ..., "item": {verbatim item}}`; other wires ignore that type.
- No subscription/OAuth backends, no vendor-specific quirks (xAI, Copilot, Azure), no native
  compaction, no prompt cache key (P2 / later).
- Effort: the profile's `reasoning_efforts`, else `minimal, low, medium, high`, clamped with the
  shared ladder rule; reasoning off sends no `reasoning` field.
- Content-free events (`response.created`, `in_progress`, part added/done) come out as
  `KeepAlive` so the stale watchdog sees a live connection but nothing counts as content.

Tests we will write: replay of the two captures (contract suite + unit); conversion golden
(system → instructions, tool pair, image part, reasoning replay order); encrypted reasoning bytes
identical after a round trip; incomplete / failed / cancelled mapping; reasoning-only streak; id
policy (long call id, item-id rejection compat retry); effort clamp.

## Decisions for the lead
- Captures use the landed P03 format (`<mode>/<scenario>.json`, base64 chunks), not `*.jsonl` as
  the phase file's table says; both are synthetic until recorded (D-04.5).
- The bundled `openai` profile still says `chat_completions`; only routes whose mode comes from the
  URL (custom providers on official OpenAI hosts) or from config land on `responses`. Flip the
  bundled profile when real captures exist.
- `ResponseCancelled` classifies as `unknown` in P03's classifier; the loop should end such a turn
  as interrupted (P04 owner) — not wired here.

Compare checklist (filled after the rewrite; reopened the two reference files):
- [x] reasoning items replayed before the assistant message, untouched (test: `tests/unit/providers/transports/test_responses.py::test_reasoning_replayed_byte_identical`)
- [x] incomplete with output cap → length; content filter → content_filter (test: `::test_status_mapping`)
- [x] failed classified by embedded error (test: `::test_failed_event_carries_embedded_code`)
- [x] long call ids clamped deterministically on both sides (test: `::test_long_call_ids_clamped_on_both_sides`)
- Deliberate differences: the reference strips stored ids from replayed reasoning items always and
  dedupes call ids itself; we keep ids until a route rejects them, and rely on P04's wire
  sanitizer for duplicate call ids.
- Not taken: leaked-tool-call text recovery, issuer kinds, compaction checkpoints.

## Status

Done (2026-10-18). Captures synthetic.

## Integration (lead)

Merged in the P08 wave 1 integration (2026-10-18). Accepted: captures as `<mode>/<scenario>.json`;
the bundled `openai` profile stays on chat completions until live captures exist (D-16.102). Wired:
a `ResponseCancelled` (bare or inside P03's `RetryFailed`) ends the turn
`interrupted_by_system` without a failure (`core.loop._end_cancelled`, D-16.107). The P03 types
note was updated for the retired `test_registry_refuses_later_modes`.
