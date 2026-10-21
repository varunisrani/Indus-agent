# flake-parts module: `nix flake check` for the packages (P23-13). The module evaluation cases
# live in nix/module-checks.nix.
{ ... }:
{
  perSystem =
    {
      pkgs,
      lib,
      self',
      ...
    }:
    let
      pyproject = builtins.fromTOML (builtins.readFile ../pyproject.toml);
      version = pyproject.project.version;
      declaredExtras = builtins.attrNames (pyproject.project.optional-dependencies or { });
      variants = import ./variants.nix;
      unknownExtras = lib.subtractLists declaredExtras (
        lib.unique (lib.concatLists (builtins.attrValues variants))
      );

      pass = name: pkgs.runCommand "indus-agent-check-${name}" { } "touch $out";
    in
    {
      checks =
        # Every package builds.
        lib.mapAttrs' (name: drv: lib.nameValuePair "build-${name}" drv) self'.packages
        // {
          # The devshell's inputs build (a shell derivation itself is not meant to be built).
          build-devshell = self'.devShells.default.inputDerivation;

          # A variant naming an extra pyproject.toml does not declare fails evaluation here,
          # with the names, instead of deep inside uv2nix.
          variant-extras =
            if unknownExtras == [ ] then
              pass "variant-extras"
            else
              throw "nix/variants.nix names extras pyproject.toml does not declare: ${toString unknownExtras}";

          # `indus --version` runs from the store and knows it is a Nix install (so the in-app
          # updater refuses it and points at Nix).
          version =
            pkgs.runCommand "indus-agent-check-version"
              {
                nativeBuildInputs = [ self'.packages.default ];
              }
              ''
                export HOME="$TMPDIR/home" INDUS_HOME="$TMPDIR/indus-home"
                mkdir -p "$HOME"
                indus --version > version.txt
                cat version.txt
                grep -F "Indus Agent ${version}" version.txt
                grep -F "Install method: nix" version.txt
                touch $out
              '';
        };
    };
}
