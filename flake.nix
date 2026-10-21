{
  description = "Indus Agent: a self-improving AI agent";

  # Every input follows our nixpkgs, so one nixpkgs serves the packages, the modules and the
  # checks. flake.lock is written by the Nix CI lane (`nix flake lock`).
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
    # Only the module checks evaluate against it; users import the module into their own
    # home-manager.
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    inputs@{ self, flake-parts, ... }:
    let
      packagesFor = system: self.packages.${system} or { };
    in
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      imports = [
        ./nix/packages.nix
        ./nix/checks.nix
        ./nix/module-checks.nix
      ];

      flake = {
        overlays.default = final: _prev: {
          indus-agent = (packagesFor final.stdenv.hostPlatform.system).default;
        };
        nixosModules.default = import ./nix/nixos-module.nix { inherit packagesFor; };
        homeManagerModules.default = import ./nix/home-manager-module.nix { inherit packagesFor; };
      };
    };
}
