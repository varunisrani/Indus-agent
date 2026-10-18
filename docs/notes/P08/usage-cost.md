# Task: Usage and cost (P08-08) Phase: 08 Owner: P08 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/usage_pricing.py` (usage
buckets, billing route, cost status, label rendering — the price rows were looked at only to
see their shape; none were copied), `agent/turn_usage.py` (what one response folds into).
Research `01` §3.20. Closed before writing.
Evidence: none.

Pricing data source (vendors' own public pages, read 2026-10-18 for this task):
- Anthropic: https://platform.claude.com/docs/en/about-claude/pricing (model table, fast mode
  table, long context section) and https://platform.claude.com/docs/en/about-claude/models/overview
  (API ids; ids are dateless from the 4.6 generation on, older ones carry a date we strip).
- OpenAI: https://developers.openai.com/api/docs/pricing (Standard tier, cached input, cache-write
  rows, the 272K long-context columns). openai.com/api/pricing answered 403 to our fetch.
- Google: https://ai.google.dev/gemini-api/docs/pricing (paid tier, text rates, the >200k tier,
  the price changes scheduled for 2026-12-01 and 2027-01-01).
- Usage field shapes: OpenAI Responses reference (`usage.input_tokens_details.{cached_tokens,
  cache_write_tokens}`, `output_tokens_details.reasoning_tokens`), AWS Bedrock `TokenUsage`
  (`inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheWriteInputTokens`), Google
  `UsageMetadata` (`promptTokenCount` includes `cachedContentTokenCount`; `thoughtsTokenCount`).

What it does (own words): every answered model call is turned into one canonical usage record
(uncached input, output, cache read, cache write, reasoning, request count, raw vendor usage),
whatever wire format produced it: vendors that report the whole prompt (chat completions,
Responses, Gemini) have the cache buckets taken out; vendors that report uncached input
(Anthropic, Bedrock) are taken as is. A call is counted even when the provider sent no usage.
The record is priced from our own table: list prices apply only when the request went to that
vendor's own host over HTTPS on port 443; an aggregator that reports what it billed gives an
`actual` cost; a local server is `included`; everything else is `unknown` — the cost status never
pretends. The session ledger keeps the counters, the last 10 latencies and output sizes, the
newest rate-limit headers, logs one INFO line per call, and hands a `UsageDelta` to the store
(main loop rows, or `model_usage` under a task tag for auxiliary calls).

Inputs → outputs: `Usage`/raw usage + route + latency + headers → `CanonicalUsage`, `CostResult`,
`UsageDelta` (stored), INFO log line, `UsageLedger.snapshot()` for `/usage`, `-z --usage-file`
and the status bar.

Files: `src/indus/core/usage.py`, `src/indus/providers/pricing.py`,
`src/indus/providers/pricing_table.yaml`, `tests/unit/core/test_usage.py`,
`tests/unit/providers/test_pricing.py`. Shared: `src/indus/core/agent.py` (the ledger wraps the
session's model caller and records into the store).

Edge cases / failure modes:
- usage missing → call counted, tokens untouched, cost of that call unknown;
- negative or non-integer counters → 0; cache buckets bigger than the reported total → input 0;
- a bucket with tokens but no price in the table (cache write on a model without a write price)
  → unknown, never a silent zero; tier above a prompt threshold priced for the whole request;
- a served tier the table does not know (OpenAI `flex`/`priority`) → unknown;
- vendor model id on a proxy or a non-443 port → unknown; dated ids fall back to the dateless row;
- a scheduled price change (Gemini) applies from its date by the injected clock;
- store write fails → logged once per call, the turn goes on.

Our design differences / decisions:
- the price table is data (`pricing_table.yaml`), each vendor block carries its source URL and
  check date; rates are decimal strings (USD per million tokens) read into `Decimal`;
- vendor is chosen by the request host, not by the provider name, so a renamed or custom
  provider pointing at a vendor host is still priced and a vendor name on another host is not;
- loopback endpoints are `included` (no invoice) rather than unknown;
- labels: exact zero `$0.00`; sub-cent four decimals (`$0.0046`), below that `<$0.0001`.
- DeepSeek is not in the table: its page prices peak and off-peak hours differently; costs there
  stay `unknown` until a time-window rate is added. xAI, Mistral and Bedrock are not priced yet.

Tests we will write:
- normalisation per wire format (chat, chat with DeepSeek hit field, Anthropic, Responses,
  Bedrock, Gemini) and from P03's `Usage`; missing usage still counts the call;
- cost status table: vendor host, vendor model on a proxy, aggregator with and without a reported
  cost, custom host, loopback, non-443 port, unknown model;
- tier above threshold, scheduled period, fast mode, unknown service tier, missing cache-write rate;
- rendering table; rate-limit header parsing (durations, hourly bucket); ledger rolling history,
  store delta with task tag, INFO line content.

## Decisions for the lead
- Rate-limit headers: the P03 stream driver does not hand response headers to the caller, so the
  ledger takes them as an argument; wiring them from `streaming.py` is a follow-up.
- `/usage` already reads the store's session totals (`state.usage.totals`), which the ledger now
  fills, so it shows real tokens and cost without a CLI change. `-z --usage-file` and the status
  bar should read `Agent.usage.snapshot()` (`as_dict()` has the usage-file numbers); that CLI
  wiring is left to the P07 integration (we do not touch `src/indus/cli/` in this wave).
- Bedrock Converse `inputTokens` is taken as the uncached input (like Anthropic); AWS's
  `TokenUsage` page does not say it outright — confirm against the P08-10 live capture.
- Context lengths: only provider-confirmed windows are persisted already (P03's
  `context_length.remember` refuses other sources); P08-06's probe tiers must keep using a
  non-confirmed source.
- Pricing for DeepSeek peak/off-peak, xAI, Mistral, Bedrock regions: follow-up rows.

Compare checklist (filled after the rewrite; reopened `agent/usage_pricing.py`, `agent/turn_usage.py`):
- [x] Prompt total = input + cache read + cache write; whole-prompt vendors subtract the cache
  buckets (test: `tests/unit/core/test_usage.py::test_normalise_per_wire_format`)
- [x] Call counted when usage is missing (test: `tests/unit/core/test_usage.py::test_missing_usage_still_counts_the_call`)
- [x] Vendor list price only on the vendor host over 443 (test: `tests/unit/providers/test_pricing.py::test_cost_status_table`)
- [x] Sub-cent rendering never `$0.00` (test: `tests/unit/providers/test_pricing.py::test_format_cost_table`)
- [x] Tier above a prompt threshold covers the whole request (test: `tests/unit/providers/test_pricing.py::test_tier_above_threshold_prices_the_whole_request`)
- [x] Fast mode and served tier are priced only when the table has them (test: `tests/unit/providers/test_pricing.py::test_served_tier_unknown_is_unknown_cost`)
- Deliberate differences: the reference picks a billing route from provider names plus host
  checks and falls back to live model catalogues; we decide by host only and never fetch prices
  at run time. The reference's "included" status covers subscription routes (we have none, D-16.42)
  and we use it for loopback servers.
- Not taken: live price catalogues (aggregator `/models` pricing, third-party catalogues),
  multi-model fan-out accounting, image-token calibration (that is P08-01's anchor).

## Status

Done (2026-10-18). Ledger wired into `core/agent.py` (shared); CLI surfaces and response headers
handed on (see decisions).

## Integration (lead)

Merged in the P08 wave 1 integration (2026-10-18).
- One usage path: `UsageLedger` listeners now hear **every** answered call (usage may be None, so
  the usage anchor learns that a provider never reports usage) and are awaited when async; the
  context engine registers `on_usage` there (`Agent._open_context_engine`); the loop only
  announces which rows the next request prices (`compression.engine.expect_usage`). The loop's
  direct `observe_usage` call is gone.
- `CanonicalUsage.completion_tokens` (alias of `output_tokens`) so the anchor reads either usage type;
  `normalize` accepts our mode name `bedrock` (and `bedrock_converse`).
- `RecordingCaller.task`: the main-route compression summary (no aux client) is recorded under
  `compression`, never as a main-loop call.
- `-z --usage-file` reads `Agent.usage.snapshot().as_dict()` (tokens, cost, cost status; null and
  `unknown` when nothing answered or nothing could be priced); the REPL status bar reads the same
  ledger (`cli/launch.status_from_agent`); `/usage` shows the store's cost with its status and the
  cache hit share (`cache_plan.cache_hit_percent`). D-16.104.
- Follow-ups recorded for P08 wave 2 / later: the stream driver still does not hand response
  headers to the caller (rate-limit buckets stay empty in practice); DeepSeek (peak/off-peak), xAI,
  Mistral and Bedrock are unpriced (`unknown`); Bedrock `inputTokens` = uncached input is what both
  this module and the P08-10 transport assume — the captures are synthetic, confirm with the first
  live capture.
