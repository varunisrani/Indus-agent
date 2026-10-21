# Task: Compose files and image lint (P23-11) Phase: 23 Owner: wave-36 builder K1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `docker-compose.yml` (all),
`docker-compose.windows.yml` (all), `.hadolint.yaml` (all), `website/docs/user-guide/docker.md`
(compose and volume sections, skim). Closed before writing.
Evidence: none.

What it does (own words): ready-to-run Compose files for a gateway plus the dashboard from one image,
and the lint policy for both Dockerfiles.

- `docker/compose.yaml` (Linux): services `gateway` (`gateway run`) and `dashboard` (`dashboard
  --host 127.0.0.1 --no-open`), both `network_mode: host`, `${INDUS_DATA_DIR:-~/.indus}` mounted at
  `/opt/data`, `restart: unless-stopped`, `INDUS_UID`/`INDUS_GID` defaulting to 10000. The dashboard
  stays on loopback, so no login is needed.
- `docker/compose.windows.yaml` (Docker Desktop: no host networking): the dashboard must listen on
  the container interface (`--host 0.0.0.0`), which is a non-loopback bind, so per D-16.25 it refuses
  to start until a login provider is configured (basic login or OIDC in `config.yaml` on the volume);
  there is no flag that skips it. The host side publishes only `127.0.0.1:9119:9119`. The file says
  this in its header; the user docs (P25-16) carry the provider setup.
- `.hadolint.yaml`: failure threshold `warning`, trusted registries `docker.io` and `ghcr.io`, each
  ignored rule with its reason (DL3008 apt versions — the base is digest-pinned; DL3002 last USER
  root — `/init` must start as root, every program drops to `indus`).

Files: `docker/compose.yaml`, `docker/compose.windows.yaml`, `.hadolint.yaml`,
`tests/unit/packaging/test_compose.py`.

Edge cases / failure modes:
- an image reference: Compose files name the published `:stable` alias (P23-23 moves it) and accept
  `INDUS_IMAGE` to override (a local build);
- no `--insecure`-style flag anywhere; a test fails on one;
- Windows path for the volume: `${USERPROFILE}/.indus`.

Our design differences / decisions:
- the Windows dashboard without a provider fails closed (the reference passes an insecure flag);
- one image reference variable shared by both services.

Tests we will write:
- both files parse (ruamel), services/commands/env/volumes/restart/network as above; image tags never
  `latest`;
- Windows: no host networking, port bound to 127.0.0.1 only, dashboard command parsed and run through
  the real `indus dashboard` launch (`run_launch`, bind faked, non-interactive) without a provider →
  exit 1 with the D-16.25 message; with a basic-login config → it serves;
- `.hadolint.yaml` parses, threshold warning, every ignored rule has a reason comment, registries;
- hadolint over both Dockerfiles when the binary is installed (CI lane), else skipped.

## Decisions for the lead
- Image reference default `ghcr.io/indus-agent/indus-agent:stable` (namespace from `docker/pins.json`,
  a test ties them).
- CI lane: `hadolint docker/Dockerfile docker/sandbox/Dockerfile` added to `.github/workflows/docker.yml`
  (lint job) rather than `ci.yml` (lead's file); actionlint likewise for the workflows.

Compare checklist (filled after the rewrite; reopened `docker-compose.yml`, `docker-compose.windows.yml`, `.hadolint.yaml`, the user guide's compose/volume sections):
- [x] two services from one image, host networking, data at /opt/data, restart policy, UID/GID defaults (`tests/unit/packaging/test_compose.py::test_two_services_from_one_image`, `::test_linux_uses_host_networking_and_a_loopback_dashboard`)
- [x] Windows: no host networking, loopback-only publish, no insecure flag, login provider required (`::test_windows_has_no_host_networking_and_publishes_loopback_only`, `::test_no_insecure_switch_and_no_latest_tag`, `::test_windows_dashboard_without_a_login_provider_refuses_to_start`, `::test_windows_dashboard_with_a_login_provider_serves`)
- [x] hadolint policy: threshold, trusted registries, reasons (`::test_hadolint_policy`); hadolint/compose run where installed (`::test_hadolint_is_clean_when_installed`, `::test_compose_config_is_valid_when_docker_compose_is_installed` — `docker compose config` passed here for both files)
- Deliberate differences: Windows dashboard fails closed without a provider (reference: an insecure flag,
  D-16.25); only DL3008 is ignored (the reference also ignores DL3002/DL3003/DL3059, which our files do not
  trigger); one `INDUS_IMAGE` variable and a Compose project name instead of per-container names.
- Not taken: commented per-platform credential blocks in the compose file (the user docs carry them).

## Status

Done (2026-10-21). Files: `docker/compose.yaml`, `docker/compose.windows.yaml`, `.hadolint.yaml`,
`tests/unit/packaging/test_compose.py`. Owed to P25-16: the Docker user page states the Windows login
requirement and shows the provider setup. hadolint is not installed here (runs in the Docker workflow).

## Integration (wave 36)

The default image is `ghcr.io/indusagi/indus-agent:stable` (the release base, D-16.740).
