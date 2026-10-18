# Task: Scriptable fake model (P03-18) Phase: 03 Owner: P03-B2 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tests/fakes/fake_llm_provider.py`
(the list of scripted behaviours only: text, tool call, error answer, hang, drop mid-stream,
stall mid-stream, raw body), `tests/fakes/providers/chat_variants.py` (class names only: plain
text, tools, error, stream error, dropped tool call). Closed before writing. Wire formats taken
from the public Chat Completions streaming reference and the Anthropic Messages streaming
reference (event and field names only), not from the reference code.
Evidence: none.

What it does (own words): a model API that lives inside the test process. A script (the P00-11
vocabulary) says what each model request gets back; the server speaks the real wire formats, so
the real transports, the stream driver and later the turn loop are tested end to end without a
socket. Stalls wait on the fake clock, so stale detection is tested without real waiting. Every
request is logged with its parsed body for assertions, and a request the script did not expect
fails loudly, naming the script.

Inputs → outputs: `FakeModelServer(script, *, clock=None, model="fake-model-1", models=None,
name=None, yield_every=0, on_frame=None)` → `.transport` (`httpx.MockTransport`), `.client()`,
`.requests` / `.model_requests` (`RecordedRequest{method, path, headers, body, turn, at,
timeout}`), `.streams` (`StreamRecord{turn, wire, opened_at, closed_at, frames_sent,
completed}`), `.turns_left`. Routes: `POST …/chat/completions` (Chat Completions SSE, or JSON when
`stream` is false), `POST …/messages` (Anthropic Messages SSE or JSON), `GET …/models`.
`fake_route(server, api_mode)` → a `RuntimeRoute` with a fake key on the `fake.invalid` host.
`one_off_router()` → a respx router for single canned answers.

New vocabulary: `Eof()` (clean end without a finish), `KeepAlive(count)` (SSE comment / Anthropic `ping`), `MidStreamError(body)`
(JSON error frame, or plain-text data when the body is a string), `ToolCall.late_id` (id sent
with the second fragment), `Script.name` (set from the file stem; not part of equality).

Files: `tests/fakes/model/server.py`, `tests/fakes/model/wire_chat.py`,
`tests/fakes/model/wire_anthropic.py`, `tests/fakes/model/README.md`,
`tests/fakes/model/__init__.py` (vocabulary + exports), `tests/unit/fakes/test_fake_model_server.py`,
`tests/fixtures/model_scripts/{tool_then_text,stall_then_text,rate_limited}.yaml`.

Edge cases / failure modes:
- `fail` anywhere but a turn's first step, or a `stall` without a clock → `ScriptError` when the
  server is built (not when the request arrives).
- request past the last turn → `FakeScriptExhausted` (an `AssertionError`) naming the script and
  the request number; the stream driver and `retry_call` never retry it (it is a test bug).
- `disconnect` → `httpx.RemoteProtocolError` from the body (EOF without a complete message).
- client closes the response mid-stream → the body generator is closed and `closed_at` is
  recorded (used for the 200 ms close measurements).
- a turn without `finish` gets an implied one (`tool_calls` if it called a tool, else `stop`);
  `eof` ends the body cleanly without any finish (an incomplete stream).

Our design differences / decisions:
- No real HTTP server or thread (the reference fake binds a port); everything is MockTransport.
- One script drives both wire formats, so "same script, same normalized response" is testable.
- Anthropic input usage is sent in `message_start` and output usage in `message_delta`, both from
  the turn's `usage` step; thinking blocks get a fake signature delta before they close.

Tests we will write: both wire formats parse with the transports into the same normalized
response; a 3-request tool → tool → text conversation; `Stall(200)` triggers stale detection in
the driver (P03-14 tests); `Fail(429, Retry-After: 2)` → `rate_limit` with 2.0; extra request
names the script; request log keeps bodies; non-stream JSON answers; `/models`; keep-alive and
error frames; late id; script errors at build time; respx one-off router.

## Decisions for the lead
- The P03-11 replay transport (`tests/fakes/model/replay.py`, P03-B1) lives beside this server;
  `__init__` exports only the server names listed in the files table plus `FakeScriptExhausted`
  and `RecordedRequest`.

Compare checklist (filled after the rewrite):
- [x] scripted behaviours covered: text, reasoning, tool call (fragments, late id), usage,
  keep-alive, HTTP error, mid-stream error, stall, disconnect, finish (test:
  `tests/unit/fakes/test_fake_model_server.py`)
- [x] unexpected extra request fails clearly (test: `test_extra_request_names_the_script`)
- Deliberate differences: in-process MockTransport instead of a bound port; auxiliary-request
  routing and "raw body" responses not taken (no consumer yet; P04 adds script helpers).

## Status

Done (2026-10-18).

## Integration (lead)

- `fake_route` imports `RuntimeRoute` and `Secret` directly (the `importlib` lookup for a module
  that might not exist yet is gone). `test_fake_model_server.py` builds its error with
  `transports.http_error_from_response(..., now=clock.now())` since `streaming.http_error` is gone.
