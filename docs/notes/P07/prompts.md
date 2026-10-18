# Task: Prompt toolkit for wizards (P07-01) Phase: 07 Owner: P07 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/secret_prompt.py`
(masked typing, key handling), `hermes_cli/setup.py` (prompt helpers, paste clean-up, the
navigation state machine and section runner). Research `04` §3.18, trap R04.19. Closed before
writing.
Evidence: none.

What it does (own words): one small module every interactive command uses to ask questions: a
line of text with a default, a secret typed with one `*` per character, yes/no, one choice from a
list (arrow keys; a numbered list when the terminal is dumb) and a checklist. Every helper takes
an `Interactive` object. Without a terminal, any helper raises `NonInteractive` carrying the
command's guidance text, so the command prints it and exits 1 instead of hanging on a prompt
nobody can answer (R04.19). Ctrl+C raises `Cancelled`. Inside a multi-section flow (setup, the
model picker) the left arrow goes back: on the first prompt of a section it returns to the
previous section; on a later prompt the section is run again with the earlier answers replayed
silently, and the prompt before the current one is asked again. Pasted text is cleaned of
bracketed-paste markers and carriage returns; keys also lose surrounding quotes and spaces.

Inputs → outputs: `ask_text`, `ask_secret`, `ask_yes_no`, `ask_choice`, `ask_checklist`
(question, default, rows) → the answer, or `Cancelled` / `Back` / `NonInteractive`.
`run_steps(io, [(label, fn), …])` runs sections with back navigation.

Files: `src/indus/cli/prompts.py`, `tests/unit/cli/test_prompts.py`, `tests/fakes/prompts.py`
(scripted driver for every P07 flow test).

Edge cases / failure modes:
- no TTY on stdin or stdout → `NonInteractive(guidance)` before any output is drawn;
- Ctrl+C anywhere → `Cancelled`; Ctrl+D on a text or secret line ends the line (keeps what was
  typed); a closed input stream (real EOF) → `Cancelled`;
- left arrow in a text prompt goes back only when the line is empty (otherwise it moves the
  cursor), and only inside a navigated flow where going back is possible;
- numbered fallback: blank answer = default, out-of-range or non-number asks again;
- a replayed answer is returned without drawing anything; the back target is the prompt before
  the one where left was pressed;
- secrets never reach the output stream (only `*`).

Our design differences / decisions:
- The terminal work sits behind a `PromptDriver` (prompt_toolkit by default); navigation,
  replay and the non-TTY rule live above it, so they are the same for every driver and tests can
  script whole flows (`ScriptedDriver`) without key timing. The prompt_toolkit driver itself is
  tested with one pipe input per prompt.
- `Back` carries the prompt index; the section runner (`run_steps`) owns replay.
- Escape cancels a list like Ctrl+C (lists only; text prompts keep Escape for editing).
- Yes/no is a line prompt (`[Y/n]`), so it works on dumb terminals too.

Tests we will write:
- prompt_toolkit pipe input: masked echo shows `*` and never the secret; Backspace edits;
  Ctrl+C → `Cancelled`; Ctrl+D ends a secret; left arrow → back only on an empty line;
- list: arrows + Enter, digits jump, left → back, Ctrl+C → cancel; checklist toggles with Space;
- numbered fallback for a dumb terminal;
- `NonInteractive` from every helper without a TTY, carrying the guidance;
- `run_steps`: back from a later prompt replays earlier answers and re-asks the previous one;
  back from a section's first prompt returns to the previous section's last prompt;
- paste sanitising table.

## Decisions for the lead
- Real EOF on input (closed stdin while a TTY was reported) is treated as `Cancelled`, the same
  as Ctrl+C; easy to change in `_terminal_eof`.

Compare checklist (filled after the rewrite; reopened `hermes_cli/secret_prompt.py`,
`hermes_cli/setup.py`):
- [x] one `*` per typed character, Backspace removes one (test:
  `tests/unit/cli/test_prompts.py::test_secret_is_masked`)
- [x] Ctrl+C raises (test: `tests/unit/cli/test_prompts.py::test_ctrl_c_cancels_text_and_list`)
- [x] Ctrl+D/EOF ends a secret line (test: `tests/unit/cli/test_prompts.py::test_ctrl_d_ends_secret`)
- [x] escape-prefixed navigation keys never become secret text (prompt_toolkit parses them as
  keys; test: `tests/unit/cli/test_prompts.py::test_secret_is_masked` sends Left inside the secret)
- [x] bracketed-paste markers stripped (test: `tests/unit/cli/test_prompts.py::test_sanitise_table`)
- [x] back from a later prompt replays earlier answers silently and reopens the previous prompt;
  back at a section's first prompt returns to the previous section (tests:
  `tests/unit/cli/test_prompts.py::test_back_within_a_section_replays`,
  `tests/unit/cli/test_prompts.py::test_back_from_first_prompt_goes_to_previous_section`)
- [x] a new answer drops every later recorded answer of that section (test:
  `tests/unit/cli/test_prompts.py::test_back_within_a_section_replays`)
- [x] back on the very first prompt of the first section is not offered (test:
  `tests/unit/cli/test_prompts.py::test_no_back_on_the_very_first_prompt`)
- [x] gap found: a save earlier in a section can change the flow on replay (a stored key turns
  the key prompt into "keep or replace") — fixed by replaying only to the same kind and question
  and dropping the rest of the replay at the first difference, pinned by
  `tests/unit/cli/test_prompts.py::test_replay_stops_where_the_flow_changed` and
  `tests/unit/cli/setup/test_wizard.py::test_back_from_terminal_returns_to_model`
- Deliberate differences: the reference's yes/no returns the default on EOF and on a
  non-interactive env flag; ours raises `NonInteractive` without a TTY (callers decide) and
  treats EOF as cancel. The reference falls back to `getpass` without a TTY; we refuse instead
  (R04.19). The reference prints a "Skipped (keeping current)" line when the default is
  re-chosen; we print nothing.
- Not taken: curses menus (prompt_toolkit already covers arrows and redraws).

## Status

Done (2026-10-18). Shared by setup, the model picker, fallback and the first-run guard.

## Integration (lead)

- Shared helpers added for the management commands: `io_for(state, guidance)` (built through the
  swappable `io_factory`, which tests replace with a scripted `Interactive`) and
  `confirm(io, question, assume_yes=)` → True / False / None (no terminal: guidance on stderr).
  Slice D's `cmdkit.confirm` moved here; auth, tools, profile delete and sessions browse use the
  kit instead of click prompts.
