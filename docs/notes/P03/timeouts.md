# Task: Timeout policy (P03-13) Phase: 03 Owner: P03-B2 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/chat_completion_helpers.py`
(`_stale_streak` and the breaker helpers ~555–630, `_local_stream_stale_timeout_default` 636,
`_scale_stale_timeout_for_context` 651, `_cloud_stale_timeout` 661, `_derive_stream_stale_timeout`
674, `cap_to_run_budget` and `_cloud_stale_timeout_for` ~680–720, `_resolve_direct_stale_timeout`
836), `run_agent.py` `_compute_non_stream_stale_timeout` / `_stale_timeout_is_explicit` (563–592,
followed from 836 for the non-stream numbers), `agent/chat_completion_local.py` `stale_giveup`
(names only), `agent/reasoning_timeouts.py` (structure only: a per-model floor table). Closed
before writing.
Evidence: none.

What it does (own words): one pure function decides how long a model call may stay silent before
the stream driver (P03-14) gives up on the attempt, and what the socket read timeout is. A fixed
number kills healthy reasoning streams on big prompts, an infinite one hangs a session on a dead
endpoint, so the numbers scale with the estimated prompt size, have a floor for reasoning
models, a long but finite patience for local servers, and an operator setting that beats every
implicit rule in both directions (R01.5).

Inputs → outputs: `TimeoutPolicy(explicit_stream_stale, explicit_call_stale,
local_stream_stale, run_budget_remaining, breaker_threshold, read_timeout)` built from a scope
(`TimeoutPolicy.from_scope`) or directly; `for_call(route, est_prompt_tokens, *, streaming,
reasoning)` → `CallTimeouts{stale (float | None = disabled), read, explicit, local,
breaker_threshold}`.

Rules (exact numbers from the phase row):
- streaming, cloud: 180 s; > 50,000 est. tokens → at least 240 s; > 100,000 → at least 300 s;
  reasoning flag → at least 300 s.
- streaming, local (loopback, or a private-network address on a custom endpoint): 900 s from
  `agent.local_stream_stale_timeout` (or `INDUS_LOCAL_STREAM_STALE_TIMEOUT`).
- non-stream, cloud: 90 s; > 50,000 → at least 150 s; > 100,000 → at least 240 s.
- non-stream, local, nothing configured: disabled (`None`).
- explicit operator value (`INDUS_STREAM_STALE_TIMEOUT` / `INDUS_API_CALL_STALE_TIMEOUT`, or the
  config keys `agent.stream_stale_timeout` / `agent.api_call_stale_timeout`) is returned as is:
  it can shorten (60 on a 120K reasoning call) or lengthen (2,000) patience.
- run budget: an implicit value is capped at max(60, 50 % of the remaining budget); an explicit
  one is never capped.
- read timeout: base 120 s, raised to the stale timeout when that is larger (never lowered).
- breaker threshold: 5 consecutive stale attempts (`INDUS_STREAM_STALE_GIVEUP`), 0 disables.

Files: `src/indus/providers/timeouts.py`, `tests/unit/providers/test_timeouts.py`,
`src/indus/foundation/config/schema.py` (shared: `agent.local_stream_stale_timeout: 900`).

Edge cases / failure modes:
- bad env value (`abc`, negative) → ignored with one warning, implicit rule applies.
- explicit 0 → disabled (None) — an operator can turn stale detection off.
- route with an unparseable base URL → treated as cloud (never as local; local gets the longest
  patience).
- run budget already spent (remaining ≤ 0) → cap is 60 s.

Our design differences / decisions:
- The reference reads globals and agent attributes at call time; ours is a frozen value built
  once from a scope and a pure method, so the table test needs no environment at all.
- The reference's env stale base is still floored by the context tiers; ours lets an explicit env
  value win in both directions, as the phase row says (only the per-provider config did so there).
- Reasoning is a flag passed by the caller (the profile/model decides), not a model-name table here.
- Responses-API floors, the high-effort silence floor and the long-decode multiplier are P08.

Tests we will write:
- the ≥ 20-row table from the phase row plus local/explicit/budget combinations; env parsing;
  read-timeout raise; breaker threshold from env; `from_scope` reading config and env.

## Decisions for the lead
- Config keys for the explicit values are `agent.stream_stale_timeout` and
  `agent.api_call_stale_timeout` (unset by default, read with `scope.config.get`); only
  `agent.local_stream_stale_timeout` was added to the schema, the two others stay schema-less so a
  default does not count as "explicit". Add them to the schema as `float | None` if wanted.
- "Private-network custom endpoint": a literal RFC 1918 / CGNAT / ULA / link-local IP or a `.local`
  / `.internal` host counts as local for any provider (a vendor profile pointed at a private proxy
  gets the local patience too). Loopback comes from `providers.hosts.is_loopback` (P03-04).
  P03-A also added `hosts.is_private_network`; the lead may swap our private-address check for it
  (ours additionally counts CGNAT 100.64/10 and `.local` / `.internal` / `.lan` names).

Compare checklist (filled after the rewrite; reopened the files above):
- [x] 180/240/300 cloud stream tiers at > 50K / > 100K (test: `tests/unit/providers/test_timeouts.py::test_table`)
- [x] reasoning floor 300 for implicit values only (test: `test_table`, rows `reasoning-*`)
- [x] local 900 from config / env, non-stream local disabled when unset (test: `test_table`, rows `local-*`)
- [x] non-stream 90/150/240 (test: `test_table`, rows `nonstream-*`)
- [x] explicit wins in both directions and is never capped by the run budget (test: `test_table`, rows `explicit-*`, `budget-*`)
- [x] run budget cap max(60, 50 %) on implicit values (test: `test_table`, rows `budget-*`)
- [x] breaker 5, 0 disables (test: `test_breaker_threshold_from_env`)
- [x] read timeout raised to the stale value (test: `test_read_timeout_raised_never_lowered`)
- Deliberate differences: explicit env value is not floored (phase row); reasoning floor is a flag,
  not a name table; one frozen policy value instead of agent attributes.
- Not taken: Responses/Codex floors and hard ceiling, high-effort silence floor, long-decode
  multiplier, Bedrock id normalisation (all P08).

## Status

Done (2026-10-18). `agent.local_stream_stale_timeout` added to the config schema (shared file).

## Integration (lead)

- `is_local_endpoint` now starts from `providers.hosts.is_private_network` (loopback, RFC 1918,
  link-local, unique-local, IPv4-mapped forms) and keeps only what that function does not cover:
  the unspecified address (`0.0.0.0`, `::`), CGNAT `100.64.0.0/10` and the local-only suffixes. Hosts
  are read with `hosts.host_of` (strict parse; a non-http(s) URL is not local). Table tests
  unchanged and passing.
