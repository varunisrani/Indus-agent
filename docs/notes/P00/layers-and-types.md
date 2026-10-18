# Task: Layer contract and type checking (P00-10) Phase: P00 Owner: builder (layers)

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none. This is our own architecture
(architecture §2–§3, 00-layout §1); there is no reference behaviour to match.
Evidence: none.

What it does (own words): fixes the package skeleton of `indus` before any code lands, and makes
the import direction a CI rule instead of a convention. Every layer package exists from P00 on as
an empty package with a one-line docstring, so later phases only add modules. import-linter's
`layers` contract fails the build when a lower layer imports a higher one (for example
`foundation` importing `core`). pyright runs in basic mode over `src`, `tests` and `tooling`, and in
strict mode on the packages whose types other code leans on (`foundation`, `protocol`, `state`,
`service`, `core`). `py.typed` marks the package as typed for downstream checkers.

Inputs → outputs: `uv run lint-imports` → exit 0 with "1 kept, 0 broken" on the repo, non-zero
when an upward import appears. `uv run pyright` → "0 errors" on the repo, errors on an untyped
function under a strict path.

Files: 22 `src/indus/<layer>/__init__.py`, `src/indus/py.typed`, `pyproject.toml`
(`requires-python`, dev deps, `[tool.importlinter]`, `[tool.pyright]`), `tests/unit/test_layers.py`,
this note.

Layer order (low → high, `|` = independent siblings on one layer):

1. `platform`
2. `foundation`
3. `protocol | state`
4. `providers | safety | pm`
5. `tools | backends`
6. `core`
7. `learning | plugins | mcp | cron | board`
8. `service`
9. `cli | tui_bridge | gateway | web | acp | mcp_serve | research`

Edge cases / failure modes:
- `indus.cli` is still a module (`cli.py`), not a package; import-linter accepts a module as a
  layer member, and P06-01 replaces it with a package of the same name, so the contract does not
  change.
- `indus/__init__.py` and `indus/__main__.py` sit outside the contract (root package glue):
  `__main__` imports `cli`, which is fine.
- `|` siblings are independent: `protocol` may not import `state` and vice versa. A later task that
  needs a sibling import must change the contract on purpose.
- Empty packages give pyright nothing to report; the strict probe test creates its own temp file.
- pyright from PyPI downloads its Node runtime on first run; CI needs network for that (same as
  every other uv install step).

Our design differences / decisions:
- One contract, exactly the 00-layout §1 order; no per-package forbidden contracts yet.
- No ruff `per-file-ignores` is used to quiet pyright (spec).

Tests we will write (`tests/unit/test_layers.py`):
- The pyproject contract lists every layer package in the 00-layout order (config shape).
- Seeded violation: copy the skeleton to a temp dir, add `indus/foundation/x.py` importing
  `indus.core`, run import-linter on it with our contract → broken.
- Reverse import: `indus/core/x.py` importing `indus.foundation` → kept.
- Every layer package exists with a one-line docstring; `py.typed` exists.
- No `ruff.toml` / `.ruff.toml` / `pyrightconfig.json` beside `pyproject.toml` (R10.13).
- `slow`: an untyped function in a temp file under a strict path fails `pyright --outputjson`; the
  same file outside the strict paths passes.

## Decisions for the lead
- `indus.cli` is placed in the top layer alongside `tui_bridge`, `gateway`, `web`, `acp`,
  `mcp_serve`, `research` as independent siblings, as 00-layout §1 lists it. The P00-10 row reads
  "`indus.cli` as the top layer"; if a separate layer above the surfaces was meant, move it to its
  own first line of `layers` in `pyproject.toml`.
- The import-linter tests run in-process through import-linter's Python API against a temp copy of
  the skeleton (cwd + `sys.path` switched for the call) rather than a subprocess, so they stay fast
  and are not marked `slow`.

Compare checklist: not applicable (no reference counterpart).

## Status

Partial (2026-10-18). Contract, packages, config and tests are done; `lint-imports` is green and
pyright reports 0 errors in `src/` and `tests/`. `uv run pyright` still reports 10 basic-mode
errors in `tooling/checks/` (licenses.py:245, tests/test_licenses.py:39–52,
tests/test_similarity.py:242), which this task may not edit; the owner of `tooling/` fixes them
before the pyright CI step goes in. CI steps (`uv run lint-imports`, `uv run pyright`) are owned by the pins
builder per the lead's split; `uv.lock` was regenerated locally only for testing.
