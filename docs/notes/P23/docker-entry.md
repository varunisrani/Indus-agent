# Task: Container entry: dispatcher, stage-2, wrapper (P23-10) Phase: 23 Owner: wave-36 builder K1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `docker/entrypoint-dispatch.sh` (all),
`docker/stage2-hook.sh` (UID remap, refusal paths, symlink checks, seeding, socket group),
`docker/main-wrapper.sh` (all), `docker/entrypoint.sh` (all), `docker/s6-rc.d/` (service layout).
Closed before writing.
Evidence: none.

What it does (own words): three POSIX `sh` scripts decide how a container starts.

1. `docker/entrypoint.sh` (ENTRYPOINT). As PID 1 it records the start directory
   (`INDUS_START_DIR`) and execs s6-overlay's `/init` with the wrapper as the main program; s6 then
   runs the `indus-prepare` oneshot (stage-2) and the enabled services before the wrapper. Not PID 1
   (a platform init or `docker run --init` owns PID 1; `/init` would abort) → one warning on stderr,
   stage-2 directly, then exec the wrapper; supervised services are unavailable in that mode.
2. `docker/stage2.sh` (root, before any service or the main program):
   - started as a UID that is neither root nor the `indus` user (a `docker run --user` start) →
     refuse, exit 1, with guidance to start as root and pass `INDUS_UID`/`INDUS_GID` (R10.25);
     started as the `indus` user itself → skip the root-only steps;
   - `INDUS_HOME` (default `/opt/data`): any symlinked component on the way → refuse, exit 1;
   - target ids: `INDUS_UID`/`INDUS_GID`, else `PUID`/`PGID`, else 10000; each must be digits in
     1..65534 (else refuse, exit 1); `usermod -u` / `groupmod -o -g` only when they differ;
   - ownership, targeted: the data root itself (not recursive) when its owner differs; our own
     subfolders and files (home skeleton, profiles, state files, `config.yaml`, `.env`) only when
     something inside is owned by another id, recursively without following symlinks; a symlinked
     subfolder is skipped with a warning. Nothing outside the data root is ever chowned, and the code
     tree stays root-owned;
   - a bind-mounted Docker socket: give `indus` a group with the socket's GID (create `hostdocker`
     when none exists) so the Docker terminal backend works from inside;
   - `.env` exists, owner-only (0600) and owned by `indus`;
   - first boot (no `config.yaml`): as `indus`, `indus doctor --fix` (seeds the commented settings
     file and the home); browser variant: point `browser.executable_path` at the image's Chromium;
   - every start: `indus skills sync` as `indus` (a new image's shipped skills arrive; edits,
     deletions and the opt-out are kept); output of these runs is appended, by `indus`, to
     `logs/container-start.log`; a failure only warns.
3. `docker/main-wrapper.sh` (main program): same `--user` refusal; `HOME=$INDUS_HOME`; returns to
   `INDUS_START_DIR` (the `-w` dir); routing — no args → `indus`; a first argument starting with `-`
   is always an `indus` option (never looked up as a program, R10.25); a first argument found on PATH
   → exec it; anything else → `indus <args>`; root drops to `indus` through `s6-setuidgid`.

s6 services (`docker/s6/` → `/etc/s6-overlay/s6-rc.d/`): `indus-prepare` (oneshot running
stage-2), `gateway` (`INDUS_GATEWAY=1` → `indus gateway run`) and `dashboard` (`INDUS_DASHBOARD=1`
→ `indus dashboard --host ${INDUS_DASHBOARD_HOST:-127.0.0.1} --port ${INDUS_DASHBOARD_PORT:-9119}
--no-open`), both in the `user` bundle, depending on `indus-prepare`; disabled → `run` exits 0 and
`finish` exits 125 so s6 keeps the slot down instead of restarting it.

Inputs → outputs: env `INDUS_UID/GID`, `PUID/PGID`, `INDUS_HOME`, `INDUS_GATEWAY`, `INDUS_DASHBOARD*`,
the container args → the program run, its uid, the data volume's ownership, exit codes (1 refusal).

Files: `docker/entrypoint.sh`, `docker/stage2.sh`, `docker/main-wrapper.sh`, `docker/s6/**`,
`tests/integration/docker/test_entry_scripts.py`.

Edge cases / failure modes:
- `-p work gateway run`, `-v`, `--version`: routed to `indus`, never `command -v -p …`;
- an `indus` subcommand that is also a program name (`dashboard`? not on PATH) → indus; `bash`,
  `sleep`, `sh` → exec'd;
- the volume owned by the host user (bind mount): only our folders are touched;
- `docker exec` as root leaving root-owned files in our folders → healed on the next start;
- rootless runtimes where chown fails → warning, continue;
- symlink planted by the agent in a folder we chown (TOCTOU) → never followed.

Test seam: `_INDUS_IMAGE_ROOT` prefixes the fixed image paths (`/opt/indus`, `/init`, `/command`,
the socket paths) and `_INDUS_ENTRY_PID` replaces `$$` in the dispatcher, so the scripts run under
bash/dash in a temp root with fake `id`, `usermod`, `groupmod`, `chown`, `chmod`, `s6-setuidgid`,
`indus`, `getent`, `groupadd`.

Our design differences / decisions:
- stage-2 is an s6-rc oneshot the services depend on (s6-overlay v3 style), not a cont-init script;
- invalid ids and a symlinked data path refuse the start (the reference warns and continues);
- no routing-override env syncing, no generated API keys, no auth bootstrap env, no Fly socket, no
  dependency-generation refresh: none of those exist in our product;
- no deprecated `entrypoint.sh` shim (our ENTRYPOINT is `entrypoint.sh` itself).

Tests we will write:
- dispatcher: PID 1 → execs `/init wrapper args`; not PID 1 → warning, stage-2 ran, wrapper exec'd;
- stage-2: `--user` refused (message names `INDUS_UID`), remap calls usermod/groupmod, every chown
  target inside the data root, symlinked data root refused, symlinked subfolder skipped, bad ids
  refused, PUID/PGID aliases, `.env` 0600, first-boot seeding runs once, skills synced every start, socket group;
- wrapper: `-v` → `indus -v`; `-p x gateway run` → indus; `bash -c …` exec'd; no args → `indus`;
  unknown word → `indus word`; root drops via s6-setuidgid, the `indus` user does not; `--user`
  refused; start dir restored;
- s6 tree: types, run/finish scripts exist and are executable, bundle contents, finish exits 125 when
  disabled; shellcheck when installed.

## Decisions for the lead
- Refusal instead of warning for bad ids and a symlinked data root (fail closed).
- First-boot config seeding through `indus doctor --fix` (no separate seeding command exists).

Compare checklist (filled after the rewrite; reopened `docker/entrypoint-dispatch.sh`, `docker/stage2-hook.sh`, `docker/main-wrapper.sh`, `docker/s6-rc.d/`):
- [x] PID-1 rule: /init with the wrapper; otherwise warn, stage-2, wrapper (`tests/integration/docker/test_entry_scripts.py::test_pid_1_hands_over_to_s6_init_with_the_wrapper`, `::test_not_pid_1_warns_runs_stage2_then_the_wrapper`)
- [x] gap found: an interactive command shared the terminal with service output — the dispatcher now sends service output to s6's log when stdin/stdout are a TTY (unless the user set `S6_LOGGING`)
- [x] `--user` refusal in stage-2 and the wrapper, with guidance (`::test_a_docker_run_user_start_is_refused_with_guidance`)
- [x] leading `-flag` always to indus; programs on PATH exec'd; else indus subcommand (`::test_a_leading_flag_is_always_an_indus_option`, `::test_a_program_on_path_is_executed`, `::test_anything_else_is_an_indus_subcommand`)
- [x] UID/GID remap with PUID/PGID aliases, targeted ownership, symlinks never followed (`::test_remap_changes_the_ids_and_only_touches_the_data_folder`, `::test_puid_pgid_are_aliases_and_indus_ids_win`, `::test_a_symlinked_subfolder_is_never_followed`, `::test_a_symlinked_data_folder_is_refused`)
- [x] Docker socket group (`::test_a_docker_socket_gets_a_group_for_indus`, `::test_an_existing_socket_group_is_reused_and_membership_is_idempotent`)
- [x] `.env` owner-only (`::test_env_file_is_created_and_kept_owner_only`); first-boot seeding (`::test_first_boot_seeds_once_as_the_indus_user`)
- [x] gap found: shipped skills were synced only on the first start — every start now runs `indus skills sync` (edits, deletions and the opt-out kept) (`::test_every_start_brings_the_shipped_skills_up_to_date`)
- [x] gap found (ours): root appended to the start log — the log is now opened by `indus`, `.env` is chmodded as `indus`, and single-path chowns use `-h`, so a planted symlink cannot steer a root write
- [x] working directory restored, HOME = the data folder (`::test_the_start_directory_and_home_are_restored`)
- [x] services off unless switched on; finish 125 keeps the slot down (`::test_services_are_off_unless_switched_on`)
- Deliberate differences: invalid ids and a symlinked data root refuse the start (reference: warn and
  continue); the dashboard service binds loopback by default (reference: all interfaces); stage-2 is an
  s6-rc oneshot the services depend on (reference: a cont-init script); `S6_KEEP_ENV=1` instead of a
  `with-contenv` re-exec.
- Not taken: routing-override env syncing, API-key generation, auth/gateway-state bootstrap env, the
  hosting platform's API socket, dependency-generation refresh, XDG runtime dir, the stale data-dir
  install-stamp cleanup (we never stamped the data folder), the deprecated `entrypoint.sh` shim.

## Status

Done (2026-10-21). Files: `docker/entrypoint.sh`, `docker/stage2.sh`, `docker/main-wrapper.sh`,
`docker/s6/**`, `tests/integration/docker/test_entry_scripts.py` (bash and dash). shellcheck is not
installed here: the scripts pass `bash -n` / `dash -n` and the test runs shellcheck where it exists
(the Docker workflow's lint job installs a pinned one). Follow-up: a root-drop shim for `docker exec
CONTAINER indus …` (files it writes as root are healed only at the next start).
