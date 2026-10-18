# Task: Resume behaviour (P06-13) Phase: 06 Owner: P06 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/cli_session_mixin.py`
(`_restore_session_cwd`, `_restore_session_yolo`, `_write_terminal_breadcrumb`),
`hermes_cli/terminal_breadcrumbs.py` (terminal identity, staleness), `hermes_cli/main.py`
(cwd restore in `_resolve_chat_session_args`), research `04` §3.4.3. Closed before writing.
Evidence: none.

What it does (own words): when a session is resumed the CLI moves into the directory the session
recorded (unless `--no-restore-cwd` or `--in`; a missing dir prints a warning and stays), puts the
session's own switches back (auto-approve from the stored `yolo` flag, reasoning and toolsets from
the launch record) and gives the REPL the stored history for the recap (slice B renders it). On
every new or resumed session the terminal's breadcrumb is written: a small JSON file under
`<home>/terminal-sessions/<terminal id>` naming the session; the terminal id is
`INDUS_TERMINAL_ID` when set, else the controlling TTY device, else a known multiplexer variable;
none → no breadcrumb. Breadcrumbs older than 30 days are ignored and pruned.

`resume_session(scope, session_id, *, store=None, host=None, restore=True, breadcrumb=True)`
does all of it in one call (asked for by slice C so the REPL's `/resume` and `/branch` reuse it):
returns `Resumed{row, policy, reasoning, toolsets, auto_approve, cwd, history, notices}`; with a
`host` it loads the stored transcript through the host for the recap. Reasoning comes from
`model_config["reasoning_effort"]` (what `/reasoning` writes), else the launch record.

Inputs → outputs: session row → cwd change, approval switch, launch overrides, breadcrumb file.

Files: `src/indus/cli/resume.py`, `tests/unit/cli/test_resume.py`.

Edge cases / failure modes:
- Breadcrumb unreadable or naming a deleted session → ignored (fallback to workspace).
- `session.terminal_continue: false` → neither written nor read.
- Recorded cwd equals current → nothing printed.

Our design differences / decisions:
- `INDUS_TERMINAL_ID` beats the TTY (the plan's wording), so tests and wrappers can pin it.
- Breadcrumb writes go through `fsio` (atomic, owner-only).

Tests we will write: cwd restored / not restored / missing dir; breadcrumb per terminal id;
switches rehydrated (yolo, reasoning, toolsets).

## Decisions for the lead
- `/model` (slice C) does not write the session row; a resumed session uses the model in its
  launch record (`model_config["launch"]["model"]`, the resolved route at creation). To make a
  `/model` switch survive resume, `/model` should also patch that record (or a `model_config`
  key this function then prefers, like `reasoning_effort`).

Compare checklist (filled after the rewrite; reopened `cli_session_mixin.py`,
`terminal_breadcrumbs.py`):
- [x] cwd restore rules (tests: `test_resume.py::test_cwd_*`)
- [x] yolo re-armed from the row (test: `test_switches_rehydrated`)
- [x] breadcrumb identity and staleness (tests: `test_breadcrumb_*`)
- [x] one call for every resume path (tests: `test_resume_session_*`)
- Deliberate differences: the reference also re-points a `TERMINAL_CWD` variable; our terminal
  backend reads the session cwd from the agent options, so we pass the restored cwd there instead.

## Status

Done (2026-10-18).

## Integration (lead)

- `/model` across resume (lead decision): `/model` writes `model_config["model_override"] =
  {provider, model}` on the session row (or once the row exists); `resume_session` and the chat's
  frozen-flag check read it before `model_config["launch"]["model"]` (`resume.apply_switches`,
  `MODEL_OVERRIDE_KEY`). Test: `tests/unit/cli/slash/test_control_cmds.py::test_model_switch_survives_resume`.
- `resume_session` is also the REPL's `on_resume` (via `indus.cli.launch.make_on_resume`).
