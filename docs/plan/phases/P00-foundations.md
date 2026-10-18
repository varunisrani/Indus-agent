# P00 · Foundations

**Weeks:** 1 (week 1; 7 of 14 tasks already landed) · **Tracks:** backend (repo, guards, Python tooling, fakes, async trial), frontend (pnpm workspace, `@indus/ui` tokens) · **Depends on:** — · **Status:** 🔨 in progress (P00-01 … P00-07 ✅ done, landed in `c790e93`)

## Goal
A repository where every later task can run one loop and trust it: Python and TypeScript both lint,
format, type-check and test in CI; the clean-room guards (similarity, banned names, assets,
licences, ledgers, PR body) and the repo guards (dependency pins, layer imports, locale parity)
fail the build on a seeded violation; one aggregate CI gate treats a skipped job as a failure;
fakes and an injectable clock exist so no test ever sleeps; `@indus/ui` carries our own design
tokens; and the D-04.1 asyncio trial has its harness and first two measurements. It proves that the
build machinery, not the product, is ready (milestone M0 together with P01–P02).

## Tasks (14)
| ID | Task | Read in reference | New files | Verify |
|---|---|---|---|---|
| P00-01 | **Status: done.** Similarity guard: `python -m tooling.checks similarity` indexes the pinned reference (tokens / lines / prose detectors; gates 50 tokens, 6 meaningful lines, 12 prose words), caches the index per reference commit and checker hash, verifies every hash hit against the real reference text, supports `--closest N` reports and an allow-list with mandatory reasons. Design in `docs/notes/P00/guards.md`. | — (reference only listed, never read for content) | `tooling/checks/similarity.py`, `tooling/checks/simtext.py`, `tooling/checks/similarity_allow.json`, `tooling/checks/tests/test_similarity.py` | Landed: synthetic clean file passes; 60-token slice of a reference `.py` and 12-word run of a reference `.md` copied at test time are caught (`ref` marker). |
| P00-02 | **Status: done.** Banned-identifier and asset guards: `banned` scans contents and paths under `src/ apps/ scripts/ tests/ tooling/` with the patterns in `banned_patterns.json` (17 §d) and prints the replacement hint; `assets` compares SHA-256 of media files with the reference's. | — | `tooling/checks/banned.py`, `tooling/checks/banned_patterns.json`, `tooling/checks/assets.py`, `tooling/checks/tests/test_banned.py`, `tooling/checks/tests/test_assets.py` | Landed: each pattern fires on a synthetic hit and a path hit; identical asset bytes fail; root notice files exempt. |
| P00-03 | **Status: done.** Licence guard: runtime deps from `pyproject.toml` plus transitive `Requires-Dist` (and `apps/*/package.json`) must be on `licenses_allow.json` (MIT, BSD-2/3, ISC, Apache-2.0, PSF-2.0, Python-2.0, MPL-2.0, Unlicense, 0BSD, CC0-1.0) or carry a written exception. | — | `tooling/checks/licenses.py`, `tooling/checks/licenses_allow.json`, `tooling/checks/tests/test_licenses.py` | Landed: a GPL dep fails; an exception with a reason passes; empty runtime deps pass. |
| P00-04 | **Status: done.** Ledger guards: `traps` (every `R<nn>.<n>` / `T<nn>.<n>` defined in a plan "traps" section has a row in `docs/internals/traps-ledger.md` with a test ref or `Review:` line; test refs must exist, checked with `ast`) and `acceptance` (every P0/P1 inventory feature has an `ACC-<feature>-<n>` row; `--strict` fails on Review rows); both have `--write-skeleton`. | — | `tooling/checks/traps.py`, `tooling/checks/acceptance.py`, `tooling/checks/ledger.py`, `tooling/checks/tests/test_ledgers.py`, `tooling/checks/tests/fixtures/ledger_repo/…`, `docs/internals/traps-ledger.md` | Landed: missing row fails; Review row passes; nonexistent `path::test` fails. |
| P00-05 | **Status: done.** PR-body guard and runner: `prbody` requires `Reference:` and `Task note:` lines (body from `--body-file`, `PR_BODY` or the event file); `python -m tooling.checks [--require-ref] [--json]` runs every check, keeps going, prints a table and exits with the worst code (0 clean, 1 violation, 2 setup problem). | — | `tooling/checks/prbody.py`, `tooling/checks/__main__.py`, `tooling/checks/common.py`, `tooling/checks/reference.json`, `tooling/checks/tests/test_prbody.py`, `tooling/checks/tests/test_runner.py`, `tooling/checks/tests/conftest.py` | Landed: missing line fails; local run without a body is skipped with a note; `--require-ref` without a reference exits 2. |
| P00-06 | **Status: done.** Python scaffold and CI: `pyproject.toml` (Python ≥3.13, hatchling, src layout, script `indus = indus.cli:main`, dev group pytest + ruff, ruff config, pytest markers `slow`/`ref`/`network`, `--strict-markers`), `uv.lock`, `.python-version` 3.13, placeholder CLI with `--version`, smoke tests, `ci.yml` (uv sync --locked, ruff, format, pytest, guards with the reference fetched at the pinned SHA outside the workspace, PR-body step), `LICENSE` (MIT, owner decision round 3), `THIRD_PARTY_NOTICES.md` (reference MIT text verbatim, allow-listed), `README.md`, `.editorconfig`, `.gitignore`. | `LICENSE` [21: reproduced in notices only] | `pyproject.toml`, `uv.lock`, `.python-version`, `src/indus/__init__.py`, `src/indus/__main__.py`, `src/indus/cli.py`, `tests/test_smoke.py`, `.github/workflows/ci.yml`, `LICENSE`, `THIRD_PARTY_NOTICES.md`, `README.md`, `.editorconfig`, `.gitignore` | Landed: `indus --version` prints `indus 0.0.1`; `python -m indus --version` works; CI green on `main`. |
| P00-07 | **Status: done.** Rules and two-agent mailbox: `AGENTS.md` (layout, reference handling, Read → Note → Rewrite → Compare → Check, names, commands, commits, definition of done), `CLAUDE.md` pointer, `HANDOFF.md`, `.agents/` (turn lock, per-agent state, usage logs, sync and wait scripts), PR template, `docs/notes/README.md` + `_template.md`. | — | `AGENTS.md`, `CLAUDE.md`, `HANDOFF.md`, `.agents/turn.json`, `.agents/agent-1.json`, `.agents/agent-2.json`, `.agents/usage-log-agent-1.md`, `.agents/usage-log-agent-2.md`, `.agents/agent-sync.sh`, `.agents/wait-for-turn.sh`, `.github/pull_request_template.md`, `docs/notes/README.md`, `docs/notes/_template.md` | Landed: files present; PR template lines satisfy `prbody`. |
| P00-08 | **pnpm workspace and TS skeleton.** Root `package.json` (`"private": true`, `"type": "module"`, `"packageManager": "pnpm@12.9.1"`, `engines.node` `>=24.21.0 <25`, scripts `lint` = `biome check .`, `fmt` = `biome format --write .`, `fmt:check` = `biome format .`, `typecheck` = `pnpm -r --if-present typecheck`, `test` = `vitest run`); `pnpm-workspace.yaml` with `packages: [apps/*, packages/*]`, `minimumReleaseAge: 20160` (14 days, minutes) and `onlyBuiltDependencies: []` (no install scripts allowed by default); `.nvmrc` `24.21.0`; `biome.json` (formatter: 2-space indent, line width 100, double quotes; linter recommended rules plus `noExplicitAny` error, `noConsole` error outside `scripts/`; ignore `dist/`, `node_modules/`, `packages/protocol/src/generated/`); `tsconfig.base.json` (target ES2023, module/moduleResolution `NodeNext`, `strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `verbatimModuleSyntax`, `isolatedModules`, `skipLibCheck` false); `vitest.config.ts` with `test.projects: ["packages/*", "apps/*"]`, `passWithNoTests: false`; first package `packages/ui` (`@indus/ui`, `exports` → `./src/index.ts`, script `typecheck` = `tsc --noEmit -p .`) whose `src/index.ts` exports a `version` constant and one test; and the skeleton of `packages/protocol` (`@indus/protocol`, `package.json` with `exports` → `./src/index.ts`, `tsconfig.json`, `src/index.ts` exporting an empty `PROTOCOL_VERSION = 0` placeholder that P04's generator overwrites, `src/generated/.gitkeep`). All dev deps exact pins from 00-layout §4. Extend the licence guard to read `packages/*/package.json` and the root `package.json` (dependencies only; devDependencies reported, not gated). CI: new `ts` job — setup Node from `.nvmrc`, `corepack enable`, `pnpm install --frozen-lockfile`, `pnpm lint`, `pnpm fmt:check`, `pnpm typecheck`, `pnpm test`. | `package.json` [91: workspaces, engines, allow-scripts idea only], `tests-js/node-engine-alignment.test.ts` [66: what it guards, names only] | `package.json`, `pnpm-workspace.yaml`, `pnpm-lock.yaml`, `.nvmrc`, `biome.json`, `tsconfig.base.json`, `vitest.config.ts`, `packages/ui/package.json`, `packages/ui/tsconfig.json`, `packages/ui/src/index.ts`, `packages/ui/src/index.test.ts`, `packages/protocol/package.json`, `packages/protocol/tsconfig.json`, `packages/protocol/src/index.ts`, `packages/protocol/src/generated/.gitkeep`, `tooling/checks/tests/test_licenses_packages.py` | `pnpm install --frozen-lockfile` from clean works; `pnpm lint`, `fmt:check`, `typecheck`, `test` green; a `^1.2.3` range in a package fails P00-09; a seeded `any` fails Biome; licence test: a `packages/x/package.json` with a GPL runtime dep fails, a GPL devDependency only notes; Node engine in `package.json` equals `.nvmrc` major (unit test in `index.test.ts` reading both files). |
| P00-09 | **Pin policy guard and aggregate CI gate.** New check `pins` (D-16.52, enforced literally, R10.11): (1) every `[project].dependencies`, optional-dependency and `[dependency-groups]` entry is `name==X.Y[.Z]` (extras allowed, markers forbidden except `sys_platform`/`platform_machine`, R10.12) unless it sits on `pins_allow.json` with a reason (bounded range `>=a,<b` only); (2) `[tool.uv] exclude-newer` exists and is an RFC 3339 timestamp at least 14 days before the newest `uv.lock` change date we record in `pins_meta.json` (the lead bumps both together); (3) every `package.json` (`dependencies`, `devDependencies`, `optionalDependencies`, `peerDependencies` exempt) uses an exact `X.Y.Z` (no `^ ~ * x latest >=`; `workspace:*` allowed for `@indus/*`); (4) `pnpm-workspace.yaml` has `minimumReleaseAge >= 20160`; (5) any git source (`git+…`, `{ git = … }`) pins a 40-hex `rev`. Violations print file, entry and the fix. Register in the runner. CI: add `requires-python` 3.14 lane (`uv run --python 3.14 pytest -q`), and a final `gate` job (`needs:` every job, `if: always()`) that fails when any needed job's result is not `success` — `skipped` and `cancelled` count as failures (R10.18). On pull requests every guard reads its policy files (`*_allow.json`, `banned_patterns.json`, `pins_meta.json`, `reference.json`) from the **base branch**, never from the PR head, so a PR cannot relax the check that judges it (R10.22); a policy change takes effect from the next PR after it lands. | `.github/workflows/ci.yaml` [585: the aggregate gate job and how needs/results are read; names only], `scripts/gen-bootstrap-pins.py` [measure only] | `tooling/checks/pins.py`, `tooling/checks/pins_allow.json`, `tooling/checks/pins_meta.json`, `tooling/checks/tests/test_pins.py` | Synthetic repos in `tmp_path`: `httpx>=0.27` fails, `httpx==0.28.1` passes, `foo>=1,<2` passes only with an allow entry carrying a reason (missing reason → exit 2), marker `python_version>='3.14'` fails, missing `exclude-newer` fails, a window of 3 days fails, `^1.0.0` in any `package.json` fails, `workspace:*` passes, `minimumReleaseAge: 1440` fails, git dep with a branch name fails and with a 40-hex SHA passes; real repo passes. CI gate: a workflow unit test parses `ci.yml` and asserts `gate.needs` lists every other job and `if: always()` is set. |
| P00-10 | **Layer contract and type checking.** Create the empty layer packages (`__init__.py` with a one-line docstring each) for `platform, foundation, protocol, state, providers, safety, pm, tools, backends, core, learning, plugins, mcp, cron, board, service, tui_bridge, gateway, web, acp, mcp_serve, research` plus `src/indus/py.typed` (`pm` sits on the providers/safety layer and `board` beside `cron`, per 00-layout §1; later phases fill these packages and never re-create them). Add `[tool.importlinter]` with `root_package = "indus"` and one `layers` contract in the order of 00-layout §1 (siblings on one layer separated by `|` as independent), `indus.cli` as the top layer (still a module). Add `[tool.pyright]`: `pythonVersion = "3.13"`, `include = ["src", "tests", "tooling"]`, `typeCheckingMode = "basic"`, `strict = ["src/indus/foundation", "src/indus/protocol", "src/indus/state", "src/indus/service", "src/indus/core"]`, `reportMissingTypeStubs = false`. Set `requires-python = ">=3.13,<3.15"` (architecture D-04 #1). Dev deps `import-linter==2.15`, `pyright==1.1.414`. CI steps `uv run lint-imports` and `uv run pyright` in the checks job. A ruff `per-file-ignores` entry must not be used to silence pyright findings. | — (our own contract; architecture §2) | `src/indus/platform/__init__.py`, `src/indus/foundation/__init__.py`, `src/indus/protocol/__init__.py`, `src/indus/state/__init__.py`, `src/indus/providers/__init__.py`, `src/indus/safety/__init__.py`, `src/indus/tools/__init__.py`, `src/indus/backends/__init__.py`, `src/indus/core/__init__.py`, `src/indus/learning/__init__.py`, `src/indus/plugins/__init__.py`, `src/indus/mcp/__init__.py`, `src/indus/cron/__init__.py`, `src/indus/service/__init__.py`, `src/indus/tui_bridge/__init__.py`, `src/indus/gateway/__init__.py`, `src/indus/web/__init__.py`, `src/indus/acp/__init__.py`, `src/indus/mcp_serve/__init__.py`, `src/indus/research/__init__.py`, `src/indus/pm/__init__.py`, `src/indus/board/__init__.py`, `src/indus/py.typed`, `tests/unit/test_layers.py` | `lint-imports` green on the repo; `test_layers.py` writes a temp copy of the package skeleton where `indus/foundation/x.py` imports `indus.core` and runs import-linter's API on it → the contract is broken (seeded violation, M0); a reverse import (`core` → `foundation`) is kept; `pyright` green; a deliberately untyped function in a strict package fails pyright (test runs `pyright --outputjson` on a temp file under a strict path, `slow`). |
| P00-11 | **Fakes skeleton, clock fake and the no-sleep rule.** `tests/fakes/clock.py`: `FakeClock(start_wall: datetime aware UTC, start_mono=0.0)` with `now()`, `monotonic()`, `async sleep(s)` that parks the caller until `advance(s)` moves virtual time past its deadline (waiters released in deadline order, ties FIFO), `advance()` returns the number released, `run_until_idle()` helper for asyncio; sync `sleep` raises unless `advance` is driven from another thread (documented). `tests/fakes/model/__init__.py`: the script vocabulary only — dataclasses `Text(chunks)`, `Reasoning(chunks)`, `ToolCall(name, arguments, id=None, fragments=3)`, `Usage(prompt, completion, cached=0)`, `Fail(status, body, headers)`, `Stall(seconds)`, `Disconnect()`, `Finish(reason)`, `Turn(steps)` and `Script(turns)`, plus `load_script(path)` reading YAML/JSON from `tests/fixtures/model_scripts/`; wire servers come in P03-18. `tests/fakes/platform/__init__.py`: `FakePlatform` recording outbound sends (chat id, text, attachments, edits) and scripted inbound events; `tests/fakes/backend/__init__.py`: `FakeBackend` returning scripted `(exit_code, stdout, stderr, duration)` per command pattern and recording argv, cwd and the env it was given. `tests/conftest.py`: autouse fixture setting `INDUS_HOME` to a fresh `tmp_path / "home"` and clearing every `INDUS_*` var; fixtures `fake_clock`, `fake_model_script`. Ruff: enable `TID251` with banned APIs `time.sleep` and `asyncio.sleep` ("inject a Clock"), per-file ignore only for `src/indus/foundation/clock.py` (P01-19) and `tests/fakes/clock.py`. Dev deps `pytest-asyncio==1.4.0` (`asyncio_mode = "strict"`), `hypothesis==6.168.3` (profile `ci`: `deadline=None`, `derandomize=True`). | `tests/fakes/fake_llm_provider.py` [463: skim for which behaviours a fake must script], `tests/conftest.py` [1533: lines 1–120 only, the home-isolation idea] | `tests/conftest.py`, `tests/fakes/__init__.py`, `tests/fakes/clock.py`, `tests/fakes/model/__init__.py`, `tests/fakes/platform/__init__.py`, `tests/fakes/backend/__init__.py`, `tests/unit/fakes/test_clock.py`, `tests/unit/fakes/test_model_script.py`, `tests/unit/fakes/test_platform_backend.py`, `tests/fixtures/model_scripts/hello.yaml` | Clock: three sleepers (5, 1, 1 s) wake in order 1,1,5 after `advance(5)`; `advance(0.5)` wakes none; cancelling a sleeper removes it; `now()` moves with `advance`. Script: `hello.yaml` loads to one `Turn(Text)`; an unknown step kind is a clear error naming the line. Platform/backend fakes record and replay. Lint: a test file calling `time.sleep(1)` is flagged by ruff `TID251` (test runs ruff on a temp file). Autouse home fixture: `os.environ["INDUS_HOME"]` is under `tmp_path`. |
| P00-12 | **Brand tokens for `@indus/ui`.** Our own palette (no reference colours, fonts, theme names; R09.22): named ramps `river` (blue-teal, primary), `silt` (warm neutrals), `clay` (terracotta accent), `reed` (green, success), `ochre` (warning), `ember` (danger), 11 steps each (50…950) as OKLCH values with hex fallbacks; semantic tokens (`bg`, `bg-raised`, `fg`, `fg-muted`, `border`, `accent`, `accent-fg`, `success`, `warning`, `danger`, `focus-ring`, `code-bg`, `selection`) for themes `indus-light` and `indus-dark`; type scale (12/13/14/16/18/22/28/36 px, line heights), font stacks by family name with system fallbacks (`"Inter", system-ui, …`; mono `"JetBrains Mono", ui-monospace, …`; no font files), spacing (4 px base, 0–16 steps), radii (0, 4, 6, 10, 16, full), shadows (3 levels), motion (durations 120/200/320 ms, two easings; `prefers-reduced-motion` → 0). Export typed objects plus `tokensToCss(theme)` producing CSS custom properties `--indus-*`; terminal ANSI 16-colour map per theme for the TUI and xterm (P12, P18). Update `packages/ui/src/index.ts` to export them. | `web/src/themes/types.ts` [208: token categories only, to see which surfaces need values] | `packages/ui/src/tokens/palette.ts`, `packages/ui/src/tokens/semantic.ts`, `packages/ui/src/tokens/type.ts`, `packages/ui/src/tokens/space.ts`, `packages/ui/src/tokens/motion.ts`, `packages/ui/src/tokens/ansi.ts`, `packages/ui/src/tokens/css.ts`, `packages/ui/src/tokens/tokens.test.ts` | vitest: every semantic text/background pair (`fg`/`bg`, `fg`/`bg-raised`, `accent-fg`/`accent`, `fg-muted`/`bg`) has WCAG contrast ≥ 4.5 in both themes (≥ 3.0 for `border`/`bg`); each ramp is monotonic in lightness; `tokensToCss` output is stable (snapshot) and every name starts `--indus-`; ANSI map has exactly 16 entries per theme, all valid hex; no token value string contains a banned name (cross-check via a list in the test). |
| P00-13 | **D-04.1 async trial: harness and criteria (a) and (c).** Write `docs/notes/P00/async-trial.md` first (criteria a–e from architecture §8, method, numbers to record, who finishes: P03-19). Build a throwaway prototype under `tests/trials/async_core/`: `harness.py` with a minimal async "turn" (task per turn, generation number, single exit funnel that appends a synthetic result for unanswered calls and marks interrupted), a fake streaming source (async generator yielding a delta every N loop iterations, `aclose()` observable) and a per-session event queue with timestamps from `time.perf_counter_ns` (measurement only). (a) interrupt mid-stream: cancel the turn task while a delta is pending → the source's `aclose` runs and the turn reaches its exit funnel within 200 ms, history alternation is valid (user, assistant-partial-sealed, no dangling tool call); (c) 50 concurrent sessions each streaming 200 deltas through one loop → p99 delay between "delta produced" and "event dequeued by the surface consumer" < 50 ms. No product code: nothing under `src/` imports the trial. | `agent/chat_completion_helpers.py` [4017: lines 2703–2760 only, the worker-thread call shape the trial is replacing], `agent/interrupt_control.py` [425: public method names only] | `docs/notes/P00/async-trial.md`, `tests/trials/__init__.py`, `tests/trials/async_core/__init__.py`, `tests/trials/async_core/harness.py`, `tests/trials/async_core/test_interrupt_latency.py`, `tests/trials/async_core/test_fanout_latency.py` | `slow` tests print measured p50/p99 and assert (a) close ≤ 200 ms in 20 of 20 runs and (c) p99 < 50 ms; a negative control (a source that ignores cancellation for 1 s) shows the harness detects a violation; results table filled in the trial note. |
| P00-14 | **Locale catalogue and key-parity check skeleton.** `locales/en.yaml`: nested YAML, top-level sections `common`, `cli`, `errors` with a handful of fresh English strings used by P01/P02 messages (e.g. `errors.store.locked`, `errors.config.unreadable`), placeholders written `{name}`. New check `locales`: every `locales/<lang>.yaml` other than `en` must have exactly the English leaf-key set (missing and extra keys listed) and the same placeholder names per key; every value is a string; YAML duplicate keys are an error; a key-list file `locales/_keys.<surface>.json` (later TUI/desktop) is compared the same way when present; English-only today, so the check passes with one file (D-16.51). Register in the runner. Parsing with stdlib only (the guards stay dependency-free): a small strict YAML subset reader (mappings, plain/quoted scalars, comments) that refuses anything else with a clear message. | `locales/en.yaml` [4004: section names only, never values], `tests/agent/test_i18n_tui_packs.py` [56: what parity means] | `locales/en.yaml`, `tooling/checks/locales.py`, `tooling/checks/tests/test_locales.py` | Temp repos: `fr.yaml` missing one key fails naming it; extra key fails; `{count}` vs `{n}` placeholder mismatch fails; non-string value fails; duplicate key fails; anchors/flow syntax refused with a message; English-only repo passes; real repo passes. |

## Files to create (71)
| Path | What it holds | Task |
|---|---|---|
| `tooling/checks/similarity.py`, `simtext.py`, `similarity_allow.json`, `tests/test_similarity.py` | similarity guard (exists) | P00-01 |
| `tooling/checks/banned.py`, `banned_patterns.json`, `assets.py`, `tests/test_banned.py`, `tests/test_assets.py` | banned names and asset hashes (exist) | P00-02 |
| `tooling/checks/licenses.py`, `licenses_allow.json`, `tests/test_licenses.py` | licence allow-list (exist) | P00-03 |
| `tooling/checks/traps.py`, `acceptance.py`, `ledger.py`, `tests/test_ledgers.py`, `tests/fixtures/ledger_repo/…`, `docs/internals/traps-ledger.md` | ledgers (exist) | P00-04 |
| `tooling/checks/prbody.py`, `__main__.py`, `__init__.py`, `common.py`, `reference.json`, `tests/test_prbody.py`, `tests/test_runner.py`, `tests/conftest.py`, `tooling/__init__.py` | runner and PR body (exist) | P00-05 |
| `pyproject.toml`, `uv.lock`, `.python-version`, `src/indus/__init__.py`, `src/indus/__main__.py`, `src/indus/cli.py`, `tests/test_smoke.py`, `.github/workflows/ci.yml`, `LICENSE`, `THIRD_PARTY_NOTICES.md`, `README.md`, `.editorconfig`, `.gitignore` | scaffold (exist) | P00-06 |
| `AGENTS.md`, `CLAUDE.md`, `HANDOFF.md`, `.agents/*`, `.github/pull_request_template.md`, `docs/notes/README.md`, `docs/notes/_template.md` | rules and mailbox (exist) | P00-07 |
| `package.json` | private workspace root, scripts, `packageManager`, engines | P00-08 |
| `pnpm-workspace.yaml` | globs, 14-day minimum release age, no install scripts | P00-08 |
| `pnpm-lock.yaml` | JS lock | P00-08 |
| `.nvmrc` | `24.21.0` | P00-08 |
| `biome.json` | lint + format config | P00-08 |
| `tsconfig.base.json` | strict TS base | P00-08 |
| `vitest.config.ts` | workspace projects | P00-08 |
| `packages/ui/package.json`, `packages/ui/tsconfig.json`, `packages/ui/src/index.ts`, `packages/ui/src/index.test.ts` | first package and its smoke test (engine/.nvmrc alignment) | P00-08 |
| `packages/protocol/package.json`, `packages/protocol/tsconfig.json`, `packages/protocol/src/index.ts`, `packages/protocol/src/generated/.gitkeep` | `@indus/protocol` skeleton; P04 generator fills `src/generated/` | P00-08 |
| `tooling/checks/tests/test_licenses_packages.py` | licence check over `packages/*` and root | P00-08 |
| `tooling/checks/pins.py` | pin policy check | P00-09 |
| `tooling/checks/pins_allow.json` | bounded-range exceptions with reasons (starts empty) | P00-09 |
| `tooling/checks/pins_meta.json` | lock date that `exclude-newer` is measured against | P00-09 |
| `tooling/checks/tests/test_pins.py` | pin cases + CI gate shape test | P00-09 |
| `src/indus/{platform,foundation,protocol,state,providers,safety,pm,tools,backends,core,learning,plugins,mcp,cron,board,service,tui_bridge,gateway,web,acp,mcp_serve,research}/__init__.py` | 22 empty layer packages | P00-10 |
| `src/indus/py.typed` | typed marker | P00-10 |
| `tests/unit/test_layers.py` | seeded layer violation, pyright strict probe | P00-10 |
| `tests/conftest.py` | home isolation, fake clock fixture | P00-11 |
| `tests/fakes/__init__.py`, `tests/fakes/clock.py` | fakes package, `FakeClock` | P00-11 |
| `tests/fakes/model/__init__.py` | script vocabulary + loader | P00-11 |
| `tests/fakes/platform/__init__.py`, `tests/fakes/backend/__init__.py` | messaging and backend fakes | P00-11 |
| `tests/unit/fakes/test_clock.py`, `test_model_script.py`, `test_platform_backend.py` | fake tests | P00-11 |
| `tests/fixtures/model_scripts/hello.yaml` | smallest script | P00-11 |
| `packages/ui/src/tokens/palette.ts`, `semantic.ts`, `type.ts`, `space.ts`, `motion.ts`, `ansi.ts`, `css.ts` | design tokens | P00-12 |
| `packages/ui/src/tokens/tokens.test.ts` | contrast, monotonic ramps, CSS snapshot | P00-12 |
| `docs/notes/P00/async-trial.md` | trial plan and results (P03-19 completes) | P00-13 |
| `tests/trials/__init__.py`, `tests/trials/async_core/__init__.py`, `harness.py` | trial prototype | P00-13 |
| `tests/trials/async_core/test_interrupt_latency.py`, `test_fanout_latency.py` | criteria (a), (c) | P00-13 |
| `locales/en.yaml` | English catalogue seed | P00-14 |
| `tooling/checks/locales.py`, `tooling/checks/tests/test_locales.py` | parity check | P00-14 |

(69 paths for the remaining tasks P00-08 … P00-14, each grouped row counted per path;
the done rows list what exists.)

## Files to update (6)
| Path | Change | Task |
|---|---|---|
| `pyproject.toml` | P00-10: `requires-python` upper bound, `[tool.importlinter]`, `[tool.pyright]`, dev deps; P00-11: `TID251` banned APIs + per-file ignores, `pytest-asyncio`/`hypothesis` dev deps, `asyncio_mode`; P00-09: `[tool.uv] exclude-newer`. Land in the order P00-09 → P00-10 → P00-11 (one slice each, the lead rebases) | P00-09, P00-10, P00-11 |
| `uv.lock` | relocked after each pyproject change | P00-09, P00-10, P00-11 |
| `.github/workflows/ci.yml` | `ts` job (P00-08); 3.14 lane and `gate` job (P00-09); `lint-imports` + `pyright` steps (P00-10) | P00-08, P00-09, P00-10 |
| `tooling/checks/licenses.py` | also read root and `packages/*/package.json` | P00-08 |
| `tooling/checks/__main__.py` | register `pins` and `locales` | P00-09, P00-14 |
| `packages/ui/src/index.ts` | export tokens | P00-12 |

## Fixtures and fakes (3)
| Path | What it holds | Task |
|---|---|---|
| `tests/fakes/` | clock, model script vocabulary, platform and backend fakes (skeletons grown by P03, P05, P15) | P00-11 |
| `tests/fixtures/model_scripts/hello.yaml` | one text turn: "hello" in three chunks, usage 10/3 | P00-11 |
| `tooling/checks/tests/` (temp repos built in `tmp_path`) | synthetic violations for pins and locales; never reference text | P00-09, P00-14 |

## Task notes (10)
`docs/notes/P00/<slug>.md`, one per task, written before the code:
- `docs/notes/P00/guards.md` (P00-01 … P00-05, exists)
- `docs/notes/P00/scaffold.md` (P00-06, P00-07, exists)
- `docs/notes/P00/ts-workspace.md` (P00-08)
- `docs/notes/P00/pins-and-gate.md` (P00-09)
- `docs/notes/P00/layers-and-types.md` (P00-10)
- `docs/notes/P00/fakes-and-clock.md` (P00-11)
- `docs/notes/P00/brand-tokens.md` (P00-12)
- `docs/notes/P00/async-trial.md` (P00-13; P03-19 appends criteria b, d, e and the decision input)
- `docs/notes/P00/locales-parity.md` (P00-14)
- the lead records the landing of each slice in `HANDOFF.md`

## Run until green
```bash
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run pyright                       # from P00-10
uv run lint-imports                  # from P00-10
uv run pytest -q tooling/checks/tests tests/unit tests/test_smoke.py
uv run pytest -q -m slow tests/trials/async_core tests/unit/test_layers.py
uv run python -m tooling.checks --require-ref        # now also pins + locales
pnpm install --frozen-lockfile
pnpm lint && pnpm fmt:check && pnpm typecheck && pnpm test
```
Seeded-violation drill before calling the phase done (M0): on a scratch branch add, one at a time, a
banned identifier in `src/`, a 12-word run copied from a reference doc into `docs/user/`, an
unbounded pin, a `foundation → core` import and a `^` range in `packages/ui/package.json`; each
must turn CI red at the right job and the `gate` job red. Revert the scratch branch.
On any failure fix the cause and rerun from `uv sync`. Never skip a test or widen an allow-list
without a written reason.

## Compare checklist (reopen the reference after writing)
- [ ] The reference's CI has an aggregate gate; ours fails on `skipped` for every selected job (R10.18) — our own job graph, not theirs.
- [ ] The reference pins with a 3-day/14-day split and a weak check (R10.11); ours is one 14-day rule checked literally (D-16.52).
- [ ] No per-dependency interpreter markers (R10.12); one ruff config, one target version (R10.13).
- [ ] The reference's locale parity covers main and TUI catalogues (R10.28); our check handles both shapes even though only `en` exists.
- [ ] Theme tokens: our names, colours and fonts; nothing from the reference's design system (R09.22).
- [ ] The trial measures what the reference solves with abandoned worker threads (R01.4) and records it.

## Traps covered
| Trap | How this phase pins it | Task |
|---|---|---|
| R10.11 | `pins` check enforces one written policy; tests for every rule | P00-09 |
| R10.12 | `pins` refuses interpreter markers; one `requires-python` line | P00-09, P00-10 |
| R10.13 | single ruff/pyright config in `pyproject.toml`; test that no `ruff.toml` / `.ruff.toml` exists | P00-10 |
| R10.18 | `gate` job with `if: always()` failing on non-success; shape test | P00-09 |
| R10.28 | `locales` parity check with missing/extra/placeholder tests | P00-14 |
| R10.30 | exact pins + 14-day age window in both ecosystems; no install scripts by default | P00-08, P00-09 |
| R09.22 | own tokens, banned-name cross-check in token test | P00-12 |
| R01.4 | trial (a): interrupt closes the stream and reaches the exit funnel (the async answer to abandoned workers) | P00-13 |
| R10.22 | policy files for every guard are read from the base branch on PRs; test: a PR fixture that widens an allow-list still fails | P00-09 |

Later phases own R10.17 (change classifier, P23), R10.19–R10.21 (per-file runner and OS markers, S13 in P23).

## Acceptance
| Feature | Tier | Planned ids |
|---|---|---|
| S17 Clean-room guards in CI | P0 | ACC-S17-1 (each guard fails on a seeded violation; P00-01…05), ACC-S17-2 (CI runs guards with `--require-ref` against the pinned reference; P00-06) |
| S8 Dependency policy | P1 | ACC-S8-1 (`pins` rejects ranges, young versions, unpinned git; P00-09) |
| S11 CI with one aggregate gate | P1 | ACC-S11-1 (skipped job fails the gate; P00-09) — classifier and lint-diff parts in P23 |
| S18 Third-party notices | P1 | ACC-S18-1 (`THIRD_PARTY_NOTICES.md` carries the reference credit; P00-06) — dependency list completed in P25 |
| Q9 Localisation mechanism | P1 | ACC-Q9-1 (catalogue parity check live with English only; P00-14) — runtime lookup in P12 |

## Done when
- CI on `main` is green with jobs `checks` (ruff, format, pyright, lint-imports, pytest, guards incl. `pins` and `locales`), `py314`, `ts` and `gate`; `gate` is the only required status.
- The seeded-violation drill above turns CI red for each of the five seeds.
- `pnpm install --frozen-lockfile && pnpm test` passes on a clean checkout with Node from `.nvmrc`.
- `tests/fakes/` imports cleanly; `FakeClock` tests pass; ruff flags `time.sleep` in a test file.
- `@indus/ui` exports light and dark tokens that pass the contrast test.
- `docs/notes/P00/async-trial.md` has measured numbers for criteria (a) and (c) and lists (b), (d), (e) as open for P03-19.
- Traps ledger rows for the traps above point at tests.
