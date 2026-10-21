# Wave 36 parked patches (P23 except 15/16)

## w36-n1.patch: P23-13 Nix flake and packages + P23-14 NixOS and home-manager modules — PARTIAL (Nix lane never run) (base d5e575e)
- Builder checks: ruff, format, pyright 0 on scripts/nix + tests/unit/packaging; lint-imports; pytest tests/unit/packaging 41
  passed (20 passed / 21 skipped without nix); tooling.checks ok; pins.py --check ok; nix-instantiate --parse on every file +
  renderer eval cases. NOT run: `nix flake check`, `nix run .#default -- --version` (ACC-S4-1/2) — no nixpkgs offline.
  flake.lock not generated; nix/pins.json pnpmDepsHash null.
- New (all N1-owned): flake.nix (flake-parts; inputs nixpkgs unstable, flake-parts, pyproject-nix, uv2nix,
  pyproject-build-systems, home-manager, all follow nixpkgs; x86_64-linux, aarch64-linux, aarch64-darwin; overlays.default,
  nixosModules.default, homeManagerModules.default), nix/{packages,variants,checks,render,module-common,nixos-module,
  home-manager-module,module-checks}.nix, nix/pins.json, scripts/nix/pins.py (--check, --pnpm-hash),
  tests/unit/packaging/{__init__,test_nix_files}.py, notes nix-flake.md, nix-modules.md.
- Wiring: add a Nix CI lane (ubuntu + macos; checkout + nix installer pinned by SHA; nix flake lock once and commit
  flake.lock; nix flake check -L; nix run .#default -- --version; record the pnpm hash via pins.py --pnpm-hash); optionally
  pins.py --check in the checks job; U1's `indus update` refuses when install_method() == "nix" ("Nix owns updates: run `nix
  flake update` and rebuild"); tests/unit/packaging/__init__.py may also come from I1 (both empty); move "Tool versions" to
  docs/notes/P23/tool-versions.md.
- Decisions (D-ids): default variant = messaging + web, docs, pty, dashboard, acp (browser, voice, research, modal, daytona,
  vercel, discord-voice, matrix-e2ee left out until the lane builds them); container.image no default until the GHCR owner
  is fixed (D-16.690, one release-base constant); module owns config.yaml (whole file, .managed marker); secrets via
  EnvironmentFile= / container env files; native.enable + container.enable with an exactly-one assertion; config.yaml via
  builtins.toJSON; left out of 1.0: dashboard unit, documents, directory plugins, auth-file seeding, container host-user
  passthrough.
- Follow-ups: confirm uv2nix / build-system-pkgs / nixpkgs pnpm API names in the lane; pnpm_12 in nixpkgs; trim tui
  devDependencies; matrix-e2ee build override.

## w36-k1.patch: P23-08 product image, P23-09 sandbox images, P23-10 container entry, P23-11 compose + image lint, P23-12 Docker publishing workflow — done statically (base d5e575e)
- Builder checks: ruff, format, pyright 0, lint-imports; pytest (scripts/docker, tests/unit/packaging, tests/unit/ci,
  tests/integration/docker, tests/docker) 147 passed / 17 skipped (docker lane without an image; hadolint / actionlint /
  shellcheck / P23-02 pins absent); tooling.checks ok; `docker compose config` accepts both files; entry scripts run under
  bash + dash. No image built (no daemon); workflow never ran.
- BEFORE THE FIRST DOCKER CI RUN: every digest in docker/pins.json is null — run `uv run python scripts/docker/pins.py
  refresh` (network), review, commit; VERIFY the three action SHAs in docker.yml (written from memory: checkout v4.2.2
  11bd719…, upload-artifact v4.6.2 ea165f8…, download-artifact v4.3.0 d3f86a1…).
- New: docker/{Dockerfile (fetch → web → build → product; VARIANT slim|browser), sandbox/Dockerfile, pins.json,
  entrypoint.sh, stage2.sh, main-wrapper.sh, s6/*, compose.yaml, compose.windows.yaml}, .dockerignore, .hadolint.yaml,
  .github/workflows/docker.yml, scripts/docker/pins.py (stdlib; check [--strict], render, fetch, image, refresh); tests
  tests/unit/packaging/{__init__, docker_files, test_docker_pins, test_dockerfile, test_compose}, tests/unit/ci/*,
  tests/integration/docker/*, tests/docker/* (docker-marked lane, skips without INDUS_TEST_IMAGE /
  INDUS_TEST_SANDBOX_IMAGE); notes docker-image.md, sandbox-images.md, docker-entry.md, compose.md, docker-workflow.md,
  tool-versions.md (K1 section — merge with N1's "Tool versions").
- Shared: traps R10.23/24/25 (U1 also touches R10.23/24 — union); ACC-S3-1 tests, new ACC-S3-2, ACC-S3-3.
  tests/unit/packaging/__init__.py also created by N1 (empty — trivial).
- Wiring: assumed I1's scripts/install/pins.json `uv.targets.<key>` {url, sha256, mirror?} with keys linux-x64-glibc /
  linux-arm64-glibc, then *-gnu, then Rust triples — adjust uv_artefact() if I1 differs; U1's release workflow calls
  docker.yml via workflow_call (commit, attempt_ref rc.N-vX.Y.Z, phase test|publish; outputs product-digest,
  product-browser-digest, sandbox-digest, sandbox-browser-digest; only the controller moves latest/stable/canary + sandbox
  slim/browser tags); install stamp /opt/indus/.install_method = docker; registry owner in docker/pins.json images
  (namespace indus-agent; tied by a test to schema terminal.docker_image) — fold into the single release-base constant;
  consider adding docker/, .github/, nix/ to tooling/checks scan lists; optionally pins.py check in ci.yml.
- Tools (no new Python/npm deps): s6-overlay 3.2.1.0 (ISC, shipped); Node 24.21.0 (MIT, web build only); hadolint 2.12.0
  (GPL-3.0, CI binary only, never shipped — OWNER NOTE: copyleft tool in CI); actionlint 1.7.7 (MIT, CI); Debian packages
  incl. Chromium in browser variants (third-party notices owed).
- Decisions (D-ids): INDUS_EXTRAS build arg default "dashboard pty web docs slack discord mattermost" (+browser); S6_KEEP_ENV=1;
  stage-2 as an s6-rc oneshot; invalid INDUS_UID/GID/PUID/PGID or a symlinked data folder refuse start (reference warns);
  dashboard binds loopback by default; Windows compose needs a login provider (no insecure flag); first start `indus doctor
  --fix`, every start `indus skills sync`; stage-2 never writes as root through agent-replaceable paths (chown -h etc.);
  sandbox default user root (P11 mounts /root/.indus) with non-root indus 10000, sudo off unless SANDBOX_SUDO=1; publishing
  via plain docker / skopeo / buildx imagetools, uv from our pins.
- Follow-ups: pins refresh + action SHAs; CI image builds + slim sandbox size (≤ 600 MB); notices for shipped image
  contents; root-drop shim for `docker exec … indus`; P25-16 docs on the Windows login provider; extras on Python 3.14.
