# Task: `cronjob_manage` tool (P14-18) Phase: 14 Owner: wave-17 builder C3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/cronjob_tools.py` (actions,
schema fields, availability check, forwarded-argument list, run handle and background completion),
`tools/cronjob_job_args.py` (deliver normalisation, origin for cron-created jobs, base_url origin
rule, script path rule, continuity, context_from check), `tools/cronjob_prompt_scan.py` (which shapes
it refuses), `cron/lifecycle_guard.py` (header and the systemctl/launchctl branches only). Research
§3.15.8. Closed before writing.

What it does (own words): one model tool, `cronjob_manage`, with `action ∈ {create, list, update,
pause, resume, remove, run}`. The model can schedule a job (prompt and/or skills, or a script-only
job), look at jobs, change, pause, resume or delete one, or fire one now. The model may not choose
which model, provider, endpoint or reasoning effort a job uses (those are the user's spend
decisions): such fields are refused with a pointer to `indus cron edit`. `pinned=true` copies the
current main model and provider onto the job and is meant only for when the user asked. `run`
returns a handle at once; the result comes back into the conversation as an internal message when
the run ends; a job already running is refused; a surface that cannot take detached results gets a
synchronous run instead. Prompts (create, update, run context) are scanned with the P05 threat scan
(injection, exfiltration, invisible Unicode) and refused on a hit; a job whose prompt or script
would stop or restart the Indus gateway is refused (it would kill the process that runs it).

Availability: interactive CLI/TUI and gateway sessions; a cron session only when
`cron.allow_agent_scheduling` is true (R07.15); one-shot (`-z`) and other unattended runs: refused.
The tool reads the run context the host offers (`run_context` extra: `interactive`, `cron`,
`single_query`, `unattended`; none = interactive). It is in the `cronjob` toolset, in the core
list, and in `toolsets.DEFERRED_BY_DEFAULT` for the future tool-search bridge.

Inputs → outputs: tool arguments → JSON text `{"success": bool, …}` (job view, list, handle,
`error`). Store work goes through the `ports.CronBackend` (C1 store + C2 ticker).

Files: `src/indus/cron/tool.py`, `tests/unit/cron/test_tool.py`. Shared: `src/indus/tools/toolsets.py`
(`cronjob` toolset, `cronjob_manage` in `CORE_TOOLS` and `DEFERRED_BY_DEFAULT`),
`src/indus/cli/runtime.py` (register the tool in the CLI registry), config row
`cron.allow_agent_scheduling`.

Edge cases / failure modes:
- `update` with `skills=[]` clears the list; `failure_deliver=""`, `workdir=""`, `monitor=""`
  clear; `enabled_toolsets=[]` is kept as an explicit empty allow-list;
- `continuity: true/false` adds/removes the reserved `self` in `context_from`;
- `context_from` naming a job that does not exist → refused;
- `script` must be a plain file name under `<home>/scripts/` (no absolute, `~`, traversal, spaces
  that make it look like a command line; the file must exist);
- `monitor` is one field: an http(s) URL → `monitor_url`, else a script name → `monitor_script`;
  refused with `no_agent`;
- `deliver` from inside a cron run: `origin` is pinned to the parent job's concrete target or
  `local` (P14-13); deliver values are parsed and an unknown platform is refused at create;
- job references: id or case-insensitive name; ambiguous → refused with the candidate ids;
- `paused_reason` without `paused` → refused.

Our design differences / decisions:
- schema description and per-field texts written fresh; the script hint names the active profile's
  scripts folder (a dynamic schema override);
- the restart-loop guard is a short pattern list of our own (`indus gateway restart|stop`,
  `systemctl … indus-gateway`, `launchctl … ai.indus.gateway`), applied to the prompt and the
  script file's text;
- `base_url` (user CLI only) checked by `base_url_problem(scope, provider, url)`: needs an explicit
  provider whose configured endpoint has the same scheme, host and port (R01.11).

Tests we will write: agent-supplied `model`/`provider`/`base_url`/`reasoning_effort` refused;
`skills=[]` clears; `run` returns a handle and the result arrives as an internal message; double
`run` refused; synchronous run without a result sink; refused in a cron session by default and
allowed with the opt-in; refused for `single_query`; injection prompt refused; `systemctl restart
indus-gateway` refused; base_url rule table; origin pinning from a cron session; continuity.

## Decisions for the lead
- Detached results: `tool.result_sinks` is a per-session registry (`register_result_sink(session_id,
  fn)` → undo). The service host should register `lambda text: service.deliver_internal(attached,
  text)` next to `subagents.set_delivery` in `SessionService._attach` (one line), and unregister at
  close. Without a sink the run is synchronous.
- Threat-scan scope: uses the `strict` set until C2's `cron` scope lands in `threat_scan.py`
  (then switch `_SCAN_SCOPE` to `"cron"`).

Compare checklist (reopened `tools/cronjob_tools.py`, `tools/cronjob_job_args.py`,
`tools/cronjob_prompt_scan.py`, `cron/lifecycle_guard.py` header):
- [x] seven actions, `job_id` by id or name, ambiguous refused with ids (`tests/unit/cron/test_tool.py::test_refs_errors_and_lifecycle_actions`)
- [x] model/provider/base_url/reasoning not agent-settable (`::test_agent_supplied_inference_pins_are_refused`); `pinned` copies the main route (`::test_pinned_copies_the_main_model_and_unpin_clears`)
- [x] `skills=[]` clears, `''` clears workdir/failure_deliver, `enabled_toolsets=[]` kept, continuity adds/removes `self`, `context_from` checked (`::test_update_skills_empty_list_clears`, `::test_clearing_fields_and_continuity`)
- [x] `run` handle + detached result, already-running refused, inline without a sink (`::test_run_returns_a_handle_and_the_result_arrives_later`, `::test_run_without_a_result_sink_is_synchronous`)
- [x] availability: cron sessions need the opt-in, one-shot refused (`::test_cron_session_needs_the_opt_in`)
- [x] prompt scan on create/update/run context; restart-loop guard on prompt and script text (`::test_dangerous_prompts_are_refused`, `::test_script_rules`)
- [x] script must be a relative file in the scripts folder; command lines refused (`::test_script_rules`)
- [x] base_url same-origin rule needing an explicit provider (`::test_base_url_rule`)
- [x] origin pinned for jobs created inside a cron run (`::test_cron_created_job_origin_is_pinned_to_the_parent`)
- Deliberate differences: refused (not silently dropped) inference fields; a narrower restart
  guard of our own (no profile-flag sibling exception: any `indus … gateway restart|stop` in a job
  is refused); the threat scan is the P05 `strict` set (switch to C2's `cron` scope when it
  lands). No "the ticker is not running" note on create/list (status shows it).
- Not taken: `skill` singular, `attach_to_session`, bot-chat validation, relay/primary hand-offs.

## Status

Done (2026-10-19). `src/indus/cron/tool.py`, `tests/unit/cron/test_tool.py`. Shared:
`tools/toolsets.py` (`cronjob` set, `cronjob_manage` in `CORE_TOOLS` and `DEFERRED_BY_DEFAULT`;
`indus-cron` still includes it — C2's bundle change removes it unless allowed),
`tests/unit/tools/test_toolsets.py`, `tests/unit/cli/setup/test_wizard.py` (bundle row
`cronjob`), `cli/runtime.py` (`_tool_registry` registers the tool), `service/session_service.py`
(registers the session's result sink, undone at close), config row `cron.allow_agent_scheduling`.

## Integration (lead, P14 landing)
Landed with C1/C2/C3 together: one backend `service.cron_host.CronHost` behind `ports.BACKEND_FACTORY` (C2's `set_ports_factory`/`ports_for` were folded into it), stand-ins deleted, tests on the real store (`tests/unit/cron/world.py`). Details and open seams: `docs/plan/phases/LEAD-NOTES.md`, "P14 landing notes (lead)"; differences D-16.220 … D-16.237.
