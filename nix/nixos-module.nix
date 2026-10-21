# NixOS module: services.indus-agent (P23-14).
#
# Runs the Indus Agent gateway either natively (a hardened systemd unit) or as an OCI container
# (docker or podman through virtualisation.oci-containers), never both. A oneshot setup unit
# writes config.yaml and the managed marker into the state directory before either starts.
#
#   services.indus-agent = {
#     enable = true;
#     settings.model.default = "provider/model-name";
#     environmentFiles = [ config.sops.secrets."indus-agent/env".path ];
#   };
{ packagesFor }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib) mkOption types literalExpression;

  optionPath = "services.indus-agent";
  # The product image comes from the one release base (RELEASE_REPOSITORY, D-16.690) through
  # nix/pins.json (scripts/nix/pins.py).
  pins = lib.importJSON ./pins.json;
  cfg = config.services.indus-agent;
  system = pkgs.stdenv.hostPlatform.system;

  common = import ./module-common.nix {
    inherit lib pkgs;
    defaultPackage =
      (packagesFor system).default
        or (throw "${optionPath}: this flake has no package for ${system}; set ${optionPath}.package");
    defaultWorkingDirectory = "${cfg.stateDir}/workspace";
    defaultWorkingDirectoryText = literalExpression ''"''${config.${optionPath}.stateDir}/workspace"'';
  };

  home = cfg.stateDir;
  nativeMode = cfg.native.enable && !cfg.container.enable;
  containerMode = cfg.container.enable && !cfg.native.enable;
  serviceUser = config.users.users.${cfg.user} or { };
  containerUnit = "${cfg.container.backend}-indus-agent";
in
{
  options.services.indus-agent = common.options // {
    user = mkOption {
      type = types.str;
      default = "indus-agent";
      description = "User the gateway runs as (and owner of the state directory).";
    };

    group = mkOption {
      type = types.str;
      default = "indus-agent";
      description = "Group of the gateway user.";
    };

    createUser = mkOption {
      type = types.bool;
      default = true;
      description = "Create `user` and `group` as system accounts. Off: declare them yourself.";
    };

    stateDir = mkOption {
      type = types.str;
      default = "/var/lib/indus-agent";
      description = "The agent's home (`INDUS_HOME`): config, sessions, logs. Mounted at /opt/data in container mode.";
    };

    addToSystemPackages = mkOption {
      type = types.bool;
      default = false;
      description = "Also put `indus` on every user's PATH (environment.systemPackages).";
    };

    native.enable = mkOption {
      type = types.bool;
      default = !cfg.container.enable;
      defaultText = literalExpression "!config.${optionPath}.container.enable";
      description = "Run the gateway as a native systemd service.";
    };

    container = {
      enable = lib.mkEnableOption "running the gateway from the Indus Agent OCI image instead of natively";

      backend = mkOption {
        type = types.enum [
          "docker"
          "podman"
        ];
        default = "podman";
        description = "Container engine (virtualisation.oci-containers.backend).";
      };

      image = mkOption {
        type = types.nullOr types.str;
        default = "${pins.image}:stable";
        defaultText = literalExpression ''"${pins.image}:stable"'';
        example = "${pins.image}@sha256:<digest>";
        description = "Image to run in container mode (our stable product image by default; pin a digest for reproducible hosts).";
      };

      extraOptions = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "Extra arguments for the container engine's `run`.";
      };

      extraVolumes = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [ "/srv/projects:/srv/projects" ];
        description = "Extra `host:container[:options]` volumes.";
      };
    };
  };

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        assertions = common.assertionsFor {
          inherit cfg optionPath;
          native = cfg.native.enable;
          container = cfg.container.enable;
          containerImage = cfg.container.image;
        };

        warnings =
          lib.optional (cfg.container.enable && (cfg.extraPackages != [ ] || cfg.extraPythonPackages != [ ]))
            "${optionPath}: extraPackages and extraPythonPackages do not reach container mode; the image carries its own environment.";

        services.indus-agent.renderedConfig = common.renderConfig cfg;

        users.users = lib.mkIf cfg.createUser {
          ${cfg.user} = {
            isSystemUser = true;
            group = cfg.group;
            home = home;
            description = "Indus Agent gateway";
            # A user manager for this account (per-user units and timers) without a login session.
            linger = lib.mkDefault nativeMode;
          };
        };
        users.groups = lib.mkIf cfg.createUser { ${cfg.group} = { }; };

        environment.systemPackages = lib.mkIf cfg.addToSystemPackages [ cfg.package ];

        systemd.tmpfiles.rules = [
          "d ${home} 0700 ${cfg.user} ${cfg.group} - -"
        ]
        ++ lib.optional (
          cfg.workingDirectory != null
        ) "d ${cfg.workingDirectory} 0750 ${cfg.user} ${cfg.group} - -";

        systemd.services.indus-agent-setup = {
          description = "Write the Indus Agent configuration";
          wantedBy = [ "multi-user.target" ];
          after = [ "systemd-tmpfiles-setup.service" ];
          restartTriggers = [ (common.configSource cfg) ];
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            User = cfg.user;
            Group = cfg.group;
            UMask = "0077";
          };
          script = common.installConfigScript { inherit cfg home; };
        };
      }

      (lib.mkIf nativeMode {
        systemd.services.indus-agent = {
          description = "Indus Agent gateway";
          wantedBy = [ "multi-user.target" ];
          wants = [ "network-online.target" ];
          requires = [ "indus-agent-setup.service" ];
          after = [
            "network-online.target"
            "indus-agent-setup.service"
          ];
          # What the agent's terminal tools expect on any host, then the user's extras.
          path = [
            pkgs.bash
            pkgs.coreutils
            pkgs.git
          ]
          ++ cfg.extraPackages;
          environment = common.serviceEnvironment { inherit cfg home; };
          restartTriggers = [ (common.configSource cfg) ];
          serviceConfig = {
            User = cfg.user;
            Group = cfg.group;
            WorkingDirectory = if cfg.workingDirectory != null then cfg.workingDirectory else home;
            ExecStart = lib.escapeShellArgs (common.gatewayCommand cfg);
            EnvironmentFile = cfg.environmentFiles;
            Restart = cfg.restart;
            RestartSec = cfg.restartSec;
            UMask = "0077";
            NoNewPrivileges = true;
            PrivateTmp = true;
            ProtectSystem = "full";
            ProtectKernelTunables = true;
            ProtectKernelModules = true;
            ProtectControlGroups = true;
            RestrictSUIDSGID = true;
            LockPersonality = true;
          };
        };
      })

      (lib.mkIf containerMode {
        virtualisation.oci-containers.backend = cfg.container.backend;
        virtualisation.oci-containers.containers.indus-agent = {
          image = cfg.container.image;
          autoStart = true;
          cmd = [
            "gateway"
            "run"
          ]
          ++ cfg.extraArgs;
          volumes = [
            "${home}:/opt/data"
          ]
          ++ lib.optional (
            cfg.workingDirectory != null
          ) "${cfg.workingDirectory}:${cfg.workingDirectory}"
          ++ cfg.container.extraVolumes;
          # The image's stage-2 remaps its user to these ids (P23-10) so /opt/data stays ours.
          environment =
            {
              INDUS_MANAGED = "1";
            }
            // lib.optionalAttrs ((serviceUser.uid or null) != null) {
              INDUS_UID = toString serviceUser.uid;
            }
            // lib.optionalAttrs ((config.users.groups.${cfg.group}.gid or null) != null) {
              INDUS_GID = toString config.users.groups.${cfg.group}.gid;
            }
            // cfg.environment;
          environmentFiles = cfg.environmentFiles;
          extraOptions = [ "--network=host" ] ++ cfg.container.extraOptions;
        };

        systemd.services.${containerUnit} = {
          requires = [ "indus-agent-setup.service" ];
          after = [ "indus-agent-setup.service" ];
          restartTriggers = [ (common.configSource cfg) ];
        };
      })
    ]
  );
}
