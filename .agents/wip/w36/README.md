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
