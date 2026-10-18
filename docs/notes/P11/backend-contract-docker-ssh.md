# Task: Contract suite for Docker and SSH + live lanes (P11-21) Phase: 11 Owner: lead (integration)

Read: the phase file (P11-21 row) and the P05 contract suite (`tests/contract/backends/`). The
reference's test edge-case lists were not reopened: every case here is the plan's list.
Evidence: none.

What it does (own words): the P05 execution-contract suite runs once per backend row. P11 adds two
rows, `docker` over the fake `docker` client and `ssh` over the fake `ssh`/`scp` clients, so every
existing case (protocol, output and marker, trailing newline, `cd`, exit without marker, odd bytes,
the 1 s deadline with rc 124 and the flag, a command's own 124, interrupt rc 130, stdin, bounded
and unbounded capture) runs against both unchanged. A second file adds what a backend that is not
this machine must also keep:

- per-session values never frozen into the snapshot (R02.1), for `local`, `docker` and `ssh`;
- values never on a client command line (R02.7), from the fake clients' recorded argv;
- the `environment_recreated` warning once after a replaced container (and never on SSH);
- background jobs started, polled and killed inside the backend (exit code, output, `killed`);
- a lost connection (daemon gone, connection refused) answers `degraded` with a reason and hint.

Live lanes: `tests/integration/backends/test_docker_live.py` (marker `docker`; runs where a daemon
answers and `INDUS_LIVE_DOCKER_IMAGE`, default `debian:bookworm-slim`, is present, never pulls)
checks a hardened run, the `/proc/<pid>/cmdline` scan for a passthrough value while the client
runs, and a background job that notifies. `tests/integration/backends/test_ssh_live.py` (markers
`docker` and `network`, opt-in with `INDUS_LIVE_SSH=1`) builds `tests/fixtures/backends/sshd`,
runs it with a fresh key, and checks a remote run, the cmdline scan and a background job. Its
ssh/scp wrappers keep the known-hosts file in the test's temp dir (our client has no option for it,
and OpenSSH would otherwise write the real home's `~/.ssh/known_hosts`).

Files: `tests/contract/backends/conftest.py` (rows, rig, fixtures `backend`, `shell_backend`,
`remote_backend`), `tests/contract/backends/test_docker_ssh_contract.py`,
`tests/integration/backends/_live.py`, `tests/integration/backends/test_docker_live.py`,
`tests/integration/backends/test_ssh_live.py`, `tests/fixtures/backends/sshd/Dockerfile`,
`pyproject.toml` (marker `docker`). Shared: `src/indus/backends/local.py` (see below),
`tests/fakes/backend/ssh_cli.py` (`redirect_remote_tmp`).

Edge cases:
- the fake clients run "container" and "remote" commands on this machine: each test points the
  Docker sandbox temp dir and the SSH remote temp dir at its own directory, so nothing lands in the
  shared `/tmp` (and another process's files there are never touched);
- a daemon that is down when the environment is first created is `disabled` (availability probe),
  not `degraded`; the degraded case is a connection lost after a good call;
- control sockets need a short private dir (macOS 104-byte limit): a `mkdtemp` in `/tmp`, removed
  after the test.

Found while writing (fixed): the local backend froze passthrough values into its snapshot (the
R02.1 case failed for `local`): `LocalEnvironment._excluded_names` now returns the accepted
passthrough names, as Docker and SSH already did; the values are injected fresh for every command.

Tests: 12 shared cases × 4 rows (`local`, `docker`, `ssh`, `fake`) = 48, plus 11 cases in
`test_docker_ssh_contract.py`; live lanes skip here (no Docker daemon in this container).

Decisions for the lead:
- The SSH live lane is opt-in (`INDUS_LIVE_SSH=1`) because it builds an image; the Docker lane runs
  wherever its image is already present. A CI job that pre-pulls the image and sets the variable is
  still to add (workflow is lead-owned).

Compare checklist:
- [x] every P05 contract case green for `docker` and `ssh` over the fakes (test: `tests/contract/backends/test_execution_contract.py`)
- [x] R02.1 for every shell row (test: `tests/contract/backends/test_docker_ssh_contract.py::test_session_values_are_never_frozen_into_the_snapshot`)
- [x] R02.7 argv (test: `::test_values_never_ride_a_client_command_line`); live cmdline scan (`tests/integration/backends/test_docker_live.py::test_hardened_run_and_values_off_the_command_line`, skipped here)
- [x] recreated warning once (test: `::test_replaced_container_carries_the_warning_once`)
- [x] background start/poll/kill (test: `::test_background_job_starts_polls_and_is_killed_inside`)
- [x] degraded on a lost connection (test: `::test_lost_connection_answers_degraded`)
