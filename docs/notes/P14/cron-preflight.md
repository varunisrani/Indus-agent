# Task: preflight, no tokens on misconfiguration (P14-08) Phase: 14 Owner: wave builder C2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/scheduler_preflight.py`
(the checks, fail-open rule, reconnecting MCP servers), `cron/scheduler.py`
(`_preflight_or_block`, `_blocked_config_result`: alert once, cleared on a healthy run). Closed
before writing. Evidence: none.

What it does (own words): before a job spends anything, check that it can possibly succeed. A
failing check gives the run `blocked_config` with a plain reason, makes no model call, and alerts
the job's destination only the first time that exact reason shows up (the next ticks with the same
reason stay quiet); the first healthy run clears the record so a later break alerts again.
`cron.preflight: false` turns the whole check off.

Checks, in order:
1. structure (also for script-only jobs): the script resolves inside `<home>/scripts/`; the
   workdir (when set) is an existing absolute directory.
2. provider key (agent jobs): the job's route (job pin, else `cron.model`/`cron.provider`, else the
   main model) resolves with a usable key. Skipped for an unpinned job when `fallback_providers`
   lists anything (the chain may rescue it). The message names the profile and the home folder
   whose keys were read.
3. skills (agent jobs): every attached skill resolves through the P09 catalog and its declared
   environment variables have values (scope env or secret store). The check runs through a port
   the service layer fills (cron may not import learning).
4. delivery: every target of `deliver` and `failure_deliver` is `local`, `origin` (resolves to
   local before P15) or a platform with a registered deliverer; anything else blocks with "delivery
   to <platform> needs the gateway" (P15 registers platforms).
5. MCP: each MCP server named in the job's `enabled_toolsets` resolved at least one tool; a server
   that is reconnecting (connected earlier) does not block. Port-driven; without the port the check
   passes (MCP P13 wave 2 lands its status API in parallel).

Each check fails open on its own internal error (logged): preflight blocks only on a clear verdict.

Inputs → outputs: `JobSpec`, scope, probes → `None` or a reason; `AlertBook` (file
`<home>/cron/preflight_alerts.json`, job id → last alerted reason) → alert yes/no.

Files: `src/indus/cron/preflight.py`, `tests/unit/cron/test_preflight.py`; probes filled in
`src/indus/service/cron_host.py`. Shared: `foundation/config/schema.py` (`cron.preflight`).

Edge cases / failure modes:
- reason text changes (a different missing key) → alerts again (distinct reason).
- alert book unreadable/corrupt → treated as empty (alert again rather than stay silent).
- blocked across 5 ticks → 1 alert; fixed config → cleared on the next run.

Our design differences / decisions:
- Alert-once is keyed by the reason text (spec: one alert per distinct reason), kept in our own
  small file instead of a job field, so C1's model needs no extra field.
- Structural checks run before the wake gate (a missing script cannot gate); key/skill/MCP checks
  run after it so a gated-off tick stays silent, as in the reference.

Tests we will write:
- missing key → blocked, fake model never called, one alert across 5 ticks; fixed → cleared.
- unpinned job with a fallback chain skips the key check; pinned job does not.
- reconnecting MCP server passes; never-connected one blocks.
- platform target before P15 → blocked with the gateway reason; `local`/`origin` never checked.
- script outside scripts dir / missing workdir → blocked; `cron.preflight: false` → no check.

## Decisions for the lead
- Delivery check uses a tiny local split of the target list (comma, `platform[:chat[:thread]]`)
  until C3's `targets.py` lands; replace `preflight.platforms_of` with C3's parser at integration.

Compare checklist (filled after the rewrite; reopened `cron/scheduler_preflight.py`,
`_preflight_or_block`, `_blocked_config_result`):
- [x] every check fails open (`tests/unit/cron/test_preflight.py::test_a_check_that_breaks_fails_open`)
- [x] key check skipped for an unpinned job with a chain, kept for a pinned one
  (`::test_unpinned_job_with_a_fallback_chain_skips_the_key_check`); message names profile and home
  (`::test_missing_key_names_the_profile_and_home`)
- [x] blocked → no model call, one alert, cleared by a healthy run
  (`::test_missing_key_blocks_without_a_model_call_and_alerts_once`)
- [x] reconnecting MCP server passes (`::test_reconnecting_mcp_server_passes_and_empty_one_blocks`)
- [x] delivery: local/origin never checked; platform targets before P15 blocked; failure lane too
  (`::test_platform_target_before_the_gateway_is_blocked`)
- [x] runs after the wake gate (structure before it) — `src/indus/cron/runner.py::_pipeline`
- Deliberate differences: alert-once keyed by reason text (a different reason alerts again) in our
  own file; `all` is blocked before P15 (the reference never checks it) because nothing can
  deliver to every platform here; no satellite-profile routing rescue (gateway, P15). The
  reference warns once per reconnecting MCP server; ours passes silently.
- Not taken: rate-limited auth errors as a pass (our resolver does not report quota there; C3's
  quota hold handles a closed usage window).

## Status

Done (2026-10-19). `src/indus/cron/preflight.py`; the skill probe (`skill_problem`) lives in
`src/indus/service/cron_host.py` (learning sits beside cron in the layers). The MCP probe is a
seam (`PreflightProbes.mcp`) left unset until P13 wave 2's server status API is merged — the lead
wires it in `cron_host.run_env`.
