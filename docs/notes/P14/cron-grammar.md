# Task: Schedule grammar (P14-01) Phase: 14 Owner: wave-17 builder C1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/jobs.py` (`parse_schedule`,
`parse_duration`, the clock-time and day-spec helpers above it), `website/docs/user-guide/
features/cron.md` (skimmed). Closed before writing.
Research: plan decisions P6 (bare duration repeats), D-16.36, D-16.54 (`every Nd at HH:MM`),
traps R07.9, R07.10, R07.11. Evidence: none.

What it does (own words): turns what a person types into one of three schedules. Tried in order:
1. a day/time phrase, with or without a leading `every`: a day spec (`day`, `daily`, `everyday`
   → every day; `weekday(s)` → Mon–Fri; `weekend(s)` → Sat+Sun; or a list of weekday names /
   abbreviations joined by spaces, commas or `and`), an optional `at`, and a time (`9am`,
   `9:30pm`, `14:00`, bare 24 h hour `7`, `noon`/`midday`, `midnight`) → 5-field cron;
2. `every <N> day(s) at <time>` (also `every Nd at HH:MM`): N = 1 → daily cron; N = 7 → weekly
   cron on the weekday of the first occurrence; any other N → an interval of N days anchored on
   the next occurrence of that wall-clock time (see decisions);
3. any other `every <duration>` → interval; durations are `<n><unit>` with m/min/mins/minute(s),
   h/hr/hrs/hour(s), d/day(s), a bare unit meaning 1;
4. five or more whitespace-separated fields made of letters, digits and `*,-/` → cron (exactly
   five accepted; checked with croniter);
5. an ISO timestamp (contains `T` or starts `YYYY-MM-DD`) → once; a naive time is read in the
   configured agent zone (`timezone` / `INDUS_TIMEZONE`), never the server's;
6. `in <duration>` → once at now + duration, computed in UTC;
7. a bare duration (`30m`) → recurring interval (P6);
8. anything else → a multi-line error listing the accepted forms (our wording).

Every schedule carries a canonical `display` string that parses back to the same schedule:
`every 30m` / `every 2h` / `every 1d`, `every 3d at 09:00`, cron expression for raw cron,
`daily at 09:00` / `weekdays at 09:00` / `weekends at 09:00` / `mon,wed,fri at 07:00` for
phrases, and the ISO instant in the configured zone with its offset for one-shots.

Inputs → outputs: `parse_schedule(text, tz, *, now=None)` → `OnceSchedule | IntervalSchedule |
CronSchedule` (see `cron-store.md` for shapes) or `ScheduleError`.

Files: `src/indus/cron/grammar.py`, `src/indus/cron/errors.py`, `tests/unit/cron/test_grammar.py`;
shared: `pyproject.toml` (croniter 6.2.4 — row already in 00-layout §4), `uv.lock` (regenerated
locally only; lead re-locks).

Edge cases / failure modes:
- case and surrounding whitespace ignored; `Tuesday 9am` is a phrase, not an ISO timestamp;
- `every tuesday` (no time) is neither a phrase nor a duration → the forms error;
- `0m`, `every 0h` → error (a zero interval would spin);
- `13pm`, `25:00`, `9:75` → not a time → error;
- six-field cron → error saying five fields are expected (no seconds field);
- invalid cron (`61 * * * *`) → error naming croniter's complaint;
- `in` durations across a DST change measure elapsed time (UTC arithmetic);
- naive ISO in a DST gap is resolved to the next valid instant; an ISO with an offset is kept.

Our design differences / decisions:
- D-16.54 (`every Nd at HH:MM`): N = 1 daily cron, N = 7 weekly cron, otherwise an anchored
  interval (true every-N-days cadence across month ends; a day-of-month step like `*/2` would
  run on the 31st and the 1st back to back). Anchored intervals step in UTC, so the wall-clock
  time moves by the DST offset change twice a year — documented in the display help.
- Exactly five cron fields (no croniter seconds field).
- Display strings are canonical rather than echoing the input.

Tests we will write: the plan's table (`30m`, `every 30m`, `in 30m`, `weekdays at 9am`,
`mon, wed and fri 7`, `every 1d at 09:00`, naive ISO in Europe/Berlin, `noon`, `midnight`,
garbage), more phrase/duration/cron rows, error cases, and a seeded property table: every
accepted string round-trips through its display to the same schedule (hypothesis is not
installed; a seeded generator stands in, as in P02).

## Decisions for the lead
- D-16.54 resolution above (N = 7 weekly, other N anchored interval) — record as the D-id text.
- hypothesis not added (not in the dev group yet); property tests are seeded tables.

Compare checklist (filled after the rewrite; reopened `cron/jobs.py` `parse_schedule` and helpers):
- [x] order of forms and every accepted form (test: `tests/unit/cron/test_grammar.py::test_phrases`,
  `::test_durations`, `::test_cron_expressions`)
- [x] bare duration recurring, `in` once, naive ISO in the configured zone
  (`::test_bare_duration_is_recurring`, `::test_in_duration_is_once`,
  `::test_naive_iso_uses_configured_zone`)
- [x] `every Nd at HH:MM` (the reference rejects it, R07.10) (`::test_every_1d_at_is_daily`,
  `::test_every_n_days_anchored_interval`, `::test_every_7_days_is_weekly_cron`)
- [x] error lists the forms (`::test_garbage_lists_the_forms`)
- [x] gap found: five plain words matched the cron shape and produced a croniter error instead of
  the forms list — fixed (a cron candidate needs a digit or `*`), pinned by
  `::test_garbage_lists_the_forms`
- Deliberate differences: the reference accepts croniter's six-field (seconds) form, we take five;
  the reference also knows `tues`/`thur(s)`/`weds`, we take full names, plurals and three letters;
  displays are canonical (the reference echoes the input); D-16.54 resolution above.
- Not taken: nothing.

## Status

Done (2026-10-19). Shared: `pyproject.toml` (+ `croniter==6.2.4`, row already in 00-layout §4),
`uv.lock` regenerated locally only (lead re-locks).
