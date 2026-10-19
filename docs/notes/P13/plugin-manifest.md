# Task: Plugin manifest (P13-13) Phase: 13 Owner: wave-10 builder M2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/plugins_manifest.py`
(fields, version gate, dependency ordering, lenient field checks), research
`07-learning-skills-plugins-cron.md` §3.13.1. Closed before writing.
Evidence: none.

What it does (own words): every plugin folder carries a small description of itself, either
`plugin.yaml` (or `.yml`) or the portable `plugin.json`. We read it into one typed record. Reading
is forgiving on purpose: a typo or a field from a newer format version must never stop a plugin
from loading, so every bad field is dropped with a warning and the rest is kept. Two checks can
still hold a plugin back later, both with a plain reason: a `requires_indus` floor newer than the
running Indus, and a file that cannot be parsed at all.

Inputs → outputs: `read_manifest(folder, *, source, prefix="") -> ManifestRead` (manifest or
`None`, warnings, error); `manifest_from_mapping(data, *, fallback_name, ...)` for entry points and
tests; `version_gate(spec, running) -> str | None` (the skip reason); `satisfies(spec, version)`.
`PluginManifest` (Pydantic, frozen): name, version, description, author, license, homepage, tags,
kind (`standalone|backend|platform|exclusive`), requires_env (`str | {name, prompt, secret}`),
provides_tools, provides_hooks, capabilities, manifest_version (1|2), api_version,
requires_plugins (`{name, version?}`), requires_indus, python_dependencies, config_schema, emits,
listens, skill_namespace, update_url; plus where it came from: source, path, key, category.

Files: `src/indus/plugins/manifest.py`, `tests/unit/plugins/test_manifest.py`,
`tests/fixtures/plugins/*/plugin.{yaml,json}`.

Edge cases / failure modes:
- no manifest file → not a plugin (`None`, no warning); unreadable YAML/JSON or a top level that
  is not a mapping → `error`, the folder is listed as broken and never imported.
- missing or empty `name` → the folder name, with a warning.
- unknown field → warning naming it, value ignored; wrong type for a known field → warning, the
  field's default is used.
- `manifest_version` above 2 → warning, still read; not an integer → treated as 1 with a warning.
- unknown `kind` → `standalone` with a warning; `requires_plugins` entries may be a bare name.
- `requires_indus`: comparators `>= > <= < == !=`, comma means "and", a bare version is a floor;
  unparseable versions never block (warned).

Our design differences / decisions:
- Pydantic model, frozen; leniency lives in one field-by-field checker, not in the model.
- `requires_plugins` entries use `name` (the row's spelling); the reference used another key.
- Our version gate field is `requires_indus`; the kinds list has no `model-provider` kind —
  model providers live in their own sub-category folder (P13-14).
- Not taken: locale packs, foreign-harness folder skipping, kind sniffing from source text
  (a category folder or an explicit `kind` decides).

Tests we will write:
- unknown field warns; missing name derived from the folder with a warning; wrong-typed field
  warns and falls back; YAML and JSON forms equal; `manifest_version: 3` warns; bad kind;
  version gate table (floor, ranges, unparseable); unreadable file → error.

## Decisions for the lead
- A manifest that cannot be parsed makes the plugin "broken" (listed with the parse error, never
  imported) rather than "loaded with defaults": loading code nobody described is the riskier way.

Compare checklist (filled after the rewrite; reopened `hermes_cli/plugins_manifest.py`):
- [x] unknown fields warn, never fail (test: `test_manifest.py::test_unknown_field_warns_and_is_ignored`)
- [x] name falls back to the folder (test: `::test_missing_name_comes_from_the_folder`)
- [x] version newer than supported warns (test: `::test_newer_manifest_version_warns`)
- [x] floor above the running version → skip reason (test: `::test_version_gate_table`, `test_loader.py::test_requires_indus_above_running_skips_without_import`)
- [x] wrong-typed fields warn and fall back (test: `::test_wrong_types_fall_back_with_warnings`)
- [x] YAML and JSON equal (test: `::test_yaml_and_json_forms_are_equal`)
- [x] dependency entries accept a bare name (test: `::test_requires_plugins_shapes`)
- Deliberate differences: unparseable file → broken record instead of silently skipped; no kind
  sniffing from source; no locale packs; config-schema type checks are left to P13-22 settings.
- Not taken: portable skill-namespace hashing (our default namespace is the plugin key).

## Status

Done (2026-10-19). Shared files: none for this task.
