# Task: Service install and `indus gateway` commands (P15-28) Phase: 15 Owner: wave-20 builder W4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/gateway.py`
(`generate_systemd_unit`, `_systemd_command`, `_mark_planned_stop`, `stop_profile_gateway`, the
systemd probe helpers 1194–1440, `get_service_name` / unit dir docstrings), `gateway/restart.py`
(`resolve_systemd_timeout_stop_sec` only), `hermes_cli/gateway_launchd.py` (plist builder and
the program-arguments wrapper), `hermes_cli/service_manager.py` (skim). Research 06 §3.22, traps
R06.17. Closed before writing.
Evidence: none.

What it does (own words): `indus gateway` runs and looks after the messaging gateway of the
current profile (`-p NAME` picks another):
- `run`: foreground daemon (P15-24, W3's command; registered here by `"module:attr"`).
- `start`: through the installed service when there is one, else a detached `indus gateway run`
  with its output in `<home>/logs/gateway-stdio.log`; waits until the control socket answers.
- `stop`: records a planned stop (pid + start time) before any signal, so the daemon knows the
  SIGTERM was asked for (not an external kill); then `systemctl --user stop` / launchd SIGTERM, or
  SIGTERM to the pid. It waits for the exit; it never sends SIGKILL on its own (`--force` after
  the wait only).
- `restart`: planned stop + service restart, or stop then start.
- `status`: the control socket first (live answer), then the runtime status file (stale after
  120 s, or when its pid is gone / reused), then the unit's state. `--json` for scripts. Exit 0
  when running, 1 otherwise.
- `install` / `uninstall`: a systemd **user** unit (Linux) or a launchd agent (macOS). No root,
  nothing written outside the user's own config dirs. Windows: prints that 1.0 has no native
  service (use WSL2, D-16.55).
Unit names: `indus-gateway.service` for the platform-default home, `indus-gateway-<hash>.service`
for any other home (hash = first 8 hex of sha256 of the home path); launchd label
`ai.indus.gateway[.<hash>]`. Exit codes (R06.17): 75 = restart me, 78 = fatal config.
systemd: `Restart=on-failure` (75 restarts) and `RestartPreventExitStatus=78`; launchd cannot gate
on an exit status, so a small `/bin/sh` wrapper maps 78 to 0 and `KeepAlive.SuccessfulExit=false`
leaves a clean exit (and a fatal config) stopped.

Inputs → outputs:
- `indus.service.gateway_units`: `service_name(home, default_home)`, `launchd_label(...)`,
  `timeout_stop_seconds(restart_drain, cron_drain)`, `systemd_unit(UnitSpec) -> str`,
  `launchd_plist(UnitSpec) -> str`, `UnitSpec.for_scope(scope, view)`, `user_unit_dir(env)`,
  `launch_agents_dir(env)`.
- `indus.service.gateway_manager`: `ServiceManager` per kind (`systemd`, `launchd`, `none`) with
  `installed()`, `install(spec, force)`, `uninstall()`, `start()`, `stop()`, `restart()`,
  `state() -> UnitState`; the `systemctl` / `launchctl` binaries are found on the scope's PATH
  (tests put a fake one there).
- `indus.service.gateway_runtime`: runtime-status reader (`read_status(home, now)` → running /
  stale / stopped), planned-stop record (`mark_planned_stop`, `read_planned_stop`,
  `clear_planned_stop`), `alive(pid, start_time)`, file names.
- `indus.cli.commands.gateway`: the click group.

Files: `src/indus/service/gateway_units.py`, `src/indus/service/gateway_manager.py`,
`src/indus/service/gateway_runtime.py`, `src/indus/cli/commands/gateway.py`,
`src/indus/cli/app.py` (shared: one registry row), `tests/unit/gateway/service/test_units.py`,
`tests/unit/cli/commands/test_gateway.py`, `tests/fixtures/gateway/units/*`.

Edge cases / failure modes:
- `start` while a gateway already answers: "already running (pid N)", exit 0, nothing spawned.
- detached child dies before answering: its exit code and the log path are printed, exit 1.
- `stop` with nothing running: "not running", exit 0; stale status file: reported as stale.
- `stop` on a pid that was reused by another process (start time differs): never signalled.
- `stop` that times out: says the gateway is still draining, exit 1 (no automatic SIGKILL).
- `status` on a dead pid with a fresh file → `stale`; a file older than 120 s → `stale`.
- `install` over a different existing unit without `--force` → refused with the diff hint;
  same content → "already installed"; `systemctl` missing → a clear message, exit 1.
- `install --system`: we never write system-wide; the unit text is printed with the commands
  to place it as root.
- unit text quoting: spaces, quotes, `%` and `$` in paths are escaped for systemd.
- the profile is pinned by `INDUS_HOME` in the unit, `INDUS_GATEWAY_SUPERVISED=1` tells the
  daemon a supervisor restarts it (P15-24 exit 75 semantics).

Our design differences / decisions:
- Our names (`indus-gateway`, `ai.indus.gateway`, `INDUS_HOME`, `INDUS_GATEWAY_SUPERVISED`).
- Shared code in `indus.service` (D-16.246), not `gateway/service/` as the phase file lists.
- Unit naming: bare name only for the platform-default home (`~/.indus`); any other home,
  including a custom `INDUS_HOME` root, gets the hash (a temp-home test can never name the real
  unit). The reference also uses profile names; we use the hash for every non-default home.
- `Restart=on-failure` (phase row) instead of `always`: 0 stays stopped, 75 and crashes restart,
  78 never does.
- `TimeoutStopSec = max(60, max(restart_drain, cron_drain + 10) + 30)` (drain headroom 30 s,
  60 s floor; the cron drain gets its 10 s cleanup reserve).
- `ExecStop=-<indus> gateway mark-stop --pid $MAINPID` so a plain `systemctl --user stop` is also
  recorded as planned; `ExecReload` sends SIGUSR1 (graceful restart, P15-25).
- launchd: a `/bin/sh -c` wrapper maps 78 → 0 (our own wrapper; no osascript identity trick, no
  stderr timestamper), `ExitTimeOut` 60, `ThrottleInterval` 30.
- `--system` prints instead of writing (task brief: no root, no system-wide writes).
- No s6 / Windows service / scale-to-zero / multiplex profiles (later phases or dropped).

Tests we will write:
- golden systemd unit (default home, other home, watchdog on, system variant) and plist files.
- `systemd-analyze verify` on the generated unit when the tool exists (skipped otherwise).
- naming: bare vs hashed; quoting of odd paths; timeout table.
- CLI with a fake `systemctl` on PATH: install writes the unit under `$XDG_CONFIG_HOME`, runs
  daemon-reload + enable; uninstall disables and removes; start/stop/restart call the right
  verbs; `stop` writes the planned-stop record before the stop call.
- CLI without a service: start spawns a stand-in daemon (test stub serving the real control
  socket), status answers through the socket, stop records the planned stop before SIGTERM and
  waits for the exit; status on a dead pid with a fresh file says stale.
- Windows: install prints the WSL2 note.

## Decisions for the lead
- The `run` subcommand target is one constant, `RUN_TARGET` in `cli/commands/gateway.py`
  (`indus.gateway.daemon.process:run_command` assumed); align it with W3's command at landing.
- Runtime status file name `gateway_status.json` and planned-stop record
  `gateway_planned_stop.json` in the home are assumed in `service/gateway_runtime.py`; W3's
  writer (P15-24) and drain classifier (P15-25) should use these helpers (or the lead renames the
  constants in one place).
- `alive(pid, start_time)` here reads the start time like `token_locks.process_start_time`
  (Linux `/proc/<pid>/stat` field 22, `ps` elsewhere); W3's `daemon/liveness.py` should import it
  from `indus.service.gateway_runtime` so the CLI and the daemon compare the same numbers.
- `agent.restart_drain_timeout`, `agent.cron_drain_timeout` and `gateway.systemd_watchdog_seconds`
  belong to P15-21/25; the unit reads them with defaults 0 / 30 / 0 when absent.

Compare checklist (filled after the rewrite; reopened the files above):
- [x] unit directives: Type simple/notify + NotifyAccess + WatchdogSec, Restart, RestartPreventExitStatus=78, KillMode=mixed, TimeoutStopSec from the drain budget, INDUS_HOME pinned, ExecReload → SIGUSR1 (tests: `test_units.py::test_golden`, `::test_timeout_stop_seconds`, `::test_systemd_analyze_accepts_the_unit`)
- [x] argv quoting for systemd (`%`, `$`, quotes, backslash) (test: `::test_quoting_neutralises_specifiers_and_expansion`)
- [x] planned stop recorded before the signal, also from the unit's ExecStop (tests: `test_gateway.py::test_start_status_stop_without_a_service`, `::test_service_start_stop_restart_status_uninstall`, `::test_mark_stop_records_a_planned_stop_for_the_unit`)
- [x] no unconditional kill on stop; a reused pid is never signalled (tests: `::test_a_reused_pid_is_never_signalled`, `::test_status_on_a_dead_pid_with_a_fresh_file_is_stale`)
- [x] status precedence socket → file → unit (tests: `::test_status_file_alone_counts_while_fresh`, `::test_service_start_stop_restart_status_uninstall`)
- [x] launchd SuccessfulExit=false, ExitTimeOut 60, ThrottleInterval 30, 78 → 0 (tests: `::test_plist_shape`, `::test_launchd_wrapper_maps_78_to_0`, `::test_launchd_wrapper_forwards_sigterm`)
- [x] gap found: unit directory must follow `$XDG_CONFIG_HOME` and the account home, not the
  process's (test: `::test_unit_dir_follows_xdg`)
- [x] gap found while testing: an exited-but-unreaped gateway (zombie) counted as alive, so
  `stop` waited out its budget — `alive()` now treats zombies as gone.
- Deliberate differences: `Restart=on-failure` instead of `always` + force/success status 75
  (same effect for 0/75/78); no start-limit override (the 5 s restart delay keeps under systemd's
  default burst); no cgroup cleanup `ExecStopPost`; launchd runs a plain `/bin/sh` wrapper (no
  osascript identity, no stderr timestamper, no `LimitLoadToSessionType`); unit names hash every
  non-default home (no profile-name suffix); `--system` prints only.
- Not taken: s6 / Windows service / detached-watcher update hand-off / orphan sweeps of older
  gateways (the pid lock of P15-24 keeps one gateway per profile).

## Status

Done (2026-10-19). Shared: `src/indus/cli/app.py` (one registry row `gateway`). `run` is loaded
from `RUN_TARGET` (`indus.gateway.daemon.command:run_command`). Integration (lead): the stand-in
is gone; `tests/unit/cli/commands/test_gateway.py` starts and stops the real `indus gateway run`
over the spool plugin platform, and the runtime records live in `indus.state.gateway_runtime`
(`runtime_view`, `record_planned_stop`, `alive`; the status file is `gateway_state.json`).
