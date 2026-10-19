# Task: `indus research` CLI (P22-09) Phase: 22 Owner: wave-19 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `batch_runner.py` (`main` and the
comma-list helper). Research 10 §3.1 (reasoning switch, prefill file). Closed before writing.

What it does (own words): `indus research` is the researcher's entry: `batch` runs a dataset (P22-08),
`distributions` lists the 17 toolset distributions; `compress`, `swe` and `sample` exist so
`--help` shows the whole family but say plainly that they come with a later wave (exit 1).

Inputs → outputs: `indus research batch DATASET --run-name NAME [--batch-size N (≥1)]
[--distribution NAME] [--model M] [--base-url URL] [--max-turns N] [--workers N] [--resume]
[--new-dataset] [--seed N] [--reasoning-effort LEVEL] [--no-reasoning] [--prefill FILE]
[--ephemeral-system-prompt TEXT] [--providers-allowed CSV] [--providers-ignored CSV]
[--providers-order CSV] [--provider-sort price|throughput|latency] [--max-samples N] [--verbose]`.
Exit codes: 0 done, 1 runtime failure (bad prefill file included), 2 usage / dataset error, 130
interrupted. `indus research distributions [NAME]` prints each distribution with its description
and entries sorted high → low.

Rules: `--reasoning-effort` takes `none minimal low medium high xhigh max` (other → exit 2);
`--no-reasoning` wins and sends `none`; neither → nothing sent ("provider default" in the effective
config). `--prefill` must parse as a JSON array (else exit 1). Comma strings split and trimmed,
empty items dropped, an empty list means "not set". `--batch-size` default comes from the defaults
table (P22-08). The group and each verb load lazily.

Files: `src/indus/cli/commands/research.py`, `tests/unit/cli/commands/test_research.py`,
`src/indus/cli/app.py` (shared: one REGISTRY row), `tests/contract/cli/test_help_catalogue.py`
(shared: golden order gains `research`).

Our design differences / decisions: click group under `indus` instead of a standalone script;
kebab-case flags; no `--api-key` (keys never in argv; the profile's credentials are used); no
`--list-distributions` flag (a verb instead); `ultra` is not accepted (phase list).

Tests we will write: CliRunner: bad prefill exit 1; bad effort exit 2; `--no-reasoning` with
`--reasoning-effort high` sends `none`; routing strings trimmed; `distributions` lists 17 sorted;
later verbs answer "not yet" with exit 1; batch size < 1 exits 2.

## Decisions for the lead
- `--batch-size` default 10 (defaults table); the phase file lists it without a default.
- Later verbs exit 1 with a one-line "comes with a later wave" message.
- Layering: `indus.research` is a sibling of `indus.cli` in the import contract, so
  `cli/commands/research.py` imports it by name (`importlib`), as the app's lazy registry does.
  P22-10 (`chat --save-trajectories`) needs the same reach; the cleaner fix is a contract change
  that puts `indus.research` one layer below the surfaces (pyproject, `tests/unit/test_layers.py`,
  00-layout §1) — a lead call.

Compare checklist (filled after the rewrite; reopened `main` and the comma helper):
- [x] prefill must be a JSON array, else exit 1 (test: `tests/unit/cli/commands/test_research.py::test_bad_prefill_exits_1`)
- [x] disabled reasoning wins (test: `test_no_reasoning_wins_and_sends_none`)
- [x] bad effort refused (test: `test_bad_reasoning_effort_exits_2`; the reference exits 1, ours 2 = usage)
- [x] comma lists trimmed (test: `test_routing_strings_are_split_and_trimmed`)
- [x] distributions listed sorted (test: `test_distributions_lists_all_17_sorted`)
- Deliberate differences: no API key flag; reasoning unset sends nothing and says so (R10.4; the
  reference's help claims a default it never sends); empty comma items dropped; `ultra` not
  offered; routing goes to `extra_body.provider` as `only` / `ignore` / `order` / `sort`.
- Not taken: `--list-distributions` (a verb instead), log prefix chars.

## Status

Done (2026-10-19). Shared: `src/indus/cli/app.py` (one registry row),
`tests/contract/cli/test_help_catalogue.py` (golden order) and `tests/contract/cli/non_tty_table.yaml`
(rows for the new verbs). `indus.research` is a sibling of `indus.cli` in the layer contract, so
the command module reaches it by name (`importlib`), like the app's lazy registry.

Integrated (lead, 2026-10-19): `indus.research` moved one layer below the user surfaces
(import-linter contract in `pyproject.toml`, `tests/unit/test_layers.py`, 00-layout section 1), so
`cli/commands/research.py` imports `indus.research.batch` / `indus.research.distributions` directly
inside its verbs; the `importlib` lookup is gone. P22-10's saver can be imported the same way.
