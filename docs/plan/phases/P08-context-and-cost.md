# P08 · Context and cost

**Weeks:** 14–15 (1.5 weeks) · **Track:** B · **Depends on:** P06 (walking skeleton: loop, REPL, `/compress` seam); P04 (`RouteBinding`, prompt lifecycle `invalidate`, tool round, response checks); P03 (transports, SSE, classifier, retry, context lengths, recorded captures); P02 (compression records, usage rows, session-level anchor field); P05 (approval pipeline `smart` slot); P07 (credential pool data model, auth store) · **Status:** planned

## Goal
A two-hour session keeps working without losing the task: context compresses at the right
threshold using real provider token counts, tool output is pruned before anything is summarised,
the summary call has a deterministic fallback and escalating cooldowns, and a provider-proven
overflow is recovered in one bounded attempt. Cache markers keep long sessions cheap; usage and
cost are tracked per session with a cost status that never pretends. Failing keys rotate within a
credential pool and failing routes fall back along the configured chain for the rest of the turn,
with the primary restored on the next turn. Three more transports (Responses, Bedrock, Gemini
native) join the two from P03. Smart approvals are available as an opt-in; fast mode and a liveness
watchdog bound long turns.

Interop note: OpenAI Responses API (input items, `function_call`/`function_call_output`,
`reasoning` items with `encrypted_content`, `status: incomplete|failed|cancelled`), AWS Bedrock
Converse / ConverseStream (via botocore signing only, D-04.5), Google Gemini `generateContent` /
`streamGenerateContent` (`contents`, `parts`, `functionCall`, `generationConfig.thinkingConfig`),
and Anthropic `cache_control` (`type: ephemeral`, optional `ttl: "1h"`, at most 4 breakpoints)
are vendor protocols. Check every field against the vendor's API reference as of the capture date
recorded with each transport's two live captures (D-04.5), and against the botocore version
pinned in `uv.lock`. Read the reference only for behaviour and edge cases.

## Tasks (19)

| ID | Task | Read in reference | New files | Verify |
|---|---|---|---|---|
| P08-01 | **Token accounting: the usage anchor.** The provider's last real `prompt_tokens` (+ completion tokens) is authoritative; only rows appended since are estimated locally at 4 chars/token; opaque encrypted blobs count 0; images cost a learned per-`model@host` amount (residual between real prompt tokens and the text-only projection; default 1,500 tokens per image before the first vision turn). The anchor is identified by a content fingerprint of the rows it covers and persisted on the session row so a restart keeps it; compaction or reset clears it. Without an anchor an over-threshold **estimate waits one request** for real usage rather than compressing (estimates are not evidence) (R01.8). | `agent/usage_anchor.py` [203], `agent/context_engine.py` [250: engine interface] | `src/indus/core/compression/__init__.py`, `src/indus/core/compression/anchor.py`, `tests/unit/core/compression/test_anchor.py` | Anchor + 3 appended rows → anchor + estimate; restart reload keeps anchor; fingerprint mismatch after an edit → anchor dropped; no anchor + huge estimate → "wait" verdict, not compress |
| P08-02 | **Threshold arithmetic.** `trigger_tokens(window, max_output, cfg, aux_window?)`: effective window = `context_length − max_tokens` (whole window if unset); percent = per-model override (longest substring key; `provider:substring` keys scoped to a provider) else `compression.threshold` (0.50); **raise-only floor**: windows <512,000 use ≥0.75; trigger = `max(window × percent, 64,000)`, but when the 64K minimum decides it is limited to 85 % of the window; a trigger ≥ window becomes 85 %; optional absolute cap `compression.threshold_tokens`; a smaller auxiliary summariser window lowers the trigger durably. The developer guide's "200K → 100K" example is wrong per code (150K) — follow code (R01.7, D-16.24). | `agent/context_compressor.py` [5823: constants block ~100–1170, `_effective_threshold_percent` ~2761, `_compute_threshold_tokens` ~2775, `should_compress_info` ~3004], `website/docs/developer-guide/context-compression-and-caching.md` [814: §Configuration] | `src/indus/core/compression/threshold.py`, `tests/unit/core/compression/test_threshold.py` | Table test: 32K, 64K, 128K, 200K, 400K, 1M windows × output reservations × overrides × caps; 200K default → 150K; hypothesis: trigger < window always |
| P08-03 | **Pruning and boundaries.** Step 1 (no model call): tool outputs longer than 200 chars before the protected tail become a one-line placeholder; the newest unanswered tool round stays verbatim; pruned skill loads get a `[skill elided: <name>]` marker listed under one heading (≤20). Step 2: head = first 3 rows (system + first exchange); tail by token budget walking backwards — lean mode (default) `2.5 % × window` clamped to [10,000, 25,000] tokens and ≤20 % of the window, soft overrun 1.5×, at least 8 rows and at least `min_tail_user_messages` (1) real user messages; legacy mode `threshold × 0.20`; never split a tool-call group (walk back to the parent assistant). | `agent/context_compressor.py` [5823: the `compress` method's prune and boundary steps], `agent/conversation_compression.py` [4500: skim the contract docstring] | `src/indus/core/compression/boundaries.py`, `tests/unit/core/compression/test_boundaries.py` | Property: boundary never between an assistant tool call and its results; tail meets row/user minimums; pruned placeholders only before the tail; skill markers capped at 20 |
| P08-04 | **Summary call + deterministic fallback.** One auxiliary-model call (P08-17 client, task `compression`) with our own structured sections (goal, constraints, done / in progress / blocked, decisions, relevant files, next steps, critical context); lean mode adds a session log (~4,000 tokens), a regex-extracted anchor index (PR numbers, SHAs, paths, error lines; ≤7,000 chars), every real user message verbatim newest-first (24,000-char budget, ≤4,000 each) and a pointer to session search. Summary budget `content × 0.20` clamped to [2,000, min(window × 0.05, 10,000)] — the code constant (10,000) applies until REA answers 01 Q6; recorded in D-16.24. Summariser input ≤160,000 chars with even sampling and elision markers. Re-compression updates the previous summary. **Deterministic fallback summary** (no model): structured from the anchor index, the user messages and the file list, committed through the same pipeline. Prompt text written fresh (D-16.23). | `agent/context_compressor.py` [5823: summary budget and input sampling], research `01` §3.19 step 3 | `src/indus/core/compression/summarize.py`, `src/indus/core/compression/fallback_summary.py`, `src/indus/core/compression/texts.py`, `tests/unit/core/compression/test_summarize.py` | Fake aux model: budget clamp table; sampled input ≤160,000; update-not-restart on second pass; fallback summary contains every user message (within budget) and extracted SHAs/paths |
| P08-05 | **Compression engine + gates + cooldowns.** `ContextEngine` interface (`should_compress`, `compress`, `on_session_start/end`, `update_from_response`) with the built-in engine selected by `context.engine` (plugins never auto-activate; P13 adds the plugin path). Gates: turn-start preflight, pre-request pressure, post-tool, provider-proven overflow (P08-06), manual `/compress [focus topic] [here N] [--preview]`, idle (opt-in, off). Attempt cap `compression.max_attempts` 3 (≤10) ineffective attempts per turn, re-armed only when a provider response reports prompt tokens below threshold after a completed compaction; two provider-confirmed ineffective compactions trip a breaker. Failure cooldowns persisted in the store: stalls/timeouts 60 → 300 → 900 s (never below `compression.timeout_s` 120); output-cap-truncated summaries escalate on their own counter along the same ladder; decode/closed-stream/empty → flat 30 s; manual compress clears; a repeated stall commits the deterministic summary; 3 consecutive summary-provider overload aborts commit it too; `abort_on_summary_failure: true` always aborts. Assemble head + summary (role chosen to keep alternation) + tail; orphan calls get stub results and orphan results are dropped; call `PromptLifecycle.invalidate()` (the one sanctioned rebuild point); in-place (default, soft-archive pre-compaction rows via P02) or rotating (child session); emit `session.compressed{mode}` event. Preflight timeout while still oversized → turn ends `compression_exhausted`, partial, with a "nothing dropped, start a new session" message. Re-inject the todo list (P05) after compaction. Trap R01.9. | `agent/compression_facade.py` [328], `agent/conversation_compression.py` [4500: commit pipeline], `agent/turn_context_compaction.py` [512], `agent/turn_preflight.py` [396] | `src/indus/core/compression/engine.py`, `src/indus/core/compression/cooldown.py`, `tests/unit/core/compression/test_engine.py`, `tests/unit/core/compression/test_cooldown.py` | Fake clock: cooldown ladder per failure class; attempt cap and re-arm; breaker after two ineffective; manual clears cooldown; stall ×2 → fallback summary committed; alternation valid after every assemble (property); prompt rebuilt exactly once per compaction |
| P08-06 | **Overflow recovery.** 413 / payload too large → compress and retry, progress scored in bytes; "max_tokens too large" (input fits, input + output does not) → clamp the output cap from the error; "prompt too long" → adopt the provider-stated limit only when one is stated (else keep), compress, retry; for unknown models probe down the tiers 256K, 128K, 64K, 32K, 16K, 8K; a provider-proven overflow bypasses cooldowns for **one** bounded attempt without clearing them (R01.8); failure → typed terminal result. The P03 classifier supplies the reason; this module owns the recovery decisions. | `agent/turn_overflow.py` [511], `agent/model_metadata.py` [2636: context-limit error parsers ~1221–1400] | `src/indus/core/overflow.py`, `tests/unit/core/test_overflow.py` | Fake model returning each error shape (from P03 captures); clamp arithmetic; tier probe sequence; cooldown bypass once; terminal result carries reason + retryable |
| P08-07 | **Prompt-cache markers.** A cache plan built **last**, after every transcript mutation, per destination route: at most 4 breakpoints — with a static system prefix: that prefix, the end of the system prompt and the two newest non-system rows; without one: one system marker plus the three newest rows. Placement: string content → one-part list carrying the marker; list content → mark the last part; empty content or tool messages → mark the envelope (tool envelope only on the native Anthropic wire). TTL `5m` default, `1h`, or `auto` = 1 h when a person drives the session and 5 min for automated sources (subagent, cron, one-shot, webhook, API, batch). Enabled for Claude models on the native API or the aggregator and for routes whose profile declares support; markers stripped for every other route. Re-planned per attempt so a fallback route gets its own plan. Cache hit % surfaced in usage. | `agent/prompt_caching.py` [377], `website/docs/developer-guide/context-compression-and-caching.md` [814: §Prompt Caching] | `src/indus/core/cache_plan.py`, `tests/unit/core/test_cache_plan.py` | Golden request bodies for both layouts; ≤4 markers always (property); unsupported route has none; `auto` TTL by source; plan rebuilt after a fallback |
| P08-08 | **Usage and cost.** `CanonicalUsage{input, output, cache_read, cache_write, reasoning, request_count, raw?}` with `prompt_tokens = input + cache_read + cache_write`; per transport normalisation (Anthropic reports uncached input; Chat/Responses totals include cached tokens, so subtract). Per response: count the API call even when usage is missing; fold into the anchor (P08-01) and session counters (P02 usage rows, auxiliary calls tagged by task); keep the last 10 latencies/output sizes; persist only provider-confirmed context lengths; one INFO line with in/out/total, latency, cache hit %, cache write tokens, response id. Cost: pricing table (ours, data file with source URLs and dates) per provider/model; status `actual | estimated | included | unknown` with a source label; list prices applied only when the request went to that vendor's own host on 443; sub-cent costs render with 4 decimals, never `$0.00`. Rate-limit headers `x-ratelimit-{limit,remaining,reset}-{requests,tokens}` captured for `/usage`. Fills the `-z --usage-file` cost fields and the status-bar cost (P06). | `agent/usage_pricing.py` [785], `agent/turn_usage.py` [304] | `src/indus/core/usage.py`, `src/indus/providers/pricing.py`, `src/indus/providers/pricing_table.yaml`, `tests/unit/core/test_usage.py`, `tests/unit/providers/test_pricing.py` | Normalisation per transport from P03 captures; missing usage still counts the call; cost status table (aggregator, vendor host, custom host → unknown); 4-decimal rendering; headers parsed |
| P08-09 | **Responses transport.** Messages ↔ input items (`message`, `function_call`, `function_call_output`), tools → Responses function tools, reasoning items with `encrypted_content` replayed in arrival order and never rewritten, item id policy (stable ids for replay; drop ids the route rejects), streaming events parsed by P03's SSE parser, `status: incomplete` with max-output exhaustion treated as a normal length stop, `failed` classified by its embedded error, `cancelled` → interrupted. API-mode auto-detection already routes official OpenAI hosts here (P03). Incomplete reasoning-only output for 3 consecutive continuations → `incomplete_response` reason (fallback trigger). Subscription/OAuth routes are out (P2). | `agent/transports/codex.py` [971: convert/normalize], `agent/codex_responses_adapter.py` [1312: id policy, reasoning replay, incomplete status] | `src/indus/providers/transports/responses.py`, `tests/unit/providers/transports/test_responses.py`, `tests/fixtures/captures/responses/*.jsonl` | Replay two recorded live captures; round-trip conversion; encrypted reasoning bytes identical after replay; incomplete/failed/cancelled mapping |
| P08-10 | **Bedrock Converse transport.** botocore SigV4 signing only (extra `bedrock`), our own HTTP via httpx; Converse/ConverseStream message and tool shapes; event-stream binary framing decoded; region and credential chain from the standard AWS chain (the Bedrock bearer token is blocked from children, P05); context-length table for Bedrock models; streaming refusal flips the session to non-streaming (P03 switch). | `agent/bedrock_adapter.py` [1536: skim conversion and stream events], `agent/transports/bedrock.py` [89] | `src/indus/providers/transports/bedrock.py`, `tests/unit/providers/transports/test_bedrock.py`, `tests/fixtures/captures/bedrock/*.bin` | Signing test vectors (AWS published examples); event-stream decoder on a recorded capture; tool-use round trip; missing extra → named error |
| P08-11 | **Gemini native transport.** `generateContent`/`streamGenerateContent` on Google's endpoint as a facade under the chat-completions mode: `contents`/`parts`, `functionCall`/`functionResponse`, system instruction, `generationConfig.thinkingConfig` from the reasoning ladder, safety-block finish reasons → `content_filter`; key in the `x-goog-api-key` header (never the query string, so it stays out of logs). | `agent/gemini_native_adapter.py` [1012: skim] | `src/indus/providers/transports/gemini.py`, `tests/unit/providers/transports/test_gemini.py`, `tests/fixtures/captures/gemini/*.jsonl` | Two recorded captures replayed; thinking config mapping per level; key never in URL (assert on request); safety block mapping |
| P08-12 | **Credential pool runtime.** Selection by strategy (P07-07 model) with in-memory request counts; tried **before** cross-provider fallback. Error policy: generic 429 → retry the same key once, a second consecutive 429 rotates (cooldown 1 h); plan/usage-limit 429 → rotate at once (1 h or the provider's reset); 402 → rotate (1 h); 401 → refresh OAuth first, rotate when refresh fails (5 min); 400 model-entitlement → bench that key for that model only until manual reset; all keys exhausted → fall through to the fallback chain. Provider `reset_at` overrides defaults; the "retried 429" flag resets on every success; per-model 429 vendors bench only that model; a benched session returns to its original key as soon as the cooldown lifts; a counter limits refreshes of one entry while 401s persist. Subagents share the parent's pool object. Status reads are peeks. | `agent/credential_pool.py` [3097: selection and exhaustion/cooldown methods], `website/docs/user-guide/features/credential-pools.md` [380] | `src/indus/providers/pool.py`, `tests/unit/providers/test_pool.py` | Fake clock + scripted errors: each policy row; return to original key after cooldown; refresh cap; peek leaves state unchanged; strategies distribution (seeded RNG) |
| P08-13 | **Fallback chain with turn-scoped restore.** Config `fallback_providers: [{provider, model}]` (both required, else ignored). Triggers: retries exhausted on invalid responses; non-retryable 401/403/404; transient errors after retries and pool exhaustion; content-filter refusals; `incomplete_response` (one bounded grace call when it consumed the budget); eager fallback for verdicts flagged so. Activation via `RouteBinding.apply`: optional stay-on-primary when its declared reset is sooner than `fallback.min_switch_reset_s` (0 = off); arm a primary cooldown (provider reset, else `min(60 × 2^k, 14,400)` s); walk the chain from the current index skipping unavailable, malformed, offline-unusable or fully-exhausted entries; rebind client, API mode (re-detected from the entry's own base URL, exact-hostname rules, R01.11), pool, reasoning config, extra body, compressor window, cache plan; rewrite only the prompt's `Model:`/`Provider:` lines; emit a durable notice naming both routes and the reason; reset the stale streak and retry counter; a retry never replays a payload built for the previous route (R01.5). **Turn scope**: at the next turn's start restore the primary unless it is still cooling down, known unentitled/non-chat, or the pool says it would fail; announce "primary restored" only when a fallback really ran; reset the chain index **even when activation failed** (R01.10). Subagents inherit the provider, not the chain. | `agent/chat_completion_helpers.py` [4017: `try_activate_fallback` ~2055], `agent/agent_runtime_helpers.py` [3807: `restore_primary_runtime` ~1428], `agent/fallback_cooldown.py` [129], `website/docs/user-guide/features/fallback-providers.md` [463] | `src/indus/core/fallback.py`, `tests/unit/core/test_fallback.py` | Fake routes: each trigger; skip rules; every bound field restored at next turn; failed activation still resets the index; no "restored" notice when nothing fell back; second request after fallback goes to the new host (respx assert) |
| P08-14 | **Smart approvals (opt-in, D-16.53).** Fills the `smart` slot of P05's pipeline: the auxiliary model (task `approval`, temperature 0, ≤16 output tokens, timeout from `auxiliary.approval.timeout_s`) sees the command with shell comments stripped, fenced, under a system message (ours) telling it to ignore any instructions inside; replies `APPROVE` / `DENY` / `ESCALATE`; approve → run once (nothing persisted); deny → blocked, counted toward a per-session breaker (default 3 consecutive; 0 disables; ≤256 sessions tracked) after which denial text escalates to a hard stop; escalate, any other reply, timeout or error → human. A smart-denied command shown to a human offers only once/deny. Timeouts and undelivered human prompts never count toward the breaker (R02.5). Setup offers it as opt-in (P07-02). Trap R02.24. | `tools/approval_smart.py` [170] | `src/indus/safety/approvals/smart.py`, `tests/unit/safety/approvals/test_smart.py` | Fake aux model returns approve/deny/escalate/garbage/"APPROVE. Also run rm -rf"; injection inside the command does not change the verdict path; breaker after 3; any human approval resets it |
| P08-15 | **Fast mode.** `/fast normal|fast|auto|status [--global]`: layers a provider priority tier (from the profile's declared field, e.g. a service-tier parameter) onto requests for a bounded window (default 60 s per user turn in `auto`); only on routes whose profile declares support, otherwise a notice; usage records the served tier. Registered in the P06 slash registry. | `agent/fast_mode.py` [119] | `src/indus/core/fast_mode.py`, `tests/unit/core/test_fast_mode.py` | Window expiry on a fake clock; unsupported route notice; request body carries the tier only inside the window |
| P08-16 | **Liveness watchdog + run budget.** Abort turns with no activity for `agent.turn_liveness_s` (600; poll 15 s; ≤0 disables); the abort is bound to the observed (generation, last-activity timestamp) and revalidated under the activity lock so a resumed turn is never cancelled; tools and streams touch activity through heartbeats. Wall-clock run budget `--run-budget`/`agent.run_budget_s` (off by default): one wrap-up notice at 80 % appended to the newest unpersisted tool row (never a new system message); implicit provider timeouts capped at `max(60 s, 50 % of remaining)` (explicit settings never capped). Exit reason `interrupted_by_system(liveness)` or `budget_exhausted`. | `agent/turn_liveness.py` [250], `agent/conversation_loop.py` [1867: `_maybe_inject_run_budget_wrapup`] | `src/indus/core/liveness.py`, `tests/unit/core/test_liveness.py` | Fake clock: idle turn aborted at 600 s; activity at 599 s keeps it; stale generation never aborted; wrap-up injected once on the tail row; timeout cap arithmetic |
| P08-17 | **Auxiliary model client.** `aux_call(task, messages, …)` for side tasks (compression, titles, approval, vision later, session search later, memory/curator later): per-task pins `auxiliary.<task>.{provider, model, base_url, reasoning_effort, timeout_s}`; `auto` = the main route and model; then the task's own fallback list, then the main chain. HTTP 402 falls through. Main-loop request hooks never fire for auxiliary calls; auxiliary usage is recorded with its task tag (B24). Warn when the summariser window is smaller than the main model's (compaction would degrade) and lower the trigger (P08-02). Also wires the first-exchange auto-title trigger off the critical path (P02 title rules). | `agent/auxiliary_client.py` [8392: only `_resolve_auto_route` ~4665 and the `call_llm` entry] | `src/indus/providers/auxiliary.py`, `tests/unit/providers/test_auxiliary.py` | Resolution order table; 402 falls through; usage tagged by task; small-window warning; title generated after first exchange with the fake aux model, not blocking the turn |
| P08-18 | **Long-session soak (M2 demo).** Integration test: the fake model drives a 300-turn coding session with large tool outputs on an injected clock (two simulated hours); assert compression fires at the computed trigger, the final answer still references the task stated in turn 1, no row alternation errors, prompt rebuilt only at compactions (count), cost and usage totals equal the sum of fake usage, and a mid-run forced 429 storm rotates keys then falls back and restores the primary next turn. A `live`-marked variant runs a short version against a real key. | none (our scenario) | `tests/integration/core/test_long_session.py`, `tests/fixtures/model_scripts/long_session.yaml` | Passes in CI (non-live); live variant skips cleanly without a key |
| P08-19 | **`@` context references (A8).** `core.references.expand(text, scope, cwd) -> Expansion{text, blocks, refusals}` turns reference tokens in a user message into context blocks before the turn is persisted: `@file:PATH[:a-b]` (optional line range; 100 KiB per file with a truncation marker), `@folder:PATH` (listing, depth 2, at most 200 entries), `@diff` (working-tree diff of the git root, capped), `@git:REF` (one commit's patch, capped), `@url:URL` (through P05's `web_extract`, so the SSRF policy applies), plus a prefix registry plugins extend (P13 seam). Paths resolve against the session cwd through P05's path guards; credential files (`.env`, key stores, SSH keys, the auth store, anything on the secret-path deny list) are refused with a visible note instead of expanded. The total is budgeted against the context window with the P08-02 arithmetic; an item over budget becomes a one-line pointer. Expansion runs inside `SessionService.submit`, so every surface shares it; the store keeps the typed text next to the expanded text. | research `01` §3.5 and `05` §3.9 (token forms, refusal rules) | `src/indus/core/references.py`, `tests/unit/core/test_references.py` | `@file:a.py:2-4` inserts exactly three lines; `@file:.env` refused with a note; `@folder:` caps depth and count; `@url:` to a private address refused by the SSRF policy; an over-budget item becomes a pointer; a plugin prefix registered through the seam expands |

## Files to create (49)

| Path | What it holds | Task |
|---|---|---|
| `src/indus/core/compression/__init__.py`, `src/indus/core/compression/anchor.py`, `tests/unit/core/compression/test_anchor.py` | usage anchor | P08-01 |
| `src/indus/core/compression/threshold.py`, `tests/unit/core/compression/test_threshold.py` | trigger arithmetic | P08-02 |
| `src/indus/core/compression/boundaries.py`, `tests/unit/core/compression/test_boundaries.py` | pruning + boundaries | P08-03 |
| `src/indus/core/compression/summarize.py`, `src/indus/core/compression/fallback_summary.py`, `src/indus/core/compression/texts.py`, `tests/unit/core/compression/test_summarize.py` | summary call, deterministic fallback, our prompt text | P08-04 |
| `src/indus/core/compression/engine.py`, `src/indus/core/compression/cooldown.py`, `tests/unit/core/compression/test_engine.py`, `tests/unit/core/compression/test_cooldown.py` | engine, gates, cooldowns | P08-05 |
| `src/indus/core/overflow.py`, `tests/unit/core/test_overflow.py` | overflow recovery | P08-06 |
| `src/indus/core/cache_plan.py`, `tests/unit/core/test_cache_plan.py` | cache markers | P08-07 |
| `src/indus/core/usage.py`, `src/indus/providers/pricing.py`, `src/indus/providers/pricing_table.yaml`, `tests/unit/core/test_usage.py`, `tests/unit/providers/test_pricing.py` | usage + cost | P08-08 |
| `src/indus/providers/transports/responses.py`, `tests/unit/providers/transports/test_responses.py`, `tests/fixtures/captures/responses/*.jsonl` | Responses transport | P08-09 |
| `src/indus/providers/transports/bedrock.py`, `tests/unit/providers/transports/test_bedrock.py`, `tests/fixtures/captures/bedrock/*.bin` | Bedrock transport | P08-10 |
| `src/indus/providers/transports/gemini.py`, `tests/unit/providers/transports/test_gemini.py`, `tests/fixtures/captures/gemini/*.jsonl` | Gemini transport | P08-11 |
| `src/indus/providers/pool.py`, `tests/unit/providers/test_pool.py` | pool runtime | P08-12 |
| `src/indus/core/fallback.py`, `tests/unit/core/test_fallback.py` | fallback chain | P08-13 |
| `src/indus/safety/approvals/smart.py`, `tests/unit/safety/approvals/test_smart.py` | smart pre-judge | P08-14 |
| `src/indus/core/fast_mode.py`, `tests/unit/core/test_fast_mode.py` | fast mode | P08-15 |
| `src/indus/core/liveness.py`, `tests/unit/core/test_liveness.py` | watchdog + run budget | P08-16 |
| `src/indus/providers/auxiliary.py`, `tests/unit/providers/test_auxiliary.py` | aux client | P08-17 |
| `tests/integration/core/test_long_session.py`, `tests/fixtures/model_scripts/long_session.yaml` | soak test | P08-18 |
| `src/indus/core/references.py`, `tests/unit/core/test_references.py` | `@` context references | P08-19 |

(Capture globs count as one path each; each holds the two live captures per transport, D-04.5.)

## Files to update (9)

| Path | Change | Task |
|---|---|---|
| `src/indus/core/loop.py` (P04) | call the engine gates, overflow recovery, pool rotation then fallback after P03 retries; restore primary in the prologue | P08-05, P08-06, P08-13 |
| `src/indus/core/prologue.py` (P04) | preflight compression gate; primary restore; liveness registration | P08-05, P08-13, P08-16 |
| `src/indus/core/wire.py` (P04) | apply the cache plan as the last step | P08-07 |
| `src/indus/safety/approvals/policy.py` (P05) | route `smart` mode to P08-14 | P08-14 |
| `src/indus/cli/slash/control_cmds.py` (P06), `src/indus/service/commands/registry.py` (P06-08) | `/compress` and `/usage` wired to real engine/usage; `/fast` defined in the shared registry with its CLI handler | P08-05, P08-08, P08-15 |
| `src/indus/providers/transports/__init__.py` (P03) | register `responses`, `bedrock`, `gemini` | P08-09, P08-10, P08-11 |
| `src/indus/foundation/config/schema.py` (P01) | `compression.*`, `prompt_caching.*`, `auxiliary.*`, `fallback.*`, `agent.turn_liveness_s`, `agent.run_budget_s` | P08-02, P08-07, P08-17 |
| `pyproject.toml` | extra `bedrock` (botocore, exact pin) | P08-10 |
| `src/indus/service/session_service.py` (P04-21) | run `@` reference expansion in `submit` before persisting the user row | P08-19 |

## Fixtures and fakes

| Path | What it holds | Task |
|---|---|---|
| `tests/fakes/aux_model.py` | fake auxiliary client: scripted summaries, stalls, overloads, truncations, verdicts | P08-04, P08-14 |
| `tests/fixtures/captures/{responses,bedrock,gemini}/` | two live captures per transport, recorded once with a real key and replayed (never hand-edited) | P08-09–P08-11 |
| `tests/fixtures/model_scripts/long_session.yaml` | 300-turn soak script | P08-18 |
| `tests/fixtures/errors/*.json` | overflow and rate-limit error bodies per provider (from P03 captures) | P08-06, P08-12 |

## Task notes (19)
`docs/notes/P08/<slug>.md`: `usage-anchor` (01), `threshold` (02), `boundaries` (03),
`summary` (04), `compression-engine` (05), `overflow` (06), `cache-plan` (07), `usage-cost` (08),
`responses-transport` (09), `bedrock-transport` (10), `gemini-transport` (11), `pool-runtime` (12),
`fallback-chain` (13), `smart-approvals` (14), `fast-mode` (15), `liveness` (16),
`aux-client` (17), `long-session` (18), `context-references` (19).

## Run until green
```bash
cd indus-agent
uv sync --all-extras
uv run ruff check && uv run ruff format --check
uv run pyright src/indus/core src/indus/providers src/indus/safety
uv run pytest tests/unit/core tests/unit/providers tests/unit/safety/approvals tests/integration/core
uv run python -m tooling.checks --require-ref
uv run python -m tooling.checks acceptance traps
INDUS_LIVE=1 uv run pytest -m live tests/integration/core/test_long_session.py   # gated: real key
```
On any failure, fix the cause and rerun from `uv sync`. Never hand-edit a recorded capture to make
a test pass; re-record and note the capture date.

## Compare checklist (reopen the reference after writing)
- [ ] Threshold arithmetic matches code on every row of the table (R01.7)
- [ ] Anchor rules: wait one request without real usage; bypass once on proven overflow (R01.8)
- [ ] Cooldown ladder, attempt cap, re-arm and breaker; deterministic summary commit paths (R01.9)
- [ ] Boundary never splits a tool group; tail minimums
- [ ] Summary ceiling choice recorded (01 Q6) in D-16.24
- [ ] Cache marker layouts and TTL policy per source; markers stripped on unsupported routes
- [ ] Usage normalisation per transport; cost only on the vendor's own host
- [ ] Pool policy table rows and return-to-original rule
- [ ] Fallback rebinding list complete; restore undoes all of it; index reset on failed activation (R01.10)
- [ ] Exact-hostname API-mode detection on fallback routes (R01.11)
- [ ] No retry replays a payload for a previous route (R01.5)
- [ ] Smart judge fences, strips comments, caps tokens, escalates on non-verdicts (R02.24)
- [ ] Liveness bound to generation + timestamp

## Traps covered
| Trap | Pinned by |
|---|---|
| R01.5 (retry after a route switch never replays the old payload) | P08-13 |
| R01.7 threshold arithmetic and docs drift | P08-02 |
| R01.8 estimates are not evidence; proven overflow bypass | P08-01, P08-06 |
| R01.9 compression failure loops | P08-05 |
| R01.10 fallback is turn-scoped and stateful | P08-13 |
| R01.11 key leakage across endpoints on fallback | P08-13 |
| R02.5 timeouts not counted by the smart breaker | P08-14 |
| R02.24 smart approver as an injection target | P08-14 |

## Acceptance
| Feature | Planned acceptance ids |
|---|---|
| A23 credential pools | ACC-A23-2 (rotation policy) |
| A24 fallback chain + restore | ACC-A24-1, ACC-A24-2 (restore next turn) |
| A34 run budget wrap-up (P1 part) | ACC-A34-2 |
| A36 automatic compression | ACC-A36-1 (threshold table), ACC-A36-2 (fallback summary), ACC-A36-3 (cooldowns) |
| A37 manual `/compress` | ACC-A37-1 |
| A38 overflow recovery | ACC-A38-1 |
| A40 cache markers | ACC-A40-1 |
| A41 usage and cost | ACC-A41-1 |
| A42 auxiliary client | ACC-A42-1 |
| A46 Responses transport | ACC-A46-1 |
| A47 Bedrock transport | ACC-A47-1 |
| A48 Gemini native | ACC-A48-1 |
| A62 fast mode | ACC-A62-1 |
| A63 liveness watchdog (lease part is P17) | ACC-A63-1 |
| A68 context-engine interface (built-in side) | ACC-A68-1 |
| B10 compression lineage (engine side) | ACC-B10-1 |
| B24 usage incl. auxiliary | ACC-B24-1 |
| E5 smart pre-judge | ACC-E5-1 |
| A8 `@` references expand into the user message | ACC-A8-1 (forms and caps), ACC-A8-2 (credential paths refused) |

## Done when
- All suites pass, including the 300-turn soak with compression, rotation and fallback.
- A live two-hour session (manual, recorded in the P08-18 note) compresses at least once, keeps the
  original task, and its reported cost matches the provider's dashboard within rounding.
- Each new transport replays its two recorded captures, and a live smoke call per transport passes
  in the weekly `live` lane.
