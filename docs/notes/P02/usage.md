# Task: usage accounting (P02-16) Phase: 02 Owner: P02 wave-4 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_usage.py` (all: the
sessions update in both modes, first-accounted route, per-model upsert, auxiliary path, lineage
reads, store totals), `hermes_state_sessions.py` (`get_recent_session_model_route` 839),
`tests/hermes_state/test_aux_usage_accounting.py` (all). Closed before writing.
Evidence: none.

What it does (own words): every model call reports tokens and cost. The session row keeps the
conversation's running totals and its route (which model, which billing provider, which base URL);
`model_usage` keeps a per-route breakdown so a mid-session model switch is not billed to the first
model. Gateways hold cumulative totals and report them as absolute values; the CLI reports per-call
deltas. Side calls (title generation, compression summaries, vision) cost money too but are not the
conversation's own turns, so they land only in the per-model table under their task name.

Inputs → outputs:
- `UsageDelta(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
  reasoning_tokens, api_calls=1, estimated_cost_usd=None, actual_cost_usd=None, cost_status=None,
  model=None, billing_provider=None, billing_base_url=None)`.
- `record(store, session_id, delta, *, mode="incremental", task="") -> bool` (False when the
  session row does not exist); `record_in(conn, …, now)`.
  - `task == ""`, incremental: counters added; `estimated_cost_usd` += (None counts 0);
    `actual_cost_usd` added only when given (kept otherwise); `cost_status` latest non-None; route
    columns (model, billing provider, base URL) filled only where NULL and only when the delta
    carries usage; `api_call_count` += `api_calls`. First accounted call (stored
    `api_call_count` 0) with a model and provider different from what create recorded → that route
    replaces the requested one (a fallback answered). The delta is also upserted into
    `model_usage` keyed by (session, model, provider, base URL, ''), missing fields taken from the
    session's route ('unknown' / '' when none).
  - `task == ""`, absolute: counters and `api_call_count` set; estimated set (None → 0); actual set
    when given; route COALESCE-filled; no `model_usage` row (cumulative totals cannot be split by
    route).
  - `task != ""` (auxiliary): only `model_usage` under that task; model/provider/base URL from the
    delta only (never the session's main route); `api_calls` as reported (default 1).
- `set_route(store, session_id, *, billing_provider, billing_base_url, model=None) -> bool`:
  unconditional write of the route (a `/model` switch), model only when given.
- `recent_route(store, session_id) -> Route | None`: newest main-loop `model_usage` row with a known
  model and provider across the session's compression lineage, by `last_seen` then insertion
  order.
- `totals(store, session_id) -> UsageTotals`: session counters summed over the compression lineage
  (root … tip); cost = Σ actual-else-estimate.
- `by_model(store, session_id) -> list[ModelUsage]`: `model_usage` summed over the lineage, grouped
  by (model, provider, base URL, task).
- `store_totals(store, *, include_archived=False) -> UsageTotals`: every session row plus auxiliary
  `model_usage` rows; cost per row prefers `actual_cost_usd` over `estimated_cost_usd`.

Files: `src/indus/state/usage.py`, `tests/unit/state/test_usage.py`.

Edge cases / failure modes:
- a delta with all zeros and no cost does not fill the route.
- three deltas on one route → one `model_usage` row; a route switch → a second row.
- auxiliary call with no model → model 'unknown', provider ''.
- unknown session → False, logged, nothing written (no placeholder row: usage for a deleted or
  never-created session is not invented).
- writes go through the `ROUTINE` writer synchronously (the background coalescing writer is P08).

Our design differences / decisions:
- no placeholder session creation, no billing mode / cost source / pricing version columns
  (schema 1 has none), no background queue yet.
- `recent_route` walks the lineage so a fresh continuation reports the route its root used.
- store totals count every segment's own counters once plus auxiliary usage.

Tests we will write: three incremental deltas sum; absolute replaces; auxiliary `title` call does
not change session counters but appears per model; lineage of two segments sums; actual cost
preferred in totals; route switch mid-session reflected by `recent_route` (and `set_route`);
first-accounted route replaces the requested one; zero delta leaves route NULL; unknown session.

## Decisions for the lead
- Unknown session → `record` returns False instead of creating a placeholder row.
- `UsageDelta.api_calls` defaults to 1 (one delta = one API call).

Compare checklist (filled after the rewrite; reopened the files named in `Read:`):
- [x] incremental adds, absolute sets; estimated COALESCE, actual only when given; route
  COALESCE-filled only when the delta accounts usage (tests: `tests/unit/state/test_usage.py::test_incremental_deltas_sum`,
  `::test_absolute_replaces`, `::test_empty_delta_does_not_fill_the_route`)
- [x] first accounted route replaces the requested one
  (`::test_first_accounted_route_replaces_the_requested_one`)
- [x] per-model upsert keys, session fallbacks for the main loop, none for side calls
  (`::test_auxiliary_call_only_lands_per_model`)
- [x] lineage reads; store totals prefer actual cost (`::test_lineage_of_two_segments_sums`,
  `::test_store_totals_prefer_actual_cost`)
- [x] recent route by `last_seen` then insertion order, main loop only
  (`::test_route_switch_is_reflected`, `::test_recent_route_reads_the_lineage`)
- [x] gap found: a side call reporting `api_calls=0` keeps 0 (only "not given" means one call; the
  dataclass default) — pinned by `::test_auxiliary_call_only_lands_per_model`
- Deliberate differences: no placeholder session row for unknown ids; store totals count every
  segment once plus side calls (the reference sums roots only); `recent_route` reads the lineage.
- Not taken: background coalescing writer (P08), billing mode / cost source / pricing version
  columns (not in schema 1).

## Status

Done (2026-10-18). `usage.py` with 10 unit tests. Uses `lineage.lineage_ids_in` (slice A).
