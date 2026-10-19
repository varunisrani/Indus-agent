# Task: Sampling and elicitation (P13-10) Phase: 13 Owner: wave-16 builder N2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/mcp_tool_sampling.py`
(limits, limiter, message conversion, elicitation routing). Research `07` §3.16. Ours:
`mcp/config.py` (`SamplingSettings`, `ElicitationSettings`), `mcp/connect.py`
(`ConnectOptions.session_kwargs`), `providers/auxiliary.py` (`AuxClient.call`, usage recorder with
the task tag), `protocol/requests_port.py` + `protocol/server_requests.py` (`clarify` requests),
`tools/builtin/clarify.py`. SDK `mcp` 2.3.0: `ClientSession(sampling_callback=,
elicitation_callback=, sampling_capabilities=)`, `mcp_types.CreateMessageRequestParams`,
`CreateMessageResult[WithTools]`, `SamplingMessage`, `ToolUseContent`, `ToolResultContent`,
`ElicitRequestFormParams` / `ElicitRequestURLParams` (`mode`), `ElicitResult`, `ErrorData`.
Spec checked 2026-10-19: MCP 2025-11-25 client features (sampling with tools, elicitation form and
URL modes).

What it does (own words):
- **Sampling** (`SamplingHandler`, one per server): `sampling/createMessage` from a server becomes
  one auxiliary call with task `mcp_sampling` (usage recorded under that tag by the aux client).
  Order of checks: enabled; sliding 60 s window of `max_rpm` (10) admitted requests on the injected
  clock (the 11th within 60 s is refused); model = config `sampling.model`, else the server's first
  hint; a named model outside a non-empty `allowed_models` is refused; `max_tokens` is
  min(request, `max_tokens_cap` 4096); a request whose history already holds more than
  `max_tool_rounds` (5) assistant tool-use rounds is refused (0 = tools off). Messages become chat
  messages (text, images as data URLs, tool use → assistant tool_calls, tool results → tool
  messages); server tools are passed as function tools. The call is bounded by `sampling.timeout`
  (30 s). The answer comes back as text, or as tool use (`stopReason: toolUse`). Failures return
  `ErrorData`, never raise into the SDK. Per-server counters: requests, refused, errors, tokens,
  tool-use answers; one audit log line per request at `sampling.log_level`.
- **Elicitation** (`ElicitationHandler`, one per server): URL mode is declined. Form mode needs a
  person: the request is turned into one `clarify` server request (one question per schema field:
  enums and booleans become choices; an empty schema becomes one accept/decline question) on the
  port of the session whose tool call is in flight on that server (`Requesters` slot, bound by the
  call path). Answers are coerced to the field types; a skipped required field declines. No bound
  session, an unattended session (cron, API, background) or a client that cannot answer → decline.
  The wait is `elicitation.timeout` (300 s); timeout or withdrawal → cancel.
- `session_kwargs(...)` gives the `ClientSession` keyword arguments (callbacks only for enabled
  features, so a disabled feature is not advertised).

Files: `src/indus/mcp/sampling.py`, `tests/unit/mcp/test_sampling.py`.

Edge cases: `max_rpm: 0` refuses all; empty choices from the model → error; a malformed tool
arguments string from the model is kept as `{"_raw": text}`; aux unavailable → error; image blocks
in results are not produced (text or tool use only).

Our design differences / decisions:
- Elicitation answers carry the person's field values (the reference only approved, with empty
  content).
- The requester is chosen by the call in flight (latest bound first), not a contextvars snapshot.
- The tool-round limit is read from the request history (stateless per server), not a counter.

Tests planned: 11th request in 60 s refused, allowed again after the window (fake clock); max_tokens
capped at 4096; model outside `allowed_models` refused; tool-round limit; text and tool-use
answers mapped; aux timeout → error; form elicitation answered through a fake clarify port with
typed values; URL mode declined; unattended (cron) requester declines; no requester declines;
disabled features not advertised.

## Decisions for the lead
- `Requesters` (bind per in-flight call) is the seam P13-06's call path uses:
  `with requesters.serving(port, unattended=...)` around `tools/call`.
- Sampling advertises the `tools` sub-capability when `max_tool_rounds > 0` (the reference made it
  opt-in because older strict servers rejected it; SDK 2.3 negotiates 2025-11-25 where it is
  standard).
- A configured `sampling.model` (or an allowed hint) goes through `AuxClient.call_route` with a
  pinned candidate; shared change: `call_route` gained `temperature` and `tools` (additive).
- SDK 2.3 marks server-side sampling deprecated as of protocol 2026-07-28 (SEP-2577); the client
  side stays for servers on earlier revisions.

## Compare checklist (after writing)
(reopened `tools/mcp_tool_sampling.py`)
- [x] sliding 60 s window `max_rpm` (`test_the_eleventh_request_in_a_minute_is_refused`, `test_sliding_window_counts_only_the_last_minute`)
- [x] model = config override, else first hint; `allowed_models` gate (`test_a_model_outside_allowed_models_is_refused`, `test_configured_model_wins_over_the_hint_and_any_model_by_default`)
- [x] `max_tokens` capped (`test_max_tokens_is_capped`); timeout → error (`test_failures_become_error_data`)
- [x] tool loop bound (`test_tool_loops_are_bounded`); message conversion incl. tool results first, tool use, images (`test_tool_use_answer_and_message_conversion`); malformed tool args kept as `_raw`
- [x] stop reason mapping (`test_text_answer_maps_the_stop_reason`); usage tag `mcp_sampling` (`test_the_auxiliary_call_is_labelled_mcp_sampling`); per-server metrics and audit level
- [x] elicitation: URL declined (`test_url_mode_is_declined`); form through the person's surface (`test_form_elicitation_is_answered_through_clarify`); fail closed: unattended/none decline, timeout cancels (`test_unattended_or_missing_requester_declines`, `test_timeouts_cancel_and_unsupported_clients_decline`); 5 s outer grace over the wait
- [x] end to end over the real SDK (stdio server sampling + form + URL elicitation): `test_a_real_server_samples_and_asks_through_the_session`
- Differences: answers carry field values (reference: consent only, empty content); tool rounds counted from the request history, not a per-handler counter; requester chosen by the call in flight.

## Status
Done (2026-10-19). `src/indus/mcp/sampling.py`, `tests/unit/mcp/test_sampling.py`,
`tests/fakes/mcp/asking_server.py`. Host wiring (aux client per session, `Requesters.serving`
around `tools/call` in P13-06) belongs to N1/the lead.
