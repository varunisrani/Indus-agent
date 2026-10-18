# Task: `.env` precedence into the scope (P01-15) Phase: 01 Owner: P01 S-env builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/env_loader.py` lines
90–176 (which keys get cleared when the file stops defining them), 328–420 (loading one file with
override), 560–590 (re-asserting the terminal bridge). Research `04-cli-config.md` §3.14.1, startup
steps 5–6, traps R04.11, R04.13. Closed before writing.
Evidence: none.

What it does (own words): builds the environment a profile sees, as a value, without touching the
process environment. Start from what the process inherited, lay the profile's `.env` over it (the
file wins, so a stale shell export cannot shadow a key the user just changed), then drop the keys we
manage that the file no longer defines (otherwise a key removed from `.env` stays alive through the
inherited env until restart). Two config flags are bridged here, at build time: the redaction switch
goes into `INDUS_REDACT_SECRETS` only if nothing set it already (R04.13), and the explicit `terminal.*`
config keys are written over whatever `.env` said, on every build, so a stray `INDUS_TERMINAL_*`
line cannot switch the backend of a running process on reload (R04.11). The CLI entry in
single-profile mode calls `publish_to_process(view)` once; multi-profile hosts keep one view per scope.

Inputs → outputs: `build_env(home, inherited, *, config=None, managed_keys=()) -> EnvView`;
`publish_to_process(view, environ=os.environ)`; `register_managed_keys(*names)` (registry hook for
P03 provider key names); `terminal_bridge(config) -> dict`; `force_ipv4(config) -> bool`;
`redaction_flag(env) -> bool` (the one parser for the switch, used by P01-17).

Files: `src/indus/foundation/envload.py`, `tests/unit/foundation/test_envload.py`.

Edge cases / failure modes:
- No `.env`: nothing from the file, and only names recorded in `.env.keys` (written by us) are
  cleared; registry names are left to the shell (a bare profile still works with exported keys).
- `.env` sets `INDUS_HOME` / `INDUS_PROFILE`: ignored with a warning — the home is already chosen.
- Managed names are never location names (they are never cleared).
- Config missing or not a mapping: no bridges. Non-scalar `terminal.*` values are skipped.
- Unreadable `.env` (permission): the reader's `OSError` is logged and the file is treated as empty.

Our design differences / decisions:
- Pure function returning a view instead of in-place `os.environ` edits; no import-time snapshot.
- Terminal bridge names: `terminal.<key>` → `INDUS_TERMINAL_<KEY>` (bools as `true`/`false`).
- `network.force_ipv4` is "noted": exposed through `force_ipv4(config)` for the caller, not put in env.
- External secret managers (I19) and the admin-managed `.env` (I20) are not here (P3); the hook is
  `managed_keys` / `register_managed_keys`.

Tests we will write: shell old / file new → new; removed key cleared (recorded and registry); bare
profile keeps shell keys; `os.environ` untouched; redaction bridge set when unset and left when set;
terminal bridge beats `.env` and is re-applied on a rebuild; location names ignored; publish makes
a mapping match the view.

## Decisions for the lead
- `EnvView` is constructed as `EnvView(mapping)` (one positional mapping). If P01-06 lands another
  constructor, only `_make_view` in `envload.py` changes.
- Registry starts empty (the reference's list is narrow: keys that switch provider routes). P03 adds
  provider key names through `register_managed_keys`.

Compare checklist (filled after the rewrite; reopened `hermes_cli/env_loader.py` 90–176, 328–420, 560–590):
- [x] override load (test: `tests/unit/foundation/test_envload.py::test_file_beats_shell`)
- [x] removed keys cleared, not when the file is absent for registry names (tests: `::test_removed_recorded_key_is_cleared`, `::test_registry_key_absent_from_file_is_cleared`, `::test_bare_profile_keeps_shell_registry_keys`)
- [x] terminal bridge after every reload (test: `::test_terminal_bridge_reapplied_on_rebuild`)
- [x] redaction bridge only when unset (tests: `::test_redaction_flag_bridged_when_unset`, `::test_redaction_flag_left_when_set`)
- Deliberate differences: no in-place env mutation; clearing also covers names we wrote (`.env.keys`).
- Not taken: `.op.env`, install-dir `.env` fallback, managed `.env`, external sources (I19/I20, P3).

## Status

Done (2026-10-18). `EnvView` comes from P01-06 (core slice), stand-in in tests until it lands.

## Integration (lead)

- `_make_view(inherited, values)` builds the real `EnvView(base, overlay)`: `inherited` is the
  snapshot, and the overlay holds what `.env` and the config bridges changed (`None` for a name
  cleared). The merged mapping is what it was; new test
  `test_view_keeps_inherited_as_base_and_changes_as_overlay`.
- `bootstrap.attach_profile_state` now calls `build_env(home, env, config=view.as_dict())`, so the
  `terminal.*` and `security.redact_secrets` bridges apply to the root scope.
- Tests no longer import `_stubs`.
