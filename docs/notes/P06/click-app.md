# Task: click app, lazy groups, global flags, unknown-command UX (P06-02) Phase: 06 Owner: P06 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/_parser.py` (top-level
flag list, value-flag derivation and fallback, chat subparser defaults, unknown-subcommand
override), `hermes_cli/main.py` (`_parse_cli_args`, `_coalesce_session_name_args`,
`_build_cli_parser` registration order — to know the cases, not the method),
`hermes_cli/cli_unknown_command.py` (slash-command variant; only the copy shape). Research `04`
§3.2.1–3.2.4, trap R04.21. Closed before writing.
Evidence: none.

What it does (own words): the `indus` command is a click group whose subcommands are named in one
ordered registration list (`name → "module:attr"`) and imported only when used, so `indus --help`
loads no subcommand module and the help order never moves. The group carries the global flags;
bare `indus` runs `chat`. Before click sees argv, a small pass rewrites the session flags:
`-c/--continue` takes an optional name that may span several words (`indus -c my long name`), a
bare `-c` means "latest", and a word that names a registered command ends the name unless it is
the first word and another command follows (`indus -c model chat` → session "model", command
chat). The rewritten form is always `--continue=<value>` so click never has to guess. An unknown
first word prints exactly three lines (not-a-command, a hint, how to get help) and exits 2; nested
groups do the same with their own name. The app exports the value-taking flag set for the profile
pre-parse (P06-01), derived from the live click params with a hand-kept fallback.

Inputs → outputs: argv → a `CliArgs` record of global values on `ctx.obj`, the chosen subcommand;
help text; exit code 2 for usage errors.

Files: `src/indus/cli/app.py`, `src/indus/cli/lazy.py`, `src/indus/cli/unknown.py`,
`tests/unit/cli/test_app.py`.

Edge cases / failure modes:
- `indus -m x chat` keeps `x`: chat's own copies of the global flags default to None/False and are
  merged over the group's values only when given (R04.21).
- `-r NAME words` coalesces like `-c` but always takes at least one word.
- `--continue=NAME` and `-c` followed by a flag are left alone.
- Unknown command with no close match and no hint: the middle line still exists (a generic hint),
  so the output is always three lines.

Our design differences / decisions:
- click instead of argparse; no speculative double parse — the pre-pass decides the `-c` value.
- Moved-command and plugin install hints are tables/seams (empty until a command moves or P13
  adds catalogue plugins).
- Registration list holds only commands that exist (`chat` today); later phases append in the
  order of research §3.2.4.

Tests we will write:
- `indus -c model chat`, `indus -c my long name`, bare `-c`, `-c` before a flag, `indus -m x chat`.
- `--help` import spy: no subcommand module imported.
- unknown command: three lines + exit 2, also in a nested group; help order golden.
- value-flag set: derived set equals the fallback (parity).

## Decisions for the lead
- With only `chat` registered, `indus -c model` reads `model` as a session name; once a `model`
  command exists it becomes "bare -c, run `model`" (same rule as the reference's pre-pass).

Compare checklist (filled after the rewrite; reopened `_parser.py`, `main.py`):
- [x] every global flag present under our names (test: `test_app.py::test_global_flags_present`)
- [x] `-c` optional value cases (tests: `test_continue_*`)
- [x] values before `chat` survive (test: `test_model_before_chat_is_kept`)
- [x] three-line unknown command + exit 2, nested too (tests: `test_unknown_*`)
- [x] value flags derived from the live parser, fallback kept in step (test:
  `test_value_flags_match_fallback`)
- Deliberate differences: `-w/--worktree`, `--accept-hooks`, `--checkpoints`, `--native`, `--dev`
  are not added until their phases exist (P09/P12/P13/P24); the reference's epilogue examples are
  not reproduced (our help text is our own).
- Not taken: speculative parse with captured stdout.

## Status

Done (2026-10-18).
