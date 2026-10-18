# Task: Chat Completions transport (P03-09) Phase: 03 Owner: P03-B1 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/transports/chat_completions.py`
(request build: `_base_kwargs`, `_apply_max_tokens`, `_build_kwargs_from_profile`,
`normalize_response`), `agent/chat_completion_helpers.py` (stream loop around lines 2915–3345:
choiceless chunks, tool-call accumulation, finish handling, `_rejects_stream_options`;
`ProviderStreamError` 105–370), `agent/chat_completion_helpers_tool_calls.py` (accumulator),
`tests/fakes/providers/chat_variants.py` (variant names only). Closed before writing.
Wire format checked against OpenAI's published OpenAPI description
(https://github.com/openai/openai-openapi, `openapi.yaml`, read 2026-10-18): `reasoning_effort` enum
`none|minimal|low|medium|high|xhigh|max`; `stream_options.include_usage` adds a final chunk whose
`choices` is empty; `finish_reason` `stop|length|tool_calls|content_filter|function_call`;
usage `prompt_tokens_details.cached_tokens`, `completion_tokens_details.reasoning_tokens`.
Evidence: none.

What it does (own words): builds `POST {base_url}/chat/completions` requests and turns the SSE
reply (or a JSON reply) into normalized events / a `NormalizedResponse`. Text, reasoning
(`delta.reasoning` or `delta.reasoning_content`), opaque `reasoning_details`, tool calls assembled
by index, usage and finish reason. Errors hidden inside a 200 stream (an `error` object, a
`finish_reason: "error"`, a non-JSON data line, an `event: error`) become `ProviderStreamError` with
an OpenAI-like `{"error": {...}}` body for the classifier.

Inputs → outputs: route + `ChatRequest` → `httpx.Request`; SSE events → stream events; JSON → response.

Files: `src/indus/providers/transports/chat_completions.py`,
`tests/unit/providers/transports/test_chat_completions.py`.

Edge cases / failure modes:
- first tool fragment carries id + name, later ones arguments; an id that arrives in a later
  fragment is accepted (a repeated `ToolCallStart` fills it); a name resent on every fragment is
  not doubled; a fragment without `index` counts as index 0; integer ids become strings; a fresh id
  with a name on an index whose arguments already closed opens a new call (servers that reuse
  index 0 for parallel calls).
- argument fragments that arrive before the name are held until the call starts.
- usage arrives in a chunk with empty `choices` after the finish chunk; `Finish` is emitted once at
  the end of the stream so usage is never lost.
- `[DONE]` ends; EOF without `[DONE]` ends cleanly only if a finish reason arrived, otherwise
  `ProviderStreamError` (code `incomplete_stream`).
- empty `data:` frames are keep-alives.
- a 400/422 naming `stream_options` as unknown/unsupported → the route is remembered and the same
  call is sent once more without it (not counted as a retry).
- non-stream reply with only a `refusal` → content = refusal, finish `content_filter`.

Our design differences / decisions:
- `reasoning_effort`: ladder value clamped to the profile's supported list (default
  `low, medium, high`), nearest weaker level, else the weakest supported; `none` sends nothing.
- `max_tokens` key comes from `profile.max_tokens_param`; value from the request, else the
  profile's `default_max_tokens`, else omitted.
- temperature: profile `fixed_temperature` wins over the request.
- `extra_body` is merged at the top level of the JSON body (raw HTTP; there is no SDK).
- Messages are copied with `reasoning`, `reasoning_content` and `reasoning_details` removed.
- The profile is found as `profile=` argument, `route.profile`, or `route.provider` when that is a
  profile object; with none we use plain defaults.
- Non-JSON stream text is passed through the redactor and cut to 2,000 chars before it goes into
  the error.

Tests we will write: request goldens (text, tools, effort clamp, `max_completion_tokens`, keyless
local, fixed temperature, extra_body); stream fixtures (interleaved text + two parallel tool calls,
id in 2nd fragment, usage-only last chunk, error object mid-stream, plain-text error line,
`finish_reason: error`, missing `[DONE]` both ways, reasoning fields); stream_options rejection
retried once; JSON parse incl. refusal.

## Decisions for the lead
- Profile extras read with `getattr` and a default: `reasoning_efforts` (supported ladder levels).
  Add it to `ProviderProfile` (P03-01) or tell me the name you chose.
- Bearer key read from `route.api_key` by duck typing (`reveal()`, `get_secret_value()`, `.value`,
  or a plain `str`).

Compare checklist (filled after the rewrite; reopened the stream loop, the accumulator and
`_rejects_stream_options`):
- [x] tool fragments assembled by index; missing index → 0; integer ids; name resent not doubled
  (test: `tests/unit/providers/transports/test_chat_completions.py::test_arguments_before_name_and_resent_names_and_missing_index`)
- [x] late id accepted (test: `test_tool_call_id_in_second_fragment`)
- [x] fresh id + name on a reused index after the arguments closed → new call; fresh id on every
  fragment → one call (tests: `test_reused_index_with_fresh_id_opens_a_new_call`,
  `test_fresh_id_on_every_fragment_stays_one_call`)
- [x] usage-only final chunk; cached / reasoning token details (test:
  `test_usage_only_last_chunk_with_details`)
- [x] in-stream error shapes: error object, `event: error`, non-JSON data, `finish_reason: error`,
  choiceless `error_type`/`error_message` chunk (tests: `test_error_object_mid_stream`,
  `test_error_event_type`, `test_plain_text_error_line`, `test_finish_reason_error`,
  `test_choiceless_error_chunk`) — the last one was a gap found here and added.
- [x] streamed refusal-only reply → content + `content_filter` (gap found; test
  `test_streamed_refusal_only_reads_as_content_filter`)
- [x] `stream_options` compatibility retry, remembered, not counted (test:
  `test_stream_options_rejection_retried_once_without_it`)
- Deliberate differences: the reference returns a partial-stream stub on a clean EOF without a
  finish reason and treats a usage object as proof of completion; the task row asks for
  `ProviderStreamError(incomplete)` whenever no finish reason arrived, and the stub belongs to the
  P03-14 driver. The reference's "structural completion" check counts brackets; ours asks whether
  the arguments already parse as one JSON value.
- Not taken: vendor quirks (developer-role swap, Gemini thought signatures, Mistral id rewriting,
  xAI tool alias, Moonshot schema rewriting, prompt-cache keys, router-timeout shims, portal
  `lastOne` frames, runaway/repetition guards, inline `<think>` extraction) — P04/P08 or not needed.

## Status

Done (2026-10-18).

## Integration (lead)

- Tests now use the real `RuntimeRoute` / `ProviderProfile` (helpers `make_route`, `make_profile`,
  `FAKE_KEY` in `tests/unit/providers/_support.py`); `tests/unit/providers/_stubs_p03a.py` deleted.
