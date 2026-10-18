# Task: Normalized types and transport registry (P03-06) Phase: 03 Owner: P03-B1 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/transports/types.py` (all),
`agent/transports/base.py` (all), `agent/transports/__init__.py` (all). Closed before writing.
Evidence: none.

What it does (own words): one provider-neutral vocabulary between the wire transports and the
rest of the runtime. Messages stay OpenAI-shaped dicts. A transport turns a `ChatRequest` into an
`httpx.Request`, and a server-sent event stream into small typed events (text, reasoning, tool-call
start / argument fragments, usage, finish, keep-alive). `assemble(events)` folds any event sequence
into one `NormalizedResponse`, so streaming and non-streaming replies end in the same shape. The
registry maps an `api_mode` to a transport; modes that arrive later (`responses`, `bedrock`,
`gemini`) raise a typed error until P08 registers them.

Inputs → outputs: `ChatRequest` + route → `httpx.Request`; `AsyncIterator[SSEEvent | KeepAlive]` →
`AsyncIterator[StreamEvent]`; events → `NormalizedResponse`; raw stop reason + table → one of
`stop|tool_calls|length|content_filter|error`.

Files: `src/indus/providers/types.py`, `src/indus/providers/transports/__init__.py`,
`tests/unit/providers/test_types.py`; shared: `src/indus/foundation/errors.py`
(`ProviderHTTPError`, `ProviderStreamError`, codes `provider.http`, `provider.stream`).

Edge cases / failure modes:
- tool-call arguments are kept as the exact string the server sent (no JSON round trip; R01.1
  groundwork); an empty argument string assembles as `{}`.
- a second `ToolCallStart` for the same index fills in a late id / name, never opens a new call.
- `reasoning_details` records keep arrival order and are never merged.
- unknown or missing stop reason → `stop`.
- an unregistered api mode → `UnsupportedApiMode` naming the mode and the phase that adds it.

Our design differences / decisions:
- Events are frozen dataclasses; `ReasoningDetail(detail)` is one extra event (not in the row's
  list) so opaque records (OpenRouter `reasoning_details`, Anthropic signed thinking blocks) travel
  through the stream in order.
- `UsageReport` carries a cumulative `Usage` snapshot; `assemble` keeps the last one.
- `Usage.prompt_tokens` is the whole prompt including cached tokens on every wire (Anthropic's
  `input_tokens` excludes cache reads/writes, so the Anthropic transport adds them).
- The reasoning-effort ladder and `clamp_effort` live here so both transports share them (A53).
- Shared transport helpers (`open_call`, `stream_events`, `http_error_from_response`) live in the
  registry package; `open_call` takes an optional `send` so the P03-08 stream handle can be used.
- No vendor SDK objects, no back-compat attribute shims.

Tests we will write: finish-reason table; arguments byte-stable; reasoning-details order through
`assemble`; late id; usage last-wins; registry returns both transports, raises for `responses`;
`clamp_effort` table; HTTP error mapping with `Retry-After`.

## Decisions for the lead
- `ProviderHTTPError` / `ProviderStreamError` are defined in `foundation/errors.py` now (the phase
  lists them there for P03-05/P03-14); merge with the other slices' additions to that file.
- `Retry-After` is read here only as delta-seconds (RFC 9110); P03-16's `parse_retry_after` can
  replace `_retry_after_seconds` once landed.
- The `Transport` protocol adds `on_request_rejected(route, error) -> bool` (compatibility retry
  hook used by `open_call`) and a keyword `profile=None` on `build_request`; `get_transport`
  returns one shared instance per mode (the stream_options flag lives on it).
- `ChatRequest` gains `tool_choice` (the body needs it; the row's field list omits it).

Compare checklist (filled after the rewrite; reopened the three files):
- [x] finish map unknown → stop (test: `tests/unit/providers/test_types.py::test_map_finish_reason_table`)
- [x] tool call arguments are a JSON string (test: `test_assemble_keeps_arguments_bytes`)
- [x] registry by api mode, unknown handled (test: `test_registry_returns_p03_transports`,
  `test_registry_refuses_later_modes`)
- Deliberate differences: theirs returns `None` for an unknown mode and lets callers fall back to a
  legacy path; ours raises a typed error. Theirs has no stream-event types (streaming lives in the
  agent); ours puts stream parsing in the transport.
- Not taken: `provider_data` back-compat properties, Codex/Gemini/Bedrock fields (P08).

## Status

Done (2026-10-18). Shared change in `foundation/errors.py` (two classes, two codes).

## Integration (lead)

- `foundation/errors.py`: B1 and B2 both defined `ProviderHTTPError` / `ProviderStreamError`
  identically; one copy kept, all codes kept.
- `_retry_after_seconds` removed; `http_error_from_response` now reads `Retry-After` with
  `providers.retry.parse_retry_after` (so HTTP dates and the 600 s cap apply, and a `now=` argument
  takes an injected clock's time) and stores header names lowercased.
- `http_error_from_response` is the one builder for HTTP error answers: `providers.streaming`'s
  public `http_error` was removed and its two call sites use this function (via a private
  `_http_error` that passes the route key and clock).
