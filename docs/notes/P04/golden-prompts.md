# Task: Golden prompt-surface fixtures (P04-22) Phase: 04 Owner: wave-6a builder Y

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none. The task row allows only the
idea of a per-surface golden review from `docs/plan/research/01-agent-core.md` §3.4 ("prompt-surface
review", ours); the reference's fixture files were deliberately not opened, so no fixture text,
surface list or file layout of theirs can leak in. Also read (ours): `docs/notes/P04/prompt-builder.md`,
`prompt-lifecycle.md`, `definitions.md`, `toolsets.md`, the landed `core/prompt/*`,
`tools/{definitions,toolsets,registry}.py`, `tests/fakes/model/server.py`.
Evidence: none.

What it does (own words): the system prompt and the tool array are the bytes every provider prefix
cache keys on (trap R01.1). A golden test renders both, for every surface known at P04, from a
fully pinned input (clock, home, working directory, runtime facts, fake-model route, no config,
no files on disk) and compares them with checked-in files byte for byte. Any change to prompt
prose, tier order, tool schemas or their ordering then shows up as a reviewed diff instead of a
silent cache-busting drift. A second test pins the M1 criterion: a prompt built, saved to the real
P02 store, and restored by a fresh lifecycle over a reopened store is the same bytes as the golden
(and the tool array re-derived from a fresh registry is the same JSON).

Inputs → outputs: surface spec (`name`, `platform`, platform bundle, context-file switches) →
`tests/golden/prompt_surface/<surface>/prompt.txt` (prompt text + one trailing newline) and
`tools.json` (the definitions array, `json.dumps(indent=2, sort_keys=True)` + newline).

Surfaces at P04 and what pins each:

| Surface | platform | bundle | project files | persona alone |
|---|---|---|---|---|
| `cli` | `cli` | `indus-cli` | on | — |
| `cron` | `cron` | `indus-cron` | off | on (cron keeps the persona, never reads the cwd) |
| `api_server` | `api_server` | `indus-api-server` | on | — |
| `acp` | `acp` | `indus-acp` | on | — |

Pinned inputs: `FakeClock` at 2026-03-14 10:30 UTC, zone `UTC` via the scope env, conversation
start = clock start; home `/home/golden/.indus` (default profile, so root = home); cwd and runtime
block `/home/golden/project` with host `Linux` and home `/home/golden` (an explicit `RuntimeInfo`,
so the build machine's OS never shows); model/provider from `fake_route(FakeModelServer(...))`;
`scope.config` = an empty mapping view (all defaults; never the developer's config file); a reader
that finds no file at all, so persona and project context are always absent whatever is on disk;
no install-tree path. Tools: a fresh `ToolRegistry`, `load_builtins` (the explicit list), then
`toolsets.select([bundle], platform=…)` and `definitions(registry, names, route)`; the prompt's
`tool_names` are the names that survived into the array (what the model really gets).

Files: `tests/golden/__init__.py`, `tests/golden/conftest.py` (the `--update-golden` option),
`tests/golden/prompt_surface/__init__.py`, `generate.py`, `test_prompt_surface.py`, and the eight
fixtures `{cli,cron,api_server,acp}/{prompt.txt,tools.json}`.

Edge cases / failure modes:
- stale fixture → the test fails and prints a unified diff plus the command to regenerate.
- fixture missing → fails the same way (diff against empty) unless `--update-golden`.
- `--update-golden` rewrites only files whose bytes changed and still fails nothing; the run says
  which files it wrote so the change lands with a note.
- other processes: `python -m tests.golden.prompt_surface.generate --check` in a subprocess with a
  different hash seed must agree byte for byte (set ordering, memo state, import order).
- surface switch is not identity: the resumed prompt is restored for the same surface only.
- P05 tools are not registered yet (`BUILTIN_MODULES` is empty), so every `tools.json` is `[]` and
  no prompt carries tool guidance. **When P05 lands its tools, regenerate the goldens with
  `--update-golden` and note the reason** (expected: tool arrays fill in, the tool-guidance blocks
  appear in `cli`/`cron`/`api_server`/`acp`).

Our design differences / decisions:
- Surface names are the platform names our builder knows (`cli`, `cron`, `api_server`) plus `acp`
  (no built-in hint, so its prompt has none); later phases add rows to `SURFACES`.
- The generator is importable (`render(surface)`) and runnable (`--check` / `--update-golden`), so
  the test, the subprocess check and a human use one code path.
- The fixture is rendered through `PromptBuilder` directly; the resume test goes through
  `PromptLifecycle` + `SessionPrompts` over a real store opened from the test's isolated home
  (the store home is separate from the pinned prompt home; the prompt never names it).

Tests we will write (`tests/golden/prompt_surface/test_prompt_surface.py`):
- per surface: prompt and tools equal the fixture (diff on failure; rewrite under the flag);
- a stale fixture yields a readable unified diff (checks the diff helper on a changed copy);
- per surface: build → save in the P02 store → close → reopen → fresh lifecycle restores without
  building → bytes equal the fixture; tool array from a fresh registry equals `tools.json`;
- two processes: the generator's `--check` in a subprocess exits 0;
- rendering twice in one process gives the same bytes (memo reuse cannot change output).

## Decisions for the lead
- Fixture JSON layout: `indent=2, sort_keys=True`, LF, trailing newline (stable diffs); the prompt
  file is the exact prompt plus one newline (the test strips exactly that one).
- `--update-golden` lives in `tests/golden/conftest.py`; pytest only knows it when the run names a
  path under `tests/golden` (pytest reads options from conftests on the command-line paths), so the
  regeneration command is `uv run pytest tests/golden/prompt_surface --update-golden`, or
  `uv run python -m tests.golden.prompt_surface.generate --update-golden`.

Compare checklist (filled after the rewrite; no reference file reopened — none was read):
- [x] every surface renders and matches (test: `test_prompt_surface.py::test_surface_matches_golden`)
- [x] stale fixture prints a diff (test: `::test_stale_fixture_reports_a_diff`)
- [x] save/resume byte-equal with the real store (test: `::test_save_and_resume_keep_the_bytes`)
- [x] identical across processes (test: `::test_another_process_renders_the_same_bytes`)
- [x] repeat render in one process is stable (test: `::test_rendering_twice_is_stable`)
- Deliberate differences: no surface beyond the four P04 knows; no pinned tool array in the store
  (the lifecycle does not pin it, P04-07 note) — the array is re-derived and compared instead.
- Not taken: messaging surfaces, desktop/tui surfaces (their phases add rows).

## Status

Done (2026-10-18). Goldens hold `[]` tool arrays until P05 registers tools; regenerate then.

## Regenerations
- 2026-10-18 (Agent 2, P05 wave 1): P05-23/24 registered `web_extract`, `todo_list`, `clarify`
  (and `web_search`, hidden here because its probe needs the optional `web` extra). The prompt
  gained the matching tool-guidance lines. Regenerated with `--update-golden`; diff reviewed.
- 2026-10-18 (lead integration, P05 wave 2): P05-11/14/16/18/19/21 registered `terminal`,
  `process_manage`, `read_file`, `write_file`, `patch` and `search_files`, so every surface's
  `tools.json` gained their schemas (`patch` shows the replace shape; V4A `mode: patch` is accepted
  by the handler but not shown, see `docs/notes/P05/v4a-lint.md`). `web_search` now appears too:
  the generator's registry lets every tool probe pass (`_ProbesPass`), because which optional
  extras (`web`, `docs`, `pty`) or host binaries are installed is a fact about the machine — CI
  runs a plain `uv sync --locked`, the P05 loop `uv sync --all-extras`, and both must render the
  same bytes (checked with `ddgs` hidden). `prompt.txt` is unchanged (no guidance lines for these
  tools yet). Regenerated once with `--update-golden`; diff reviewed (912 added lines over four
  `tools.json`, nothing removed).
- 2026-10-19 (P10 wave 1, slice I): P10-12..14 registered the `browser` toolset (12 tools:
  `browser_navigate` … `browser_dialog`) and added it to `CORE_TOOLS`, so every surface's
  `tools.json` gained their schemas (`browser_cdp` shows because the generator lets every probe
  pass; live, it needs a reachable CDP endpoint). `prompt.txt` is unchanged. Regenerated once with
  `--update-golden`; only additions. Slices G and H regenerate for their toolsets too — the lead
  regenerates once after merging.
- 2026-10-19 (P10 slice J, media tools): P10-15..17 registered `vision_analyze`,
  `image_generate` and `text_to_speech` and added them to the core list, so `cli` and `cron` gained
  all three schemas, `api_server` the first two (no speech there) and `acp` only
  `vision_analyze` (the coding posture leaves out image and speech tools). `image_generate`'s
  description and optional parameters come from the active provider's capabilities (the built-in
  OpenAI-compatible images provider under the stock config). The video tools are absent: no video
  provider is registered in a stock build. `prompt.txt` is unchanged. Regenerated with
  `--update-golden`; diff reviewed (255 added lines over four `tools.json`, nothing removed).
- 2026-10-19 (P14 slice C2, cron runner): P14-11 takes `clarify` out of the `indus-cron` bundle
  (a scheduled run has nobody to answer a question), so `cron/tools.json` lost the `clarify`
  schema. `prompt.txt` is unchanged. Regenerated with `--update-golden`; diff reviewed (36 lines
  removed from `cron/tools.json`, nothing else changed).
- 2026-10-21 (P24 integration, browser tools): P24-39 `browser_exec` and P24-40 `browser_vault`
  are registered by the browser module and listed in `BROWSER_TOOLS`, so all four surfaces gained
  both schemas (probes pass here; live, `browser_exec` needs the browser CLI and `browser_vault` a
  configured vault source). `prompt.txt` is unchanged. Regenerated with `--update-golden`; diff
  reviewed (76 added lines per `tools.json`, nothing removed).
