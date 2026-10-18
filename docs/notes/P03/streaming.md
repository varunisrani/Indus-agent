# Task: Streaming call driver (P03-14) Phase: 03 Owner: P03-B2 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/chat_completion_helpers.py`
(`_StreamingCall` 2703–3993: the constructor state, route-switch check, attempt id / cancel /
discard helpers 2703–2830, `_retry_after_drop`, `_maybe_disable_streaming`,
`_handle_stream_error`, `_unmask_server_error_with_nonstreaming`, `_call` 3498–3780, the stale
kill, interrupt abort, `_resolve_stale_timeout`, `_partial_stream_stub`, `run` 3780–3993; the
stale streak helpers ~555–630), `agent/stream_delivery.py` (module outline and the writer-token
methods 218–266). Test files named in the row (`test_partial_stream_finish_reason.py`,
`test_stream_interrupt_retry.py`) not opened. Closed before writing.
Evidence: none.

What it does (own words): runs one model call as a stream and decides what happens when the
stream goes wrong. Each attempt gets a writer token, so events from an attempt we gave up on can
never reach the caller. A watchdog on the injected clock aborts an attempt that delivers no
content within the stale timeout (keep-alive frames do not count); attempts that failed before
anything visible was shown are reconnected up to two more times with 1 s / 2 s / 4 s pauses. Once
text has been shown, a later failure is not retried (that would repeat text) but turned into a
`length` stub holding the delivered text so the turn loop can ask the model to continue; tool
calls that were mid-stream are named and dropped, never run. A context overflow after visible text
ends the turn with an empty terminal stub. A model switch while the call was in flight returns
`RouteChanged` instead of replaying the old payload against the new route. A streak of silent
attempts trips a breaker that fails calls at once. A provider that refuses streaming, or answers
only with keep-alive frames, flips the route to non-streaming and the call is redone once without
streaming. Cancellation closes the stream and propagates; nothing partial is returned as a reply.

Inputs → outputs: `await stream_call(client, transport, route, req, *, policy, clock, on_event,
route_epoch, flags=None, streak=None, classify=None, est_prompt_tokens=None, reasoning=None)` →
`CallResult{response, stub, stub_terminal, dropped_tools, streamed, attempts, requests,
discarded_events, error}` or `RouteChanged{error, attempts}`; raises the last error when the
call failed before anything visible (for `retry_call`, P03-16), `StaleBreakerTripped` when the
breaker is open, and re-raises `CancelledError`.
- `policy`: `TimeoutPolicy` (P03-13); the driver asks it for the stream and non-stream numbers.
- `on_event(event)`: sync or async callback; receives only events of the current attempt.
- `route_epoch()`: returns the route's current epoch; compared with the value at call start.
- `flags`: `StreamFlags{streaming, stream_options}` kept per route by the caller (session state).
- `streak`: `StaleStreak` shared across calls on one route (reset on success / route change).

Files: `src/indus/providers/streaming.py`, `tests/unit/providers/test_streaming.py`,
`src/indus/foundation/errors.py` (shared: `ProviderHTTPError`, `ProviderStreamError`,
`StaleBreakerTripped` and their codes).

Edge cases / failure modes:
- HTTP error status on open → body read, `ProviderHTTPError(status, body, headers, retry_after)`.
- a 400 naming `stream_options` before anything streamed → one compatibility retry without it,
  not counted against the reconnect budget, flag remembered in `flags`.
- stall before the first delta → stale strike, reconnect (fresh stale clock after the pause).
- reasoning-only or whitespace-only deltas then failure → nothing visible → reconnect allowed.
- tool-call fragments only (no text) then failure → reconnect allowed (nothing shown yet).
- stub content after a dropped tool call: delivered text only; the names are in
  `dropped_tools` (up to 3, plus a count of the rest) for the loop's warning.
- late events of a superseded attempt (a transport that keeps yielding after cancel) →
  discarded and counted, never delivered.
- `on_event` raising → the error propagates (a broken consumer is a bug, not a provider fault).
- cancellation during the reconnect pause → stream already closed, cancellation propagates.

Our design differences / decisions:
- One task per call plus a reader task per attempt and a clock watchdog; cancellation instead of
  worker threads and foreign-thread socket shutdown (D-04.1).
- The breaker streak is an explicit object the caller owns (`StaleStreak`), not agent state.
- Stub flags live on `CallResult` (`stub`, `stub_terminal`, `dropped_tools`), so the normalized
  response type (P03-06) stays provider-neutral; the stub's `NormalizedResponse` has
  `finish_reason="length"`, the delivered text and no tool calls.
- The reference retries a transient failure mid-tool-call even after text was shown (and shows a
  "reconnecting" marker); we follow the phase row: visible text → stub, dropped tools named.
- The reference's one non-streaming "unmask" probe after a 5xx is not taken (P08 may add it).
- HTTP is sent with `client.send(request, stream=True)` and closed with `response.aclose()`;
  `providers.http.open_stream` (P03-08, parallel slice) can replace the two lines when it lands.

Tests we will write: fake model + `FakeClock`: stall before first delta → reconnect at 180 s, two
requests; stall after visible text → `length` stub with that text and the dropped tool named;
overflow mid-stream → empty terminal stub; route epoch bumped mid-stream → `RouteChanged`, no
retry; 5 stalls across calls → `StaleBreakerTripped` and the next call fails without a request;
keep-alive-only server → non-stream flip, one non-stream request; superseded attempt's late
deltas discarded (count); cancellation closes the stream within 200 ms (`slow`) and raises;
HTTP 429 → `ProviderHTTPError` with `retry_after`; stream_options compatibility retry; reconnect
budget exhausted → last error raised; reconnect pauses 1 s then 2 s.

## Decisions for the lead
- `route_epoch` is a zero-argument callable (`lambda: session.route_epoch`), so the driver sees
  a switch made while it runs; an int would be frozen at call time.
- Extra keyword arguments beyond the row's signature: `flags`, `streak`, `classify`,
  `est_prompt_tokens`, `reasoning` (defaults: fresh flags, fresh streak, the P03-15 classifier,
  a chars/4 estimate of the request body, and "reasoning effort set").
- The `stream_options` compatibility retry asks the transport first: a transport with
  `on_request_rejected(route, error)` (P03-06 as landed by P03-B1) decides and remembers the change
  per route; without the hook the driver strips `stream_options` itself and clears
  `StreamFlags.stream_options`.
- Error shapes follow P03-B1: `ProviderStreamError(message, body={"error": {...code}})`; the
  driver's own codes are `incomplete_stream` (EOF without a finish) and `stale_stream` (watchdog).
- Stream-level reconnects cover `timeout`, `connection` and `incomplete_response` verdicts plus
  stale aborts; HTTP status errors (429, 5xx) go straight to `retry_call` (they carry server
  waits that the reconnect ladder would ignore).

Compare checklist (filled after the rewrite; reopened the files above):
- [x] writer token per attempt, superseded events discarded and counted (test: `test_streaming.py::test_superseded_attempt_events_are_discarded`)
- [x] reconnect ladder 2 extra attempts, 1 s → 2 s → 4 s, no jitter, stale clock restarts after the pause (tests: `test_stall_before_first_delta_reconnects`, `test_reconnect_pauses_and_budget`)
- [x] stream_options compatibility retry outside the budget (test: `test_stream_options_rejection_retried_once_without_it`)
- [x] route switched in flight → no replay (test: `test_route_epoch_change_returns_route_changed`)
- [x] partial stub: length, delivered text, tool calls stripped, up to 3 dropped names (test: `test_stall_after_visible_text_returns_length_stub`)
- [x] overflow after visible text → empty terminal stub (test: `test_overflow_mid_stream_returns_empty_terminal_stub`)
- [x] breaker counts each attempt once, resets on success, 0 disables (tests: `test_breaker_trips_after_five_stalls`, `test_breaker_disabled_with_zero`)
- [x] keep-alive-only / stream-unsupported → non-stream for the session (tests: `test_keep_alive_only_server_flips_to_non_stream`, `test_stream_unsupported_flips_to_non_stream`)
- [x] cancellation closes the stream and propagates (test: `test_cancellation_closes_stream_and_raises`)
- Deliberate differences: the breaker trips inside a call as soon as the streak reaches the
  threshold (the reference only refuses the next call); no mid-tool-call retry after visible text; no 5xx non-stream unmask
  probe; no "interrupt after 30 s of silence counts as stale" rule (interrupts are the turn
  loop's, P04); out-of-order Anthropic events and Bedrock IAM switches not taken (P08).
- Not taken: think-tag scrubber and repetition guard (P04), llama.cpp prompt-progress
  notices, first-chunk timing for hooks (P04), request-local client registry (httpx client
  per route is P03-08's).

## Status

Done (2026-10-18). Uses test stand-ins for `providers.types`, `providers.transports`,
`providers.sse`, `providers.hosts`, `providers.route` until P03-A / P03-B1 land
(`tests/_stubs_p03b2.py`).

## Integration (lead)

- `tests/_stubs_p03b2.py` and the `install()` conftests (`tests/unit/fakes/conftest.py`,
  `tests/trials/async_core/conftest.py`) deleted; everything runs on the real `hosts`, `route`,
  `types`, `sse` and `transports`. Note: the real `get_transport` returns one shared instance per
  mode (the stand-in made a fresh one per call), so the chat transport's per-base-URL
  "no stream_options" memory is process-wide; no test depended on the difference.
- The public `http_error` was removed in favour of `transports.http_error_from_response` (one
  builder; see `types.md`). A private `_http_error(response, clock, route)` passes the route key and
  `clock.now()`. Differences that come with it: message summary clipped at 500 chars (was 300,
  newlines kept), non-JSON bodies clipped at 2000 chars.
