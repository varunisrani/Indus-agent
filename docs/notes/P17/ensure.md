# Task: Ensure and discovery (P17-15) Phase: 17 Owner: wave-27 builder K6

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/gateway_runtime_discovery.py`
(all: private control-path checks, peer uid, `identify` query, lock state),
`hermes_cli/gateway_runtime_start.py` (all: the one detached-start primitive, stripped env, stdio
log, startup failure report), `hermes_cli/gateway_client.py` (`connect_gateway`: the ensure → ticket
→ dial order and its verdicts). Research 05 §3.2 mode C steps 1–5 and §3.30 (budgets), research 08
§3.4 (ensure deadline, stale-gateway restart once). Our P15-27/28 `service/gateway_control.py`,
`state/gateway_runtime.py`, `cli/commands/gateway.py` (`start`/`stop`), P17-11 `describe`. Closed
before writing.
Evidence: none.

What it does (own words): a client that wants to attach to the profile's daemon first makes sure
one is running and learns where to dial. "Running" means one thing only: the control socket answers
`identify` for this home (a pid file or a pid that exists proves nothing, R06.18).

- A daemon answers: its `code_version` is compared with the CLI's own. Equal → the descriptor is
  returned. Different → the daemon is asked to restart gracefully (SIGUSR1: it refuses new turns,
  lets running ones finish, exits 75), we wait for that process to be gone, start a fresh one and
  wait for it to answer. That happens at most once per call; a fresh daemon that still reports
  another version is accepted (the verdict says `skew`).
- Nobody answers: a fresh status file whose process is alive means a daemon is still starting: we
  wait for its answer instead of starting a second one. Otherwise we start one: through the user's
  installed service when there is one (we never install a service), else as a detached process —
  double-forked on POSIX (an intermediate launcher starts `indus gateway run` in its own session
  and exits at once, so the daemon is nobody's child), stdin from /dev/null, stdout and stderr
  appended to `<home>/logs/gateway-stdio.log`, launch-option env vars removed, the
  "started by ensure" marker set for P17-23's idle exit. Then we wait for `identify` within the
  deadline (default 60 s). A daemon that exits first is reported with its exit code, its status
  file's exit reason and the log path.
- Modes. `start` (plain `indus gateway ensure`): start whatever is needed, also a daemon a person
  stopped on purpose. `discover` (`--no-start`, and every reconnect that is not a recovery): never
  start, never restart. `recover` (a client's automatic re-ensure after it lost the daemon): start
  only a daemon that died or idled out — a planned stop (`indus gateway stop`, recorded by P15-28
  as `planned_stop:<reason>`) is respected — and only within the recover budget: 3 per 60 s sliding
  window; a fourth is refused with the verdict `recover_budget`.
- The descriptor is credential-free: `{ws_url, profile_id, instance_id, code_version}` from
  `describe` (P17-11). `ws_url` stays null until the daemon mounts its WebSocket (P17-22).

Inputs → outputs: `ensure(home, env, mode=, timeout=, version=, service=, budget=, clock=)` →
`Ensured {state, reason, detail, endpoint?, started, restarted, skew}`; `indus gateway ensure
[--json] [--timeout 60] [--no-start]` → the descriptor (JSON with `--json`, one line otherwise),
exit 0; a verdict that is not attachable → exit 1 with the reason (`--json` prints
`{state, reason, detail}`).

Files: `src/indus/service/gateway_ensure.py`, `src/indus/cli/commands/gateway_ensure.py`,
`tests/integration/gateway/test_ensure.py`. Shared: `src/indus/cli/commands/gateway.py` (the lazy
`ensure` row), `tests/contract/cli/non_tty_table.yaml` (`gateway ensure` under `long_running`).

Edge cases / failure modes:
- home writable by others → `unsafe_home`, nothing started (the control client refuses too);
- a daemon starting (status file fresh, process alive, no answer yet) → wait, do not spawn;
- the spawned daemon exits before answering (exit 78 bad config, or "already running" because
  another client won the race) → for a race the winner answers and we return it; otherwise
  `exited` with the code and the log path;
- version skew under a service manager: SIGUSR1 makes it exit 75 and the manager starts the new
  code; we wait for an answer from another pid instead of spawning;
- skew restart that does not exit within the deadline → `timeout`, nothing else started;
- planned stop + `recover` → `stopped` (no start); planned stop + `start` → started;
- Windows: no control socket (D-16.55) → `unsupported`.

Our design differences / decisions:
- The module lives in `service` (`service/gateway_ensure.py`), not `gateway/daemon/ensure.py` as the
  phase file lists: the CLI (`indus gateway ensure`, P17-18's attach) and the service-layer attach
  channel both call it, and neither may import `indus.gateway` (layer rule; P15-28 put
  `gateway_control` / `gateway_manager` there for the same reason). It needs nothing from the
  gateway package: the control client, the runtime records and a process start.
- Double fork through a tiny stdlib launcher (`python -c`), which prints the daemon's pid on a pipe;
  no `os.fork()` in a threaded client.
- No multiplexing gateway and no `--profile` selector in the argv: the child gets `INDUS_HOME`
  (multiplexed profiles are P24).
- The descriptor has no `base_url`/control pointer: clients find the socket from the home.

Tests we will write (`tests/integration/gateway/test_ensure.py`, real daemons over the spool plugin):
- cold start then warm ensure: the second call does not spawn (same pid, `started` false);
- version skew: a daemon reporting another version is restarted once, then the descriptor comes back
  from a new pid with the CLI's version;
- planned stop respected by `discover` and `recover`, overridden by `start`;
- fourth recover within 60 s refused (fake clock), the window slides;
- a starting daemon is waited for, not doubled; an early exit is reported with the log path;
- the CLI command prints the descriptor JSON and no ticket; `--no-start` on a stopped profile
  exits 1 with `absent`.

## Decisions for the lead
- Path: `service/gateway_ensure.py` (above). No `gateway/daemon/ensure.py` file.
- `INDUS_GATEWAY_AUTOSTART=1` is set on a daemon `ensure` starts (not on one the service manager
  or `gateway start` starts): the seam P17-23's idle exit reads.
- Deliberate stop = the status file says `stopped` with `planned_stop:<reason>` and the reason is
  not `restart`; `idle_exit` and crashes are not deliberate.
- Version source: the daemon's `identify.code_version` against `indus.__version__`.

Compare checklist (filled after the rewrite; reopened the three reference files):
- [x] liveness only from an `identify` answer (test: `test_ensure.py::test_cold_start_then_warm_ensure_does_not_spawn`, `::test_a_starting_daemon_is_waited_for_not_doubled`)
- [x] detached start, own session, stdio to the log, launch env stripped (test: `test_ensure.py::test_the_started_daemon_is_detached_and_logs_to_the_stdio_log`)
- [x] skew restarted once, only in `start` mode; discovery only reports it (test: `test_ensure.py::test_version_skew_restarts_once_then_succeeds`)
- [x] planned stop respected by discovery and recovery, overridden by start (test: `test_ensure.py::test_a_planned_stop_is_respected_by_discovery_and_recovery`); a crash is recovered (`::test_a_crashed_daemon_is_recovered_within_the_budget`)
- [x] recover budget (test: `test_ensure.py::test_a_fourth_recover_within_60_s_is_refused`)
- [x] gap found: the reference shows the person the redacted tail of *this launch's* stdio output
  when the started daemon dies first — added (`_launch_tail`, credential shapes masked, last 15
  lines from the log offset taken before the spawn), pinned by `test_ensure.py::test_an_early_exit_is_reported_with_the_log`.
- Deliberate differences: the reference validates the control socket path itself here (owner,
  mode, pointer target name) and authenticates the listening process by its peer uid; we reuse
  P15's `gateway_control` client (home safety, private pointer and directory checks) and do not
  check the listener's uid (follow-up below). No `--profile` selector and no "served profile
  child env" (multiplexing is P24); no Windows breakaway (D-16.55).
- Not taken: the reference's lock-mode narrowing for upgraded homes (no such legacy homes here).

## Interface for other slices (K7 CLI attach, K8 grant helper)

```python
from indus.service.gateway_ensure import ensure, Ensured, Endpoint, RecoverBudget, Mode
ensured = ensure(home, scope.env.to_dict(), mode="start" | "discover" | "recover",
                 timeout=60.0, version=None, service=None, budget=None)   # blocking
ensured.attachable / .terminal / .state / .reason / .detail / .endpoint (ws_url, profile_id,
instance_id, code_version) / .pid / .started / .restarted / .skew / .to_json()
```
`indus gateway ensure [--json] [--timeout S] [--no-start]` prints `Endpoint.to_json()` (exit 0)
or `{state, reason, detail}` (exit 1).

## Follow-ups
- `gateway_control.query` does not check the listening process's uid (SO_PEERCRED on the client
  side); the reference does. P15 file; small change.
- `INDUS_GATEWAY_AUTOSTART` is set but nothing reads it until P17-23 (idle exit).

## Status

Done (2026-10-20). Shared: `src/indus/cli/commands/gateway.py` (lazy `ensure` row),
`tests/contract/cli/non_tty_table.yaml` (`gateway ensure` under `long_running`),
`docs/internals/traps-ledger.md` (R06.18 refs).

## Integration (P17-15..22 integrator, 2026-10-20)

Callers wired: `cli/attach_remote.DaemonBackend.ensure` (mode `start`) and
`cli/commands/gateway_grant.ensure_endpoint` (the grant's mode). `_child_env` now uses the unified
launch-env scrub (`creation_policy.scrub_launch_env`: the switch vars, `INDUS_MODEL`,
`INDUS_PROVIDER`, `INDUS_CWD`, `INDUS_LAUNCH_CWD` and every `INDUS_TUI_*`; D-16.498), pinned by
`test_ensure.py::test_the_started_daemon_is_detached_and_logs_to_the_stdio_log`.

Found by the end-to-end test under load: the control socket answers `identify` early in the start
and the WebSocket is mounted last, so a cold `ensure` could return `ready` with a null `ws_url`
(the CLI then said "no attach endpoint"). `_ready` now waits (within the deadline) while the
status file still says `starting` and `describe` has no `ws_url`; a running daemon without one is
final. Pinned by `test_ensure.py::test_cold_start_then_warm_ensure_does_not_spawn` (asserts the
URL) and `tests/integration/cli/test_attach_daemon.py::test_chat_starts_a_daemon_when_none_runs_and_local_starts_nothing`.
