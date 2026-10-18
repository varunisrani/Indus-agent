# Task: Route binding + D-04.1 decision (P04-23) Phase: 04 Owner: wave-6a builder Y (route binding); wave 6b (D-04.1 criteria + decision)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/agent_runtime_helpers.py`
— the in-place model switch (`switch_model` and its private steps: the snapshot field tuple, the
snapshot/restore pair, the runtime swap, context-length re-resolution, compressor repoint, the
finishing bookkeeping) and `restore_primary_runtime` (field list only), plus a glance at
`agent/primary_runtime_snapshot.py` (the persisted primary-runtime record's field names). Closed
before writing. Ours: `docs/plan/research/01-agent-core.md` §3.8 (model switch, context length),
§3.13 (fallback and primary restore), trap R01.5; `docs/notes/P03/{route,context-length}.md`;
`docs/notes/P00/async-trial.md`; `docs/notes/P04/prompt-lifecycle.md`.
Evidence: none.

What it does (own words): a session talks to one route at a time, and many things hang off it:
the provider and model names, base URL, wire format (API mode), credential, the HTTP client, the
reasoning setting, provider-specific extra request fields, the context window (which drives
compression), the prompt-cache policy and the `Model:`/`Provider:` lines of the system prompt.
`/model` (P06) and fallback (P08) change the route mid-session. If a switch half-applies — a new
model name with the old client, or the old window with a new model — the next request fails
(often a 400/404 storm) or compresses at the wrong size. `RouteBinding` keeps all of it in one
immutable `RouteState` and changes it in one step:

`apply(new_route)` computes the whole next state first — validates the route (model and base URL
present), re-resolves the context window through P03 (`context_length.resolve`, which refuses
windows below 64,000 with `ContextTooSmall`), gets a client from the factory, derives the
reasoning effort, extra body and cache policy for the new route (the old route's extra body never
carries over) — then runs the registered dependents in order (the prompt-identity rewrite through
`PromptLifecycle.rebind_identity`; P08 adds the compressor). If any step raises, the dependents
that already ran are undone in reverse order, the state is untouched and the error is re-raised.
Only when everything succeeded is the new state swapped in (one assignment) and the generation
bumped. `rollback()` returns to the state before the last apply through the same dependent path;
`rollback(to=state)` restores an explicit snapshot (P08's "restore primary at turn start").

Inputs → outputs: `RouteBinding(scope, state, *, client_factory, list_models, dependents)`;
`RouteBinding.bind(scope, route, ...)` (first binding, same derivation as apply);
`await apply(route) -> RouteState`; `await rollback(to=None) -> RouteState | None`;
`state`, `generation`, `previous`; `add_dependent(fn)`; `prompt_identity(lifecycle)` dependent.

Files: `src/indus/core/route.py`, `tests/unit/core/test_route.py`; shared:
`src/indus/foundation/errors.py` (one row + class `RouteRefused`, `provider.route_refused`).

Edge cases / failure modes:
- window below 64,000 (config override, `/models` answer, cache) → `ContextTooSmall`, nothing
  changed, no dependent ran.
- empty model or base URL → `RouteRefused`; the reference refused to keep the previous provider's
  URL when none resolved — we refuse any route without one.
- client factory raises → nothing changed.
- a dependent raises → earlier dependents get `(new, old)` to undo, in reverse; an undo that
  raises is logged and the original error still propagates.
- two applies at once (UI + fallback) → serialized by an `asyncio.Lock`; the second sees the
  first's result as its "old".
- rollback with nothing to roll back to → `None`, nothing changes.
- a pooled client the rollback target holds may have been closed meanwhile → a fresh one is taken
  from the factory.
- the loop reads `binding.state` once per request and compares `generation` before a retry, so a
  retry never replays a payload built for the old route (R01.5); that check is the loop's (6b).

Our design differences / decisions:
- One frozen `RouteState` value instead of a list of attribute names copied off an agent object;
  rollback is "put the old value back", so there is no partial restore and no "attribute did not
  exist" sentinel.
- Route-dependent extras without a P03 home are derived here from free-form config (no schema
  change): `providers.<name>.models.<id>.{reasoning_effort,extra_body}` over
  `providers.<name>.{reasoning_effort,extra_body}`; the session's own requested effort (what
  `/reasoning` sets) carries across switches when the config names none. Cache policy: native
  cache markers for the `anthropic_messages` wire, none otherwise, `providers.<name>.prompt_caching:
  false` turns them off. P08 replaces `cache_policy_for` when prompt caching lands.
- The prompt is not rebuilt on a switch: only its identity lines are rewritten (P04-07's
  `rebind_identity`), so the cache prefix survives as far as possible; the reference drops its
  cached prompt and rebuilds on a switch.
- Not here (other phases): credential pools and pool reload (P07), fallback chain bookkeeping
  and primary-restore gates (P08), compressor (P08 registers a dependent), billing-route
  persistence (P08 usage), reasoning-replay verdict reset (P08 recovery), capability maps (P08).

Tests we will write (`tests/unit/core/test_route.py`):
- bind derives every field (route parts, client, window, reasoning, extra body, cache policy,
  identity) from the route and config;
- apply swaps everything and bumps the generation; identity dependent rewrites prompt lines;
- window < 64K (config override and `/models` answer) refused, state identical, no dependent ran;
- failing step at each point (client factory, each dependent position) → every field restored,
  earlier dependents undone in reverse;
- undo that itself raises is logged, original error kept;
- rollback returns to the previous state and re-runs dependents; nothing to roll back → None;
  explicit `rollback(to=)`; closed client replaced on rollback;
- extra body never leaks across providers; requested effort carries over unless config names one;
- concurrent applies serialize.

## For wave 6b (not built here)
- `tests/integration/core/test_d04_1_criteria.py` against the real loop: (a) interrupt mid-stream
  closes the stream ≤200 ms with valid alternation; (b) a command with timeout killed by timeout
  + 2 s while the loop stays responsive (fake blocking tool until P05); (c) 50 concurrent fake
  sessions p99 event latency < 50 ms; (d) a hanging sync hook abandoned after its timeout without
  blocking others; (e) a sync caller drives and interrupts a turn (through P04-21's
  `InProcessHost`). Injected clocks where possible, wall-clock bounds marked `perf` (a new marker
  must be added to `pyproject.toml` because of `--strict-markers`).
- `docs/notes/P04/d-04-1-decision.md`: the numbers, the verdict; P00/P03 trial numbers are the
  baseline (`docs/notes/P00/async-trial.md`).
- Wire `RouteBinding` into the Agent (P04-20): the Agent owns one binding, registers
  `prompt_identity(lifecycle)` as a dependent, reads `binding.state` once per provider request and
  drops a retry whose `generation` changed (R01.5).
- Unify the two blocking helpers (below).

### Proposal: the one D-04.1 blocking helper (for 6b to build)
Today: `tools.dispatch.run_sync` (default executor + `wait_for`; executor threads are joined at
interpreter exit, so a stuck handler can hang shutdown; no pool bound) and the private
`_start_read`/`read_bounded` pair in `core/prompt/context_files.py` (daemon thread per read,
deadline on the scope clock, result drained). The shared helper must sit below `tools` and `core`
(`tools` may not import `core`), so: `src/indus/foundation/blocking.py`.

```python
class PoolFull(IndusError): ...  # code "blocking.pool_full" (new ERROR_CODES row)


class DeadlineMissed(TimeoutError): ...  # the work is abandoned, never killed


class BlockingPool:
    """A fixed number of daemon worker threads; never grows; one per purpose."""

    def __init__(self, name: str, workers: int) -> None: ...
    @property
    def busy(self) -> int: ...  # abandoned work keeps its slot until it returns
    async def run[T](
        self,
        fn: Callable[[], T],
        *,
        deadline: float | None,  # seconds on `clock`; None = wait for ever
        clock: Clock | None = None,  # scope clock (fake in tests); None = loop time
        label: str = "",  # for the timeout log line
    ) -> T: ...

    # - copies contextvars (profile scope travels, R01.16);
    # - raises PoolFull at once when every worker is busy (refuse, do not queue for ever);
    # - on deadline or caller cancellation: abandon, drain the late result/exception, log once;
    # - daemon threads, so a stuck call never blocks process exit.


POOLS: Mapping[str, BlockingPool]  # "io" (8: file reads), "tools" (8: sync handlers),


# "hooks" (2: sync plugin hooks, as in the P00 trial)
async def run_blocking[T](fn, *, pool="io", deadline=None, clock=None, label="") -> T: ...
```
`dispatch.run_sync` becomes `run_blocking(fn, pool="tools", deadline=entry.timeout_s)` and
`read_bounded` becomes `run_blocking(lambda: reader(path), pool="io", deadline=…, clock=…)` with
its own None-on-error mapping kept. Child processes are not this helper's job: P05's terminal
runner keeps the SIGTERM-at-timeout, SIGKILL-2-s-later backstop from the P00 trial.

## Decisions for the lead
- `RouteRefused` (`provider.route_refused`) added to `foundation/errors.py` for a route with no
  model or base URL (shared file, one row + one class).
- Reasoning/extra-body/cache-policy sources as above (free-form `providers.*` keys, no schema
  change); P07/P08 may move them into the schema.
- Identity lines are rewritten, not rebuilt, on a switch (keeps D-16 R01.1 spirit); if the lead
  prefers the reference's rebuild, the dependent can call `invalidate` instead once P08 allows it.

Compare checklist (filled after the rewrite; reopened the switch steps, the snapshot field tuple,
`restore_primary_runtime` and the primary-runtime record):
- [x] provider, model, base URL, API mode, credential, client snapshotted and restored (test:
  `tests/unit/core/test_route.py::test_failing_step_restores_every_field`)
- [x] context length re-resolved on switch, config override re-read for the new route (test:
  `::test_apply_swaps_every_field`, `::test_config_override_for_another_model_is_not_carried`)
- [x] reasoning re-resolved per model; extra body of the old provider dropped (test:
  `::test_extra_body_never_leaks_across_providers`, `::test_reasoning_follows_config_then_session`)
- [x] cache policy re-evaluated (test: `::test_cache_policy_follows_the_wire`)
- [x] prompt identity rewritten, not left stale (test: `::test_identity_dependent_rewrites_prompt_lines`)
- [x] a failure anywhere leaves the old runtime whole (tests: `::test_failing_step_restores_every_field`,
  `::test_small_window_refused_before_any_dependent`)
- [x] missing base URL refused instead of keeping the old provider's URL (test:
  `::test_incomplete_route_refused`)
- [x] restore to an earlier snapshot (primary restore shape) (test: `::test_rollback_to_explicit_snapshot`)
- Deliberate differences: one immutable state value swapped once instead of per-attribute
  setattr with a rollback snapshot; identity lines rewritten instead of a prompt rebuild;
  dependents are registered callables instead of fixed calls into the compressor.
- Not taken: credential pool reload, local-server preload before the window probe, fallback chain
  pruning on a deliberate switch, stale-streak reset, capability map publication, billing route
  persistence, reasoning-replay reset (P07/P08 own these; each becomes a dependent or a state field);
  session-level request overrides that survive a switch (service tier / speed) — no such setting
  exists yet; when P08 adds one it becomes a carried field like `requested_effort`.

## Status

Partial (2026-10-18): the RouteBinding half is done; the D-04.1 criteria suite and decision note
are wave 6b (see "For wave 6b").
