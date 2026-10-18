# Task: pnpm workspace and TS skeleton (P00-08) Phase: P00 Owner: TS builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `package.json` (only that it uses
workspaces, an engines range and an allow-list for install scripts), `tests-js/node-engine-alignment.test.ts`
(test names only: it guards that declared Node engines agree).
Evidence: none beyond the task row.

What it does (own words): sets up the TypeScript side of the repo as one pnpm workspace
(`apps/*`, `packages/*`) with a single lint/format tool (Biome), one strict tsconfig every package
extends, one root vitest run over all packages, and the first two packages: `@indus/ui` (later
holds design tokens and components) and the empty `@indus/protocol` that P04's generator fills.
Without it later phases (TUI, web, desktop) would each invent their own tooling.

Inputs → outputs: `pnpm install --frozen-lockfile` → `node_modules`; `pnpm lint` / `fmt:check` /
`typecheck` / `test` → exit 0 on a clean tree, non-zero on a lint, format, type or test failure.

Files: `package.json`, `pnpm-workspace.yaml`, `pnpm-lock.yaml`, `.nvmrc`, `biome.json`,
`tsconfig.base.json`, `vitest.config.ts`, `packages/ui/{package.json,tsconfig.json,src/index.ts,src/index.test.ts}`,
`packages/protocol/{package.json,tsconfig.json,src/index.ts,src/generated/.gitkeep}`.

Edge cases / failure modes:
- Supply chain: `minimumReleaseAge: 20160` refuses packages younger than 14 days, including
  transitive ones; `onlyBuiltDependencies: []` means no dependency runs install scripts.
- Node drift: `engines.node` in `package.json` and `.nvmrc` can disagree; the ui smoke test reads
  both and checks that the `.nvmrc` version satisfies the lower bound and shares its major.
- `apps/*` matches nothing yet; vitest `projects` globs must tolerate that.
- `packages/protocol/src/generated/` is ignored by Biome (machine output).
- `passWithNoTests: false`: the run fails when no test file is found at all; a single project
  without tests (protocol today) does not fail it (checked with vitest 5.0.3).

Our design differences / decisions:
- One root vitest run with `test.projects`; no per-package vitest configs.
- pnpm `onlyBuiltDependencies: []` instead of a per-package allow-list (stricter: nothing allowed).
- Biome only (no ESLint/Prettier).

Tests we will write:
- `packages/ui/src/index.test.ts`: `version` is a semver string; `.nvmrc` equals the engines
  lower bound and has the same major as the engines range.

## Decisions for the lead
- Local toolchain is Node 22.22 / pnpm 10.28 while the spec pins Node 24.21.0 / pnpm 12.9.1. The
  files keep the spec values; local runs use the method recorded in Status.
- The licence-guard extension (`tooling/checks/licenses.py`, `tooling/checks/tests/test_licenses_packages.py`)
  is part of P00-08 but `tooling/` was outside this builder's ownership; handed back to the lead.
- `packages/protocol` gets a `typecheck` script too so `pnpm -r typecheck` covers it.
- `noConsole` is an error everywhere via the root config with an override that turns it off for
  `scripts/**`.
- pnpm 11+ ignores `onlyBuiltDependencies` (replaced by `allowBuilds`, default deny). The file keeps
  the spec's `onlyBuiltDependencies: []` and adds `allowBuilds: {}` so the rule is real under the
  pinned pnpm 12.9.1. Drop the old key whenever convenient.
- Biome 2.5 deprecates `linter.rules.recommended`; `biome migrate` rewrote it to
  `"preset": "recommended"` (same rule set).
- Biome excludes `tooling/` (Python-owned JSON data such as `banned_patterns.json`, formatted by
  hand); otherwise `pnpm fmt:check` would demand reformatting files this task does not own.
- Lead ruling: vitest's required peer `vite` is a direct exact dev dependency, `vite@8.3.2`
  (released 2026-10-01, MIT), so the pin guard sees it. Lead adds the row to 00-layout §4.
- `tsconfig.base.json` adds `noEmit`, `allowImportingTsExtensions` (packages export `./src/*.ts`
  directly, imports carry `.ts`), `types: ["node"]`, `lib: ["ES2023"]`.

Compare checklist (filled after the rewrite; reopened nothing — task has no reference behaviour):
- [x] Names, scripts and layout are ours; no reference file copied (reference used npm workspaces
  and a script allow-list; we use pnpm with default-deny builds and a release-age window).
- [x] Engine/`.nvmrc` alignment test (`packages/ui/src/index.test.ts`); seeded `22.22.0` in
  `.nvmrc` turns it red.
- [x] Seeded `any` and `console.log` in `packages/ui/src` fail `pnpm lint`.

## Status

Partly done (2026-10-18). Workspace, configs, both packages, lock and tests in place; lint, format,
typecheck and test are green with pnpm 12.9.1 (the system pnpm 10.28 switches to it through
`packageManager`) on Node 22.22 (no engine-strict failure: pnpm does not enforce the root
project's own `engines`). Not done (outside this builder's files): the licence guard reading
`packages/*/package.json` and the root (`tooling/checks/licenses.py`,
`tooling/checks/tests/test_licenses_packages.py`) and the CI `ts` job — handed to the lead.
