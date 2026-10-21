# flake-parts module: the Indus Agent packages and the devshell (P23-13).
#
# The Python closure comes from uv.lock through uv2nix (the same pins as every other install
# method; no PyPI resolution at build time). The wheel build guard (P23-01) refuses to build
# unless INDUS_NIX_BUILD=1, which only our own package gets here. The two frontends are built
# from pnpm-lock.yaml; the dashboard pages go inside the Python package (where the dashboard looks
# for them), the full-screen renderer is found through INDUS_TUI_DIR.
#
#   nix run .#default -- --version     nix profile install .#messaging     nix develop
{ inputs, ... }:
{
  perSystem =
    { pkgs, lib, ... }:
    let
      pins = lib.importJSON ./pins.json;
      variants = import ./variants.nix;
      pyproject = builtins.fromTOML (builtins.readFile ../pyproject.toml);
      version = pyproject.project.version;

      python = pkgs.${pins.python};
      nodejs = pkgs.${pins.nodejs};
      pnpm =
        pkgs.${pins.pnpm}
          or (throw "nixpkgs has no ${pins.pnpm} (package.json packageManager); update the nixpkgs input or the pin");

      # -- frontends (apps/tui, apps/web) ---------------------------------------------------------

      frontendSrc = lib.fileset.toSource {
        root = ../.;
        fileset = lib.fileset.unions [
          ../package.json
          ../pnpm-lock.yaml
          ../pnpm-workspace.yaml
          ../tsconfig.base.json
          ../apps
          ../packages
        ];
      };

      pnpmWorkspaces = [
        "@indus/tui..."
        "@indus/web..."
      ];

      # One fixed-output fetch of the pnpm store for both frontends. The hash needs network to
      # compute: until the Nix lane records it in pins.json, a fake hash makes the build fail
      # with the real one in the message (scripts/nix/pins.py --pnpm-hash records it).
      pnpmDepsArgs = {
        pname = "indus-agent-frontends";
        inherit version pnpmWorkspaces;
        src = frontendSrc;
        fetcherVersion = 2;
        hash = if pins.pnpmDepsHash != null then pins.pnpmDepsHash else lib.fakeHash;
      };
      pnpmDeps =
        if pkgs ? fetchPnpmDeps then
          pkgs.fetchPnpmDeps (pnpmDepsArgs // { inherit pnpm; })
        else
          pnpm.fetchDeps pnpmDepsArgs;
      pnpmConfigHook = pkgs.pnpmConfigHook or pnpm.configHook;

      buildFrontend =
        {
          pname,
          workspace,
          installPhase,
        }:
        pkgs.stdenvNoCC.mkDerivation {
          inherit
            pname
            version
            pnpmDeps
            pnpmWorkspaces
            installPhase
            ;
          src = frontendSrc;
          nativeBuildInputs = [
            nodejs
            pnpm
            pnpmConfigHook
          ];
          # Real directories in one node_modules tree, so the renderer's runtime imports can be
          # shipped next to its bundle.
          pnpmInstallFlags = [ "--config.node-linker=hoisted" ];
          buildPhase = ''
            runHook preBuild
            pnpm --filter ${workspace} run build
            runHook postBuild
          '';
        };

      # The dashboard pages: apps/web/dist.
      web = buildFrontend {
        pname = "indus-agent-web";
        workspace = "@indus/web";
        installPhase = ''
          runHook preInstall
          test -f apps/web/dist/index.html
          cp -r apps/web/dist $out
          runHook postInstall
        '';
      };

      # The full-screen renderer: dist/entry.js plus the packages it imports at run time (the SSR
      # bundle keeps npm dependencies external; workspace packages are bundled, so their links go).
      tui = buildFrontend {
        pname = "indus-agent-tui";
        workspace = "@indus/tui";
        installPhase = ''
          runHook preInstall
          test -f apps/tui/dist/entry.js
          mkdir -p $out
          cp -r apps/tui/dist apps/tui/package.json $out/
          cp -r node_modules $out/node_modules
          rm -rf $out/node_modules/@indus $out/node_modules/.cache
          runHook postInstall
        '';
      };

      # -- Python ---------------------------------------------------------------------------------

      workspace = inputs.uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ../.; };
      lockOverlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
      buildSystems =
        inputs.pyproject-build-systems.overlays.wheel or inputs.pyproject-build-systems.overlays.default;

      ourPackage = _final: prev: {
        indus-agent = prev.indus-agent.overrideAttrs (old: {
          # The wheel build guard (scripts/build_hooks/no_wheel.py) lets only Nix build a wheel.
          env = (old.env or { }) // {
            INDUS_NIX_BUILD = "1";
          };
          # The dashboard pages are git-ignored, so a flake source never carries them.
          preBuild = (old.preBuild or "") + ''
            mkdir -p src/indus/web/static
            cp -r ${web}/. src/indus/web/static/
            chmod -R u+w src/indus/web/static
          '';
        });
      };

      pythonSet =
        (pkgs.callPackage inputs.pyproject-nix.build.packages { inherit python; }).overrideScope
          (
            lib.composeManyExtensions [
              buildSystems
              lockOverlay
              ourPackage
            ]
          );

      # One sealed virtualenv per variant, wrapped so `indus` finds the renderer and Node.
      mkVariant =
        { name, withTui }:
        let
          extras = variants.${name};
          venv = pythonSet.mkVirtualEnv "indus-agent-${name}-env" { indus-agent = extras; };
        in
        pkgs.runCommand "indus-agent-${name}-${version}"
          {
            nativeBuildInputs = [ pkgs.makeWrapper ];
            passthru = {
              inherit python venv extras;
              inherit (python) sitePackages;
            };
            meta = {
              description = "Indus Agent (${name} variant)";
              license = lib.licenses.mit;
              mainProgram = "indus";
            };
          }
          ''
            mkdir -p $out/bin
            makeWrapper ${venv}/bin/indus $out/bin/indus ${
              lib.optionalString withTui (
                lib.escapeShellArgs [
                  "--set-default"
                  "INDUS_TUI_DIR"
                  "${tui}"
                  "--suffix"
                  "PATH"
                  ":"
                  (lib.makeBinPath [ nodejs ])
                ]
              )
            }
          '';
    in
    {
      # packages:begin (names read by tests/unit/packaging/test_nix_files.py)
      packages = {
        default = mkVariant {
          name = "default";
          withTui = true;
        };
        minimal = mkVariant {
          name = "minimal";
          withTui = false;
        };
        messaging = mkVariant {
          name = "messaging";
          withTui = false;
        };
        tui = tui;
        web = web;
      };
      # packages:end

      devShells.default = pkgs.mkShell {
        packages = [
          python
          pkgs.uv
          nodejs
          pnpm
        ];
        env = {
          # uv uses the Nix interpreter and never downloads one.
          UV_PYTHON = python.interpreter;
          UV_PYTHON_DOWNLOADS = "never";
        };
        shellHook = ''
          unset PYTHONPATH
        '';
      };
    };
}
