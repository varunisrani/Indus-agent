# Task: `indus cron` CLI, `/cron`, status and doctor (P14-19) Phase: 14 Owner: wave-17 builder C3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/cron.py` (verbs, list
rows, overdue label, status layout, doctor checks and exit code, create/edit flag handling,
notepad verb), `cron/scheduler_diagnostics.py` (skimmed). Research §3.15.9. Closed before writing.

What it does (own words): the person's side of cron. `indus cron create <schedule> <prompt>` with
`--name`, repeated `--skill`, `--deliver`, `--script`, `--no-agent`, `--workdir`, `--toolsets`,
`--model/--provider` (and `--base-url`, `--reasoning`), `--paused [--reason]`; `edit <job>` with
`--skill` (replaces the list), `--add-skill`, `--remove-skill`, `--clear-skills`, `--workdir ""`
(clears) and the other create fields; `list` (with `--all`) showing schedule, state, next run (or
"overdue" when it is more than 15 min in the past on an active job), last status, failure streak
and delivery warnings; `pause`, `resume`, `run <job> [--context …]`, `remove`; `status` (ticker
alive from the heartbeat marker's age, last success, the soonest next run ordered by instant,
store health, global pause); `tick` and `daemon` (C2's ticker); `pause-all [--reason]` /
`resume-all`; `runs <job>` / `history`; `incidents [--all] [--ack <id>]`; `notepad <job>
get|set|unset|list`; `doctor` (read-only, exit 1 while any finding stands). Slash `/cron
add|list|edit|pause|resume|run|remove` on the classic CLI (TUI uses the same handler through its
slash worker; the gateway in P15).

Doctor findings per active job: last run failed; last delivery failed or unverified; last dispatch
late or catch-up (when C1/C2 record it as `last_dispatch`); next run missing or more than 15 min
overdue; script missing or resolving outside `<home>/scripts/`; script-only job without a script;
workdir gone. Plus a store-health finding when the store is not writable.

Inputs → outputs: argv → text and exit codes (0 done/informational, 1 refused or findings, 2
usage). `doctor.findings_for(job, now=…, home=…)`, `doctor.run(backend, scope)` → `DoctorReport`.

Files: `src/indus/cli/commands/cron.py`, `src/indus/cron/doctor.py`, `src/indus/cli/slash/cron_cmds.py`
(new), `tests/unit/cli/commands/test_cron.py`, `tests/unit/cron/test_doctor.py`. Shared:
`src/indus/cli/slash/dispatch.py` (handler table), `tests/unit/service/commands/test_registry.py`
(`cron` in the landed commands).

Edge cases / failure modes: non-TTY never prompts (`remove` asks only on a terminal, `-y` skips;
without a terminal and without `-y` it refuses with exit 1, per P07); unknown job → exit 1 naming
`indus cron list`; ambiguous name → exit 1 listing the ids; store not wired / unwritable → exit 1
with the reason; `/cron add` needs the schedule quoted unless it is one word (`30m`); markers
missing → "ticker: never ran".

Our design differences / decisions: our wording and layout; `status` reads the marker files'
modification times (no format coupling with C2); the global pause is read through the backend.

Tests we will write: CliRunner per verb on a stand-in backend; `--skill a --skill b` replaces,
`--add-skill` appends, `--remove-skill`, `--clear-skills`; `--workdir ""` clears; overdue label at
16 min, not at 14; `doctor` exits 1 with a vanished workdir and 0 when healthy; `status` reads the
markers; `/cron add "every 30m" check the build` creates a recurring job with `deliver=local`;
incidents list and `--ack`.

## Decisions for the lead
- `cron` is **not yet registered in `cli/app.py`** in this slice: its verbs need the C1/C2 backend,
  and the P07-26 non-TTY contract runs each verb in a fresh profile. At integration add
  `("cron", "indus.cli.commands.cron:cron_group", "Schedule jobs and look after them.")` and the
  rows listed in this note's Status section.

Compare checklist (reopened `hermes_cli/cron.py` list/status/doctor/create/edit/notepad):
- [x] create/edit flags incl. replace/add/remove/clear skills and `--workdir ""` (`tests/unit/cli/commands/test_cron.py::test_edit_skills_replace_add_remove_clear`)
- [x] overdue only past 15 min on active jobs, shared by list/status/doctor (`::test_list_overdue_label_at_sixteen_minutes`, `tests/unit/cron/test_doctor.py::test_overdue_needs_more_than_fifteen_minutes`)
- [x] doctor findings and exit 1 / 0 (`test_doctor.py::test_findings`, `test_cron.py::test_doctor_exit_codes`)
- [x] status: ticker heartbeat age, last success, soonest by instant, store health, global pause (`::test_status_reads_the_markers`)
- [x] runs/history, incidents with ack, notepad get/set/unset/list (`::test_runs_and_history`, `::test_incidents_list_and_ack`, `::test_notepad`)
- [x] `/cron add "every 30m" check the build` → recurring, `deliver=local` (`::test_slash_add_every_30m_creates_a_recurring_local_job`)
- Deliberate differences: bare `indus cron` prints the group help (P07 group rule) instead of
  listing; notepad verb `unset` (row) instead of `delete`; missing notepad arguments are usage
  errors (exit 2). `remove` without a terminal needs `-y`.
- Not taken: `resume --at/--run-now` re-arm of finished one-shots, hosted-provider rows,
  gateway-process probing in `status` (P15-21).

## Status

Done (2026-10-19) except the `cli/app.py` registration (decision above). Files:
`src/indus/cli/commands/cron.py`, `src/indus/cron/doctor.py`, `src/indus/cli/slash/cron_cmds.py`,
`tests/unit/cli/commands/test_cron.py`, `tests/unit/cron/test_doctor.py`. Shared:
`cli/slash/dispatch.py` (`cron_cmds.HANDLERS`), `tests/unit/service/commands/test_registry.py`
(`cron` landed; help-group golden gains `cron`).

Rows to add to `tests/contract/cli/non_tty_table.yaml` once `cron` is in `REGISTRY` and the backend
is wired (fresh profile): `cron: 2`, `cron create: 2`, `cron edit: 2`, `cron list: 0`,
`cron pause: 2`, `cron resume: 2`, `cron run: 2`, `cron remove: 2`, `cron status: 0`,
`cron tick: 0`, `cron daemon: ?` (C2: it loops; the 5 s watchdog needs a decision),
`cron pause-all: 0`, `cron resume-all: 0`, `cron runs: 2`, `cron history: 0`,
`cron incidents: 0`, `cron notepad: 2`, `cron doctor: 0`; plus `cron` in the help-order golden
(`tests/contract/cli/test_help_catalogue.py`) after the P13 entries.
