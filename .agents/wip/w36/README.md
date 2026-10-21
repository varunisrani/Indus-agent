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

## w36-i1.patch: P23-01 no-PyPI guard + metadata, P23-02 bootstrap pins, P23-03 install.sh core, P23-04 repository stage, P23-05 runtime stages — done; pins PROVISIONAL (base d5e575e)
- Builder checks: ruff, format, pyright 0, lint-imports; tests/unit/packaging + tests/integration/install + smoke 159 passed;
  wider cli/contract/integration cli/tooling 2060 passed / 1 skipped; full collect 15983 with no usage errors;
  tooling.checks ok (3 comments reworded, 1 table restructured for similarity). shellcheck not installed (directives added,
  scripts unchecked). End-to-end non-interactive ladder test ends with `indus --version` from the published launcher.
- PINS: uv 0.11.32 for 8 targets + Git for Windows 2.51.0.windows.1 for 2 targets, digests null; installer refuses unknown
  digests; `pins.py --check --strict` fails until `uv run python scripts/install/pins.py update --uv 0.11.32 --git
  2.51.0.windows.1` (network) — confirm the Git for Windows version + arm64 asset. `--open-pr` opens a draft PR only.
- RE-LOCK owed: uv.lock (dynamic version 0.0.0 from src/indus/__init__.py, new extras `messaging` + `all` written as exact
  pins with a union test, hatchling==1.32.4 MIT build pin); CI `uv sync --locked` fails until relocked.
- New: scripts/build_hooks/no_wheel.py, scripts/install/{pins.json, pins.py, build.py, stages.json (SHARED with I2 —
  install.ps1 reads the same file via build.py TARGETS; pins.render_ps1() ready)}, scripts/install/sh/{00-core,
  10-repository, 20-runtime}.sh, scripts/install.sh (generated), tests/platform_marks.py (platforms(*specs) marker + lint
  against bare skipif(sys.platform…); 22 existing bare marks in 18 files baselined in platform_skip_baseline.json, may only
  shrink), tests unit/packaging + integration/install + fakes/install, notes no-wheel, pins, install-sh-core,
  install-sh-repository, install-sh-runtime, tool-versions (merge with N1 and K1).
- Shared: pyproject.toml, src/indus/__init__.py, tests/conftest.py, tests/test_smoke.py, .gitignore (src/indus/_tui_dist/,
  /.indus-bootstrap.json), ci.yml (build.py --check + pins.py --check step), traps R10.11/12/14/15/16/21, ACC-S1-1, S7-1,
  S8-1 + new S1-2/3/4. tests/unit/packaging/__init__.py also from N1/K1.
- Wiring: release base in pins.json release.repository = indusagi/indus-agent (D-16.690; owner says varunisrani/Indus-agent?
  one constant for installer, Docker, Nix, desktop); installer writes update.installs.<id16>.channel / .skip for U1's
  schema; K1: no `gateway` extra — use `--extra messaging` (+ web) and fix K1's INDUS_EXTRAS default + uv_artefact() keys
  against this pins.json shape.
- Decisions (D-ids): extras messaging + all; default installer extras web docs pty dashboard acp messaging (+browser unless
  --skip-browser; --skip-computer-use recorded only); receipts in <home>/logs/install_receipts/ (success|failed|interrupted,
  full-ladder runs only); saved/explicit channel moves a checkout forward only; version-API/redirect lookups only for our own
  repo URL, canary from remote tags; uv + Python under <home>/tools; PATH in each shell rc once inside an exact marker block;
  gateway stage asks y/N; channel save failure warns; CI/GITHUB_ACTIONS turns on --verbose.
- Follow-ups: real digests; shellcheck in CI; web/TUI pages from a release payload (P23-23 artefact manifest);
  --include-desktop only warns; migrate baseline skipifs; fresh-VM install lane (P23-23).

## w36-i2.patch: P23-06 install.ps1 core and stages, P23-07 Windows bootstrap + cmd shim, P23-20 uninstall finished — done (pwsh untested) (base d5e575e)
- RESTORED by the builder from its own context after the lead's parking error removed the original worktree. Not
  byte-identical: one extra pwsh-only test (test_pwsh_missing_digest_is_refused_before_any_download) + a static assertion;
  two interactive prompt lines in uninstall.py rewrapped (strings tests check unchanged); test line wrapping.
- Builder checks (restored tree): ruff, format (13 files), pyright 0, lint-imports, tooling.checks ok; uninstall +
  integration/install + contract cli + test_app + test_entry 616 passed / 28 skipped (pwsh cases; no pwsh here — static checks
  via tests/integration/install/ps_static.py). Windows installs blocked until I1's pins have real digests.
- ENV SLIP (owner item): the first `uv run` of the ORIGINAL run used HOME=/root and wrote into /root/.cache/uv (an editable
  build + a few cache entries). Nothing under /root in the restored run.
- New: scripts/install/ps/{00-core,10-bootstrap,20-stages,90-entry}.ps1, scripts/install.ps1 (built with STAND-IN pins —
  REBUILD at integration), scripts/install.cmd (CRLF; .gitattributes rule suggested), scripts/install/stages.json (I1 also
  wrote one — keep I1's + add `dispatch_only: ["desktop"]` and `extras`), src/indus/cli/uninstall.py,
  docs/internals/uninstall-levels.md (P24-34 consumes), tests integration/install (__init__, ps_static, test_install_ps1,
  test_install_ps1_bootstrap), tests/unit/cli/test_uninstall.py, notes install-ps1.md, install-ps1-bootstrap.md, uninstall.md.
- STAND-IN: tests/integration/install/_stubs_i2.py — move render_ps1 into I1's build.py (+ --check): header, then 00-core.ps1
  with `# @@INDUS_EMBED@@` replaced by compact stages.json + pins.json here-strings, then other ps/*.ps1 in name order.
- Shared: cli/app.py (uninstall row before acp), contract cli help catalogue + non_tty_table (uninstall: 1), traps R10.15,
  ACC-I42-1..3 + new ACC-S2-1/2.
- Wiring: Get-IndusPin reads I1's pins shape (mirror, python.provision, release.repository); release origin = pins.json
  release.repository (a test catches install.cmd URL drift); skip choices update.installs.<id>.skip via `indus config set
  --force`; receipts installer "ps1"; uninstall install kind from cli/version.install_method + .git (U1's detector may
  replace); launchers recognised by `# indus launcher` / `rem indus launcher`; click command raises Exit(code).
- Decisions (D-ids): POSIX spellings via ValueFromRemainingArguments, exit codes as install.sh; ASCII-only, PowerShell 5.1
  compatible; mirror only after network-class failures; user PATH keeps its registry value kind; native Windows gateway
  stage points to WSL2 (D-16.55); no -ProtocolVersion / COM path resolver / AppContainer ACL grant; uninstall removes PATH
  only between exact markers; declined named profiles keep <root>/profiles, -y never removes them; running gateway stopped
  by SIGTERM to its recorded pid; --data refuses while this profile's gateway runs; cancel exits 0.
- Follow-ups: pwsh tests + Windows install-e2e lane; PowerShell 5.1/7 in tool-versions.md.
