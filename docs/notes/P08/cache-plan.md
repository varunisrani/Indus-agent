# Task: Prompt-cache markers (P08-07) Phase: 08 Owner: P08 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/prompt_caching.py` (all),
`website/docs/developer-guide/context-compression-and-caching.md` (§Prompt Caching only).
Closed before writing. Vendor facts checked against Anthropic's prompt-caching guide
(https://docs.claude.com/en/docs/build-with-claude/prompt-caching, read 2026-10-18):
`cache_control: {"type": "ephemeral"}` with optional `"ttl": "1h"` (default five minutes), on
system text blocks, message content blocks and tool definitions, at most 4 breakpoints per request;
OpenRouter passes the same field through on content parts for Claude models
(https://openrouter.ai/docs/features/prompt-caching, read 2026-10-18).
Evidence: none.

What it does (own words): long sessions re-send the same prefix every request; providers with
explicit cache breakpoints only reuse a prefix up to a marked block. Right before a request leaves
(after every transcript change, after the system message is attached, for the route the request is
really going to) we decide whether that route takes markers and, if so, place at most four of them:

- layout A, a static system prefix is known and the system text starts with it (and has more after
  it): the system message becomes two text parts, both marked (prefix, end of system), plus the two
  newest non-system messages that can carry a marker;
- layout B, otherwise: one marker on the system message plus the three newest carriers.

Placement on one message: string content → a one-part list `[{"type": "text", "text": ...,
"cache_control": ...}]`; list content → the last part gets the marker; empty content, or a tool
message, → the marker sits on the message itself — the tool-message envelope only on the native
Anthropic wire. On the aggregator (Chat Completions) wire a message whose content is empty cannot
carry one, so it is skipped and the budget goes to the next older message.

TTL: config `prompt_caching.cache_ttl` = `5m` (default), `1h`, or `auto` (1 h when a person drives
the session — CLI, TUI, desktop, messaging; 5 min for automated sources: subagent, cron, oneshot,
webhook, API, batch).

Who gets markers: Claude models on the native Anthropic wire or on the aggregator (OpenRouter),
routes whose profile declares support (`prompt_cache: true` attribute) or whose provider entry
says `prompt_caching: true`; a provider entry with `prompt_caching: false` turns them off. Every
other route gets the request with all markers stripped (envelopes and parts, tools too).

Inputs → outputs: `ChatRequest` + `CachePolicy` (+ source, static prefix) → a new `ChatRequest`
(the input and the stored transcript are never mutated). `cache_hit_percent(usage)` turns a
`Usage` into a hit percentage for the usage line (P08-08 shows it).

Files: `src/indus/core/cache_plan.py`, `src/indus/core/wire.py` (`finish_request`, the last
step), `tests/unit/core/test_cache_plan.py`; shared: `src/indus/core/route.py` (`CachePolicy` gains
`ttl`, `cache_policy_for` follows the rules above), `src/indus/core/agent.py` (`BoundCaller`
applies the plan per request for the bound state), `src/indus/providers/transports/anthropic.py`
(markers survive the conversion to blocks), `src/indus/foundation/config/schema.py`
(`prompt_caching.cache_ttl`).

Edge cases / failure modes:
- messages already carrying markers (a retry, a caller that decorated them) are cleaned first, so
  planning twice never exceeds four markers.
- system prompt equal to the static prefix (nothing after it) → layout B, never an empty block.
- fewer than three carriers → fewer markers, never more than four in total.
- a fallback route is a different `RouteState`: `BoundCaller` plans again for it (P08-13 reuses
  this), so no marker leaks to a route that does not take them.
- unknown TTL text → `5m` with a debug log.

Our design differences / decisions:
- Planning happens in `BoundCaller.call` (it reads the bound state once per request) via
  `wire.finish_request`; the transcript and the wire copy from `prepare_wire` stay untouched.
- No "skill scaffold" splitting and no tool-array marker layout (later, if measured worth it).
- No special TTL clamping for other vendors' caches; `1h` is sent as asked where markers are on.
- The static prefix comes from the prompt builder's stable tier of the current process
  (`PromptLifecycle.last_parts`); after a restore the prompt has no known split, so layout B.

Tests we will write: golden request bodies for both layouts on the native wire and on the
aggregator wire; ≤ 4 markers for any message list (hypothesis); unsupported route has none
(markers in the input stripped); `auto` TTL per source; re-plan for a fallback state; policy table
(Claude on anthropic / openrouter, profile flag, config on/off, others off); the anthropic
transport carries markers onto system blocks, text blocks and `tool_result`.

## Decisions for the lead
- Marker support is decided per route in `core/route.cache_policy_for` (P04-23 file) — the old rule
  "any model on the Anthropic wire" became "Claude models on the Anthropic wire or OpenRouter, or a
  declared/configured opt-in". Change `cache_plan.decide` to widen it.
- `auto` treats `tool` and `kanban` sources as automated too (our internal session sources).

Compare checklist (filled after the rewrite; reopened `agent/prompt_caching.py`):
- [x] four breakpoints, two layouts (test: `tests/unit/core/test_cache_plan.py::test_golden_native_static_prefix`, `::test_golden_native_system_and_three`)
- [x] placement table incl. tool envelope native-only (test: `::test_tool_message_placement_per_wire`)
- [x] re-planning never accumulates past four markers (test: `::test_replan_is_idempotent`, `::test_never_more_than_four_markers`)
- [x] a fallback route gets its own plan (test: `::test_plan_rebuilt_for_the_route_that_is_bound_now`)
- [x] TTL `auto` by source (test: `::test_auto_ttl_by_source`)
- [x] aggregator skips messages that cannot carry a part marker (test: `::test_aggregator_skips_empty_messages`)
- Deliberate differences: the reference also had a tool-array layout, a request-shape rule that
  skips markers for one-shot requests, vendor TTL clamps and a proxy-specific tool rule; none taken.
- Not taken: skill-scaffold splitting of user messages.

## Status

Done (2026-10-18). Shared edits listed under Files.

## Integration (lead)

Merged in the P08 wave 1 integration (2026-10-18). The marker rule (Claude on the Anthropic wire
or OpenRouter, or an opt-in) and the `auto` TTL are recorded as D-16.101. `/usage` shows the cache
hit share with `cache_hit_percent`.
