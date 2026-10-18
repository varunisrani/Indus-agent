# Task: Sudo support (P05-10) Phase: 05 Owner: wave-2 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/terminal_tool_sudo.py`
(rewrite rules, password sources and order, NOPASSWD probe, failure annotation, cache clearing;
tty plumbing skipped). Research `02-tools.md` §3.23, trap R02.7. Closed before writing.
Evidence: none.

What it does (own words): when a command calls `sudo` and the machine wants a password, Indus can
feed the password on the command's stdin instead of the command hanging on a prompt nobody sees.
Literal `sudo` words are rewritten to `sudo -S -p ''`; one password line per rewritten call is put
in front of the command's stdin. The password comes from the session cache, the profile's secret
store (`sudo_password`), or a masked `sudo` server request on an interactive surface (45 s). It
never goes on a command line (R02.7), into a log, or back to the model.

Inputs → outputs:
- `rewrite_sudo(command) -> (text, count)`: pure rewrite. Rewritten: `sudo` at a command position
  (start, after `; & && || ( ) {`, newlines, `!`, `time`, `if/then/else/elif/do/while/until`),
  absolute paths ending in `/sudo`, `sudo` after `env` with ordinary options and assignments, after
  leading `NAME=value` words. Never: quoted words (`bash -c 'sudo …'`), `env -S`, dynamic paths
  (`$SUDO`, `$(which sudo)`, backticks, globs, `~`), command substitutions, comments, here-doc
  bodies, and a `sudo` whose stdin is not the command's own (after `|`, or with `<`, `<<`, `<<<`
  on that command): it would read the password from the pipe or file and eat the first line.
- `SudoSupport(scope, session_id)` installed on a spawned environment as `env.sudo`;
  `SpawnedEnvironment.execute` calls `before()` after `_prepare` and `after()` on the result.
  `before`: no rewrite → unchanged; heredoc-stdin backends → unchanged (the password would end up
  in the script text); probe `sudo -n true` (3 s): passwordless → unchanged, probe failure or
  timeout → unchanged (fail closed), password needed → find a password (cache → secret store →
  prompt) → rewritten text + password lines in front of stdin.
  `after`: password scrubbed from the output; headless failure tip; wrong password → cache
  cleared, `sudo_auth_failed`, `sudo_cache_cleared`; account without sudo → plain note, no
  re-prompt promised.
- Prompting needs a caller: the terminal tool wraps `env.execute` in
  `use_sudo_caller(broker=…, turn_id=…)`; without it the run is headless.
- `ExecResult` gained `sudo_auth_failed` / `sudo_cache_cleared` (default False) so P05-12 can put
  them in the result.

Files: `src/indus/backends/sudo.py`, `tests/unit/backends/test_sudo.py`; shared:
`src/indus/backends/spawned.py` (hook call + `probe_sudo`), `src/indus/backends/contract.py` (two
result flags), `src/indus/backends/local.py` (`open_local` installs the hook).

Edge cases / failure modes:
- Several `sudo` calls in one command: one password line each.
- The person cancels or the 45 s pass: no rewrite, the command fails with sudo's own message plus
  the tip.
- A configured password that sudo rejects: flagged, store left alone (the person's explicit
  setting), note says to update it.
- The password appears in the output (a command that echoes stdin): replaced by the mask.
- Delegated children (P10) run headless: no caller is installed for them.

Our design differences / decisions:
- Password order follows the phase text: session cache, then secret store, then prompt.
- The probe runs whenever a rewrite is about to happen (not only before a prompt), so passwordless
  sudo never receives a password on stdin.
- Secret name `sudo_password` (also `SUDO_PASSWORD` accepted).

Tests we will write:
- Rewrite table (positive and negative cases).
- Probe: passwordless → no rewrite; probe error/timeout → no rewrite; needs password + cached →
  rewrite and stdin lines; secret store source; prompt via a fake broker caches the answer;
  heredoc backend skipped.
- Annotations: headless tip; wrong password clears the cache and sets both flags; configured
  password rejected → flag only; no-sudo account note.
- Redaction: password never in output, never in the logged records, never in the script text.

## Decisions for the lead
- `ExecResult` gets the two sudo flags (contract change, defaults keep every backend unchanged).
- The masked prompt uses the protocol's existing `sudo` server request (45 s).

Compare checklist (filled after the rewrite; reopened `tools/terminal_tool_sudo.py`):
- [x] Literal and absolute-path sudo, env prefixes with options; no `bash -c`, `env -S`, dynamic
  paths (test: `test_sudo.py::test_rewrite_table`)
- [x] One stdin line per invocation (test: `test_sudo.py::test_cached_password_rewrites_and_feeds_stdin`)
- [x] NOPASSWD probe skips the rewrite (test: `test_sudo.py::test_passwordless_sudo_is_left_alone`)
- [x] Headless tip, wrong-password cache clear, configured password kept (tests:
  `test_sudo.py::test_headless_failure_gets_tip`, `test_sudo.py::test_wrong_cached_password_clears_cache`,
  `test_sudo.py::test_wrong_configured_password_is_flagged_not_cleared`)
- [x] No re-prompt promise for accounts without sudo (test: `test_sudo.py::test_account_without_sudo`)
- Deliberate differences: probe before using any password (reference probes only before a
  prompt); cache-first order per the phase file; probe failure means no rewrite; a piped or
  redirected `sudo` (`make | sudo tee f`) is not rewritten (the reference rewrites it, and sudo
  then reads the pipe's first line as the password).
- Not taken: raw `/dev/tty` prompt (surfaces answer the `sudo` server request); Windows input.

## Status

Done (2026-10-18). Shared edits: `src/indus/backends/spawned.py`, `src/indus/backends/contract.py`,
`src/indus/backends/local.py`. P05-11/12 wrap `env.execute` in `use_sudo_caller` and copy the two
flags into the terminal result.

## Integration (lead)

Merged in the P05 wave 2 integration (2026-10-18, lead's verify tree).
- `terminal_result.run_foreground` imports `use_sudo_caller` directly (no `importlib` probe) and
  reads `ExecResult.sudo_auth_failed` / `sudo_cache_cleared` as real fields (no `getattr`).
- The terminal passes `interactive=False` to `use_sudo_caller` for every non-interactive
  `run_context` (cron, one-shot, unattended), so those runs never prompt for a password. Pinned by
  `tests/unit/tools/builtin/test_terminal_result.py::test_runs_inside_the_sudo_caller_context`.
