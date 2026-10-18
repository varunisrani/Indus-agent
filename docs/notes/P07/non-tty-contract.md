# Task: Non-TTY contract suite + help catalogue (P07-26) Phase: 07 Owner: lead (integration)

Read: none (our contract). Research `04-cli-config.md` §3.2.4 (help order), trap R04.19.
Evidence: none.

What it does (own words): one contract over the whole `indus` command tree.
- `tests/contract/cli/non_tty_table.yaml` names every visible command and subcommand with the exit
  code it ends with when run with no further arguments, stdin and stdout not terminals, in a fresh
  profile: 0 informational, 1 guidance, 2 usage. A second section lists a few guidance cases that
  need an argument (`auth add openrouter`, `profile delete work`, ...).
- `test_non_tty.py` walks the click tree (lazy groups loaded on the way), checks the table names
  exactly the registered commands (a new command without a row fails, so does a stale row), and
  runs each one under a 5 s watchdog with a stdin that records any read. A run must not hang, not
  read stdin, not raise, print something when it exits 1, and exit with the declared code.
- `test_help_catalogue.py`: the top-level order equals a golden list (research §3.2.4 order) both
  in `REGISTRY` and in `indus --help`; every command and subcommand answers `--help` with exit 0
  and has a one-line description; `indus NAME --help` in a fresh process imports NAME's module and
  no other top-level command's module (`SHARED_IMPORTS` allows `dump` → `status`, which it reuses).

Inputs → outputs: the app's command tree → pass/fail.

Files: `tests/contract/cli/_walk.py`, `tests/contract/cli/test_non_tty.py`,
`tests/contract/cli/test_help_catalogue.py`, `tests/contract/cli/non_tty_table.yaml`.

Edge cases / failure modes:
- The run uses a temp `HOME` and working dir: `indus backup` with no arguments writes its zip into
  the user's home folder, so the contract must never see the real one (see "Integration").
- `sessions` is a group without a default action, so bare `indus sessions` prints its help and
  exits 2 (declared as usage); the other bare groups list something and exit 0.
- `doctor` exits 1 in a fresh profile (no settings file and no provider are reported as issues).
- `chat` exits 1 through the first-run guard before it would read a prompt from stdin.

Our design differences / decisions:
- The import check uses one subprocess per top-level command (four at a time); subcommands share
  their group's module, so their `--help` runs in process.
- The stdin tripwire answers end-of-input, so a prompt that slipped past the no-terminal rule fails
  fast and is reported instead of hanging the suite.

Tests: the suite itself (one parametrised case per table row and per command for `--help`).

## Status

Done (2026-10-18), built at P07 integration.
