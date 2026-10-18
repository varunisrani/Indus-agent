# Task: Clock and time zone (P01-19) Phase: 01 Owner: P01 S-core builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_time.py` (all). Closed before
writing.
Evidence: none.

What it does (own words): the `Clock` protocol every waiting or timestamping piece of code takes
(`now()` aware, `monotonic()`, `async sleep()`), the real `SystemClock`, and time-zone resolution
for a scope: `INDUS_TIMEZONE` from the scope env (only when the scope is the process's own profile),
then config `timezone`, else the system local zone. Bad names warn once and fall back. This is the
only product module that may call `time.sleep` / `asyncio.sleep`. `safe_strftime` formats a
datetime even when a locale zone name carries lone surrogates.

Inputs → outputs: `Scope` → `ZoneInfo | None` (None = local); `SystemClock(zone)` → aware datetimes.

Files: `src/indus/foundation/clock.py`, `tests/unit/foundation/test_clock.py`.

Edge cases / failure modes:
- invalid zone (`Mars/Base`) → local zone, one warning per (home, name).
- routed scope with `INDUS_TIMEZONE` in the inherited env → ignored, config of that profile used.
- empty / whitespace values → treated as unset.
- zone name with surrogates → `%Z` rendered with U+FFFD instead of raising.

Our design differences / decisions:
- Cache keyed by (home, source, name) so a config change picks the new zone without a reset; still
  `reset_zone_cache()` for tests.
- `SystemClock.sleep_sync` exists for threaded waiters (file locks); it is not part of the protocol.
- No surrogate re-decoding through the ANSI code page; we replace with U+FFFD (simpler, never
  raises).

Tests we will write: Kolkata offset; invalid zone warns once; config zone; routed scope ignores env;
FakeClock conformance; surrogate formatting; system clock monotonic/now aware.

## Decisions for the lead
- None beyond the above.

Compare checklist (filled after the rewrite; reopened `hermes_time.py`):
- [x] env → config → local order; invalid falls back with a warning (tests:
  `tests/unit/foundation/test_clock.py::test_env_zone_kolkata`, `test_invalid_zone_warns_once`)
- [x] routed profiles do not read the process env var (test: `test_routed_scope_ignores_env`)
- [x] strftime never raises on surrogate zone names (test: `test_safe_strftime_surrogate_zone`)
- Deliberate differences: theirs re-decodes surrogate-escaped bytes with the ANSI code page; ours
  replaces them.

## Status

Done (2026-10-18).
