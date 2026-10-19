# Task: Tool calls and results (P13-06) Phase: 13 Owner: wave-16 builder N1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/mcp_tool_handlers.py` (call
path order, recovery ladder, result rendering, dual-emit rule, utility handlers),
`tools/mcp_tool_content.py` (hard cap, reserved `_meta` prefixes, image/audio/resource blocks,
native image attachment). Ours: `mcp/lifecycle.py` (`ManagedServer.use/call_tool`), `tools/calls.py`
(result contract), `tools/truncate.py` (head/tail), `tools/budget.py` (MCP spill threshold 50,000
is already applied by prefix), `core/untrusted.py` (the `mcp__` prefix is already fenced),
`tools/builtin/media/imaging.py` + `vision.embed_target_bytes` (P10 embed target). Evidence: none.

What it does (own words): the handler registered for each MCP tool. Order, per call:
1. trust gate (P13-07) — before any transport work, lazy spawn included;
2. breaker (P13-07) — open → short-circuit result;
3. a live session: a lazy server starts here, a recycled stdio one restarts (`ManagedServer.use`);
4. the server's call lock unless `supports_parallel_tool_calls` (one call at a time per server);
5. `tools/call` with the server's original tool name, bounded by `timeout` (300 s) on the clock;
6. classify: `auth` (HTTP 401/403), `transport` (connection gone, server unavailable, timeout),
   `not_found` (JSON-RPC −32601), `tool_error` (`isError` or another JSON-RPC error answered by
   the server); every class is a breaker strike, a success resets it;
7. render the result.

Result: JSON text `{"result": text, "structuredContent"?: …, "_meta"?: …}`. Text parts joined with
newlines, TAG characters stripped; image blocks saved under `<home>/cache/images/` and named as
`MEDIA:<path>` lines; audio under `<home>/cache/audio/`; embedded resources: text inline, blobs
≤ 50 MiB saved under `<home>/cache/documents/` (checked on the base64 length before decoding);
resource links named with the server's `read_resource` tool; unknown block types leave an inline
"dropped" notice. `structuredContent` is kept unless a text block carries the same JSON (dual
emit); when the blocks are empty it becomes the `result`. `_meta` passes through minus
protocol-reserved keys (a `modelcontextprotocol` or `mcp` label followed by another label before
the `/`). The text and the serialised `structuredContent` are each capped at 2,000,000 chars (head
40 % / tail 60 %); P04's spill (50,000 for `mcp__`) and the untrusted fence apply afterwards.
`isError` → a P04 error result with the server's text. With `native_vision` (the host says the main
model sees tool images) up to 4 of the call's images (≤ 16 candidates prepared, each fitted to the
P10 embed target) are attached as a multimodal envelope; the rest stay `MEDIA:` paths.

Utility tools (registered in P13-05) use the same path minus the trust gate (read-only by nature):
`list_resources` / `list_prompts` (paged ≤ 50), `read_resource {uri}`, `get_prompt {name,
arguments?}`.

Inputs → outputs: (`ServerHandle`, tool spec, args, call context) → handler text or envelope.

Files: `src/indus/mcp/calls.py`, `tests/unit/mcp/test_calls.py`.

Edge cases: a 3 MB text (capped); `structuredContent` over the cap (truncated string); malformed
base64 (dropped with a log line); 5th image (stays a path); `isError` with no text (generic text);
a server tool returning non-serialisable `_meta` (dropped, call kept); two parallel calls (second
waits unless opted in); a call cancelled by the turn (lock released).

Our design differences / decisions:
- No automatic replay: a call that failed in transport is not retried by the handler (the reference
  replays read-only calls after a session expiry and re-authorises on 401). The lifecycle already
  reconnects; the model sees a transport error once and may call again. Simpler, at-most-once for
  writes.
- Breaker strikes count every failure class (reference: same), with the open-breaker text saying
  "rejected" when every strike was a tool error and "unreachable" otherwise.
- Files are named by the sha256 of their bytes (re-sent screenshots reuse one file).

Tests we will write: fake server returning text + image + structured: envelope shape; 3 MB text
capped; reserved `_meta` keys dropped, vendor keys kept; image saved and the 5th not attached
natively; `isError` → error result; two parallel calls serialised unless opted in; dual-emit drop;
structured-only result; not-found / transport / auth classification; utility handlers.

## Decisions for the lead
- No in-handler retry after a transport failure (see above).
- `MEDIA:` lines written by a server inside its text are not neutralised here; the delivery
  scanner (P15) must only deliver paths it trusts. Follow-up.

Compare checklist (filled after writing; reopened `tools/mcp_tool_handlers.py`,
`tools/mcp_tool_content.py`):
- [x] order: trust gate → breaker → session → call (`tests/unit/mcp/test_trust.py::test_untrusted_write_tool_asks_and_a_denial_sends_nothing`: no RPC on denial)
- [x] envelope `{result, structuredContent?, _meta?}`, dual-emit drop, structured-only result (`tests/unit/mcp/test_calls.py::test_text_image_and_structured_content`, `::test_dual_emitted_structured_content_is_dropped`, `::test_structured_only_result_becomes_the_result`)
- [x] reserved `_meta` prefixes (`::test_reserved_meta_keys`)
- [x] 2,000,000-char cap on text and structured content (`::test_three_megabytes_of_text_are_capped`, `::test_oversized_structured_content_only_fills_an_empty_result`)
- [x] resources ≤ 50 MiB checked before decoding (`::test_embedded_blob_over_the_cap_is_refused_before_decoding`); resource link, embedded text/blob, audio, undecodable block (`::test_other_blocks`)
- [x] images cached, ≤ 4 native from ≤ 16 candidates (`::test_five_images_attach_four_natively`)
- [x] `isError` → error; −32601 → not found; transport; auth (`::test_is_error_reaches_the_model_as_an_error`, `::test_errors_are_classified`, `::test_a_call_that_never_answers_times_out`)
- [x] one call at a time per server unless opted in (`::test_parallel_calls_to_one_server`)
- [x] utility handlers (`::test_utility_tools`)
- Deliberate differences: no replay after session expiry / dead stdio child and no 401 refresh +
  retry inside the handler (lifecycle reconnects; the model calls again); no fast-fail race against
  a stdio child dying mid-call (the transport ending fails the call); no per-image repeat budget
  (`vision.max_calls_per_image` has no Indus counterpart yet); unknown blocks give a short notice.
- Not taken: elicitation context hand-off during a call (P13-10 owns it), MCP-app availability
  declarations.

## Status

Done (2026-10-19). `src/indus/mcp/calls.py`, `tests/unit/mcp/test_calls.py`.
