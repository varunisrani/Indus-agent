# Task: root-cause four tests that failed once under full-suite load (wave 32, T1) Phase: testing Owner: builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none (test-harness and backstop
fixes in our own code; the reference has no fake-clock drivers). Evidence: none. Background:
`docs/notes/testing/drive-holds-clock.md` (155ef32), the failing full-run logs (int-rest run 1,
int-p19b runs 2 and 4, the 155ef32 gate).

Each test failed once in a loaded full run and passed alone. Each was reproduced on purpose
(probes kept out of the tree, in the session scratchpad), fixed at the root, and proved with the
fixed test 20 times under three CPU spinners (20/20 runs, 5 tests each, 0 failures).

## 1. `test_d04_1_terminal_tool_backstop.py::test_tool_answers_through_the_backstop_when_the_wait_is_wedged`

Failure: `assert _gone(pid)` false right after the tool answered (exit 124 and the timings held).

Root cause (product): the hard backstop (`wait.run_bounded`) force-kills the tree
(`local.force_kill_tree`, SIGKILL, "no waiting") and answers at once. SIGKILL acts only when the
target next runs; on a loaded machine the dying `sleep` was still `S` when the test read
`/proc/<pid>/stat` a few milliseconds later. The ordered kill (`stop_tree`) already waits a
bounded time for the tree to be gone for exactly this reason; the backstop did not, so the
answer "killed" could precede the death (a following command could still see the old process).

Reproduction: TERM and KILL delivered 0.3 s after they are sent (`os.kill` / `os.killpg` wrapped
with a `threading.Timer`), the test body unchanged: 3/3 failed with the same assertion; with the
fix 3/3 passed.

Fix: `run_bounded` takes a `reap` awaitable run after `on_expiry`; `SpawnedEnvironment` passes one
that calls the new hook `_await_killed(handle, BACKSTOP_REAP_S)` (default: poll `handle.poll()`
every `REAP_POLL_S` = 50 ms on the environment's clock for at most `BACKSTOP_REAP_S` = 0.5 s);
`LocalEnvironment` overrides it to wait for the leader and every running member of its process
group (`_await_group_gone`). The answer still comes at timeout + 2 s plus at most 0.5 s (the test
allows + 3 s). Other backends keep their `_force_kill`; the default leader wait returns at once
for handles that finish on force-kill (SDK, fakes). New tests: the late-signal case as
`test_backstop_answers_only_once_the_killed_child_is_gone` (same file) and
`tests/unit/backends/test_wait.py::test_backstop_answers_after_the_killed_tree_is_reaped`.

## 2. `tests/unit/cron/test_ticker.py::test_daemon_fires_an_every_30m_job_twice_in_an_hour`

Failure: one agent call, not two (log: "cron cleanup output pruning … took longer than 10 s").

Root cause (test): its loop advanced the clock 10 s after every 0.5 ms of real time, parked or
not. Fake time therefore followed real time spent on the loop thread: whenever the loop was held
up between a tick's wake and its next 60 s sleep (preemption under load, sync file and sqlite
work in the tick and the run), the test advanced again before the new sleeper was registered,
and that tick and every later one shifted by 10 s; the first run's end (the base of the next
interval slot) shifted the same way. Three such stalls in 61 ticks pushed the second fire past
the 61:30 window. The pruning log is a symptom: the pruning thread (counted by
`awaiting_off_loop`, but this loop never looked) lost its 10 s budget to one 10 s step.
`awaited_calls()` alone would not have fixed it: the decisive delays are on the loop thread.

Reproduction: 1 ms of real delay on the loop thread right after three of the tick wakes
(`FakeClock.sleep` wrapped for the 60 s sleeps): 5/5 failed with `1 == 2` (first run ending at
30:50–31:00, ticks at 70 s intervals); the probe without delays never failed (30/30 under three
spinners, ticks exactly on the minute).

Fix: the loop calls the new `tests.fakes.clock.quiesce(clock)` before each advance: run every
ready callback, then hold while an awaited off-loop call is in flight. Fake time now moves only
when the daemon is parked. The same delays, and a 2 ms delay after every wake plus 1 ms in every
tick marker write: 5/5 passed. Assertions unchanged.

## 3. `tests/unit/gateway/adapters/matrix/test_adapter.py::test_media_over_the_cap_is_refused_before_upload`

Failure: "never finished on the fake clock".

Root cause (test): the file's `drive` gives the task 4000 rounds of `run_until_idle` + 0.5 s
advance. `send_file` stats the file on a blocking-pool worker; while it runs the loop is idle,
so the 4000 rounds (2000 fake seconds) go by in a few milliseconds of real time, before a slow
worker under load hands the stat back.

Reproduction: `Path.stat` of the test file taking 0.2 s real: 3/3 failed with the same message;
with the fix 3/3 passed.

Fix: `drive` (and `pump`) use `quiesce(clock)` instead of `run_until_idle()`.

## 4. `tests/unit/gateway/adapters/msgraph/test_adapter.py::test_renewal_fires_at_eighty_percent_of_the_lifetime`

Failure: `until(lambda: rig.wire.of("PATCH"))` at 48 min: "the condition never held".

Root cause (test): `until(POST and store.exists())` returns on a side effect written by the
store-save thread, while the subscription task is still on its way to its next clock wait. When
the save's result and `until`'s 2 ms timer land in the same loop iteration, the task resumes,
computes `now` (0:00) and asks for a 48 min sleep, but `until` returns and the test advances
47:59 before that sleeper's first step registers it; the sleeper then counts its 48 minutes from
47:59, the 48:00 renewal never comes and `until` times out. The usual run passes only because
the save result normally arrives after the advance (the keeper then sees 48:00 and renews at
once).

Reproduction: a long chain of ready callbacks (5000 loop turns) between the keeper's resume and
its clock sleep, as a busy loop would cause: 7/10, 5/10, 4/10, 3/10 failed (store write 0, 1,
3, 10 ms), same message; with the fix 20/20 passed.

Fix: `until` ends with `quiesce(clock)`: once the condition holds it waits until every task is
parked again (no ready callback, no awaited off-loop call) before the test moves the clock.

## 5. The class fix: one shared hold for hand-rolled clock loops

`tests/fakes/clock.py` (module notes updated):
- `may_advance(clock)`: `awaited_calls() <= clock.sync_sleepers` (the rule `turn.drive` used;
  `turn.drive` now calls it).
- `hold_for_off_loop(clock, limit=HOLD_LIMIT_S)`: returns at once when `may_advance`; otherwise
  waits in 0.5 ms real steps until it holds, then lets the loop run what the returning calls
  made ready; fails after 30 s real of holding.
- `quiesce(clock)`: `run_until_idle()` then `hold_for_off_loop`.

Switched (helpers that advance while a blocking-pool call can be in flight on their path):
gateway adapter `drive`s in matrix (and `pump`), feishu, mattermost, wecom, weixin (and
`settle`), qqbot (`drive`, `until`; `test_upload.py` `drive`), yuanbao (`drive`, `until`);
msgraph `until`; delivery `test_send_p16_platforms._drive` and `kit.until`; contract gateway
`conftest.settle`; MCP `tests/fakes/mcp/scripted.py` (`run_for`, `drive`) and
`tests/unit/mcp/test_reload.pump`; the terminal rig `_drive`
(`tests/unit/tools/builtin/_terminal_rig.py`, with `hold=False` for one test, see below); the
cron ticker test. When nothing is off the loop each helper behaves exactly as before.

Not switched, with the reason:
- `tests/unit/backends/conftest.drive`: a command's output reader (`drain` on the terminal pool)
  is an awaited pool call for the command's whole life while its poll loop and deadline need
  the clock to move; with the hold `docker/test_environment.py::test_deadline_kills_inside_the_container_too`
  held until the limit. Its loop already gives readers a 2 ms real wait per step.
- dingtalk, google_chat, slack, providers (retry, streaming, device code, PKCE) and the other
  `until`/`settle` helpers: no blocking-pool call on the paths they drive (checked by grep of
  the product modules), so nothing to hold for.

One test changed its wait: `test_terminal_plan.py::test_guard_deadline_overrun_refuses` simulates
a guard stuck in real time past its 30 s clock deadline, so with the rig's hold it would hold
until the limit; it passes `hold=False` (assertions unchanged), as `test_output.py` did in 155ef32.

Tests for the helpers: `tests/unit/fakes/test_clock.py` (may_advance under an awaited call; a
`sleep_sync` thread does not hold; `quiesce` waits for a pool call and its caller to park again;
a stuck call fails the hold).

## Status

Done. Checks: see the report (ruff, format, pyright on touched files, lint-imports, touched areas
idle and under load, the five target tests 20× under three spinners, `tooling.checks
--require-ref`).

## Decisions for the lead
- `BACKSTOP_REAP_S` = 0.5 s (same as the hard-exit kill budget) and the poll interval 50 ms
  (as `stop_tree`). Change in `src/indus/backends/wait.py`.
- `quiesce` relies on the loop's private `_ready` (as `FakeClock.run_until_idle` already did).
- Residual, not changed: `turn.drive` still moves the clock between steps of a long on-loop chain
  on a starved machine (its note's Risks); `quiesce` would close that for drivers whose loops
  never keep a callback ready at all times.

Compare checklist: n/a (no reference counterpart).
