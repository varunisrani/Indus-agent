# Wave 41 parked patches (P25 tooling). Base for every patch: 34a3315.

## w41-d1.patch: P25-14 docs site, P25-15 generated reference pages, P25-16 user guide, P25-17 THIRD_PARTY_NOTICES generation — done
- Builder checks: ruff, format, pyright 0, lint-imports; tests/unit/docs + tests/unit/release 148 passed; tooling/checks/tests +
  packaging + ci + ACP schema drift 444 passed / 2 skipped; `mkdocs build --strict` green; gen_reference --check + notices --check
  fresh; tooling.checks 11/11; biome + desktop builderConfig.test.ts (11).
- New: mkdocs.yml (Material, strict, no web fonts), scripts/docs/{__init__, llms_txt (llms.txt + llms-full.txt; refuses pages
  outside nav), gen_reference (--check, --out; discovers from the click tree, registry, config models, AST register_tools +
  INDUS_* names, platform + provider registries)}.py, .github/workflows/docs.yml (Pages; actions on major tags — SHA-pin later),
  docs/user/reference/*.md (10 generated, marker line 1), 46 hand-written docs/user pages (getting-started, user-guide,
  messaging ×25, developer), tests/unit/docs/{test_docs_build, test_gen_reference (~50 s), test_examples (11 `<!-- test -->`
  console blocks through fake_runner)}, scripts/release/{notices.py (write / --check / refresh), notices_data.json,
  licence_texts/ (18 SPDX texts)}, docs/internals/notices-credit.md, tests/unit/release/test_notices.py; notes docs-site,
  docs-reference, docs-guide, notices.
- Shared: THIRD_PARTY_NOTICES.md now GENERATED (433 components); tooling/checks/licenses.py (load_obligations),
  licenses_allow.json (obligations MPL-2.0 / LGPL-2.1-or-later / GPL-2.0-only + DATED EXCEPTIONS numpy, regex, pyarrow, av,
  git-for-windows, debian — pending owner licence rulings), similarity_allow.json (2 dated entries: licence texts, credit
  file), docker/Dockerfile (COPY notices), electron-builder.config.cjs (extraResources notices), ci.yml (gen_reference --check
  + strict mkdocs + notices --check), pyproject dev group (mkdocs 1.6.1 BSD-2, mkdocs-material 9.7.7 MIT + 15 transitives incl.
  pathspec MPL-2.0 — all allowed, uploaded before cutoff), ACC-S15-1/2, S18-1 (tests), S18-2.
- AFTER P24 LANDS (order): `uv lock` (online) → `uv sync --all-extras` → `python scripts/release/notices.py refresh` → review new
  rows → `python scripts/release/notices.py` → `python scripts/docs/gen_reference.py`. Until then both --check steps fail by
  design. On a THIRD_PARTY_NOTICES conflict take the generated file and regenerate. Policy-file changes apply from the next PR
  (R10.22). Workflows set NO_MKDOCS_2_WARNING.
- OWNER: the Git for Windows GPL-2.0 note contains a written source offer (legal commitment); licence exceptions stand until
  the owner rules; docs use release base indusagi/indus-agent (D-16.690); site_url unset until a docs domain is chosen.
- Decisions (D-ids): committed licence table (deterministic output); `browser_*`-style tool families of ≥ 4 collapsed in the
  toolsets table; install page documents extras via updates.installs.<id>.extras + `indus update`.
- Follow-ups: user-guide pages for P24 features after wiring; Windows page mentions P25-24 (not built); env-var descriptions.

## w41-g1.patch: P25-10 acceptance strict, P25-11 traps complete, P25-12/13 perf budgets, P25-18 similarity --closest, P25-19 artefact sweep — tooling done
- Builder checks: ruff, format, pyright 0, lint-imports; tooling/checks/tests + tests/perf (not perf) 312 passed; perf 7 passed;
  tooling.checks all OK; biome + tsc (tui, desktop, web); web vitest 257; TUI bench 2.
- New: tooling/checks/{collect (pytest --collect-only subprocess + plugin; static TS scan), collect_plugin, artifacts (dirs, our
  ASAR reader, image tars, zips; banned names/hosts, reference media hashes, build-machine home folder), artifacts_allow.json},
  tests for each, fixtures/simrepo; tooling/perf/{budgets.toml, baseline.json (EMPTY — record on the CI runner with
  INDUS_PERF_RECORD=<note> pytest -m perf tests/perf), read-budgets.mjs + .d.mts}; tests/perf/*; apps/web/scripts/check-bundle
  (+test); apps/tui/bench/*; apps/desktop/e2e/startup-budget.spec.ts (skips without Electron);
  docs/internals/similarity-report.md (20 regions, verdicts EMPTY); notes acceptance-strict, traps-complete, perf-python,
  perf-ts, similarity-closest, artefact-sweep.
- Shared: tooling/checks/{acceptance, traps, similarity, ledger, common, __main__, __init__} (--report, --artifact, `artifacts`
  check registered); acceptance-map.md (71 Review → existing tests; 10 Known-gap rows Q9, K12, G19, I46, I48, N30, N33, S5, S6,
  O9); traps-ledger.md (42 placeholder rows filled from phases' Traps tables; R06.26 + R08.11 left for P24's lines);
  apps/tui/package.json (bench) + tsconfig; apps/desktop/e2e/vitest.config.ts.
- CI WIRING OWED: `node apps/web/scripts/check-bundle.mjs` after the web build; `pnpm --filter @indus/tui bench`;
  `python -m tooling.checks acceptance traps --strict` in the RELEASE gate; `python -m tooling.checks artifacts --require-ref
  --artifact apps/web/dist --artifact site --artifact apps/desktop/build/release/linux-unpacked --artifact <image.tar>
  --artifact scripts/install.sh --artifact scripts/install.ps1 --artifact scripts/install.cmd`.
- Strict gates fail BY DESIGN until people act: `acceptance --strict` 58 Review rows (owner live lanes K / D4 / D5 / S1–S3,
  captures A44–A46, unbuilt C14 / I7 / K28, no task A22 / S11 / S13, S18 → D1); 5 container-only skips (browser extra, mautrix,
  pnpm install); similarity report 20 regions need verdicts (lead/owner); traps 9 rows without tests (R03.28, R07.30, R08.20,
  R08.22, R10.17, R10.19, R10.20, R06.26, R08.11).
- Decisions (D-ids): store-append budget measured on batches of 50 (per-row commit 2.9 ms → 28.7 s / 10k because SQLite
  3.45.1 falls back to DELETE journaling — a store issue if the budget is per row); traps placeholder Review lines warn
  outside --strict (make them always fail after P24 lands?); TS skip check is static (.skipIf/.runIf runnable); similarity
  region score = sum of runs.
