# Task: Dashboard admin console (P24-36) Phase: 24 Owner: wave-39 builder U3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/console_engine.py`
(all 861 lines: the command set, read-only vs confirm-first marks, the shell-syntax refusal, the
built-ins, output capping, the local REPL); `hermes_cli/web_routers/chat_ws.py` (lines 176–445,
the `/api/console` loop; Compare step only). Research 09 §3.6 (frames, rules, limits, close codes).
Closed before writing.
Evidence: none.

What it does (own words): the dashboard gets an admin console: a modal with a terminal-like log
and one input line. It is not a shell. Every line is parsed into words and matched against a curated
list of `indus` commands (status, doctor, logs, sessions list, config show, cron list, …); anything
else — an unknown command, a flag in first position, shell syntax such as pipes, redirects, `;`,
`$(`, backticks — is refused with a plain message. Commands that change something (config set, cron
pause/resume/run, session rename, plugin enable/disable, pairing approve/revoke) are marked; the
first time such a line arrives the console answers "confirm required" with a question, and the
command runs only when the client confirms with the **exact same line**. The same engine backs
`indus console`, a local REPL that asks `[y/N]` for marked commands and refuses them without a
terminal.

The socket `/api/console` is accepted only through the shared WebSocket helper (`guarded_ws`,
purpose `web-console`, the PTY's gates: feature flag → 4404, credential → 4401, Host/Origin → 4403,
peer → 4408) and only when the dashboard was started with the console enabled. Frames are JSON
objects only:

- client → `ping`, `cancel`, `confirm {command}`, `input {line}` (`command {line}` is accepted as an
  alias of `input`);
- server → `ready {profile, prompt}`, `output {id, stream, data, command}`, `error {id?, message,
  command?}`, `confirm_required {id, command, message}`, `complete {id, status, command}` (status
  `ok | error | timeout | cancelled | confirm_required | clear | exit | idle`), `clear`, `pong`;
  every frame that carries a prompt also carries the prompt string.

Rules: one command at a time (a second `input` while one runs → `error` "already running"); a
pending confirmation must be confirmed with the same command or cancelled before anything else runs
(a different line → error, the pending one stays); `cancel` with nothing running and nothing pending
→ `complete {status: idle}`; `cancel` interrupts any agent run the command started (the engine
hands each run a cancel token whose interrupters are called) and waits up to 10 s for its worker;
a command runs at most 60 s (then the token is cancelled, the worker gets the same 10 s, and
`complete {status: timeout}`); output over 50,000 characters is cut with a marker naming how many
were left out; commands run on a pool of 4 worker threads shared by every console socket; `exit`
closes the socket with 1000; a failure building the engine closes 4400 (bad profile) or 1011
(anything else).

Inputs → outputs: WebSocket JSON frames ↔ JSON frames; `indus console` stdin lines → stdout/stderr.

Files: `src/indus/service/console.py` (the engine: command list, refusals, confirmation, limits,
runner, cancel token — in `service` because `web` and `cli` may not import each other),
`src/indus/cli/console_engine.py` (`indus console`: the REPL and `--run LINE`),
`src/indus/web/console.py` (the socket route), `tests/contract/web/test_console_socket.py`,
`tests/unit/cli/test_console_engine.py`, `apps/web/src/console/ConsoleModal.tsx`,
`apps/web/src/console/ConsoleModal.test.tsx`. Shared: `src/indus/web/app.py` (mount the route),
`src/indus/web/spa.py` + `apps/web/src/lib/bootstrap.ts` (`__INDUS_DASHBOARD_CONSOLE__`),
`src/indus/cli/app.py` (one REGISTRY line), `tests/contract/cli/test_help_catalogue.py` +
`non_tty_table.yaml` (the new command), `tests/unit/web/ws/test_ws_auth.py` (a socket whose
feature is off answers 4404), `apps/web/src/App.tsx` (console entry button).

Edge cases / failure modes:
- an empty line → `complete {status: ok}` with no output;
- a line starting with `indus` → the word is dropped (`indus status` = `status`);
- unbalanced quotes → error "could not read the line";
- a frame that is not JSON, not an object, or has an unknown `type` → `error`, socket stays open;
- an over-long frame (64 KiB) → `error`, nothing runs;
- a command that raises → `error` with the message (redacted), `complete {status: error}`;
- a command that prints only to stderr → output frames with `stream: "stderr"`;
- the socket closing mid-command → the token is cancelled and the worker is left to unwind;
- a command calling `sys.exit` / click `Exit` → its code decides ok vs error;
- concurrent consoles: output capture is per thread (a routing stream), so two consoles never see
  each other's text.

Our design differences / decisions:
- The command list is ours, matched against our click tree (a test proves every curated path exists
  in `indus --help`).
- Each command runs as its own process, `python -m indus <words>`, for the grant's profile (env
  from `safety.child_env`, `INDUS_HOME` pinned; stdin closed; new process group). The reference
  runs commands in-process and redirects the process streams; a child process gives clean output
  capture for 4 parallel workers, and cancel/timeout kill the whole tree (`backends.local.
  force_kill_tree`), which is how "cancel interrupts any agent the command forked" holds for
  `cron run`. A command costs one interpreter start (~1 s).
- `indus console --run LINE` (not `-c`: the global argv rewrite owns `-c/--continue`).
- The reference's 20,000-char default limit is not used; the row's 50,000 is.
- A bare `confirm` (no command) is refused: the row asks for the exact same command (the
  reference falls back to the pending one).

Tests we will write:
- engine: curated paths exist in the CLI; shell syntax, flags, unknown commands refused (with a
  suggestion); mutating command → confirm_required, runs only with `confirmed=True`; output cap with
  marker at 50,001 chars; builtins help/history/clear/exit.
- socket: no ticket/credential → refused by the helper (4401) and the feature flag (4404); `ready`
  frame; second command while one runs → "already running"; confirm with a different command
  refused; 60 s timeout under a fake clock; cancel waits for the worker; `exit` → 1000; bad JSON →
  error frame; the route is tagged by `guarded_ws` (enumerated with `websocket_routes`).
- modal: sends `input`, renders output, shows the confirm question with Confirm/Cancel sending the
  exact command, the credential only in the subprotocol.

## Decisions for the lead
- Console feature flag: `AppOptions.console_enabled` (already on `DashboardState`) stays the switch;
  `indus dashboard --console` (CLI flag) is the lead's to wire if wanted — today only tests and the
  app factory set it.
- Curated set kept small and read-mostly (see `COMMANDS` in `service/console.py`); widening it is a
  one-row change per command.
- Commands run in a child process (see differences); keys a person exported only in the dashboard's
  own shell are not passed to it (the child reads the profile's `.env` and secret store).

Compare checklist (filled after the rewrite; reopened `hermes_cli/console_engine.py` and
`hermes_cli/web_routers/chat_ws.py`):
- [x] shell syntax refused (`|`, `||`, `&`, `&&`, `;`, `>`, `>>`, `<`, `$(`, backtick) (test: `tests/unit/cli/test_console_engine.py::test_shell_syntax_refused`)
- [x] leading flag and unknown command refused, close matches suggested (test: `::test_unknown_command_suggests`)
- [x] mutating commands need confirmation; confirmed run executes (test: `::test_mutating_needs_confirmation`)
- [x] output cap marker (test: `::test_output_cap_marker`, `test_console_socket.py::test_output_truncated_with_marker`)
- [x] built-ins help / help <command> / history / clear / version / exit / quit (test: `::test_builtins`)
- [x] REPL: non-interactive refuses confirm-first commands with exit 1 (test: `::test_repl_non_tty_refuses_mutating`); `indus console` without a terminal exits 1, `--run status` 0 (`tests/contract/cli/non_tty_table.yaml`)
- [x] cancel kills the command and what it started (test: `::test_cancel_kills_the_command_and_what_it_started`)
- [x] socket: helper gates 4401/4404, ready, one-at-a-time, exact confirm, pending blocks input, cancel of pending, idle, 60 s timeout on a fake clock, cancel waits for the worker, exit 1000, bad frames, 4400 (tests: `tests/contract/web/test_console_socket.py`)
- [x] gap: the reference sends an error frame before 4400/1011 — added (`_say_and_close`)
- [x] gap: `input` accepts the line under `command` too — added
- Deliberate differences: our command list; child process per command; exact-command confirm only.
- Not taken: the reference's `send` command (messaging is the gateway's), `sessions export/repair/
  optimize` (file-writing work belongs to a terminal), the status-footer strip.

## Status

Done (2026-10-21). Shared files listed under Files.
