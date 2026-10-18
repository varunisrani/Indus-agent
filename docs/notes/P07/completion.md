# Task: `indus completion` (P07-25) Phase: 07 Owner: P07 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/completion.py` (whole).
Research `04-cli-config.md` §3.28. Closed before writing.
Evidence: none.

What it does (own words): `indus completion [bash|zsh|fish]` (default bash) prints a shell script
that hands every completion request back to `indus` itself (click's shell completion), so the
answers always come from the live command tree, including commands added later. On top of the
command and option names, three options complete values:
- `-p/--profile`: live profile names under the root (P01 `list_profiles`);
- `-c/--continue`, `-r/--resume`: titles of recent sessions, read from the store opened read-only
  (no migration, no checkpoint) inside a 1 s budget; a slow or missing store gives an empty list
  instead of a hang;
- `-t/--toolsets`: toolset names, completing the last item of a comma-separated list.

Inputs → outputs: the shell name → the script on stdout, exit 0; an unknown shell → usage error,
exit 2. At completion time the shell sets `_INDUS_COMPLETE` and `indus` prints the candidates.

Files: `src/indus/cli/commands/completion.py`, `tests/unit/cli/commands/test_completion.py`;
`src/indus/cli/app.py` (shared): the `-p`, `-c`, `-r`, `-t` options get lazy `shell_complete`
hooks that import this module only when the shell asks.

Edge cases / failure modes:
- No store file → no titles; a store that never opens within the budget → no titles (the worker
  thread is a daemon and is left behind).
- Titles with spaces are offered whole (the `-c` joining in `app.rewrite_session_flags` accepts
  them).
- Machine sessions (sub-agents, tools, one-shot) are not offered.

Our design differences / decisions:
- Click's own completion protocol instead of hand-built per-shell scripts: no stale lists, value
  completion for profiles, sessions and toolsets in every shell click supports.
- The budget is a parameter (tests use a tiny one).

Tests we will write:
- script head per shell (golden first lines); a completion request for `-p` returns profiles from
  a fixture root; titles from a fixture store; a blocked store opener returns an empty list within
  the budget; toolset completion after a comma; the app's options carry the hooks.

Compare checklist (filled after the rewrite; reopened `hermes_cli/completion.py`):
- [x] bash/zsh/fish scripts from the live tree (test: `test_completion.py::test_script_heads`)
- [x] profile names after `-p` (test: `::test_profile_completion_from_root`)
- Deliberate differences: click's dynamic protocol instead of static case tables; session titles
  and toolsets complete too.
- Not taken: nothing else in the reference module.

## Status

Done (2026-10-18). Shared changes: `src/indus/cli/app.py` options gained `shell_complete` hooks and
the registry got this slice's five rows (approvals, config, tools, profile, completion);
`src/indus/cli/lazy.py` now always shows a registered command's one-line registry help in
`indus --help` (it used the loaded command's own help once imported, which wrapped differently and
broke `test_app.py::test_help_order_follows_registration` as soon as a wider name was registered).

## Integration (lead)

- The `lazy.py` help-listing change (also made by slice A) is kept once; slice D's
  `test_app.py` parsing change was dropped (the real fix makes it unnecessary).
