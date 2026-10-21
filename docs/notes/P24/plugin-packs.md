# Task: Plugin packs (P24-44) Phase: 24 Owner: wave-38 builder S1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/plugin_packs.py` (all).
Closed before writing. Research: `docs/plan/research/07-learning-skills-plugins-cron.md`
§3.13.4–3.13.5. Evidence: none.

What it does (own words): a pack is a small YAML file naming a set of plugins to install together:
`{name, description?, author?, version?, plugins: [{name, ref} | {repo, ref, subdir?}], config?:
{<plugin id>: {key: seed}}, skills?: [ids]}` (the metadata may also sit under a `pack:` mapping).
Every `ref` must be a full 40-hex commit SHA; anything else (short SHA, tag, branch) fails the
whole pack, naming the entry. `config` seeds go to `plugins.entries.<id>` but are non-secret only:
keys that look secret (`token`, `secret`, `password`, `api_key`, `private_key`, `credential`,
`auth`), the consent keys (`granted_capabilities`, `capabilities_consent`) and `allow_*` gates are
stripped at any depth with one warning each; a seed never overwrites a value the user already has.
`skills` is shown, never installed.

`indus plugins pack show <pack>` prints the review screen (name, version, author, description, each
plugin with source, pinned ref and resolution problems, the seeds, the skills, and the reminder
that capabilities are asked per plugin). `install <pack>` prints the same screen and asks one
question; there is no `--yes` and without a terminal it refuses (exit 1) after the screen. Then
each plugin goes through the normal P13 install (pinned clone, scan; `--force` only lifts a
caution scan), its `requires_env` questions, enable, and its own capability consent (a pack never
grants anything). One failing plugin does not stop the others; the summary lists failures and the
exit code is 1 when any failed. `export [--name N] [--enabled-only] [-o FILE]` writes a pack of the
installed plugins that have git provenance (a git source and a 40-hex revision); local folders are
left out with a warning comment; seeds are the non-secret parts of their config entry.

A pack file is local, or an `https://` URL fetched through the SSRF-safe client (≤ 1 MiB).

Inputs → outputs: pack file → `Pack` + warnings → review lines → per-plugin `PackResult`s and an
exit code; installed records → pack YAML.

Files: `src/indus/plugins/packs.py`, `src/indus/cli/commands/plugin_pack.py`,
`tests/unit/plugins/test_packs.py`, `tests/fixtures/plugins/packs/` (valid, short SHA, secret seed);
shared: `cli/commands/plugins.py` (adds the `pack` group), CLI contract tables.

Edge cases / failure modes:
- empty `plugins`, missing name, entry with neither `name` nor `repo`, bad `subdir` → refused.
- a bare `name` entry needs a plugin catalogue; Indus has none, so it is reported unresolved
  (install skips it and exits 1).
- seed for a plugin id the pack does not install is applied when that id gets installed only.
- non-mapping `plugins.entries.<id>` in the user config → the seed is skipped with a warning.

Our design differences / decisions:
- secret-shaped seed keys are stripped with a warning (reference refuses the whole pack); the
  plan's verify line asks for "stripped with a warning".
- no catalogue lookup for bare names (no curated plugin catalogue in Indus).
- the review screen is plain text lines (shared with any future surface).

Tests we will write:
- short SHA refused; seed `api_key` stripped with a warning (and nested, `allow_*`, consent keys);
  user value wins over a seed; non-TTY install refused after the review; one failing plugin →
  others installed, exit 1; export omits a local install and keeps a git one; review lines.

## Decisions for the lead
- Seeds: strip-with-warning rather than reject; URL packs must be `https`.

Compare checklist (filled after the rewrite; reopened `hermes_cli/plugin_packs.py`):
- [ ] SHA rule, nested forbidden keys, user values win, export provenance rule
- Deliberate differences: see above.
- Not taken: see above.

## Status

Done except the Compare step (2026-10-21): packs.py, `indus plugins pack install|show|export`, fixtures, tests. Shared: cli/commands/plugins.py (public `ask_env`/`dependencies_ok`, adds the pack group), CLI contract table. Compare checklist NOT yet walked.
