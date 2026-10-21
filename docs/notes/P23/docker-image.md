# Task: Product Docker image (P23-08) Phase: 23 Owner: wave-36 builder K1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `Dockerfile` (stage headers, ENV,
user, s6-overlay block, ENTRYPOINT/CMD, the opt-in desktop package list), `.dockerignore` (skim).
Closed before writing.
Evidence: none.

What it does (own words): one image that runs `indus` for people who would rather pull a container
than run an installer. Code and its interpreter live in an immutable tree (`/opt/indus`, root-owned);
everything a user changes lives in the data volume `/opt/data`, which is `INDUS_HOME`. A process
supervisor (s6-overlay) is PID 1 so stray children are reaped and the optional gateway/dashboard
services are supervised; the user's command runs as the supervisor's "main program", so the container
exits when it does. Two variants from one file: `slim` and `browser` (adds Chromium and the
`browser` extra for the browser and computer-use tools). A Docker copy never updates itself: the tree
carries the `docker` install stamp (`/opt/indus/.install_method`, read by `indus --version` and by the
updater, which tells the user to pull a new image).

Inputs → outputs: `docker build -f docker/Dockerfile [--build-arg VARIANT=browser] .` →
`ghcr.io/<namespace>/indus-agent:<tag>`; `docker run IMAGE --version` → `indus <version>` and
`docker`; `docker run -v ~/.indus:/opt/data IMAGE gateway run` → the gateway as UID 10000 (or the
remapped `INDUS_UID`).

Stages:
1. `fetch` — the pinned bootstrap downloads: uv (from the installer's `scripts/install/pins.json`,
   the one source for uv), s6-overlay (noarch + per-arch + symlinks tarballs) and Node (for the web
   build) from `docker/pins.json`. One stdlib script (`scripts/docker/pins.py fetch`) downloads, checks
   SHA-256 before anything is unpacked, and refuses a pin without a digest. s6 is unpacked here into a
   staging root that later stages `COPY` whole (modes and symlinks kept).
2. `web` — `pnpm install --frozen-lockfile` + build of `@indus/web` with the pinned Node.
3. `build` — `uv python install 3.14` (uv verifies its own managed download), `uv sync --frozen
   --no-dev` twice (dependencies first for layer caching, then the project, editable — the no-wheel
   guard of P23-01 is not tripped), extras per variant, the dashboard pages published into the
   package (`python -m indus.web.bundle publish`), the `docker` stamp.
4. final — base + runtime packages, s6 root, `/opt/indus` from `build`, entry scripts and s6 service
   dirs, user/group `indus` 10000:10000 with home `/opt/data`, `ENV INDUS_HOME=/opt/data`,
   `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1`, venv first on PATH, `S6_KEEP_ENV=1`,
   `S6_BEHAVIOUR_IF_STAGE2_FAILS=2`, `VOLUME /opt/data`, `ENTRYPOINT` our dispatcher, `CMD []`, OCI
   labels (source, licence MIT, title, description).

Pins (`docker/pins.json`): base image `debian:trixie-slim` + index digest, s6-overlay version and
per-artefact SHA-256, Node version and per-arch SHA-256, registry coordinates (`ghcr.io`, namespace,
image names: the one place the GHCR owner is written for Docker). `scripts/docker/pins.py check`
verifies the shape, that both Dockerfiles' `ARG BASE_IMAGE` default equals the pinned
`image:tag@digest`, and that the schema's default `terminal.docker_image` names the pinned sandbox
image; `render` rewrites those ARG lines from the JSON; `refresh` (maintainers, network) fills
digests from upstream checksum files and the registry; `check --strict` (the Docker CI lane) fails
on any digest still empty.

Files: `docker/Dockerfile`, `.dockerignore`, `docker/pins.json`, `scripts/docker/pins.py`,
`tests/docker/conftest.py`, `tests/docker/test_image_build.py`, `tests/unit/packaging/test_docker_pins.py`,
`tests/unit/packaging/test_dockerfile.py`.

Edge cases / failure modes:
- a digest that does not match → the build stops before unpacking (`pins.py` exits 1 naming the file);
  an empty digest → refused with "run `pins.py refresh`";
- unsupported `TARGETARCH` (not amd64/arm64) → refused;
- the volume overlays `/opt/data` at run time, so nothing the image needs may live there (the home is
  created by stage-2, never baked);
- `.dockerignore` must keep `.venv`, `node_modules`, caches, tests and docs out (context size, and so a
  local `.venv` never leaks into the image), but keep `README.md`/`LICENSE` (pyproject references them).

Our design differences / decisions:
- one fetch script for all pinned downloads instead of inline curl blocks; the same file installs uv
  in the CI workflow (no third-party uv action to pin);
- `S6_KEEP_ENV=1`: services and the main program see the container environment directly, so no
  re-exec through `with-contenv` is needed;
- no bundled SQLite rebuild, no tini compatibility shim, no icon generation stage, no extra sidecars
  (none of ours need them in 1.0);
- digests are filled by `pins.py refresh`, never typed by hand.

Tests we will write:
- unit: Dockerfile structure (stages, user 10000, workdir, volume, env, entrypoint/cmd, ARG defaults
  equal pins, no `latest`, every `FROM` goes through the pinned ARG or an earlier stage, no `curl | sh`,
  no `ADD http`), `.dockerignore` coverage, pins shape/agreement, fetch verify/refuse paths over
  `file://` fixtures, schema default agreement, banned identifiers over every Docker file;
- docker lane (`INDUS_TEST_IMAGE`): `--version`, UID 10000 after stage-2, `/opt/data` a volume, banned
  identifiers over `docker history` and the image's `/opt/indus` file list.

## Decisions for the lead
- Extras baked into the image: `INDUS_EXTRAS` build arg, default `dashboard pty web docs slack
  discord mattermost` (+ `browser` in the browser variant). Switch to P23-01's composite extra (`all`)
  at integration if I1 defines one.
- Digests in `docker/pins.json` are empty until a maintainer runs `uv run python scripts/docker/pins.py
  refresh` (network) — agents here have no network. `check --strict` makes the Docker lane fail with
  that instruction until then.
- Registry namespace: `docker/pins.json` → `images.namespace` (`indus-agent`, matching today's schema
  default). When the owner settles D-16.690 / the GHCR owner, change it there and in the schema default
  (a test ties the two); merge into the lead's single release-base constant if one is chosen.

Compare checklist (filled after the rewrite; reopened `Dockerfile`, `.dockerignore`):
- [x] multi-stage, digest-pinned base through one ARG (test: `tests/unit/packaging/test_dockerfile.py::test_every_from_uses_the_pinned_base_arg_or_an_earlier_stage`, `::test_base_arg_default_is_the_pinned_reference`)
- [x] s6-overlay pinned by version with per-artefact SHA-256, verified before unpacking (`tests/unit/packaging/test_docker_pins.py::test_digest_mismatch_fails_at_once_and_never_tries_the_mirror`, `::test_fetch_removes_what_it_wrote_when_a_later_file_fails`)
- [x] user/group 10000, `/opt/data` volume = INDUS_HOME, unbuffered/no-bytecode env, venv first on PATH, our dispatcher as ENTRYPOINT, empty CMD (`::test_final_stage_user_volume_entrypoint_and_env`)
- [x] immutable install tree + `docker` install stamp (`::test_the_tree_carries_the_docker_install_stamp`)
- [x] layer-cached dependency install before the source copy (`::test_python_and_dependencies_come_from_the_lock`)
- [x] `.dockerignore`: VCS, envs, caches, secrets, node_modules, build output, tests, docs out; README/LICENSE/skills kept (`::test_dockerignore_keeps_out`, `::test_dockerignore_keeps_what_the_build_copies`)
- [x] gap found: a cache folder (`.cache`, `.uv-cache`) could enter the context — added to `.dockerignore`
- Deliberate differences: the reference rebuilds SQLite, ships a tini compatibility shim, an icon stage and
  sidecar installs; we do none of these. It re-execs through `with-contenv`; we set `S6_KEEP_ENV=1`. It
  pins s6 inline in the Dockerfile; we keep every digest in `docker/pins.json` behind one fetch script
  that also serves CI.
- Not taken: the desktop (VNC/Xfce) variant — our browser variant ships Chromium only; a `docker exec`
  root-drop shim (root-owned files left by `docker exec` are healed at the next start).

## Status

Done (2026-10-21), with two owed items: digests in `docker/pins.json` are empty until a maintainer runs
`scripts/docker/pins.py refresh` (no network here), and the image itself is built and tested only in the
Docker CI lane (no daemon here). Files: `docker/Dockerfile`, `.dockerignore`, `docker/pins.json`,
`scripts/docker/pins.py`, `tests/unit/packaging/{docker_files,test_dockerfile,test_docker_pins}.py`,
`tests/docker/{conftest,test_image_build}.py`. Needs P23-02's `scripts/install/pins.json` (uv) at integration.

## Integration (wave 36)

Extras: `INDUS_EXTRAS` defaults to the installers' list from `stages.json` (`web docs pty dashboard acp messaging`, + `browser` in the browser variant; a test holds them equal, D-16.743). Registry: `docker/pins.json` `images` = `ghcr.io/indusagi/indus-agent` and `…/indus-agent-sandbox`, checked equal to `RELEASE_IMAGE` / `SANDBOX_IMAGE` by `pins.py check` (D-16.740/741). uv is read from the installer's `linux-x64` / `linux-arm64` targets. Every digest is recorded at integration (`check --strict` passes): the bundles verified against upstream checksum files, the Debian base index digest read from Docker Hub.
