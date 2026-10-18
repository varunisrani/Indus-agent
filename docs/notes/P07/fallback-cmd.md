# Task: `indus fallback` (P07-05) Phase: 07 Owner: P07 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/subcommands/fallback.py`
(subcommands and aliases), `hermes_cli/fallback_cmd.py` (list/add/remove/clear behaviour, same-
as-primary and duplicate checks). Research `04` §3.17.4. Closed before writing.
Evidence: none.

What it does (own words): manages `fallback_providers`, the ordered list of `{provider, model}`
routes tried when the primary fails (P08 uses it). `indus fallback` / `list` / `ls` shows the
primary and the chain; entries missing a provider or a model (or not a mapping) are listed as
ignored. `add` runs the same picker as `indus model` (nothing about the primary changes) and
appends the pick; `add PROVIDER MODEL` does it without prompts. A pick equal to the primary or
already in the chain changes nothing. `remove` / `rm` picks an entry, `remove INDEX` (1-based)
removes it directly. `clear` asks before emptying, `clear -y` does not. Without a terminal,
`add` and `remove` need their arguments and `clear` needs `-y`: otherwise guidance and exit 1.

Inputs → outputs: subcommand + args, config → `fallback_providers` written through the P01
writer; lines on stdout; exit 0 / 1 (refused, unknown provider, bad index) / 2 (usage).

Files: `src/indus/cli/commands/__init__.py`, `src/indus/cli/commands/fallback.py`,
`tests/unit/cli/commands/test_fallback.py`; shared: `src/indus/cli/app.py` (registration line),
`src/indus/foundation/config/schema.py` (`fallback_providers`).

Edge cases / failure modes:
- `fallback_providers` not a list (hand-edited) → listed as one ignored value, `add` starts a new
  list (the old value is reported, not silently dropped: the user sees it was replaced);
- unknown provider name in `add PROVIDER MODEL` → exit 1 with a hint (`indus model` lists them);
- index out of range / not a number → exit 1;
- picker cancelled → "No fallback added." exit 0.

Our design differences / decisions:
- `clear` has `-y/--yes` so scripts can clear without a prompt.
- Entries keep any extra keys (`base_url`, `api_mode`) the picker adds for custom endpoints.
- Unknown subcommand answers with the shared three-line unknown-command text.

Tests we will write:
- add/remove/clear round trips through the config writer (comments survive);
- malformed entry flagged by `list`; non-TTY argument forms; same-as-primary and duplicate;
- picker-driven `add` and `remove` with a scripted driver.

## Decisions for the lead
- none beyond `clear -y`.

Compare checklist (filled after the rewrite; reopened `hermes_cli/fallback_cmd.py`,
`hermes_cli/subcommands/fallback.py`):
- [x] list default, `ls`/`rm` aliases (test:
  `tests/unit/cli/commands/test_fallback.py::test_aliases_and_default`)
- [x] add appends, same-as-primary refused, duplicate skipped (tests:
  `tests/unit/cli/commands/test_fallback.py::test_add_remove_clear_round_trip`,
  `tests/unit/cli/commands/test_fallback.py::test_add_same_as_primary_or_duplicate`)
- [x] add through the picker never changes the primary (test:
  `tests/unit/cli/commands/test_fallback.py::test_add_with_picker_keeps_primary`)
- [x] remove picks, cancel = no change (test:
  `tests/unit/cli/commands/test_fallback.py::test_remove_with_picker`)
- [x] clear asks; "no" = no change (test:
  `tests/unit/cli/commands/test_fallback.py::test_clear_asks_on_a_terminal`)
- [x] empty chain messages (test: `tests/unit/cli/commands/test_fallback.py::test_aliases_and_default`)
- Deliberate differences: `clear -y`; argument forms for scripts; malformed entries reported.
- Not taken: migrating a legacy single `fallback_model` key (we never had one).

## Status

Done (2026-10-18). Shared files: `cli/app.py`, `foundation/config/schema.py`.

## Integration (lead)

- `fallback add` through the picker now meets the OpenRouter "Paste an API key" / "Sign in"
  question (P07-10 key mint registered in the picker); the test script answers it.
