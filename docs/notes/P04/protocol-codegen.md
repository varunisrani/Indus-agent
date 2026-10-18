# Task: Protocol codegen + stale check (P04-02, D-04.3) Phase: 04 Owner: wave-5 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — the phase file marks this
task "our design". Research `05-tui.md` §3.3 says only that the reference generates TS types from
its Python contract and fails a test when they are stale; nothing else was used.
Evidence: none.

What it does (own words): turns the Python protocol models into the files the TypeScript
surfaces compile against, so the two sides cannot drift. `python -m indus.protocol.codegen`
writes three files under `packages/protocol/src/generated/`:
- `schema.json` — one JSON Schema (2020-12) document; every model, enum and named union sits
  under `$defs`; the top level holds the protocol version and the catalogue as plain data.
- `types.ts` — one exported type per `$defs` entry: an `interface` for objects (JSDoc from the
  description), a string-literal union for enums, a union of names for discriminated unions,
  plus a `JsonValue` helper type.
- `catalog.ts` — `PROTOCOL_VERSION`, `METHODS` (method → params/result type names and declared
  error names), `MethodTypes` (method → params/result types), `EVENT_NAMES` / `EventTypes`,
  `SERVER_REQUEST_KINDS` / `ServerRequestTypes`, and `ERROR_CODES`, deeply frozen.
Output is deterministic: sorted keys, LF line ends, one trailing newline, no timestamps.
`schema.json` keeps any object or array that fits in 100 columns on one line (one property
schema per line, description included), which keeps diffs short and the file readable. Each file
starts with a "generated, do not edit" header naming the command. `--check` builds the same text
in memory and exits 1 with a unified diff per differing or missing file (0 when equal); a stray
extra file in the folder is reported too. `tooling/checks/protocol_stale.py` runs `--check` in a
child process against the checked root so `python -m tooling.checks` (and CI) fails on stale
output.

Inputs → outputs: `python -m indus.protocol.codegen [--check] [--root DIR]` → files or a diff;
exit 0 written/clean, 1 stale, 2 usage error.

Files: `src/indus/protocol/codegen.py`, `packages/protocol/src/generated/{schema.json,types.ts,
catalog.ts}`, `packages/protocol/src/catalog.test.ts`, `packages/protocol/src/index.ts`,
`packages/protocol/package.json` (test script), `tests/unit/protocol/test_codegen.py`,
`tooling/checks/protocol_stale.py`, `tooling/checks/__init__.py` (shared: one name in `NAMES`),
`tooling/checks/tests/test_protocol_stale.py`.

Edge cases / failure modes:
- `$defs` name clashes (two classes with one name) would make pydantic emit module-qualified
  names; a test pins that every def name is a plain identifier.
- JSON Schema shapes outside the subset we translate (e.g. `allOf`, tuple `prefixItems`) raise a
  clear `CodegenError` instead of emitting `any`.
- optional fields (`required` absent) become `?:`; nullable fields `T | null`.
- generated folder missing on `--check` → every file reported missing, exit 1.
- the check run against a root without `src/indus/protocol/codegen.py` (fixture roots in the
  checks' own tests) → note "no protocol package", ok.
- the child process cannot import pydantic / indus → setup problem (exit 2), never a pass.

Our design differences / decisions:
- our own small schema → TS translator over the subset pydantic emits; no npm generator
  dependency (none named by the task) and the TS side needs no build step.
- `ERROR_CODES` is `Object.freeze` at both levels so a consumer cannot mutate it at runtime.
- `index.ts` re-exports the generated files and keeps `PROTOCOL_VERSION` from the catalogue.
- CI: the `ts` job already runs `pnpm typecheck` (all packages) and `pnpm test` (root vitest over
  `packages/*`), and the `checks` job runs `python -m tooling.checks --require-ref`, which now
  includes `protocol_stale`; so `.github/workflows/ci.yml` needed no edit.

Tests we will write:
- Python: two generations are byte-equal; output ends with exactly one LF and has no CR; header
  present; `--check` is 0 on fresh output and 1 with a diff after a model change (monkeypatched
  extra field) or a hand edit; missing file reported; translator unit cases (enum, optional,
  nullable, array, map, union, unsupported shape refused).
- TS (`catalog.test.ts`): every method's params/result type names exist in `schema.json` `$defs`;
  every declared error name is in `ERROR_CODES`; `ERROR_CODES` and its rows are frozen; codes
  unique; every event and server-request kind has a schema def; compile-time map completeness.
- Checks: `protocol_stale` ok on the repo, fail on a stale copy, note on a root with no package.

## Decisions for the lead
- `schema.json` layout: compact containers up to 100 columns instead of `indent=2` one key per
  line. The fully expanded form tripped the 6-line similarity gate against the reference's own
  generated contract JSON (shared generic JSON Schema keyword runs such as
  `"anyOf" / "type": "string" / "type": "null" / "default": null`); the compact form is ours and
  clean. Change: only with a similarity allow-list row for generated schema files.
- An uncaught exception in the generator also exits 1, so `protocol_stale` treats exit 1 as stale
  only when the output ends with the "protocol output is stale" line; otherwise it is a setup
  problem (exit 2).
- Stale check runs the generator in a subprocess with `PYTHONPATH=<root>/src`, so it checks the
  tree under `--root`, not whatever `indus` is installed. Change: import in process.
- `.github/workflows/ci.yml` left unchanged (covered by existing `pnpm typecheck`, `pnpm test`
  and the guards step); add a separate step only if the lead wants it named in the CI log.

Compare checklist (our design; nothing to reopen):
- [x] two runs byte-identical (test: `tests/unit/protocol/test_codegen.py::test_two_runs_are_byte_identical`)
- [x] model edit → `--check` exits 1 with a diff (test: `test_codegen.py::test_check_reports_a_changed_model`)
- [x] hand edit / missing file detected (test: `test_codegen.py::test_check_reports_hand_edit_and_missing_file`)
- [x] wired into `tooling.checks` (test: `tooling/checks/tests/test_protocol_stale.py`)
- Deliberate differences: no OpenRPC document (not asked for in P04).
- Not taken: none.

## Status

Done (2026-10-18). Shared: `tooling/checks/__init__.py` (added `protocol_stale` to `NAMES`).
