# Task: Fallback chain with turn-scoped restore (P08-13) Phase: 08 Owner: P08 wave 2 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/chat_completion_helpers.py`
(`try_activate_fallback` and its skip / exhausted helpers, ~1916–2110), `agent/agent_runtime_helpers.py`
(`restore_primary_runtime` ~1428), `agent/fallback_cooldown.py`,
`website/docs/user-guide/features/fallback-providers.md`. Research `01` §3.13, traps R01.5,
R01.10, R01.11. Closed before writing; reopened only for the compare step.
Evidence: none.

What it does (own words): `fallback_providers` is an ordered list of `{provider, model}` entries
(an optional `base_url` for a custom endpoint); an entry without both provider and model is
ignored. When the bound route fails in a way another route could fix, the session moves to the
next usable entry for the rest of the turn; the next turn starts on the primary again unless the
primary is known to fail.

Triggers (all after P03's retries and P08-12's pool rotation gave up):
- the classifier flags the failure for fallback (`should_fallback`: auth 401/403, billing, rate
  limits, model not found, content policy, request format, TLS) or it is a transient failure that
  outlived its retries (overloaded, 5xx, timeouts, connection, `incomplete_response`, unknown —
  a bare 404 lands here);
- a content-filter refusal with nothing shown, and the empty-answer ladder giving up (invalid
  responses);
- eager: an `upstream_rate_limit` verdict skips the remaining same-route retries when a usable
  chain entry exists (the agent's classifier wrapper asks `FallbackChain.wants_eager`).
Context overflow and payload size stay with compression (P08-05/06).

Activation (`FallbackChain.activate`):
1. stay on the primary (one wait per turn) when the failure is a rate limit whose declared reset
   is sooner than `fallback.min_switch_reset_s` (0 = off): the loop waits it out and retries;
2. leaving the primary on a rate-limit/billing failure arms a primary cooldown: the provider's
   reset, else `min(60 × 2^k, 14,400)` s with `k` counting arms in this session;
3. walk the chain from the current index, skipping entries that are known unavailable, name a
   model rejected for the account or a non-chat model, name the backend that just failed, need the
   network while the host says it is offline, or whose credential pool would fail (P08-12);
4. resolve the entry with P03 rules (the key is only sent to its own exact host; the API mode is
   detected from the entry's own base URL, R01.11) and `RouteBinding.apply` it: client, API mode,
   reasoning, extra body, window (compressor via `Agent._follow_route`), cache policy, the
   prompt's `Model:` / `Provider:` lines — all or nothing; the pool follows the provider because
   the caller picks it per request; a failed bind marks the entry unavailable and the walk goes on;
5. a `Notice` (`key="fallback"`) names both routes and the reason; the new route's stale streak is
   reset; the loop refunds the iteration and rebuilds the request for the new route (generation
   bump: a retry never replays the old payload, R01.5); when the iteration budget is spent the
   refund gives the fallback its one bounded grace call.

Turn scope (`FallbackChain.restore`, prologue, before the prompt is restored/built): when a
fallback is active, the primary snapshot is put back (`RouteBinding.rollback(to=…)`) unless the
primary is cooling down, its model was rejected for the account or is non-chat, or its pool would
fail; a "primary restored" notice is sent only when a real fallback ran. The chain index goes back
to 0 whenever no fallback is active at turn start — also after an activation that found nothing
(R01.10) — and after a restore.

Subagents inherit the provider, not the chain (they get the parent's `PoolSet`, never its
`FallbackChain`; no delegation exists yet).

Files: `src/indus/core/fallback.py`, `tests/unit/core/test_fallback.py`. Shared:
`src/indus/core/loop.py` (fallback after retries / content filter / empty ladder),
`src/indus/core/prologue.py` (restore at turn start), `src/indus/core/turn_state.py`
(`SessionRuntime.fallback`), `src/indus/core/agent.py` (chain built in the session phase),
`src/indus/foundation/config/schema.py` (`fallback.min_switch_reset_s`).

Edge cases / failure modes:
- empty or malformed chain → no fallback, the turn fails as before;
- every entry unusable → activation returns nothing; the index stays past the end for this turn
  and is reset at the next turn start (R01.10);
- an entry equal to the failing route → skipped (falling back to it loops the failure);
- the primary restore itself fails (window lookup, client) → stay on the fallback, logged;
- a rate-limited primary without a parseable reset → exponential cooldown ladder;
- a chain walked on a non-rate-limit failure arms a 5 s gate so the next turn does not replay the
  whole context over every route at once.

Our design differences / decisions:
- eager fallback only for `upstream_rate_limit` (see compare notes);
- triggers are read from the classifier's verdict, not from exception types;
- the "durable" notice is a `Notice` event with a stable key (the surfaces keep it); there is no
  separate buffered one-shot list;
- offline is a constructor flag (`offline`), no detection yet;
- non-chat models are recognised by our own short list of model-id words.

Tests planned: fake routes (respx) for each trigger; skip rules; every bound field restored at the
next turn; failed activation still resets the index; no "restored" notice when nothing fell back;
the second request after a fallback goes to the new host; cooldown ladder and provider reset;
stay-on-primary threshold.

Compare checklist (after writing):
- [x] rebinding list complete; restore undoes all of it (one RouteState snapshot)
- [x] index reset on failed activation (R01.10)
- [x] no "restored" notice unless a fallback really ran; none for an unentitled primary
- [x] exact-hostname API-mode detection on fallback routes (R01.11)
- [x] no retry replays a payload for a previous route (R01.5)

Compare notes: eager fallback — the reference also falls back early on other `should_fallback`
verdicts when the pool cannot recover; ours lets P03's retries (server-named waits) and the pool
run first for those and is eager only for an upstream (aggregator) 429, which no key can fix. Auth
failures: the reference escalates once per turn; ours walks the chain from the current index
(each activation moves it, bounded by the chain length and the loop's restart cap). Invalid tool
calls (`invalid_tool` strikes) end the turn as before — a follow-up could route them to the chain.
The reference also rebuilds a primary client once for transient transport errors
before rotating, runs an auto-recovery ladder after the chain, records fallback metrics, and
probes one vendor's quota window early; none of that is in this task. It skips a candidate whose
pool is exhausted only when the soonest key returns after more than 600 s — same rule here.

Decisions for the lead:
- new config key `fallback.min_switch_reset_s` (default 0) in `config/schema.py` (`FallbackSection`);
- the loop treats the empty-answer ladder's terminal step as an "invalid response" trigger;
- `RouteCaller` gained a `classify` field; the agent always passes a route/profile/clock-aware
  classifier, so profile `classify_error` hooks and `reset_at` now apply on the main path too.

## Integration (lead)

- `SessionRuntime.fallback` sits next to slice B's `approval_judge`; the soak (P08-18) drives a
  real `fallback_providers` entry (`custom` + `base_url`) through a 429 storm and the next turn's
  restore.
- Intended difference: D-16.111 (eager fallback only on `upstream_rate_limit`).
- Follow-ups: `invalid_tool` strikes still end the turn instead of walking the chain; subagents
  get the parent's pools but never its chain — untestable until delegation lands (P10).
