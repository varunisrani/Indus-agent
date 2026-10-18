# Task: `chat` command and session selection (P06-03) Phase: 06 Owner: P06 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/main.py` (`cmd_chat`,
`_resolve_continue_arg`, `_resolve_chat_session_args`, `_apply_in_dir`, `_resolve_last_session`,
`_resolve_session_by_name_or_id`, `_resolve_workspace_key`), `hermes_cli/terminal_breadcrumbs.py`
(breadcrumb shape), research `04` §3.4.1–3.4.6, traps R04.1, R04.19. Closed before writing.
Evidence: none.

What it does (own words): `indus chat` (and bare `indus`) decides the interface, picks or creates
the session, freezes the launch policy into a new session, builds the in-process host and hands
over to one of three paths: the classic REPL (slice B's `indus.cli.repl.app.run_repl`), a finite
query (`-q` answered and exit), or a seeded REPL (`-q` on a TTY submitted literally as the first
turn). Interface: `--cli` → classic; `--tui` → "arrives in P12" and exit 2; no TTY on stdin or
stdout → classic; `INDUS_TUI=1` or `display.interface: tui` → a one-line note and classic until
P12. Selection: `--in DIR` (expand `~`, must exist else exit 1, chdir, implies
`--no-restore-cwd`); `-r/--resume VALUE` and `-c NAME` resolve by exact id, unique id prefix, then
title (newest `NAME #n` first) and follow a compression chain to its tip; a miss prints "No
session found matching 'NAME'." plus a hint on stderr and exits 1; bare `-c` and `--resume latest`
use this terminal's breadcrumb (when `session.terminal_continue` is on and the session still
exists), else the newest CLI session of this workspace (git root, else cwd), else exit 1. The word
`latest` always means the newest session, even if a session is titled "latest".

Frozen creation flags (R04.1 kept in-process): cwd, model, provider, reasoning, toolsets, max
turns, skills, yolo, pass-session-id, ignore-rules, ignore-user-config, safe-mode are stored in the
new session's `model_config["launch"]` at creation; on resume each flag given again must match the
stored value, otherwise exit 2 naming the flag. Flags not given on resume inherit the stored ones.

Inputs → outputs: chat flags + globals → exit code; session row with launch record; breadcrumb.

Files: `src/indus/cli/chat.py`, `src/indus/cli/session_select.py`, `src/indus/cli/runtime.py`
(host wiring, new), `src/indus/cli/handoff.py` (REPL handoff record, new),
`tests/unit/cli/test_chat_select.py`; `src/indus/foundation/config/schema.py` (display.*,
quick_commands, session.terminal_continue).

Edge cases / failure modes:
- `-q` and `--query-file` together → usage error exit 2; `--query-file -` reads stdin as text,
  nothing is shell-interpreted.
- Seeded `-q "/help"` or `-q "!ls"` is sent to the model as text (no slash or bang dispatch).
- Store unavailable during lookup → treated as "no session" with the error logged.
- `--in` with a missing dir → stderr error, exit 1.

Our design differences / decisions:
- In-process agent (P1, I55); refused gateway-only flags and the reduced slash set arrive with
  P17. `--image`, `--format stream-json`, `--run-budget`, `--create-if-missing`, `--base-url`,
  `--api-key`, `-v` are not added yet.
- No first-run guard or expensive-model guard yet (P07 setup owns them); a missing provider fails
  at host start with the route error and exit 1.
- The workspace fallback does not fall back again to the global newest session (spec §3.4.3);
  the reference does.

Tests we will write:
- store fixture with sessions in two workspaces: breadcrumb wins; workspace fallback; none → 1.
- title vs `latest`; id prefix; miss → 1 with the message.
- frozen-flag conflict → 2; same value accepted.
- non-TTY forces classic; `--tui` → 2; seeded `-q` passes `/`/`!` text literally.

## Decisions for the lead
- `session.terminal_continue` lives in a new `session` section (the plan's spelling) next to the
  existing `sessions` section; merging them is a one-line move.
- `display.interface: tui` before P12 prints a note and runs classic instead of failing.

Compare checklist (filled after the rewrite; reopened `main.py`, `terminal_breadcrumbs.py`):
- [x] interface order incl. non-TTY fallback (test: `test_chat_select.py::test_non_tty_*`)
- [x] `--in` checks and implied no-restore (test: `test_in_dir_*`)
- [x] `latest` keyword beats a title (test: `test_latest_keyword_beats_title`)
- [x] breadcrumb → workspace → exit 1 (tests: `test_breadcrumb_wins`, `test_workspace_fallback`)
- [x] frozen flags (tests: `test_frozen_flag_conflict_exits_2`, `test_frozen_flag_same_value_ok`)
- Deliberate differences: see above; "arrives with P17": refused flags (exit 2 list), the
  reduced slash set, lost-owner exit 3, 10 s interrupt settle wait (I55).
- Not taken: `--resume @claude/@codex` import (P24 import-agent).

## Status

Done (2026-10-18). Shared: config schema (+ its test's display default line).

## Integration (lead)

- The REPL entry is now `indus.cli.launch:run_launch` (`handoff.REPL_ENTRY`): it builds the
  `SlashContext` (`HostPort` over the host, the chat's store handle, creation params with the
  launch cwd and toolsets, the session row's cwd for bang commands), the banner, the recap of a
  resumed session and the seeded `-q` text (sent literally once the prompt is up), and calls slice
  B's `run_repl(host, session_id, scope=…, options=…, banner=…, recap=…, slash_ctx=ctx,
  on_resume=…, on_ready=…)`. `ReplLaunch` gained `store` and `no_restore_cwd`.
- `on_resume` (after `/resume` or `/branch`) runs `resume.resume_session` (folder unless
  `--no-restore-cwd`/`--in`, auto-approve, breadcrumb, history), prints its notices and the recap,
  and puts the session back on its own model (`model_override`, else its launch record) through
  `HostPort.switch_route` when that differs from the current route.
- The frozen-flag check compares against the launch record with the later `/reasoning` and
  `/model` switches laid over it (`resume.apply_switches`), so `indus -c -m <switched model>` is
  accepted and the original model is refused (exit 2).
- `tests/unit/cli/_stubs_b.py` (STAND-IN) removed; its recording double now lives in
  `test_chat_select.py`. New tests: `tests/unit/cli/test_launch.py`,
  `test_chat_select.py::test_the_repl_entry_is_the_launch_adapter`.
- Config schema rows added for what the REPL reads: `display.cli_multiline_shortcuts`,
  `display.show_reasoning`, `display.bell_on_prompt`, `onboarding.seen.busy_input` (next to A's
  `display.busy_input_mode`, `final_response_markdown`, `cli_refresh_interval`, `resume_*`).
