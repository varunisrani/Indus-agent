# P09 wave 2 — slice reports (base a9c60dd = main fb1204d + wave 1 patches raw)

## Slice D — w14-p09d.patch — DONE (P09-11 skill_manage, P09-13 ledger, P09-24 CLI/slash)
- New: learning/skills/{ledger,manage_tool}.py; cli/commands/{skills,memory}.py; cli/slash/learning_cmds.py.
- Shared: cli/app.py (skills, memory rows), cli/slash/dispatch.py (HANDLERS += learning_cmds),
  service/commands/registry.py (skills/memory subcommands; reload-skills, bundles), locales/en.yaml,
  test_registry.py (`landed_names` for P09 — swap for "P09" in `landed` once E/F land), test_dispatch.py,
  acceptance-map ACC-F16-1.
- Wiring: manage_tool.register_tools(registry) next to view_tools; `skills` toolset = skill_manage,
  skills_list, skill_view; E appends to manage_tool.SUCCESS_LISTENERS (SkillWriteEvent) for the review
  counter reset; F sets manage_tool.ARCHIVER (scope, folder, name) -> Path; ledger API
  SkillLedger.for_scope/bind_actor/capture/record/entries/rollback; REPL sets
  ctx.extensions.skill_commands = learning_cmds.build_skill_commands(scope, session_id).
- Decisions (D-ids at landing): non-TTY confirm exits 2; skills pending/approve/reject; batch on an
  in-memory overlay; exact-match patch; ledger default actor agent, CLI user; trim to 3/4 cap;
  memory edit via temp copy + unchanged check; /reload-skills own registry entry.
- Follow-up: curator-fork delete should require `absorbed_into` (F/P09-19).
- Base-only failure: test_wizard::test_back_from_terminal_returns_to_model (wave 1; integrator told).

## Slice F — w14-p09f.patch — DONE (P09-18..20 curator, P09-23 journey)
- New: learning/curator/{__init__,settings,state,archive,ledger_port,lifecycle,report,consolidate,backup}.py,
  learning/journey.py, service/maintenance.py, cli/commands/{curator,journey}.py,
  cli/slash/learning_cmds.py (ADD/ADD CLASH with D's learning_cmds.py — merge both HANDLERS into one
  module), tests/fakes/skill_refs.py, tests/unit/learning/curator/_stubs_f.py (STAND-IN, delete).
- Shared: config schema `curator` section + `auxiliary.curator`; providers/auxiliary.py AuxClient.call
  tools=/tool_choice=; cli/app.py rows curator, journey, learning (alias row); registry /curator;
  tests/contract/cli/test_help_catalogue.py (adds missing `bundles`), non_tty_table.yaml (new verbs);
  traps R07.6–R07.8, ACC-F21-1.
- Known red: test_non_tty::test_table_names_every_command_and_nothing_else — `bundles` rows missing
  from non_tty_table.yaml (B's gap) — add them.
- Wiring: CLI session start `lifecycle.maybe_run(scope, idle_s=…, gateway_running=…)` in a thread;
  service MaintenanceTimer(clock) + lifecycle.curator_check(... step_factory=AuxStep(service.aux)),
  timer.run() task; ledger via ledger_port.set_ledger(D's SkillLedger); D's manage_tool.ARCHIVER =
  curator.archive.archive_skill; slash HANDLERS once every P09 command has a handler (then "P09" in
  `landed`, HELP_GROUPS update); P14 registers cron store via register_reference_source.
- Decisions: failing reference source blocks archiving; prune_builtins ages bundled skills from first
  sight; consolidation is an aux tool loop (not a forked Agent); classification via absorbed_into
  (covers D's follow-up); snapshot exclusions; purge TTL 0 exits 0; pin refused for unmanaged.

## Slice E — w14-p09e.patch — DONE (P09-14..17 review counters, review fork, prompts/summary, /learn /refine)
- New: learning/review/{__init__,counters,fork,idle_queue,prompts,summary}.py, learning/learn.py,
  cli/slash/learning_cmds.py (THIRD add/add with D and F — merge all three HANDLERS into one module),
  tests incl. tests/integration/learning/test_review_fork_e2e.py, golden review_prompts.txt;
  tests/unit/learning/review/_stubs_e.py (STAND-IN for D's skill_manage — delete).
- Shared: core/turn_state.py (`SessionRuntime.after_turn` hooks), core/finalize.py (runs them after
  persist/withdraw), providers/auxiliary.py (`tools=` — F also adds tools=/tool_choice=; keep one),
  protocol/events.py `LearningReviewDone` + regenerated packages/protocol generated files + sample;
  config: memory.review_interval, skills.review_interval, learning.review.*, auxiliary.review,
  display.learning_notices; traps R07.5, ACC-F5-1/F6-1/F7-1.
- Wiring: per session `runner = install_review(agent, no_review=...)` (needs AgentOptions.aux); before
  each live turn `await runner.cancel_for_live_turn()`; HostPort.refine(session_id, focus) ->
  `(await learn.refine(runner, focus=focus)).message`; hosts use `learn_turn(agent, text)`; dispatch
  HANDLERS += learning_cmds (then "P09" in `landed`, HELP_GROUPS learn→tools, refine→session,
  /re ambiguity list); D's manage_tool.SUCCESS_LISTENERS gets the counter reset.
- Decisions: budget stops before crossing; digest tail starts on an assistant row; skill counter counts
  tool-calling iterations; own memory call resets counter; idle = no live turn for 60 s; route warnings
  are log lines.

## Applying
The slice patches are against a local base a9c60dd = main fb1204d + the three wave 1 patches applied
raw (saved here as `raw-base-a9c60dd.patch`, against fb1204d). Wave 1 has since landed in integrated
form (3d55449), so apply each slice with `git apply --3way` on main and resolve against the landed
wave 1 (one write-origin var in learning/pending.py; learning/host.py does the chat wiring; skills
toolset exists; HELP_SKILL_NAME = indus-guide). If --3way lacks the base blobs, rebuild a9c60dd by
applying raw-base-a9c60dd.patch on fb1204d first.
