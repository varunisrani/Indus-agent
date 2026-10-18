# Task: Credential pool runtime (P08-12) Phase: 08 Owner: P08 wave 2 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/credential_pool.py` — only
the exhaustion TTL helpers, `select` / `_available_entries` / `_select_unlocked` / `peek` /
`reclaim` / `mark_exhausted_and_rotate` / `try_refresh_*`; the 401 refresh cap in
`agent/agent_runtime_helpers.py` (~940–990); `website/docs/user-guide/features/credential-pools.md`.
Research `01` §3.12. Closed before writing; reopened only for the compare step.
Evidence: none.

What it does (own words): a provider may hold several keys or sign-ins (P07 pool rows in
`auth.json`, secrets in the SecretStore, plus the env-seeded rows of the profile's own key
variables). For each model request the agent's caller asks the provider's live pool for a
credential and sends the request with that key; the HTTP client stays (the key travels per
request). The pool keeps the session on one credential until it fails, then applies the policy:

| verdict | action | cooldown |
|---|---|---|
| generic 429 | first one: retry on the same key; a second in a row: rotate | 1 h |
| 429 / 402 naming a plan or usage limit | rotate at once | provider reset, else 1 h |
| 402 / billing | rotate at once | 1 h |
| 401 (auth) | refresh a sign-in first (at most 2 per entry per turn); else rotate | 5 min |
| 400 naming a model the plan does not include | bench that key for that model only | until `indus auth reset` |
| nothing left | the error goes on to the fallback chain (P08-13) | — |

A provider's `reset_at` (classifier) replaces the default cooldown. The "already retried a 429"
flag clears on every answered request. For vendors whose rate limits are per model (Anthropic), a
generic 429 benches only that model. After a quota rotation the session goes back to the
credential it started on as soon as that one's cooldown has passed (checked on every request).
Every new selection is counted with P07's `note_request` (folded into the rows on the next pool
write); `round_robin` moves the chosen entry to the back of the order and writes it. Benches are
written to `auth.json` through `edit_pool`, so `indus auth list` shows them and other sessions skip
the key; an inherited (root profile) pool is benched in memory only. Status questions (`peek`,
`would_fail`) never count, rotate, refresh or bench. Subagents get the parent's `PoolSet`
(`AgentOptions.pools`) so they share the same live pools.

Keys added with `indus auth add` make a provider usable: when no key variable resolves, route
resolution (P03 `providers.route.resolve`) asks the pool for its next usable entry (`pool_key`),
and the first-run guard counts a pool with a usable entry as "set up".

Inputs → outputs: provider profile, `AuthStore`, secret reader, env, strategy, clock, RNG,
optional refresher → `Lease` (entry id, label, the key as `Secret`) and `Step` decisions; the
caller wrapper `PoolRuntime.call` returns the call outcome or re-raises `RetryFailed`.

Files: `src/indus/providers/pool.py`, `tests/unit/providers/test_pool.py`. Shared:
`src/indus/core/agent.py` (`BoundCaller.call` asks the pool; `AgentOptions.pools`),
`src/indus/core/model_call.py` (`RouteCaller.classify`), `src/indus/providers/route.py` (pool key
when no variable resolves), `src/indus/cli/first_run.py` (pool counts as set up),
`src/indus/providers/auth/pool_model.py` (`reset` also clears per-model benches).

Edge cases / failure modes:
- the auth store cannot be opened (test isolation guard, unreadable file) → no pool, the route's
  own key is used as before;
- an entry whose secret is missing or placeholder-shaped is never leased;
- a key whose host rule refuses the route's base URL (R01.11) is never leased;
- every entry cooling down → the request still goes out on the bound key (or the entry that comes
  back first) and its failure goes to the fallback chain; the gate question `would_fail` says yes
  only when every entry is out for longer than the 600 s retry-wait cap;
- a bench that cannot be written (locked store, inherited pool) stays in memory and is logged;
- the refresh grant is dead or the token endpoint fails → rotate as a plain 401;
- the route generation moved during a call (R01.5) → no rotation retry, the outcome goes back.

Our design differences / decisions:
- the per-attempt "retry the same key once" is done by a classifier wrapper inside P03's retry
  loop (`RouteCaller.classify`): the second consecutive 429, or the first plan-limit / billing
  answer, is made non-retryable only when another credential is available; with one key P03's
  normal retries run;
- selection is sticky per session (a cached credential serves many requests); counts are
  selections, not requests (as P07 documents);
- per-model benches live in the row as `model_benches` (`{model: until | ""}`), cleared by
  `indus auth reset`;
- the classifier now gets the route, its profile and the clock in the agent path, so profile hooks
  apply and `reset_at` is known (needed for cooldowns and the fallback cooldown).

Tests planned: fake clock + scripted errors for each policy row; return to the original key after
the cooldown; refresh cap; peek / would_fail leave state and disk unchanged; strategy distribution
with a seeded RNG; pool-only key resolves a route and passes the first-run check.

Compare checklist (after writing):
- [x] policy table rows (429 twice, plan limit, 402, 401 refresh then rotate, 400 entitlement)
- [x] reset_at overrides; retried flag cleared on success; per-model 429 vendor
- [x] return-to-original key once the cooldown lifts
- [x] refresh counter while 401s persist
- [x] peeks never count, refresh or bench

Compare notes: the reference also caps the cooldown of a sole credential to a short window for
non-billing failures; the plan table says 1 h, so we keep 1 h but never refuse to send while every
key cools down (the request goes out on the bound key), so a one-key user is not locked out. The
reference's dead-grant marking ("dead" status), leases per subagent task and quota probes for one
vendor are not built (no bundled provider has a refreshable sign-in yet).

Decisions for the lead:
- per-model 429 vendors: `PER_MODEL_RATE_LIMITS = {"anthropic"}` (a vendor fact, not config);
- entitlement 400s are recognised by our own phrase list (`ENTITLEMENT_PHRASES`) — the P03
  classifier has no `model_entitlement` reason; consider adding one;
- sole-credential cooldown cap not built (see compare notes).

## Integration (lead)

- Merged with P08-15 in `BoundCaller.call`: `finish_request` → fast tier → `PoolRuntime.call`
  (the pool swaps only `api_key` / `key_env`, so the fast tier's headers survive on every
  leased key; `fast_mode.served` stamps the pooled outcome).
- The P08-18 soak's 429 storm now runs through this pool (two keys, see `long-session.md`).
- Intended differences: D-16.109 (sticky selection), D-16.110 (same-key retry inside P03's retry
  loop), D-16.112 (no short cap on a sole credential's cooldown).
- Follow-ups: add a `model_entitlement` reason to the P03 classifier and drop
  `ENTITLEMENT_PHRASES` here; subagent inheritance of `AgentOptions.pools` is untestable until
  delegation exists (P10).
