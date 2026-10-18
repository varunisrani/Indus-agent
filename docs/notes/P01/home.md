# Task: Home resolution (P01-03) Phase: 01 Owner: P01 S-core builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_constants.py` (lines 26–290
home override, platform default, root rule, sudo invoker; 654–675 `secure_parent_dir`; 837–880
managed marker). `website/docs/user-guide/which-file-does-what.md` not read (the research spec §3.9
already covers the layout). Closed before writing.
Evidence: none.

What it does (own words): decides which directory is "the home" for this process or task: a
context-local override first (multi-profile servers, never touches `os.environ`), then `INDUS_HOME`
with `~`/`$VAR`/`${VAR}` expanded from the given env, then the platform default (`~/.indus` or
`%LOCALAPPDATA%\indus`, plus `INDUS_DATA_DIR_SUFFIX`). Under `sudo` the default follows the invoking
user. `root_of` maps a profile home back to its root. `secure_dir`/`secure_file` apply owner-only
modes unless the install is managed; `secure_parent_dir` refuses to chmod `/` or a top-level dir,
because that only happens when the home was resolved wrongly.

Inputs → outputs: `env: Mapping[str, str]`, `user_home: Callable[[], Path]` → `Path`; mode helpers
→ side effects on the given path or `UnsafeHomeDir`.

Files: `src/indus/foundation/home.py`, `tests/unit/foundation/test_home.py`.

Edge cases / failure modes:
- override set in one task must not be visible to a sibling task (ContextVar).
- `INDUS_HOME` blank → treated as unset.
- `INDUS_HOME_MODE` invalid (`abc`, `0999`, > 0777) → 0700 and one warning.
- suffix containing a path separator → ignored with a warning.
- Windows without `LOCALAPPDATA` → `~/AppData/Local/indus`.
- sudo with `SUDO_USER=root` or an unknown user → normal default.

Our design differences / decisions:
- Root rule simplified to the phase row: `<x>/profiles/<name>` → `<x>`, else the home itself.
- Managed marker: `INDUS_MANAGED` truthy or `<home>/.managed` present.
- No `INDUS_UID`/`INDUS_GID` chown (Docker phase).
- Container check for `secure_file` is injectable and imported lazily so `preparse` stays light.

Tests we will write: table over override/env/default/suffix/Windows combos; override isolation with
`copy_context`; `~` and `$VAR` expansion; `<r>/profiles/work` → `<r>`; `INDUS_HOME_MODE=0701`;
`secure_parent_dir` refusals; sudo case with fake lookups; managed marker skips chmod.

## Decisions for the lead
- `secure_dir` also refuses top-level targets? Not done: only `secure_parent_dir` refuses, as the
  row says.

Compare checklist (filled after the rewrite; reopened `hermes_constants.py` 26–290, 654–675):
- [x] override → env → default order; override never touches env (test:
  `tests/unit/foundation/test_home.py::test_resolution_order`)
- [x] suffix appended literally (test: `test_suffix_appended`)
- [x] sudo resolves to the invoker (test: `test_sudo_uses_invoking_user`)
- [x] parent chmod refuses `/` and top-level dirs (test: `test_secure_parent_dir_refuses_top_level`)
- Deliberate differences: theirs silently returns on `/`; ours raises (`UnsafeHomeDir`) so a
  misresolved home fails loudly (phase row). Theirs computes the root against the native home too;
  ours uses only the `profiles` parent rule.
- Not taken: install-tree guard (no install tree yet), uid/gid chown.

## Status

Done (2026-10-18).
