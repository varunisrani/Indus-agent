# Task: Slash dispatch and session commands (P06-09) Phase: 06 Owner: P06 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cli.py` (`process_command`,
the unregistered-command path, quick-command runner, prefix expansion, `_looks_like_slash_command`),
`hermes_cli/cli_commands_mixin.py` (resume, sessions, branch handlers). Research `04-cli-config.md`
§3.7.2–3.7.3. Closed before writing.
Evidence: none.

What it does (own words): the classic REPL hands every `/…` line to `dispatch(line, ctx)`. The
first word is lowercased for matching only (arguments keep their case), an alias resolves to its
canonical command, and the command runs. While a turn runs, the command's busy policy decides:
`reject` prints a short "busy" line, `interrupt_then_dispatch` stops the turn and waits for it to
settle first, `dispatch` runs at once. Lines that are not built-ins go, in order, to the user's
quick commands, plugin commands (P13 seam), skill commands (P09 seam) and finally unique-prefix
expansion over everything known. An observer hook seam fires before built-ins; it can never block
or fail the command. The session commands change which session the REPL is on, or take turns
back, or write a copy.

Inputs → outputs: `dispatch(line, ctx) -> SlashResult` where `ctx: SlashContext` carries the scope,
a `SessionPort` (create/resume/close/interrupt/wait/steer/route/tools/processes), the store handle,
the current session id, an output callable, `confirm`, `is_busy`, the REPL state (queue, armed
resume list, reasoning display, pending row writes) and the extension seams. `SlashResult` tells the
REPL what to do next: `exit`, `switch_to` (+ `resumed`), `clear_screen`, `submit` (text to send as a
turn), `prefill` (text to put back in the composer). Session commands:
- `/new [name]` (alias `/reset`; `now`/`-y`/`--yes` skip the confirm): new session with the same
  creation flags, old one closed and its background jobs released; busy → interrupt then dispatch.
- `/clear`: clear the screen and start a new session (no confirm).
- `/history`: the live transcript (user and assistant text; tool rows as one line each).
- `/title [name]`: show or set (user provenance; `TitleTaken` shown); before the row exists the
  title waits in the REPL state and is written after the first turn (`after_turn`).
- `/resume [name|N]`: bare → numbered list of recent sessions, armed for a bare number on the next
  line; any other command disarms it; a title, id (prefix) or number resumes. Busy → rejected.
- `/sessions [name|N]`: list (arms the picker); with an argument it is `/resume`.
- `/retry`: takes back the last user turn and submits its text again. `/undo [N]`: takes back N
  user turns (store soft-delete) and offers the oldest removed text back in the composer.
- `/save json|md|html [file]`: JSON lines through the P02 export, Markdown or HTML rendered here.
- `/branch [name]`: P02 branch copy, then the REPL switches to it.

Files: `src/indus/cli/slash/__init__.py`, `src/indus/cli/slash/dispatch.py`,
`src/indus/cli/slash/session_cmds.py`, `src/indus/cli/slash/context.py` (context, result, port
protocol, REPL state), `src/indus/cli/slash/port.py` (the `SessionPort` adapter over
`InProcessHost`), `tests/unit/cli/slash/test_dispatch.py`, `tests/unit/cli/slash/test_port.py`,
`tests/unit/cli/slash/_fakes.py`, `tests/unit/cli/slash/conftest.py`; placeholder
`src/indus/cli/__init__.py` (one docstring line; slice A's file wins at merge).

Edge cases / failure modes:
- `/usr/bin/ls` is not a command (second `/` in the first word) → `is_slash` is False.
- Unknown command → two lines (not one of ours, nearest names) — never forwarded to the model.
- Prefix ambiguity → "matches several" with the list; no guess.
- A planned command of a later phase → "arrives with Pnn"; a gateway-only one → "only in chats".
- A handler that raises: logged, one plain error line, the REPL survives.
- Store missing (memory store in tests): session commands that need it say so.
- `/undo` with no user turns → "nothing to undo"; N larger than the turns → all turns.
- `/resume` of the current session → "already here"; unknown title/id → not found + hint.

Our design differences / decisions:
- Handlers return a `SlashResult` instead of driving the REPL themselves; the REPL (slice B) acts
  on it, so dispatch is testable without a terminal.
- Prefix expansion: unique prefix only (no "shortest wins" tie-break).
- `/new` makes an independent session (no reset marker); the REPL's `create_params` carry the
  frozen creation flags.
- Arrives with P17: the reduced slash set and refusals that exist because the reference's classic
  chat is a gateway client (I55; here chat runs in-process).

Tests we will write:
- Precedence with fakes at every level (built-in, quick, plugin, skill, prefix); observer fires
  first and cannot block; prefix ambiguity lists; `/new` while busy interrupts then dispatches;
  reject policy while busy; `/undo 2` hides two turns in a real store; `/retry` resubmits;
  `/resume` picker armed then disarmed by another command; `/title` pending before the row exists;
  `/save` json/md/html files; `/branch` switches to a copy.

## Decisions for the lead
- Interface for slice B: `dispatch(line, ctx)`, `take_resume_pick(text, ctx)` (call for plain input
  while `ctx.state.resume_choices` is armed), `after_turn(ctx) -> str | None` (call when a turn
  ends: writes pending row changes, returns the next queued prompt), `is_slash(text)`.
- Mid-session `/resume` returns `switch_to` + `resumed=True`; the REPL's switch path should run the
  same resume behaviour as startup (cwd restore, recap, switch rehydration — P06-13, slice A).

Compare checklist (filled after the rewrite; reopened `cli.py`, `hermes_cli/cli_commands_mixin.py`):
- [x] lowercase-for-matching, args keep case (test: `test_dispatch.py::test_arguments_keep_their_case`)
- [x] picker disarmed by any other command (test: `::test_resume_picker_disarmed_by_other_command`)
- [x] precedence built-in → quick → plugin → skill → prefix (test: `::test_precedence_order`)
- [x] prefix ambiguity lists candidates (test: `::test_prefix_ambiguity_lists`)
- [x] `/resume` strips copied placeholder brackets, refuses the current session, refuses while a
  turn runs (tests: `::test_resume_by_title_and_id`, `::test_busy_reject_policy`)
- [x] `/branch` refuses with no conversation and while busy (test: `::test_branch_needs_history`)
- [x] `/undo` default 1 and offers the text back (test: `::test_undo_two_hides_two_turns`)
- Deliberate differences: handlers return a result object; unique-prefix rule without the
  "shortest wins" tie-break; `/new` asks only when the session has messages; quick-command aliases
  have a hop limit (the reference can recurse); the observer seam fires for every slash line.
- Not taken: usage metrics per command, skill bundles (P09), gateway-owned settings, `/sessions
  browse` spelling.

## Status

Done (2026-10-18).

## Integration (lead)

- Wired into the REPL by `indus.cli.repl.slash_bridge.wire_slash` (slash lines run in a terminal
  section; `take_resume_pick` gets plain lines; `after_turn` at each turn end) and the launch
  adapter `indus.cli.launch` (context, `on_resume` = slice A's resume behaviour).
- `ctx.out`, `is_busy`, `confirm`, `ask_approval` are bound by `wire_slash`; `confirm` reads y/N
  on the terminal inside the section, so `/new` with history asks for real.
