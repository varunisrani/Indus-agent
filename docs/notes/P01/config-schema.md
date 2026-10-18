# Task: config schema and defaults (P01-09) Phase: 01 Owner: S-config builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/config_defaults.py` (dumped
the top-level key tree and the leaves of the sections we model with a throwaway script; not read
linearly), `cli-config.yaml.example` (section order only). Closed before writing.
Evidence: none.

What it does (own words): the config schema is a set of Pydantic v2 models for the sections the
early phases read. The one defaults table (`DEFAULTS`, D-16.32) is generated from those models, so a
default lives in exactly one place. Sections nobody models yet are kept as raw mappings; unknown keys
inside a modelled section are kept and reported, never dropped. The same models give the type a
dotted key should be coerced to (P01-13) and render the commented example file that a fresh
`config.yaml` starts from (every leaf commented out, so nothing in it acts as a user value).

Inputs → outputs:
- models: `ConfigModel` (root) with `model`, `providers`, `agent`, `terminal`, `approvals`,
  `database`, `sessions`, `logging`, `security`, `display`, `network`, `timezone`,
  `_config_version` (field `config_version`, alias `_config_version`).
- `DEFAULTS` (read-only mapping proxy), `defaults_tree() -> dict` (deep copy), `default_for(key)`,
  `schema_for(key) -> type | None`, `validate(data) -> list[str]` (warnings: unknown keys in a
  modelled section, type errors), `render_example() -> str`, `CURRENT_VERSION = 1`.

Values (decisions given): `agent.max_turns` 150 (D-16.62), `api_max_retries` 3, `gateway_timeout`
1800, `clarify_timeout` 3600; `terminal` local / `"."` / 180; `approvals.mode` `manual` (D-16.53),
`timeout` 300, `deny` []; `database.journal_mode` wal; `sessions` 90 / false / 20000; `logging`
INFO / 5 / 3; `security.redact_secrets` true, `secret_store` auto; `display.language` en;
`network.force_ipv4` false; `timezone` "" (local); `_config_version` 1 (D-16.3).

Files: `src/indus/foundation/config/__init__.py`, `src/indus/foundation/config/schema.py`,
`tests/unit/foundation/config/test_schema.py`.

Edge cases / failure modes:
- `model` may be a string (shorthand for `model.default`) or a mapping; the default tree holds the
  mapping form (the loader canonicalises the shorthand), `schema_for("model")` is `str` so
  `config set model x` keeps the literal.
- `context_length` default is null (unknown) → `schema_for` reports `int`.
- `Literal` enums report `str`; `X | None` reports `X`; sections report `dict`; lists `list`.
- unknown top-level section → kept verbatim, no warning (later phases add models).
- the example must parse to an empty document: a comment-only file.

Our design differences / decisions:
- one defaults table generated from models (the reference keeps a hand-written dict plus a CLI
  defaults copy, R06.25 / D-16.32).
- `database.pragmas` is an empty mapping of extra pragmas; P02-02 owns the pragma policy and may
  fill or replace it.
- example text is our own prose, one short comment per section, values dumped by `yamlio`.

Tests we will write:
- defaults have `approvals.mode == "manual"`, `agent.max_turns == 150`, version 1.
- every default validates against its model (no warnings); `defaults_tree()` is a deep copy.
- unknown section survives validate/dump; unknown key in a known section warned and kept.
- `schema_for` table (`approvals.mode` str, `approvals.deny` list, `providers` dict, `agent.max_turns`
  int, `model` str, `model.context_length` int, unknown None).
- rendered example parses to nothing and uncommenting it reproduces the defaults.

## Decisions for the lead
- `database.pragmas: {}` (see above).

Compare checklist (filled after the rewrite; reopened the dumped key tree, example section order):
- [x] section order follows the reference example where sections exist (model/providers first,
  then runtime sections) (test: `tests/unit/foundation/config/test_schema.py::test_example_parses_to_nothing_and_uncomments_to_defaults`)
- Deliberate differences: `approvals.mode` manual, not smart (D-16.53); `agent.max_turns` 150, not
  null (D-16.62); version 1, not the reference's stamp (D-16.3).
- Not taken: every section the reference ships that no P01–P03 reader needs (they arrive with
  their phases; meanwhile they are kept as raw mappings).

## Status

Done (2026-10-18). `pyproject.toml` `[project].dependencies` gained `pydantic==2.13.5`.
