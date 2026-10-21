# home-manager module: programs.indus-agent (P23-14).
#
# Puts `indus` on the user's PATH and, when settings, MCP servers or a config file are declared,
# writes `<home>/config.yaml` and marks the home managed at activation. `gateway.enable` runs the
# gateway as a per-user service: a systemd user unit on Linux, a launchd agent on macOS.
#
#   programs.indus-agent = {
#     enable = true;
#     settings.model.default = "provider/model-name";
#     gateway.enable = true;
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

  optionPath = "programs.indus-agent";
  cfg = config.programs.indus-agent;
  system = pkgs.stdenv.hostPlatform.system;

  common = import ./module-common.nix {
    inherit lib pkgs;
    defaultPackage =
      (packagesFor system).default
        or (throw "${optionPath}: this flake has no package for ${system}; set ${optionPath}.package");
  };

  defaultHome = "${config.home.homeDirectory}/.indus";
  home = cfg.home;
  workingDirectory = if cfg.workingDirectory != null then cfg.workingDirectory else home;
  declarative = cfg.configFile != null || cfg.settings != { } || cfg.mcpServers != { };

  environment = common.serviceEnvironment {
    inherit cfg home;
    managed = declarative;
  };

  # The gateway's entry: extra packages first on PATH, the env files loaded where the service
  # manager cannot (launchd has no EnvironmentFile), then the gateway itself.
  mkLauncher =
    { loadEnvFiles }:
    pkgs.writeShellScript "indus-agent-gateway" ''
      set -eu
      ${lib.optionalString (cfg.extraPackages != [ ]) ''
        PATH=${lib.escapeShellArg (lib.makeBinPath cfg.extraPackages)}''${PATH:+:$PATH}
        export PATH
      ''}
      ${lib.optionalString loadEnvFiles (
        lib.concatMapStringsSep "\n" (file: ''
          set -a
          . ${lib.escapeShellArg file}
          set +a
        '') cfg.environmentFiles
      )}
      exec ${lib.escapeShellArgs (common.gatewayCommand cfg)}
    '';
in
{
  options.programs.indus-agent = common.options // {
    home = mkOption {
      type = types.str;
      default = defaultHome;
      defaultText = literalExpression ''"''${config.home.homeDirectory}/.indus"'';
      description = "The agent's home (`INDUS_HOME`).";
    };

    installPackage = mkOption {
      type = types.bool;
      default = true;
      description = "Put `indus` on the user's PATH (home.packages).";
    };

    gateway.enable = lib.mkEnableOption "the gateway as a per-user service (systemd on Linux, launchd on macOS)";
  };

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        assertions = common.assertionsFor { inherit cfg optionPath; };

        programs.indus-agent.renderedConfig = common.renderConfig cfg;

        home.packages = lib.mkIf cfg.installPackage [ cfg.package ];

        home.sessionVariables = lib.mkIf (home != defaultHome) { INDUS_HOME = home; };

        # The home (and its logs folder, where launchd writes) exists before any service starts.
        home.activation.indusAgentHome = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
          run ${pkgs.coreutils}/bin/install -d -m 0700 ${lib.escapeShellArg home} ${lib.escapeShellArg "${home}/logs"}
        '';

        home.activation.indusAgentConfig = lib.mkIf declarative (
          lib.hm.dag.entryAfter [ "indusAgentHome" ] (
            common.installConfigScript {
              inherit cfg home;
              run = "run";
            }
          )
        );
      }

      (lib.mkIf (cfg.gateway.enable && pkgs.stdenv.hostPlatform.isLinux) {
        systemd.user.services.indus-agent = {
          Unit = {
            Description = "Indus Agent gateway";
            After = [ "network-online.target" ];
          };
          Service = {
            ExecStart = "${mkLauncher { loadEnvFiles = false; }}";
            Environment = lib.mapAttrsToList (name: value: "${name}=${value}") environment;
            EnvironmentFile = cfg.environmentFiles;
            WorkingDirectory = workingDirectory;
            Restart = cfg.restart;
            RestartSec = cfg.restartSec;
          };
          Install.WantedBy = [ "default.target" ];
        };
      })

      (lib.mkIf (cfg.gateway.enable && pkgs.stdenv.hostPlatform.isDarwin) {
        launchd.agents.indus-agent = {
          enable = true;
          config = {
            ProgramArguments = [ "${mkLauncher { loadEnvFiles = true; }}" ];
            EnvironmentVariables = environment;
            WorkingDirectory = workingDirectory;
            RunAtLoad = true;
            KeepAlive = true;
            StandardOutPath = "${home}/logs/gateway.launchd.log";
            StandardErrorPath = "${home}/logs/gateway.launchd.log";
          };
        };
      })
    ]
  );
}
