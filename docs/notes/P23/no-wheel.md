# Task: No-PyPI build guard and packaging metadata (P23-01) Phase: 23 Owner: wave-36 builder I1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `pyproject.toml` (`[project]` header,
scripts, the extras block and the `all` policy comment, package data, pytest markers),
`setup.py` (all: the wheel/sdist guard). `tests/conftest.py` only grepped for the `platforms`
marker hook names (no body read). Closed before writing.
Evidence: none.

What it does (own words): Indus Agent is never published to PyPI (P5, D-16.14). A hatchling build
hook refuses to produce a wheel or an sdist unless the build runs inside our Nix derivation
(`INDUS_NIX_BUILD=1`); the refusal names the supported channels (installer, Docker, Nix) and the
editable install developers use. Editable installs (`uv sync`) pass, so development is untouched.
The packaging metadata states one interpreter range (`>=3.13,<3.15`) with no per-dependency
interpreter markers, the source version `0.0.0` (a release stamps the real one into its build
tree only), the `indus` console script, exact-pinned extras per backend/platform plus `messaging`
and `all` (the union of the common extras), and the package data the runtime reads (locales,
starter skills, built web pages). Tests get one host-gating marker, `platforms(*specs)`, applied by
a collection hook: a test that does not match the host is deselected, two `platforms` marks on one
test are a usage error, and a lint test reports bare `skipif(sys.platform…)` marks (R10.21).

Inputs → outputs: `uv build --wheel|--sdist` → refused with the message (exit non-zero);
`INDUS_NIX_BUILD=1 uv build --wheel` → a wheel; `uv sync` → editable install. `pytest` with
`@pytest.mark.platforms("linux")` → deselected on macOS/Windows.

Files: `scripts/build_hooks/no_wheel.py`, `pyproject.toml` (shared), `src/indus/__init__.py`
(version `0.0.0`), `tests/test_smoke.py` (version assertion), `tests/conftest.py` (shared: the
collection hook), `tests/platform_marks.py`, `tests/unit/packaging/test_no_wheel.py`,
`tests/unit/packaging/test_metadata.py`, `tests/unit/packaging/test_platform_marks.py`,
`tests/unit/packaging/platform_skip_baseline.json`.

Edge cases / failure modes:
- `INDUS_NIX_BUILD` set to anything but `1` → still refused.
- hatchling runs the hook for both the wheel and the sdist target; the editable wheel arrives as
  target `wheel`, version `editable` → allowed.
- the hook module must import without hatchling (tests) and with it (builds).
- a `platforms` spec that is not one of `linux`, `macos`, `windows`, `posix` (optionally prefixed
  `not `) is a usage error naming the test; two marks → usage error; no spec → usage error.
- existing bare `skipif(sys.platform…)` marks: kept as a shrinking baseline (count per file) so the
  lint test fails only on new ones and on a stale (too large) baseline.

Our design differences / decisions:
- The guard is a hatchling build hook (our backend), not setuptools command overrides; the version
  comes from `src/indus/__init__.py` through hatch's version source, so a release stamps one file.
- `all` is written out as exact pins (the pins guard rejects self-referencing extras); a test keeps
  it equal to the union of its named member extras.
- `hatchling` in `[build-system]` is pinned exactly (one pinning policy, R10.11).
- Deselect, not skip, for a non-matching host (the row's wording; keeps skip counts meaningful).

Tests we will write:
- hook decision table (wheel/sdist/editable × env); hook class refuses with the message; real
  `uv build --wheel --offline` refused and allowed with the env (skips when the build backend is not
  in the uv cache).
- metadata: one interpreter line, no `python_version` marker, no unbounded `>=`, version `0.0.0` in
  `__init__`, console script, `all` = union of its members, package data entries exist.
- platform marks: host matching table, two marks → collection error, deselection in a child pytest,
  lint over `tests/` against the baseline.

## Decisions for the lead
- Version is dynamic from `src/indus/__init__.py` (`__version__ = "0.0.0"`); `tests/test_smoke.py`
  updated to `0.0.0`. uv.lock changes (dynamic version, new extras) are left to the lead's relock.
- New extras: `messaging` (every cross-platform adapter extra) and `all` (web, docs, pty, dashboard,
  browser, acp + messaging). K1's image should use `--extra messaging` (there is no `gateway` extra).
- `hatchling==1.32.4` pinned in `[build-system]` (the version uv resolved under the current
  exclude-newer).
- Existing bare platform skipifs (22 in 18 files) are a baseline, not migrated here (other phases'
  files); `tests/unit/packaging/platform_skip_baseline.json` may only shrink.
- `src/indus/_tui_dist/` added to `.gitignore` and the wheel artifacts (the release copies the TUI
  bundle there, like the web pages); `locales/` ships as `indus/_locales`.

Compare checklist (filled after the rewrite; reopened `pyproject.toml`, `setup.py`):
- [x] wheel and sdist both refused outside Nix; editable allowed (tests: `tests/unit/packaging/test_no_wheel.py::test_decision_table`, `::test_uv_build_wheel_refused_then_allowed`)
- [x] message names installer, Docker, Nix and the editable path (test: `::test_message_points_to_channels`)
- [x] env value must be exactly `1` (test: `::test_decision_table`)
- [x] one interpreter line, no bridge markers (test: `tests/unit/packaging/test_metadata.py::test_no_interpreter_markers_or_open_ranges`)
- [x] source version 0.0.0 (test: `::test_source_version_is_zero`)
- [x] platforms marker: any-of specs and `not X` (test: `tests/unit/packaging/test_platform_marks.py::test_host_matching`)
- [x] two marks on one test → collection error; other hosts deselected (tests: `::test_two_platforms_marks_fail_collection`, `::test_other_hosts_are_deselected`)
- [x] bare skipif lint (test: `::test_no_new_bare_platform_skipifs`)
- [x] extras exact-pinned, `all`/`messaging` kept equal to their members (test: `tests/unit/packaging/test_metadata.py::test_messaging_and_all_are_unions_of_their_members`)
- Deliberate differences: the reference keeps a 3.11 floor with `python_version` bridge markers on
  every core dependency so old installs can update; we have one range and no markers (R10.12,
  D-16.34). The reference skips non-matching hosts and takes an `arch` filter; we deselect and
  have no arch filter (no test needs one yet).
- Not taken: root single-module discovery (we have one `src/indus` package).

## Status

Done (2026-10-21). Shared files changed: `pyproject.toml`, `tests/conftest.py`,
`tests/test_smoke.py`, `src/indus/__init__.py`, `.gitignore`. uv.lock not touched (relock owed to
the lead: dynamic version and the new `messaging` / `all` extras).

## Integration (wave 36)

Integration: `uv lock` re-locked (dynamic version, `messaging` and `all`; no new package). The installers' extras now come from `stages.json` and the Docker image's from the same list (D-16.743).
