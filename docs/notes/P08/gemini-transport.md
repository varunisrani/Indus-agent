# Task: Gemini native transport (P08-11) Phase: 08 Owner: P08 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/gemini_native_adapter.py`
(skim: contents building, tool call/result translation, request body, response and stream
translation, finish reasons), `agent/transports/chat_completions.py` (the Gemini thinking-config
helper only). Closed before writing. Wire checked against Google's Gemini API reference
(https://ai.google.dev/api/generate-content, https://ai.google.dev/gemini-api/docs/thinking,
https://ai.google.dev/gemini-api/docs/function-calling, read 2026-10-18):
`POST {base}/models/{model}:generateContent` and `:streamGenerateContent?alt=sse`, API key in the
`x-goog-api-key` header; body `contents [{role: user|model, parts}]`, parts `text`,
`inlineData {mimeType, data}`, `functionCall {name, args, id?}`, `functionResponse {name,
response, id?}`, `thought: true` on thought-summary parts, `thoughtSignature`;
`systemInstruction {parts}`, `tools [{functionDeclarations}]`, `toolConfig.functionCallingConfig
{mode AUTO|ANY|NONE, allowedFunctionNames}`, `generationConfig {temperature, maxOutputTokens,
thinkingConfig {thinkingBudget | thinkingLevel, includeThoughts}}`; response `candidates[0]
{content, finishReason}`, `promptFeedback.blockReason`, `usageMetadata {promptTokenCount,
candidatesTokenCount, cachedContentTokenCount, thoughtsTokenCount}`; finish reasons `STOP`,
`MAX_TOKENS`, `SAFETY`, `RECITATION`, `BLOCKLIST`, `PROHIBITED_CONTENT`, `SPII`,
`IMAGE_SAFETY`, `MALFORMED_FUNCTION_CALL`, `OTHER`.
Evidence: none.

What it does (own words): the `gemini` API mode talks to Google's own `generateContent` endpoint
(a facade beside chat completions). History → `contents` (assistant → `model`, tool rows →
`functionResponse` parts in a user turn, same-role turns merged, system text →
`systemInstruction`); the reasoning ladder → `thinkingConfig` (Gemini 2.5: a token budget; Gemini 3
and later: a level); safety blocks → `content_filter`. Thought signatures Google attaches to parts
are kept as opaque `reasoning_details` and put back on the same parts next request.

Inputs → outputs: route + `ChatRequest` → `httpx.Request` (key only in the header); SSE chunks of
`GenerateContentResponse` → events; JSON → response.

Files: `src/indus/providers/transports/gemini.py`, `tests/unit/providers/transports/test_gemini.py`,
`tests/fixtures/captures/gemini/{text,tool_call}.json`, `tests/fakes/model/wire_gemini.py`; shared:
registry entry in `providers/transports/__init__.py`.

Edge cases / failure modes:
- the key never appears in the URL (asserted), so it stays out of access logs and our errors.
- a prompt blocked before generation (`promptFeedback.blockReason`, no candidates) →
  `content_filter`.
- function calls carry no id on Gemini 2.x → we mint a stable `call_<turn-local index>_<name>` id;
  a provider id is used when present (Gemini 3).
- a stream that re-sends a function call's full arguments in a later chunk: only the new suffix is
  emitted.
- a tool result that is a JSON object goes in `response` as is; anything else as
  `{"output": text}`.
- `functionResponse.name` echoes the matching call's name (looked up by call id).
- call ids go on the wire only for Gemini 3 and later (2.x refuses the field); a Gemini 3+ call
  that came from another model has no signature, so it gets the placeholder value Google documents
  for that case.
- a key read from `OPENAI_API_KEY` (the custom-provider default) is never sent to Google; the
  provider entry must name `key_env`.
- `MAX_TOKENS` → length; `MALFORMED_FUNCTION_CALL` → error; tool calls present → tool_calls.
- the stream ends without any `finishReason` → `incomplete_stream`.
- a model id given as `models/<id>` or `google/<id>` is stripped to the bare id.

Our design differences / decisions:
- Budgets for 2.5 models: minimal 512, low 2,048, medium 8,192, high 16,384, xhigh and above
  24,576; reasoning off → `includeThoughts: false` plus `thinkingBudget: 0` on Flash models (Pro
  cannot switch off). Gemini 3+: minimal/low → `low`, medium → `medium` (Pro: `high`), high and
  above → `high`. Non-Gemini models on the endpoint (Gemma) get no `thinkingConfig`.
- No Vertex surfaces, no long-decode continuation tokens, no structured-output translation.
- No placeholder model turn between a tool-result turn and a following human turn; P04's wire
  sanitizer already keeps results paired with their calls.

Tests we will write: two captures replayed (contract + unit); thinking config per level and per
model generation; key header present and absent from the URL; safety-block mapping (finish reason
and prompt feedback); conversion golden (system, tool call, tool result name/echo, image); stream
argument suffix; signature round trip.

## Decisions for the lead
- Captures use the landed P03 JSON format, not `*.jsonl`.
- No bundled `gemini` provider profile was added (provider catalogue is P03/P07); a user reaches
  this mode with `api_mode: gemini` on a named provider.

Compare checklist (filled after the rewrite; reopened `agent/gemini_native_adapter.py`):
- [x] finish-reason table incl. safety (test: `tests/unit/providers/transports/test_gemini.py::test_safety_block_mapping`)
- [x] streamed call arguments emitted once (test: `::test_stream_resent_arguments_emit_suffix_only`)
- [x] thought signature replayed on the call part (test: `::test_signature_round_trip`)
- Deliberate differences: the reference inserts a placeholder model turn between a tool result and
  a human turn; we merge them (P04 keeps pairs valid). The reference also forwards JSON-schema-looking
  tool results as text for Gemini 3; not taken.

## Status

Done (2026-10-18). Captures synthetic.

## Integration (lead)

Merged in the P08 wave 1 integration (2026-10-18). A bundled `gemini` profile (alias `google`) was
added after `anthropic`: `https://generativelanguage.googleapis.com/v1beta`, `api_mode: gemini`,
keys `GEMINI_API_KEY` / `GOOGLE_API_KEY` (both already blocked from children), no model listing
(Google's list has another shape). The Gemini 3 placeholder thought signature was accepted.
D-16.103.
