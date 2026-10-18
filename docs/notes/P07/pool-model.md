# Task: Credential pool data model (P07-07) Phase: 07 Owner: P07 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/credential_pool.py` (entry
shape, constants, strategies, `peek`, env seeding outline — storage part only),
`website/docs/user-guide/features/credential-pools.md` (CLI output, strategies, request counts);
research `04-cli-config.md` §3.16.3. Closed before writing.
Evidence: none.

What it does (own words): a provider can hold several credentials; the pool records them in a
fixed order with a health status so the runtime (P08) can rotate when one is rate-limited or
spent. This task is the data model and the operations the `indus auth` commands and the runtime
share: add (at a priority or last), remove (by 1-based index, id or exact label), reset status,
move to a priority (others renumbered), and a side-effect-free peek of what would be used next.
Keys found under the provider profile's declared env var names (keychain or environment) show up
as entries with `source: env`, so a person who only set `OPENROUTER_API_KEY` sees a one-entry pool.

Inputs → outputs:
- `PoolEntry{id, label, auth_type: api_key|oauth, source: manual|env|device_code|pkce, priority,
  status: ok|exhausted, cooldown_until?, last_error_class?, request_count, env_var?, expires_at?}`
  ↔ JSON row (no secret fields).
- `Pool(provider, entries)`: `add(entry, priority=None)`, `remove(target)`, `reset(target=None)`,
  `set_priority(target, n)`, `resolve(target)`, `peek(strategy, now)`; misses raise
  `PoolTargetError` with the reason (no match, ambiguous label, out of range).
- `load_pool(store, provider, env_rows)` / `edit_pool(store, provider, fn, env_rows)`: read or
  change one provider's rows inside one store transaction; pending in-memory request counts are
  folded in on that write.
- `strategy_for(config, provider)`: `credential_pool_strategies.<provider>`, default `fill_first`;
  an unknown value warns once and falls back. `check_strategy(name)` raises on unknown names.
- `env_entries(profile, secrets, env)`: entries for each declared env var that holds a usable
  (non-placeholder) value.

Files: `src/indus/providers/auth/pool_model.py`, `tests/unit/providers/auth/test_pool_model.py`;
shared: `src/indus/foundation/config/schema.py` (`credential_pool_strategies`).

Edge cases / failure modes:
- Priority beyond the end clamps to last; negative clamps to 0; renumbering keeps 0..n-1.
- A label equal to a number is still found by label only if no index matches (index wins).
- Two entries with the same label → removal by that label is refused as ambiguous.
- Env entries whose variable disappeared drop out; a removed env entry is suppressed until the
  person adds a credential to that provider again.
- `peek` never changes status, counts or order, even for an expired cooldown.

Our design differences / decisions:
- Our source names are `manual`, `env`, `device_code`, `pkce` (the reference uses compound
  `env:NAME` strings); the env var name is its own field.
- `random` strategy's peek returns the first available entry (selection is not predictable).

Tests we will write:
- Priority insert at 0, middle and end; renumbering; remove by index/id/label; ambiguous label.
- Peek leaves state unchanged (rows compared before/after); exhausted entry with a past cooldown
  counts as available for peek but is not reset.
- Env-seeded entries listed; placeholder value ignored; strategy validation.
- Request counts kept in memory then written on the next pool write.

## Decisions for the lead
- Env-seeded rows are materialised into `auth.json` on the next pool write (so their status and
  priority persist), and pruned when the variable no longer resolves.

Compare checklist (filled after the rewrite; reopened `agent/credential_pool.py`, docs page):
- [x] Strategies and default (test: `test_pool_model.py::test_strategy_validation_and_default`)
- [x] Priority clamp and renumber (test: `test_pool_model.py::test_priority_insert_and_renumber`)
- [x] Peek is read-only (test: `test_pool_model.py::test_peek_leaves_state_unchanged`)
- [x] Env rows listed, persisted, dropped (test: `test_pool_model.py::test_env_rows_merge_persist_and_drop`)
- [x] Request counts written on the next pool write (test: `test_pool_model.py::test_request_counts_in_memory_until_next_write`)
- Ordering found while testing: an env key present before the first `auth add` keeps position #1 (it was
  materialised first); `indus auth priority` moves it. Same order as the reference.
- Deliberate differences: see above.
- Not taken: model-scoped cooldowns, leases, refresh-on-select, singleton seeding from other
  products' credential files (P2/P3).

## Status

Done (2026-10-18). Shared change: `src/indus/foundation/config/schema.py` (`credential_pool_strategies`, a dict of provider → strategy). Selection with rotation and benching on errors is P08's; `Pool.mark_exhausted`, `note_request` and `peek` are the seams it uses.

## Integration (lead)

- `pool_secret_names(store)` / `is_pool_secret(name, known)`: profile clones skip these
  (`AUTH_<PROVIDER>_<ID>`; also any name shaped like a generated one).
- `prune_env_rows(store, var)`: `indus config unset NAME` drops the stored env-seeded rows of NAME
  from every pool after the key is removed (they would come back on the next read while NAME is
  still set).
- P08 seam (lead): the runtime route still reads env-named keys; P08's selection must go through
  `load_pool` / `peek` / `mark_exhausted` / `note_request`.
