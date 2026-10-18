# Task: Control and info commands (P06-10) Phase: 06 Owner: P06 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`):
`hermes_cli/cli_commands_mixin.py` (approvals, reasoning, toggles, scope flags),
`hermes_cli/approval_mode.py` (whole), `hermes_cli/cli_model_switch_mixin.py` (skim: global
switch, base URL handling). Research `04-cli-config.md` §3.17.2, §3.17.3, §3.21; trap R04.9.
Closed before writing.
Evidence: none.

What it does (own words): the commands that change how the current session behaves or report on
it. `/model` switches among providers that are already configured, through the session's
`RouteBinding` (all or nothing), and with `--global` also saves the choice; leaving a custom
endpoint clears its stale base URL and wire mode so the new key never goes to the old host (R04.9).
`/reasoning` sets the effort on the ladder or shows/hides reasoning text. `/yolo` toggles
auto-approve for this session and stores it on the session row so a resume keeps it. `/approvals`
shows or saves the profile's approval mode through the config writer and reads it back. `/tools`
lists tools or changes the session's toolsets (a new session, because the prompt depends on the
tools). The info commands print what the session is, what it costs (n/a until P08) and what it can
do. `/stop`, `/steer`, `/queue`, `/compress` and `/quit` control the running work.

Inputs → outputs:
- `/model` → current route; `/model NAME`, `/model PROVIDER:NAME`, `--provider P`, `--global`.
  Busy → rejected. Failure (unknown provider, no key, refused window, dependent failed) → error
  line; the binding stays on the old route.
- `/reasoning` → level + display; `LEVEL` on `none…ultra` (session; `--global` also
  `agent.reasoning_effort`); `show`/`hide` (REPL state).
- `/yolo` → toggle; `ApprovalState.set_auto_approve(..., persist=set_yolo)`; pending write when the
  row does not exist yet.
- `/approvals [manual|smart|off]` → show; set through `writer.save`, then re-read with the loader;
  managed or unreadable config → the error text, never an exception out of the REPL.
- `/tools list|enable|disable NAME…` → list, or a new session with the changed toolset selection.
- `/config` (redacted effective config), `/status`, `/context`, `/usage`, `/whoami`, `/help [filter]`,
  `/version`, `/stop` (kill this session's background jobs + interrupt), `/steer TEXT`,
  `/queue TEXT|list|clear|rm N`, `/compress` ("arrives with P08"), `/quit` (alias `/exit`).

Files: `src/indus/cli/slash/control_cmds.py`, `tests/unit/cli/slash/test_control_cmds.py`.

Edge cases / failure modes:
- `/model` with only `--provider` keeps the current model name.
- A switch that fails half way (a route dependent raises) is walked back by the binding; the test
  checks the state after a failing fake binding.
- `/approvals off` on a managed install → "managed" message; YAML `off` read back as False is
  normalised by `approval_mode`.
- `/queue` while idle with text → submitted now (nothing to wait for); while busy → queued and run by
  `after_turn`.
- `/steer` while idle → "nothing is running" (the reference falls back to a queue; we say so).

Our design differences / decisions:
- `/model --global` writes `model.default` + `model.provider`, and `base_url`/`api_mode` = "" when
  the old saved provider was a custom endpoint and the new one differs.
- `/reasoning` session change goes through `binding.apply(route, requested_effort=…)` (same
  all-or-nothing path as `/model`).
- `/usage` prints the session's token counters with cost `n/a` until P08.
- Arrives with P17: the reference refuses some commands because its classic chat is a gateway
  client; here they run in-process.

Tests we will write:
- `/model` while busy rejected; failing fake route rolls back; `--global` persists and clears a
  stale custom base URL; `/approvals off` persists and is read back; managed install message;
  `/yolo` stored on the row and restored for a resumed session; `/queue` runs after the turn;
  `/help` grouping; `/tools disable` resets with a narrower selection; `/stop` kills jobs.

## Decisions for the lead
- `/steer` with no running turn prints a hint instead of queueing (simplest; easy to change).
- `/tools enable|disable` take toolset names (`terminal`, `file`, `web`, …); a session selection
  of "everything" is first expanded to the named sets the session actually has.

Compare checklist (filled after the rewrite; reopened `hermes_cli/approval_mode.py`,
`hermes_cli/cli_commands_mixin.py`):
- [x] approvals: no arg shows, bad arg usage, managed/unreadable errors captured, read back
  (tests: `test_control_cmds.py::test_approvals_*`)
- [x] reasoning `--global` persists, else session only (test: `::test_reasoning_level_session_and_global`)
- [x] `/stop` interrupts first, then stops background processes (test: `::test_stop_kills_jobs_and_interrupts`)
- [x] `/queue` lists with no argument; `add` forces text that starts with a management word
  (test: `::test_queue_management`)
- Deliberate differences: `/model` has no picker or `--refresh` (P07 `indus model`); no `full`/`clamp`
  reasoning display modes; `/reasoning show|hide` is per REPL session (the reference saves the
  display choice to config); no `/queue edit|move` (P12).
- Not taken: `/fast` (P08), `/busy`, `/footer`, `/timestamps`, `/statusbar` (TUI/P12 or later).

## Status

Done (2026-10-18).

## Integration (lead)

- `/model` also stores `model_override = {provider, model}` on the session row
  (`session_cmds.remember_config`, key shared with `indus.cli.resume.MODEL_OVERRIDE_KEY`) so
  `indus -c` and `/resume` start the session on it (lead decision).
- `/config`'s YAML dump moved into `_shown_settings` so the P01 config-writer ratchet (which flags
  a YAML dumper inside code that names the config path) does not read a display as a writer.
