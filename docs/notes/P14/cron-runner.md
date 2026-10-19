# Task: agent job runner (P14-11) Phase: 14 Owner: wave builder C2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/scheduler.py` (`run_job`,
the toolset and reasoning resolution, the pinned-route rule, the inactivity watchdog, the per-run
scope, `_final_response_from_result`, teardown) — worker/systemd plumbing skipped;
`website/docs/developer-guide/cron-internals.md` (skimmed). Closed before writing. Evidence: none.

What it does (own words): runs one agent job as a fresh, hidden session
(`source=cron`, id `cron_<job>_<YYYYmmdd_HHMMSS>`) in this process:
- tools: job `enabled_toolsets` (an explicit empty list means no tools) > `cron.toolsets` > the
  `indus-cron` bundle. A `cron.toolsets` value that is not a list of names fails the run instead of
  granting every tool. `clarify` (and any messaging tool) is always removed; `cronjob_manage` is
  removed unless `cron.allow_agent_scheduling` (default false, R07.15).
- approvals: the run context is `cron`, so risky commands follow `approvals.cron_mode` (deny).
- memory stores load as usual; the background review is never attached (`no_review`, R07.5).
- workdir (absolute, existing): the session's cwd (terminal and file tools start there; each run is
  its own session, so parallel runs never share a cwd) and project context files are read from it;
  without a workdir the prompt has the persona but no project files. Project-skill trust binding is
  a one-line hook (`_bind_project_trust`) until P13's `project_trust.bind_project_cwd` lands.
- model: job pin > `cron.model`/`cron.provider` > the main model at fire time; job reasoning effort
  beats the global one. The credential pool rotates for every job; the fallback chain only helps an
  unpinned job (a pinned job fails rather than switch model).
- inactivity: `INDUS_CRON_INACTIVITY_TIMEOUT` → `cron.inactivity_timeout_seconds` → 600 s with no
  progress stops the run (failed); every agent event is a progress stamp and also refreshes the
  ledger attempt's heartbeat (throttled to once a minute).
- self-removal: when the run removed its own job, the answer is still delivered and recorded
  `completed`, then the job's output folder is deleted.

The whole pipeline (`run_job`): structural preflight → monitor gate → script / wake gate →
full preflight → prompt assembly and threat scan → agent run → classification (P14-12) → output
document → `finish` port (delivery, bookkeeping, incidents: C3).

Inputs → outputs: `JobSpec`, run kind, scheduled instant, ports, agent runner → `RunOutcome`.

Files: `src/indus/cron/runner.py` (pipeline + decisions), `src/indus/service/cron_host.py`
(the real agent runner over `core.agent.Agent` + learning wiring), `tests/unit/cron/test_runner.py`,
`tests/fixtures/model_scripts/cron_*.yaml`. Shared: `src/indus/tools/toolsets.py` (`indus-cron`
bundle without `clarify`), `foundation/config/schema.py` (`cron.toolsets`, `cron.model`,
`cron.provider`, `cron.allow_agent_scheduling`, `cron.inactivity_timeout_seconds`), the regenerated
`tests/golden/prompt_surface/cron/tools.json`.

Edge cases / failure modes:
- route cannot be resolved → failed run, "model unreachable" only when no call was made and the
  error is a network one (C3's retry ladder reads `unreachable`).
- agent raises → failed with a redacted chained traceback in the document.
- teardown hangs → abandoned after `cron.cleanup_timeout_seconds` (10 s).
- run cancelled (claim lost, daemon stop) → the turn is interrupted and the agent closed (its
  terminal children go with the environment cleanup).

Our design differences / decisions:
- The agent is built directly over `core.agent.Agent` in a service-level host
  (`service/cron_host.py`) rather than through `SessionService.create`, because a cron session
  needs its own id shape and no surface ever attaches to it; the session lock is still taken.
- Inactivity reuses the core liveness watchdog by laying `agent.turn_liveness_s` over the run's
  config view (one watchdog, not two).
- Denied tools are removed by name after toolset selection (`AgentOptions.only_tools`), so no
  composite toolset can bring them back.

Tests we will write:
- fake model: tool array lacks `cronjob_manage` and `clarify`; with `allow_agent_scheduling` the
  scheduling tool may appear (when registered).
- dangerous command denied under `cron_mode: deny`.
- pinned route failure does not fall back; unpinned does.
- workdir context file in the prompt; none without workdir.
- inactivity 600 s with no progress → failed (fake clock).
- review never attached; self-removing job delivers and records completed.

## Decisions for the lead
- `CronAgentRunner(register_tools=...)` is the seam for C3's `cronjob_manage` registration
  (registered always; denied by name unless `cron.allow_agent_scheduling`).
- Without a workdir the run has no project files and the terminal starts where `terminal.cwd`
  says; with one, `terminal.cwd` is laid over that run's config (its own session environment).
- `messaging` tool names to deny: none exist before P15; `DENIED_ALWAYS` lists `clarify` and will
  take P15's send tool name.
- A pre-run script failure fails the run without a model call (see cron-script).

Compare checklist (filled after the rewrite; reopened `cron/scheduler.py` `run_job`, toolset
and reasoning resolution, `_job_route_pinned`, watchdog, teardown):
- [x] fresh hidden session `source=cron`, id `cron_<job>_<ts>`
  (`tests/unit/cron/test_runner.py::test_session_id_shape`, `::test_the_session_is_hidden_and_marked_cron`)
- [x] toolsets: job > `cron.toolsets` > bundle, explicit empty list = none, unreadable config fails
  closed (`::test_toolset_precedence`, `::test_unreadable_toolset_config_fails_the_run`)
- [x] `clarify` always out, `cronjob_manage` out unless allowed (`::test_denied_tools`,
  `::test_tool_array_lacks_scheduling_and_clarify`)
- [x] approvals deny under `cron_mode: deny` (`::test_dangerous_command_is_denied_under_cron_mode_deny`)
- [x] pinned route never falls back; unpinned does (`::test_pinned_route_does_not_fall_back`)
- [x] project files only from the workdir; persona always (`::test_workdir_context_file_is_loaded`)
- [x] inactivity 600 s → stopped and failed; progress stamps (`::test_no_progress_for_600_s_stops_the_run`,
  `::test_incomplete_turn_is_a_failure`)
- [x] review never runs (`::test_review_is_never_attached`)
- [x] self-removing job delivers and completes, output dropped after
  (`::test_self_removing_job_delivers_and_completes`)
- [x] teardown bounded by `cron.cleanup_timeout_seconds` (`service/cron_host.py::CronAgentRunner._end`)
- Deliberate differences: no per-job MCP-server merge into an explicit toolset list (MCP P13 wave 2
  owns that mapping; follow-up); `agent.disabled_toolsets` is not layered on top (no such key in
  our schema); max-iteration summaries count as a completed reply, like the reference.
- Not taken: detached/restart-safe workers (H14 → P15), prefill messages, OpenRouter routing keys.

## Status

Done (2026-10-19). Pipeline and decisions in `src/indus/cron/runner.py` (+ `jobroute.py`,
`settings.py`); the agent run in `src/indus/service/cron_host.py::CronAgentRunner`. Shared:
`tools/toolsets.py` (`indus-cron` without `clarify`), regenerated `cron/tools.json` golden (note in
`docs/notes/P04/golden-prompts.md`), `tests/unit/tools/test_toolsets.py`,
`tests/contract/tools/test_reachability.py`.
