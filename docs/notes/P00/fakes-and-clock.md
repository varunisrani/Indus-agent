# Task: fakes skeleton, fake clock and the no-sleep rule (P00-11) Phase: 00 Owner: P00 fakes/trial builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tests/fakes/fake_llm_provider.py`
(skimmed the list of scripted response kinds only: plain text, tool call, HTTP error, hang, drop
mid-stream, stall mid-stream, raw body), `tests/conftest.py` lines 1–120 (the idea: point the
product home at a per-test temp dir before anything resolves it, and strip inherited product env
vars). Closed before writing.
Evidence: none.

What it does (own words): gives every later test the same deterministic building blocks. A fake
clock that tests drive by hand, so no test waits on real time; the vocabulary in which fake model
scripts are written (the wire servers that speak it come in P03-18); first skeletons of a messaging
platform fake and a terminal backend fake; an autouse fixture that isolates `INDUS_HOME`; and a ruff
rule that makes `time.sleep` / `asyncio.sleep` a lint error everywhere except the two clock modules.

Inputs → outputs:
- `FakeClock(start_wall, start_mono=0.0)`: `now()` (aware UTC datetime), `monotonic()` (float),
  `await sleep(s)` parks until `advance()` passes the deadline; `advance(s)` → number of sleepers
  released, earliest deadline first, equal deadlines in arrival order; `await run_until_idle()`
  lets released tasks run; `sleep_sync(s)` blocks a worker thread and only works after
  `allow_threaded_advance()` (another thread must call `advance`); otherwise it raises.
  `checkpoint()` (module function) yields one loop iteration: the one sanctioned way for test code
  to give way to other tasks without a timed sleep.
- `tests/fakes/model`: frozen dataclasses `Text`, `Reasoning`, `ToolCall`, `Usage`, `Fail`, `Stall`,
  `Disconnect`, `Finish`, `Turn`, `Script`; `load_script(path)` reads `.yaml`/`.yml`/`.json`.
- `FakePlatform`: `await send(chat_id, text, attachments=())` → message id, `await edit(...)`,
  records in `sent` / `edits`; inbound events scripted with `push_inbound()`, read in order with
  `await receive()` (None when the script is used up).
- `FakeBackend`: `script(pattern, exit_code=0, stdout="", stderr="", duration=0.0)`; `await
  run(argv, cwd=None, env=None)` → `CommandResult`; every call recorded with argv, cwd and a copy of
  env. Patterns are shell-style globs over the joined command line, first match wins.
- `tests/conftest.py`: autouse `isolated_home` (clears all `INDUS_*`, sets `INDUS_HOME` to a fresh
  `tmp_path / "home"`); fixtures `fake_clock`, `fake_model_script` (load by fixture name).

Script file format (ours): a top-level mapping with `turns:`, a list of mappings each holding
`steps:`, a list of one-key mappings `kind: value`. Kinds: `text` / `reasoning` (list of chunks or
one string), `tool_call` (mapping: name, arguments, id, fragments), `usage` (prompt, completion,
cached), `fail` (status, body, headers), `stall` (seconds), `disconnect` (empty), `finish` (reason).

Files: `tests/conftest.py`, `tests/fakes/__init__.py`, `tests/fakes/clock.py`,
`tests/fakes/model/__init__.py`, `tests/fakes/model/mini_yaml.py`, `tests/fakes/platform/__init__.py`,
`tests/fakes/backend/__init__.py`, `tests/unit/fakes/test_clock.py`,
`tests/unit/fakes/test_model_script.py`, `tests/unit/fakes/test_platform_backend.py`,
`tests/unit/fakes/test_no_sleep_rule.py`, `tests/unit/fakes/test_home_isolation.py`,
`tests/fixtures/model_scripts/hello.yaml`, `pyproject.toml` (ruff section only).

Edge cases / failure modes:
- naive `start_wall` or non-UTC offset → `ValueError`; negative `advance` or `sleep` → `ValueError`.
- `sleep(0)` returns after one checkpoint, never parks.
- a cancelled sleeper is removed from the wait list, so `advance` does not count it.
- sleepers released by `advance` all see the time after the whole advance (it is synchronous); a
  test that needs each sleeper to see its own deadline advances in steps.
- `advance` from a foreign thread wakes asyncio sleepers through `call_soon_threadsafe`.
- `sleep_sync` without threaded advance would block forever on a single thread → raises at once.
- unknown step kind, step that is not a one-key mapping, missing `turns` → `ScriptError` naming the
  file and line (YAML) or the turn/step index (JSON, which has no line info from the stdlib parser).
- unscripted backend command → `UnscriptedCommand` naming the command (never a silent default).

Our design differences / decisions:
- No YAML library is in the lock yet (ruamel.yaml arrives with P01-08), so the loader uses a small
  strict YAML-subset reader of our own (`mini_yaml.py`: block mappings and sequences, plain and
  quoted scalars, one-line flow lists/maps, comments). It rejects anything else with the line.
- Fakes are async where the real seam will be async (platform adapters, terminal backend).
- No pytest-asyncio yet: these tests drive the loop with `asyncio.run`, which is enough.

Tests we will write:
- clock: wake order 1,1,5 after `advance(5)`; `advance(0.5)` wakes none; cancel removes a sleeper;
  `now()`/`monotonic()` move with `advance`; threaded `sleep_sync`; guard errors.
- script: `hello.yaml` → one `Turn` starting with `Text`; every step kind round-trips; unknown kind
  names the line; JSON scripts load; subset reader rejects unsupported syntax.
- platform/backend: record and replay.
- lint: ruff on a temp file with `time.sleep(1)` / `asyncio.sleep` reports TID251; the fake clock
  module itself is not flagged.
- home: `INDUS_HOME` is under `tmp_path`; a stray `INDUS_*` var is gone.

## Decisions for the lead
- YAML reading uses our own subset reader until ruamel.yaml is in the lock; swap `load_script` to
  ruamel in P01-08/P03-18 and delete `mini_yaml.py` (the script format stays the same).
- Dev deps `pytest-asyncio==1.4.0` and `hypothesis==6.168.3` named by the task row were not added:
  nothing here needs them and this slice may only touch the ruff section of `pyproject.toml`. The
  first task that writes an async test fixture or a property test adds them (with `asyncio_mode =
  "strict"` and the `ci` profile).
- `checkpoint()` lives in `tests/fakes/clock.py` (one of the two files allowed to call
  `asyncio.sleep`) so test helpers can yield a loop iteration without a lint exception.
- `tests/__init__.py`, `tests/unit/__init__.py`, `tests/unit/fakes/__init__.py` were added so
  `tests.fakes...` imports resolve under pytest's rootdir insertion without a `pythonpath` setting.
- The autouse fixture clears every `INDUS_*` var as the task says, including `INDUS_REF`; a future
  `ref`-marked test under `tests/` must read the reference path before isolation or the fixture
  must keep `INDUS_REF`.
- `FakeClock.wait_for_sleepers(n)` was added so threaded tests can wait for a parked `sleep_sync`
  on a condition instead of polling.
- ruff selects only `TID251` from the tidy-imports family (not `TID`), so relative-import rules do
  not start firing on existing code.

Compare checklist (filled after the rewrite; reopened `tests/fakes/fake_llm_provider.py`,
`tests/conftest.py` 1–120):
- [x] Every scripted behaviour the reference fake offers has a step kind here: text (+ reasoning),
  tool call (incl. a raw string for malformed arguments), HTTP error (`Fail` with headers for
  retry-after), hang (`Stall`), drop mid-stream (`Text` then `Disconnect`), stall mid-stream
  (`Text` then `Stall`). Raw non-JSON bodies are `Fail(status, body)` with any status. (test:
  `tests/unit/fakes/test_model_script.py::test_every_step_kind_loads`)
- [x] Home isolation before use and env stripping (test:
  `tests/unit/fakes/test_home_isolation.py`).
- Deliberate differences: the reference fake is a live HTTP server with Python-object scripts; ours
  is a data vocabulary loaded from files, the server comes later. The reference also strips
  credential-shaped env vars and pins TZ/locale in its conftest; we only clear `INDUS_*` now (task
  scope) — see follow-up.
- Not taken: per-request responders (callables) and aux/main request routing; P03-18 decides.

## Status

Done (2026-10-18). Follow-up for the lead: consider clearing credential-shaped env vars and pinning
`TZ=UTC` in the autouse fixture once providers read keys from env (P03).
