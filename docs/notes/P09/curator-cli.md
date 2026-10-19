# Task: `indus curator` and `/curator` (P09-20) Phase: 09 Owner: builder slice F

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/curator.py` (verbs and
what each prints / refuses; argparse wiring skimmed). Evidence: none.

What it does (own words): the person's handle on the curator. `status` shows whether it is on,
paused, when it last ran and what it said, counts by state, pinned skills, the five least recently
used managed skills, and the unmanaged skills with the reason they are unmanaged ("created before
tracking" = no record / no origin field, "created in the foreground" = `origin: learn`). `run`
runs a pass now (`--dry-run` previews, `--consolidate` forces phase 2, `--background` returns at
once). `pause` / `resume` persist in the state file. `pin` / `unpin` and `adopt` change the usage
record (adopt hands a skill over without resetting its inactivity clock). `archive`, `restore`,
`list-archived`, `prune [--days N]`, `purge` (archive TTL, explicit only), `backup`, `rollback [id]
[--list] [-y]`, `ledger [--skill] [--rollback <id>]`. Without a terminal nothing is asked:
destructive verbs (`prune`, `purge`, `rollback`, `ledger --rollback`) need `-y` and exit 2
otherwise. `/curator status|run|pause|resume` in chat prints the same lines.

Inputs → outputs: argv → printed lines, exit 0 / 1 (refused or failed) / 2 (confirmation needed
without a terminal, bad usage).

Files: `src/indus/cli/commands/curator.py`, `src/indus/cli/slash/learning_cmds.py` (handlers for
`/curator` and `/journey`), `tests/unit/cli/commands/test_curator.py`; shared: `cli/app.py` (rows),
`service/commands/registry.py` (`/curator` subcommands).

Edge cases / failure modes:
- `pin` / `unpin` on a bundled or hub skill → exit 1 with the reason; on an unknown skill → exit 1.
- `adopt` on bundled, hub, external or missing skills → exit 1 with the reason; already managed →
  exit 0, says so.
- `restore` when a bundled/hub skill now owns the name, or the folder exists → exit 1.
- `archive` of a pinned skill → refused with an unpin hint.
- `purge` with `archive_ttl_days: 0` and no `--days` → prints that purge is off, changes nothing.
- `prune --days 0` → exit 2.
- `rollback` with no snapshots → exit 1 with a hint; backups disabled → exit 1.
- `ledger` when no ledger is plugged in → says so, exit 0.

Our design differences / decisions: `status` lists reasons per unmanaged skill instead of counts
only; `--background` starts a thread that keeps the scope; the slash handler offers only the four
safe verbs. No `usage` verb (P09-24's `indus skills list` covers usage).

Tests we will write: CliRunner-style `run_cli` for every verb on a fixture home; adopt on bundled
exits 1 with reason; restore blocked by a same-name bundled skill; purge with ttl 0 does nothing;
non-TTY rollback without `-y` exits 2; `/curator status` through the slash handler.

## Wiring for the lead
- Slash: add `**learning_cmds.HANDLERS` to `cli/slash/dispatch.HANDLERS` once every P09 slash
  command has a handler (the registry test's `landed` set moves to include P09 then, and
  `HELP_GROUPS` gains `curator` and `journey`). Until then `/curator` reports "arrives with P09".

## Decisions for the lead
- Slash handlers live in `cli/slash/learning_cmds.py` but are not added to `dispatch.HANDLERS`
  here (that would break the registry test for the P09 commands slices D/E own).
- "Protected" skills in `adopt`: we keep no separate protected built-in list, so `adopt` refuses
  bundled, hub, external and missing skills only. Add a list in `curator.archive` if one is needed.
- `purge` with `archive_ttl_days: 0` and no `--days` prints that purge is off and exits 0 (the
  reference exits 1); `pin`/`unpin` on an unmanaged skill are refused with an `adopt` hint (the
  plan says agent-origin only; the reference records the pin anyway).
- `indus learning` is a second registry row pointing at the journey group (shows in `--help`).

Compare checklist (filled after the rewrite; reopened `hermes_cli/curator.py`):
- [x] every verb present except `usage` (tests in `test_curator.py`)
- [x] pin refused for bundled/hub (test: `test_pin_refuses_bundled`)
- [x] adopt keeps the inactivity clock (test: `test_curator.py::test_adopt`)
- [x] purge explicit, TTL 0 = off (test: `test_purge_ttl_zero_noop`)
- [x] rollback asks first; non-TTY needs `-y` (test: `test_rollback_non_tty_needs_yes`)
- Deliberate differences: `adopt --all-unmanaged` left out (one name at a time; easy to add).
- Not taken: `usage` verb (P09-24 owns the skills listing).

## Status

Done (2026-10-19). Shared: `cli/app.py` (rows `curator`, `journey`, `learning`),
`service/commands/registry.py` (`/curator` args and subcommands),
`tests/contract/cli/test_help_catalogue.py` (golden order: also `bundles`, which slice B added
without a row; `journey`/`learning` share a module), `tests/contract/cli/non_tty_table.yaml` (rows
for the new verbs; the `bundles` rows are still missing — slice B's).

## Integration (lead, P09 wave 2 landing)
`/curator` and `/journey` are in `dispatch.HANDLERS`; `indus curator ledger` uses `ledger_port.resolve(scope)` (always the profile's ledger unless a test plugs one in). D-16.158.
