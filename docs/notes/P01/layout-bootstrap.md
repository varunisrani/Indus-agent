# Task: Home layout, persona seed, scratch dir, bootstrap (P01-21) Phase: 01 Owner: P01 builder, slice S-log

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_constants_scratch.py`
(prune rules only: idle cutoff, subtree walk, symlinks not followed), `hermes_constants.py`
(scratch dir, prune stamp and interval, socket temp-root length budget), `hermes_bootstrap.py`
(skim: order of steps, UTF-8 stdio, import-path hardening, scratch env export),
`hermes_cli/config.py` (persona seed function and the home skeleton ensure function). Research:
`docs/plan/research/03-state.md` §3.1, §3.29; `01-agent-core.md` §3.5 (persona file). Closed
before writing.
Evidence: none.

What it does (own words):
- `layout.ensure_home(scope)` makes sure a profile home has its working folders (`logs`,
  `sessions`, `memories`, `skills`, `cron`, `cache`, `cache/scratch`, `backups`, `plugins`,
  `hooks`), each owner-only (0700), and seeds `PERSONA.md` with a short default identity the
  first time. It is cheap to call often (remembered per home), but the "is this named profile
  still alive" check runs before the memo, so a deleted profile is never re-created by a stale
  route (R04.25). The persona file is the user's: an existing file is never rewritten; a dangling
  symlink in its place is replaced by a real file through temp + rename, never written through.
- `scratch_dir(scope)` is `<home>/cache/scratch`: the process temp dir. `scratch_env(home, env)`
  gives `TMPDIR`/`TMP`/`TEMP` pointing there for every name the user has not set.
  `prune_scratch(scope, clock)` deletes top-level entries with no write anywhere in their tree for
  over 24 h, at most once per hour per home (stamp file `.last_prune`, mtime). Symlinks are not
  followed; an entry that cannot be scanned is kept. `socket_dir(scope)` returns the scratch dir
  unless its path is longer than 50 characters (AF_UNIX path limits), then the OS temp root.
- `bootstrap.run(argv, env)` is the first call of every entry point: drop the cwd/`''` from
  `sys.path` when they would shadow `indus`; force UTF-8 (`errors="replace"`) on stdio whose
  encoding is not UTF-8; pick the profile with `preparse.select_profile`; set `INDUS_HOME` and the
  scratch temp vars for the process; build the `.env`-aware env view and the root `Scope`;
  ensure the home; set up logging in the requested mode; return `(scope, argv)`.

Inputs → outputs: `ensure_home(scope) -> Path`; `scratch_dir(scope) -> Path`;
`scratch_env(home, env) -> dict[str, str]`; `prune_scratch(scope, clock) -> int` (entries removed;
0 when skipped); `socket_dir(scope, *, temp_root=None) -> Path`; `bootstrap.run(argv, env, *,
mode="cli", verbose=False, value_flags=(), nested_tail_flags=(), recovery_commands=()) ->
(Scope, list[str])`.

Files: `src/indus/foundation/layout.py`, `src/indus/foundation/bootstrap.py`,
`tests/unit/foundation/test_layout.py`, `tests/unit/foundation/test_bootstrap.py`; stand-ins in
`tests/unit/foundation/_stubs_log.py`.

Edge cases / failure modes:
- Named profile whose dir is missing or tombstoned → `ProfileError` (`profile.not_found` /
  `profile.tombstoned`), nothing created, memo not consulted.
- Memo hit but the home was removed since → re-run the ensure (memo needs `home.is_dir()`).
- Persona: regular file (even empty) kept; a symlink to an existing file kept (user's choice);
  dangling link replaced by a regular 0600 file, its target never created; a symlink to a dir or
  an unreadable target is left alone.
- Prune: stamp younger than 3600 s → no-op; the stamp itself is never pruned; an entry touched
  between scan and delete is not re-checked in P01 (the process reaper and the re-check are P05).
- `sys.path`: only `''`, `.` and the absolute cwd are removed, and only when the cwd holds an
  `indus` package or module that is not the installed one.
- Windows: no ANSI enabling and no `ver` stub here (later desktop/Windows phases).

Our design differences / decisions:
- Persona file name `PERSONA.md` (D-16.65) with our own seed text.
- Skeleton list per the phase file (ours): `backups`, `plugins`, `cache`, `cache/scratch` are
  created up front; integration-specific dirs come with their phases.
- The scratch prune is clock-driven (`clock.now()`), so tests use `FakeClock` instead of real age.
- Bootstrap is a function the entry calls, not an import side effect.
- Left out (later phases): self-update settling, Happy Eyeballs patching, startup watchdog,
  Windows ACL helper, glibc environ guard, process reaping inside scratch (P05).

Tests we will write:
- layout: fresh home has every dir at 0700 and `PERSONA.md` once (0600); custom persona survives a
  second run (memo reset); broken symlink replaced and target not created; tombstoned / missing
  named profile refused even after a memo hit; scratch entry 25 h idle pruned, 23 h kept, a nested
  fresh file keeps its entry, second prune within the hour no-op, after an hour runs again;
  `socket_dir` with a 60-char scratch path → temp root, short path → scratch; `scratch_env` keeps
  user-set vars.
- bootstrap: `-p work` → stripped argv and a scope for `work` with `INDUS_HOME` set; cwd and `''`
  removed from `sys.path` when a shadowing `indus/` sits in the cwd; non-UTF-8 stdio reconfigured;
  scratch vars exported unless set; logging set up in the requested mode.

## Decisions for the lead
- Liveness check: until `profiles.is_tombstoned` lands with a known signature, `layout` checks
  `<root>/profiles/.deleted/<id>` itself (the place the phase file fixes). Swap to the profiles
  helper at merge if it takes `(root, id)`.
- Dir modes are fixed 0700 here; `INDUS_HOME_MODE` / managed marker belong to `home.secure_dir`
  (P01-03) — swap `_secure_mkdir` to it at merge.
- `bootstrap.run` exports the scratch vars into `os.environ` before building the env view, so
  the view (and every child built from it) carries them; `tempfile.tempdir` is reset so the new
  value takes effect in-process.

Compare checklist (filled after the rewrite; reopened the scratch subtree scan for the
hard-link rule):
- [x] Liveness check before the memo; missing/tombstoned named profile refused, nothing created
  (`test_layout.py::test_tombstoned_profile_refused_even_after_a_memo_hit`,
  `::test_missing_named_profile_refused_and_not_created`)
- [x] Persona seeded once, customised/empty file kept, dangling and cyclic links replaced via
  temp + rename, target never created (`::test_customised_persona_survives_later_runs`,
  `::test_dangling_persona_link_replaced_and_target_not_created`, `::test_cyclic_persona_link_replaced`)
- [x] Scratch prune: > 24 h idle removed, nested fresh write keeps an entry, links not followed,
  hourly stamp (`::test_prune_removes_entries_idle_over_a_day`, `::test_prune_does_not_follow_links`,
  `::test_prune_runs_at_most_once_an_hour`)
- [x] Gap found: a hard-linked file's fresh mtime does not count as activity — added, pinned by
  `::test_prune_ignores_fresh_mtimes_of_hard_linked_files`
- [x] Socket root falls back past 50 chars (`::test_socket_dir_falls_back_for_long_scratch_paths`)
- [x] Bootstrap: cwd shadowing removed, UTF-8 stdio, profile selection, scratch env, logging
  (`test_bootstrap.py::*`)
- Deliberate differences: the reference always strips `''`/`.` and moves its root to the front of
  `sys.path`; we strip the cwd entries only when the cwd really holds an `indus` package or
  module that is not ours. The reference forces UTF-8 only on Windows; we do it wherever a stream
  is not UTF-8. macOS sockets always go to the temp root there; ours only past the 50-char budget.
  No legacy persona-template upgrade (fresh product). The prune also runs once per process in the
  reference; we rely on the cross-process stamp alone.
- Not taken: process reaping and git-worktree release inside scratch (P05), the "stuck entry" skip
  list and prune audit log, self-update settling, launcher home pinning, Happy Eyeballs, ANSI/VT
  enabling and the `ver` stub on Windows, glibc environ guard.

## Status

Done (2026-10-18). `layout.py` and `bootstrap.py` with unit tests. Bootstrap is coded against the
phase-file names of `preparse.select_profile`/`Selection`, `envload.build_env`, `home.root_of`,
`clock.SystemClock`/`resolve_zone`, `scope.Scope`; the `Scope(...)` construction in
`make_root_scope` (in particular `SystemClock(None)` then `resolve_zone`) is the spot to adjust at
merge. Tests use the stand-ins in `tests/unit/foundation/_stubs_log.py`. Ledger rows R04.25
(layout and log parts), ACC-A5-1, ACC-I54-1 and the new ACC-I12-2 point at tests.

## Integration (lead)

- `_secure_mkdir` → `home.secure_dir(path, home=scope.home, env=scope.env)`: `INDUS_HOME_MODE` and
  managed installs are honoured (chmod errors are no longer swallowed).
- `profile_home_problem` uses `profiles.named_profile_of` / `is_tombstoned`; refusals raise
  `ProfileTombstoned` / `ProfileNotFound` (same codes as before, both `ProfileError`).
- `make_root_scope` builds the real `Scope` (no explicit `config=None`/`terminal_policy`), clock via
  `clock.system_clock_for`.
- `run` passes argv and flags to `select_profile` as given; `nested_tail_flags=None` (new default)
  keeps preparse's own default `("--args",)` (the old `()` default silently disabled it);
  `recovery_commands` is passed through.
- New `attach_profile_state(scope, env)`, called after `ensure_home` and before logging setup:
  loads config through the scope, rebuilds the env view with the config bridges, re-resolves the
  clock (config `timezone`), opens the secret store per `security.secret_store`. Needed so the log
  redactor (built from the scope given to `log.setup`) masks stored secrets and `logging.*` keys
  apply; `keychain` mode with no keychain raises `SecretStoreUnavailable` at startup.
  Decision for the lead: revert to "attach later" if P06 wants to own this.
- Done-when check: `tests/integration/foundation/test_p01_done.py` (bootstrap `-p work`, writer
  save, hand-broken file still shows `approvals.deny: [rm]` in-process and after a cache reset,
  store `OPENAI_API_KEY`, masked in `work/logs/agent.log`, nothing in the root's logs). The key
  value matches no credential shape, so only the store masks it.
