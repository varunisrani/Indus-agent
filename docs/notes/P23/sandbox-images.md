# Task: Sandbox images (P23-09, D-16.64) Phase: 23 Owner: wave-36 builder K1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `Dockerfile` (the opt-in desktop
package list only, for what the browser tools need), `docker/sandbox-desktop.Dockerfile` (skim of
its package list). Closed before writing.
Evidence: none.

What it does (own words): the image the Docker terminal backend (and Modal/Daytona when they take an
image) runs the agent's commands in, so a default install never pulls someone else's image (D-16.64).
`slim` has a shell toolbox (bash, coreutils, git, curl, python3 with venv, compilers and make,
ripgrep, procps, openssh client, xz/unzip, jq); `browser` adds Chromium, a virtual display (Xvfb) and
fonts for the browser and computer-use tools. Both carry OCI labels (source, licence, title,
description, vendor) and a non-root user `indus` (10000) whose passwordless sudo is off unless the
image is built with `SANDBOX_SUDO=1`.

Inputs → outputs: `docker build -f docker/sandbox/Dockerfile --target slim|browser .` →
`ghcr.io/<namespace>/indus-sandbox:<tag>`; the P11 backend runs it as `docker run -d --init --cap-drop
ALL … IMAGE sleep infinity` and execs `bash -c` scripts in it.

Files: `docker/sandbox/Dockerfile`, `tests/docker/test_sandbox_image.py`,
`tests/unit/packaging/test_dockerfile.py` (shared with P23-08).

Edge cases / failure modes:
- the backend mounts skills and credentials read-only under `/root/.indus` and assumes root inside
  with every capability dropped but DAC_OVERRIDE/CHOWN/FOWNER(+SETUID/SETGID): the default user stays
  root so those mounts and bound work dirs stay usable; the non-root user is there for
  `docker_run_as_host_user`-style starts and for images derived from ours;
- no ENTRYPOINT (the backend passes its own command; `--init` reaps);
- Chromium under `no-new-privileges` and dropped caps cannot use its setuid sandbox: the browser tools
  already decide that (P10 sandbox check), the image only ships the binary at `/usr/bin/chromium`
  (`INDUS_SANDBOX_CHROMIUM` env names it).

Our design differences / decisions:
- one Dockerfile, two targets (`slim`, `browser` built FROM `slim`) instead of separate files;
- no desktop session (window manager, VNC) in 1.0 — Xvfb only.

Tests we will write:
- unit: targets present, labels, user 10000, no sudo unless the arg is set, base through the pinned ARG;
- docker lane (`INDUS_TEST_SANDBOX_IMAGE`): `bash -lc 'git --version && python3 --version && rg
  --version'` in slim; Chromium headless prints its version in browser; the P11 `run_args` flags
  (computed by the real function from default settings) start the image and run a command.

## Decisions for the lead
- Default user root (see edge cases); the non-root user exists, sudo off by default.
- Size budget: target ≤ 600 MB compressed for slim; measuring needs the Docker lane (`docker buildx
  imagetools inspect` sizes) — owed, recorded here when first measured.
- `terminal.docker_image` default is already `ghcr.io/indus-agent/indus-sandbox:slim`
  (schema); `pins.py check` keeps it equal to `docker/pins.json` coordinates. The `slim`/`browser`
  tags are channel aliases: only the publication controller (P23-23) moves them.

Compare checklist (filled after the rewrite; reopened `Dockerfile` desktop package list, `docker/sandbox-desktop.Dockerfile`):
- [x] slim toolbox: bash, coreutils, git, curl, python3, build tools, ripgrep (`tests/unit/packaging/test_dockerfile.py::test_sandbox_targets_and_tools`; built: `tests/docker/test_sandbox_image.py::test_slim_toolbox`)
- [x] browser target: Chromium, virtual display, fonts (`::test_sandbox_targets_and_tools`; built: `tests/docker/test_sandbox_image.py::test_browser_target_starts_chromium_headless`)
- [x] non-root user, sudo off by default (`::test_sandbox_user_labels_and_no_entrypoint`, `::test_sandbox_sudo_is_off_unless_asked`; built: `test_non_root_user_without_sudo`)
- [x] the P11 hardened flags start it unmodified (built: `tests/docker/test_sandbox_image.py::test_the_backend_hardened_flags_run_unmodified`)
- [x] the schema default names our image (`tests/unit/packaging/test_docker_pins.py::test_the_schema_default_sandbox_image_is_the_pinned_one`)
- Deliberate differences: the reference's opt-in desktop session (VNC server, window manager, terminal)
  is not in 1.0; Xvfb only.

## Status

Done (2026-10-21) statically; the built-image checks and the size measurement run in the Docker CI lane
(owed: record the compressed size of `slim` here after the first run). Files: `docker/sandbox/Dockerfile`,
`tests/docker/test_sandbox_image.py`. No change to `schema.py` was needed (the default already names
`ghcr.io/indus-agent/indus-sandbox:slim`).

## Integration (wave 36)

Integration: the image is `ghcr.io/<release base>-sandbox` and its tags always name the variant (`stable-slim`, `stable-browser`, `main-slim`); `terminal.docker_image` defaults to `<sandbox>:stable-slim` (D-16.741). Only the publication controller moves `stable-*` / `latest-*` / `canary-*`.
