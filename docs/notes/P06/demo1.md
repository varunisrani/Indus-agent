# Task: Demo 1 end to end (P06-14) Phase: 06 Owner: lead (integration)

Read: none in the reference (our flow). Phase file `docs/plan/phases/P06-classic-cli-demo1.md`,
the three P06 slices' notes, `tests/integration/cli/fake_runner.py` (P06-12's driver).

What it does (own words): the walking skeleton, proven by driving the real `indus` entry point in a
pseudo-terminal (`pexpect`) against the fake model, in a temporary home and folder, with a pinned
`INDUS_TERMINAL_ID` so `indus -c` finds this terminal's breadcrumb.

Files: `tests/e2e/test_demo1.py`, `tests/e2e/conftest.py` (`Demo` harness: spawn, run, stored
rows, the terminal-runtime fence reused from `tests/unit/cli/conftest.py`),
`tests/fixtures/model_scripts/demo1.yaml`, `scripts/demo1.sh` (manual, real key, not in CI),
`.github/workflows/ci.yml` (`e2e` job on ubuntu-latest and macos-latest, required by the gate).

Tests:
- `test_demo1_create_run_quit_then_continue`: "create hello.py and run it" → streamed text before
  the card → approval card (`chmod +x hello.py && ./hello.py` is "chmod + run straight away") →
  `1` → the command runs on the local backend (hello.py appends to `ran.log`) → final answer →
  `/quit` exit 0; the stored transcript passes `check_alternation`; `indus -c` shows "resumed", the
  recap and `/history` lists the turn; still one session, the command ran once.
- `test_kill_after_the_approval_runs_the_command_at_most_once`: the run command sleeps after
  writing its marker; SIGKILL once the marker exists (tool row not written yet); `indus -c` with a
  follow-up script answers a new message; the marker count is 1 and the repaired transcript
  alternates (M1).
- `test_ctrl_c_during_a_stream_seals_the_reply`: a stream that stalls after one and a half lines;
  Ctrl+C prints the partial line at once with "(interrupted)", the REPL stays usable, `/quit` 0.
- `test_ctrl_c_during_a_bang_command_gives_130_and_the_session_goes_on`: `!sleep 30`, Ctrl+C →
  `!: interrupted (exit 130)`; a second bang command still works; nothing stored.
- `test_oneshot_exit_codes_from_the_real_entry`: `-z` 0 with only the text on stdout, a provider
  failure 2, `--provider` without `--model` 2 (the full tables are P06-12's integration tests).

Edge cases found while building it: slash/bang output and questions under `patch_stdout` only
appeared after the command ended, and Ctrl+C never reached a running bang command (the prompt's
SIGINT handler runs on the blocked loop) — fixed in `slash_bridge.terminal_section` (see
repl-core.md "Integration (lead)").

Run three times in a row on the integration worktree: see the lead's report.
