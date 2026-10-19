# Task: Plugin discovery and the allow-list gate (P13-14) Phase: 13 Owner: wave-10 builder M2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/plugins_discovery.py`
(sources, order, category folders, collision winners, gate verdicts), research
`07-learning-skills-plugins-cron.md` §3.13.1; `website/docs/user-guide/features/plugins.md`
skimmed. Closed before writing.
Evidence: none.

What it does (own words): finds every plugin that could run for a profile and decides, without
importing anything, which ones may load. It looks in four places in a fixed order — the plugins
shipped inside Indus, the user's `<home>/plugins/`, the project's `.indus/plugins/` (only when the
user opted in, and never for the gateway), and Python packages that advertise the `indus.plugins`
entry-point group. A plugin found again later overrides the earlier copy (a user copy beats the
shipped one), except memory providers, where the first one found stays. Folders named after a
provider category route one level deeper. General plugins are only listed until the user names
them in `plugins.enabled`; `plugins.disabled` always wins.

Inputs → outputs: `discover(scope, *, bundled_dir=None, project_root=None, gateway=False,
entry_points=None) -> Discovery`. `Discovery.records` (one `PluginRecord` per winning key:
manifest, source, category, `state` enabled / disabled / not-enabled / broken, `gated`, reason),
`Discovery.loadable()` (what the general loader imports), `Discovery.category(name)`,
`Discovery.warnings`. `set_enabled(tree, key, enabled, toolsets=())` edits a config tree in place.

Files: `src/indus/plugins/discovery.py`, `src/indus/plugins/bundled/__init__.py` (the shipped
plugins folder, empty), `tests/unit/plugins/test_discovery.py`.

Edge cases / failure modes:
- a source folder missing or unreadable → skipped (unreadable warns); dunder folders skipped.
- project plugins: off unless `INDUS_ENABLE_PROJECT_PLUGINS` is truthy in the scope env or
  `plugins.project: true`; in gateway mode they are never read, one warning when the opt-in is on.
- category folders (`memory`, `image_gen`, `video_gen`, `tts`, `stt`, `web`, `browser`,
  `computer_use`, `platforms`, `context_engine`, `model_providers`, `backends`) hold plugins one
  level down, keyed `<category>/<name>`; a manifest-less folder elsewhere is ignored.
- collisions: later source wins and the override is logged; memory providers keep the first.
- gate: disabled (key or manifest name) → `disabled`; memory providers, context engines and model
  providers → specialised, never loaded here; bundled backends/platforms → load without the
  allow-list; everything else needs `plugins.enabled` (key or name) → `enabled`, else
  `not-enabled`.
- broken manifest → `broken` with the parse error; never loadable.
- entry points: listing them never imports the target; a failing lookup is skipped.

Our design differences / decisions:
- Our entry-point group `indus.plugins` (D-16.39), env `INDUS_ENABLE_PROJECT_PLUGINS`, project
  folder `.indus/plugins/` under the given project root (the session's working dir by default).
- Category set per our row (adds `backends/`, drops cron and dashboard-auth categories).
- Only listed category names recurse; the reference recursed any manifest-less folder.
- No impostor check (a user folder claiming a bundled key under another folder name) — there are
  no bundled plugins yet; noted as a follow-up for when the owner adds some.
- Not taken: discovery suppression context, catalog recall list, grandfathering migration.

Tests we will write:
- user beats bundled on the same key; memory collision keeps bundled; project ignored without
  opt-in and in gateway mode (with warning), read with env or config opt-in; disabled beats
  enabled; not-enabled listed but its module never imported (sentinel file probe); category keys;
  bundled backend bypasses the gate; entry points listed without import; `set_enabled` adds and
  removes the toolset in existing platform selections only.

## Decisions for the lead
- `plugins.project` config key added beside the env opt-in (row allows either).
- `set_enabled` edits a config tree; writing the file stays with P13-23 (`indus plugins`).

Compare checklist (filled after the rewrite; reopened `hermes_cli/plugins_discovery.py`):
- [x] order bundled → user → project → entry points; later wins (test: `test_discovery.py::test_user_copy_overrides_bundled`)
- [x] memory providers first-wins (test: `::test_memory_provider_collision_keeps_bundled`)
- [x] project opt-in and gateway refusal (test: `::test_project_plugins_need_opt_in`, `::test_gateway_never_reads_project_plugins`)
- [x] disabled wins over enabled (test: `::test_disabled_beats_enabled`)
- [x] listed-not-loaded never imported (test: `::test_not_enabled_plugin_is_listed_but_never_imported`)
- [x] bundled backends bypass the gate; user backends gated (test: `::test_bundled_backend_bypasses_gate_user_backend_does_not`)
- [x] category keys one level down (test: `::test_category_folders_route_one_level_down`)
- [x] toolset follows the toggle in saved selections (test: `::test_set_enabled_updates_saved_tool_selections`)
- Deliberate differences: only known category names recurse; enabled matched by key or manifest
  name; project root is a parameter instead of the process cwd.
- Not taken: impostor-name check, catalog kill list, relay refusal, deferred platform loaders.

## Status

Done (2026-10-19). Shared: `src/indus/foundation/config/schema.py` (`plugins` section).
