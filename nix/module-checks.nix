# flake-parts module: evaluation cases for the NixOS and home-manager modules (P23-14).
#
# Each case evaluates a small configuration and either yields a trivial derivation or fails
# `nix flake check` with the reason. Nothing is built: the modules get a stub package. Linux only
# (the NixOS module is Linux-only; the home-manager cases use the Linux service path).
{ inputs, self, ... }:
{
  perSystem =
    {
      pkgs,
      lib,
      system,
      ...
    }:
    let
      pins = lib.importJSON ./pins.json;
      stub = pkgs.writeShellScriptBin "indus" "exit 0";
      image = "registry.invalid/indus-agent:check";

      evalNixos =
        settings:
        (inputs.nixpkgs.lib.nixosSystem {
          inherit system;
          modules = [
            self.nixosModules.default
            {
              boot.isContainer = true;
              system.stateVersion = "25.11";
              services.indus-agent = {
                enable = true;
                package = stub;
              };
            }
            { services.indus-agent = settings; }
          ];
        }).config;

      evalHome =
        settings:
        (inputs.home-manager.lib.homeManagerConfiguration {
          inherit pkgs;
          modules = [
            self.homeManagerModules.default
            {
              home.username = "tester";
              home.homeDirectory = "/home/tester";
              home.stateVersion = "25.11";
              programs.indus-agent = {
                enable = true;
                package = stub;
              };
            }
            { programs.indus-agent = settings; }
          ];
        }).config;

      # Our failed assertion messages (other modules' assertions are not ours to judge).
      ours =
        config:
        map (a: a.message) (
          builtins.filter (a: !a.assertion && lib.hasInfix "indus-agent" a.message) config.assertions
        );

      expect =
        name: ok: detail:
        if ok then
          pkgs.runCommand "indus-agent-module-${name}" { } "touch $out"
        else
          throw "module check ${name} failed: ${detail}";

      refused =
        name: config: needle:
        let
          messages = ours config;
        in
        expect name (builtins.any (lib.hasInfix needle) messages)
          "expected an assertion containing '${needle}', got: ${builtins.toJSON messages}";

      native = evalNixos { workingDirectory = "/srv/indus"; };
      nativeUnit = native.systemd.services.indus-agent;

      containerOnly = evalNixos {
        container = {
          enable = true;
          image = image;
        };
      };

      rendered = evalNixos {
        workingDirectory = "/srv/indus";
        settings = {
          model.default = "provider/model-name";
          terminal.backend = "local";
        };
        mcpServers = {
          files = {
            command = "mcp-files";
            args = [
              "--root"
              "/srv/shared"
            ];
          };
          tracker = {
            url = "https://mcp.example.invalid/mcp";
            transport = "http";
            auth = "oauth";
            extraConfig.timeout = 30;
          };
        };
      };
      renderedConfig = rendered.services.indus-agent.renderedConfig;

      home = evalHome {
        gateway.enable = true;
        settings.model.default = "provider/model-name";
        mcpServers.files.command = "mcp-files";
      };
    in
    {
      checks = lib.optionalAttrs (lib.hasSuffix "-linux" system) {
        module-native-default = expect "native-default" (
          ours native == [ ]
          && lib.hasInfix "gateway run" nativeUnit.serviceConfig.ExecStart
          && nativeUnit.environment.INDUS_MANAGED == "1"
          && native.users.users.indus-agent.linger
          && !(native.virtualisation.oci-containers.containers ? indus-agent)
        ) "native mode: ${builtins.toJSON (ours native)}";

        module-container-mode = expect "container-mode" (
          ours containerOnly == [ ]
          && containerOnly.virtualisation.oci-containers.containers.indus-agent.cmd == [
            "gateway"
            "run"
          ]
          && !(containerOnly.systemd.services ? indus-agent)
          && !containerOnly.users.users.indus-agent.linger
        ) "container mode: ${builtins.toJSON (ours containerOnly)}";

        module-container-and-native-refused = refused "container-and-native-refused" (evalNixos {
          native.enable = true;
          container = {
            enable = true;
            image = image;
          };
        }) "both enabled";

        module-neither-mode-refused = refused "neither-mode-refused" (evalNixos {
          native.enable = false;
        }) "nothing would run";

        module-container-needs-image = refused "container-needs-image" (evalNixos {
          container = {
            enable = true;
            image = null;
          };
        }) "needs container.image";

        module-config-file-and-settings-refused =
          refused "config-file-and-settings-refused"
            (evalNixos {
              configFile = pkgs.writeText "config.yaml" "{}";
              settings.model.default = "provider/model-name";
            })
            "do not also set settings";

        module-mcp-command-and-url-refused = refused "mcp-command-and-url-refused" (evalNixos {
          mcpServers.both = {
            command = "mcp-files";
            url = "https://mcp.example.invalid/mcp";
          };
        }) "not both";

        module-settings-rendered = expect "settings-rendered" (
          renderedConfig.model.default == "provider/model-name"
          && renderedConfig.terminal == {
            backend = "local";
            cwd = "/srv/indus";
          }
          && renderedConfig._config_version == pins.configVersion
        ) "rendered: ${builtins.toJSON renderedConfig}";

        module-mcp-rendered = expect "mcp-rendered" (
          renderedConfig.mcp_servers == {
            files = {
              command = "mcp-files";
              args = [
                "--root"
                "/srv/shared"
              ];
            };
            tracker = {
              url = "https://mcp.example.invalid/mcp";
              transport = "http";
              auth = "oauth";
              timeout = 30;
            };
          }
        ) "rendered: ${builtins.toJSON renderedConfig.mcp_servers}";

        module-home-manager = expect "home-manager" (
          ours home == [ ]
          && home.programs.indus-agent.renderedConfig.mcp_servers.files.command == "mcp-files"
          && home.programs.indus-agent.renderedConfig.model.default == "provider/model-name"
          && home.systemd.user.services ? indus-agent
          && home.home.activation ? indusAgentConfig
        ) "home-manager: ${builtins.toJSON (ours home)}";
      };
    };
}
