# Task: In-session loops /loop (P10-25) Phase: 10 Owner: P10 slice K builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/loops.py` (grammar,
self-paced cadence, completion marker), `hermes_cli/cli_loops_mixin.py` (CLI driver, skimmed).
Closed before writing.
Evidence: none.

What it does (own words): `/loop <interval|auto> <prompt> [--times N] [--until "<condition>"]`
repeats a prompt as ordinary user turns of this session; `/loop list` and `/loop stop [id]`. A
session may hold several loops, each with an id. Interval tokens `30s`, `5m`, `2h`, `1h30m`; below
the floor `loops.min_interval_seconds` (30, never under 5) the floor is used and said so. `auto`
starts at 60 s, doubles while the reply's final text hashes the same, caps at 15 min and drops back
to 60 s on any change. A loop ends when the reply's last line is the marker `LOOP_COMPLETE` (own
line, any case, optional `.`/`!`), after `--times` runs, or when the `--until` judge (the goal
judge client) rules the condition met; `loops.max_ticks` (100, 0 = no limit) pauses it. A tick only
fires when the session is idle and nothing is waiting in its input queue; state and the next due
time live in `session_meta` namespace `loops`; `loop.update` events feed the TUI loop bar.

Inputs → outputs: slash text → reply lines; scheduler poll (now) → due loops → injected turn;
turn end → completion check, next due time.

Files: `src/indus/core/loops.py`, `tests/unit/core/test_loops.py`. Shared: as goals (events,
config `loops`, registry entry `/loop`).

Edge cases / failure modes:
- The first tick fires right away (due now) unless the session is busy.
- Busy session: the tick waits; it fires on the first poll after the session is idle.
- Process restart: a tick that was in flight is not re-fired; the next due time stands.
- The marker inside a sentence does not end the loop; on its own line it does.
- A bare number is not an interval (`/loop 3 things` is a prompt).

Our design differences / decisions:
- Several loops per session (ids `l1`, `l2`, …) with `list` / `stop [id]` (`stop` alone stops all).
- `auto` instead of an omitted interval; the hash ignores whitespace and case only.
- Wakeup text written fresh; the marker word is kept generic (`LOOP_COMPLETE`, lead to confirm).

Tests we will write:
- interval parser table (`1h30m`, `10s` below the floor → 30 s, bad tokens);
- self-paced 60 → 120 → 240 then back to 60 on a change (fake clock);
- marker on its own line ends, mid-sentence does not; `--times 3` stops after 3;
- busy session delays the tick; `max_ticks` pause; `--until` judge.

## Decisions for the lead
- Marker word `LOOP_COMPLETE` kept (row: lead to confirm).
- Intended differences: several loops per session with ids; `auto` keyword; `/loop resume [id]`
  added so a loop the tick budget paused can go on (the row lists only list|stop).

Compare checklist (filled after the rewrite; reopened `hermes_cli/loops.py`):
- [x] Grammar: tokens, `every` sugar, bare number is prompt text, floor 30 (never under 5) (`tests/unit/core/test_loops.py::test_interval_tokens`, `test_parse_loop_args`, `test_floor_is_configurable_but_never_below_five`)
- [x] Self-paced 60 → 120 → 240, back to 60 on change, cap 15 min (`test_self_paced_doubles_while_unchanged_and_snaps_back`, `test_self_paced_caps_at_fifteen_minutes`)
- [x] Marker only on its own last line (`test_marker_must_be_the_last_line`, `test_marker_on_its_own_line_ends_the_loop`)
- [x] `--times`, `--until`, `max_ticks` pause and resume (`test_times_stops_after_n_runs`, `test_until_judge_ends_the_loop`, `test_tick_budget_pauses_and_resume_goes_on`)
- [x] Busy session / queued input delays the run (`test_busy_session_delays_the_tick`); first run at once (`test_first_run_is_due_at_once_and_fires_when_idle`)
- [x] Resume re-arms a few seconds out, never a burst (same as the reference)
- [x] Restart: an in-flight run is not re-fired (`test_a_run_in_flight_at_a_restart_is_not_fired_again`)
- Deliberate differences: several loops per session with ids; `auto` keyword instead of an omitted interval; the digest ignores only case and spacing (the reference also strips clock and date tokens); a blocked `--until` verdict does not exist (no blocked verdict in our judge).
- Not taken: looping a slash command (`/loop 10m /recap` sends the text to the model), gateway chat routing in the loop row (the gateway hosts its own sessions through the same service).

## Status

Done (2026-10-19). Scheduler shared with P10-26 (`core/session_scheduler.py`). Config rows `loops.min_interval_seconds`, `loops.max_ticks`.
