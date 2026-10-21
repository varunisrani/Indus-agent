# Options and helpers shared by the NixOS module (services.indus-agent) and the home-manager
# module (programs.indus-agent). Each module adds the options only it has (users, container mode,
# per-user home and so on) and decides how the gateway runs.
{
  lib,
  pkgs,
  # The package used when `package` is not set (this flake's default for the host system).
  defaultPackage,
  # Default and its documentation text for `workingDirectory` (null: the folder indus starts in).
  defaultWorkingDirectory ? null,
  defaultWorkingDirectoryText ? null,
}:
let
  inherit (lib) mkOption types literalExpression;

  render = import ./render.nix;
  pins = lib.importJSON ./pins.json;

  # JSON-shaped values that merge deeply across module definitions.
  settingsFormat = pkgs.formats.json { };

  # `python313` → `lib/python3.13/site-packages`, for packages that carry no interpreter.
  pinnedSitePackages =
    let
      digits = lib.removePrefix "python" pins.python;
    in
    "lib/python${builtins.substring 0 1 digits}.${builtins.substring 1 (-1) digits}/site-packages";

  mcpServerType = types.submodule {
    options = {
      command = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Program to start for a stdio server. Set this or `url`, not both.";
      };
      args = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "Arguments of `command`.";
      };
      env = mkOption {
        type = types.attrsOf types.str;
        default = { };
        description = ''
          Extra environment of a stdio server. Values land in the Nix store, which every user
          can read: name secrets with `''${NAME}` placeholders and supply them through
          `environmentFiles`.
        '';
      };
      cwd = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Working directory of a stdio server.";
      };
      url = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "Endpoint of a remote (HTTP or SSE) server. Set this or `command`, not both.";
      };
      transport = mkOption {
        type = types.nullOr (
          types.enum [
            "http"
            "sse"
          ]
        );
        default = null;
        description = "Remote transport; unset lets the client choose. Only with `url`.";
      };
      headers = mkOption {
        type = types.attrsOf types.str;
        default = { };
        description = "HTTP headers of a remote server (use `\${NAME}` placeholders for secrets).";
      };
      auth = mkOption {
        type = types.nullOr (types.enum [ "oauth" ]);
        default = null;
        description = "`oauth` makes the client sign in to a remote server.";
      };
      enabled = mkOption {
        type = types.bool;
        default = true;
        description = "Whether the client connects to this server.";
      };
      extraConfig = mkOption {
        inherit (settingsFormat) type;
        default = { };
        description = ''
          Other keys of the `mcp_servers.<name>` entry (for example `timeout`, `tools`, `trust`,
          `sampling`), written as given. Typed options above win over keys repeated here.
        '';
      };
    };
  };
in
rec {
  inherit render pins settingsFormat;

  # Options both modules declare under their own path.
  options = {
    enable = lib.mkEnableOption "the Indus Agent";

    package = mkOption {
      type = types.package;
      default = defaultPackage;
      defaultText = literalExpression "indus-agent.packages.\${system}.default";
      description = "The Indus Agent package (the flake also has `minimal` and `messaging`).";
    };

    workingDirectory = mkOption {
      type = types.nullOr types.str;
      default = defaultWorkingDirectory;
      defaultText = defaultWorkingDirectoryText;
      description = ''
        Folder the agent's terminal and file tools start in; written to `config.yaml` as
        `terminal.cwd`. Null leaves `terminal.cwd` to `settings`.
      '';
    };

    configFile = mkOption {
      type = types.nullOr types.path;
      default = null;
      description = ''
        A complete `config.yaml` to install as it is, instead of rendering `settings` and
        `mcpServers` (setting both is an evaluation error).
      '';
    };

    settings = mkOption {
      inherit (settingsFormat) type;
      default = { };
      description = ''
        The agent's configuration as an attribute set, rendered to `config.yaml`. Definitions from
        several modules merge deeply. The rendered file belongs to Nix: the home is marked
        managed, so `indus config set`, `indus setup` and the dashboard refuse to change it.
      '';
      example = literalExpression ''
        {
          model.default = "provider/model-name";
          terminal.backend = "local";
        }
      '';
    };

    environmentFiles = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = ''
        Runtime paths of `NAME=value` files holding secrets (API keys, bot tokens), for example
        from sops-nix or agenix. Strings, not Nix paths, so a secret is never copied into the
        world-readable store.
      '';
      example = literalExpression ''[ config.sops.secrets."indus-agent/env".path ]'';
    };

    environment = mkOption {
      type = types.attrsOf types.str;
      default = { };
      description = "Environment of the gateway. Not for secrets: the values land in the Nix store.";
    };

    extraPackages = mkOption {
      type = types.listOf types.package;
      default = [ ];
      description = "Packages on the gateway's PATH, so the agent's tools can run them.";
    };

    extraPythonPackages = mkOption {
      type = types.listOf types.package;
      default = [ ];
      description = ''
        Python packages put on the gateway's PYTHONPATH, so plugins advertising the
        `indus.plugins` entry point are found. Build them with the package's interpreter
        (`config.<this module>.package.python.pkgs`).
      '';
    };

    mcpServers = mkOption {
      type = types.attrsOf mcpServerType;
      default = { };
      description = "MCP servers, rendered under `mcp_servers.<name>` in `config.yaml`.";
      example = literalExpression ''
        {
          files = { command = "mcp-files"; args = [ "--root" "/srv/shared" ]; };
          tracker = { url = "https://mcp.example.com/mcp"; auth = "oauth"; };
        }
      '';
    };

    extraArgs = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Extra arguments after `indus gateway run`.";
    };

    restart = mkOption {
      type = types.enum [
        "no"
        "on-success"
        "on-failure"
        "on-abnormal"
        "on-abort"
        "always"
      ];
      default = "always";
      description = "systemd `Restart=` of the gateway (launchd keeps the agent alive instead).";
    };

    restartSec = mkOption {
      type = types.ints.unsigned;
      default = 5;
      description = "systemd `RestartSec=` of the gateway, in seconds.";
    };

    renderedConfig = mkOption {
      type = types.attrs;
      default = { };
      readOnly = true;
      internal = true;
      description = "What the module writes to `config.yaml` (inspected by the flake checks).";
    };
  };

  # The `config.yaml` content of a module instance.
  renderConfig =
    cfg:
    render.renderConfig {
      inherit (cfg) settings workingDirectory;
      mcpServers = cfg.mcpServers;
      configVersion = pins.configVersion;
    };

  # The file installed as `config.yaml`: the user's own file, or the rendered settings. JSON is
  # valid YAML 1.2, which the config loader reads.
  configSource =
    cfg:
    if cfg.configFile != null then
      cfg.configFile
    else
      pkgs.writeText "indus-agent-config.yaml" (builtins.toJSON cfg.renderedConfig);

  # Shell lines that install the config and the managed marker into `home` (atomic replace).
  # `run` is the command prefix (home-manager's `run` helper, or empty).
  installConfigScript =
    {
      cfg,
      home,
      run ? "",
    }:
    let
      dir = lib.escapeShellArg home;
    in
    ''
      ${run} ${pkgs.coreutils}/bin/install -d -m 0700 ${dir}
      ${run} ${pkgs.coreutils}/bin/install -m 0600 ${configSource cfg} ${dir}/config.yaml.nix-tmp
      ${run} ${pkgs.coreutils}/bin/mv -f ${dir}/config.yaml.nix-tmp ${dir}/config.yaml
      ${run} ${pkgs.coreutils}/bin/touch ${dir}/.managed
    '';

  sitePackagesOf = cfg: cfg.package.passthru.sitePackages or pinnedSitePackages;

  # Environment of the gateway: home, managed flag, extra Python packages, the user's values.
  # `managed` is off only for a home-manager home whose config the user keeps.
  serviceEnvironment =
    {
      cfg,
      home,
      managed ? true,
    }:
    {
      INDUS_HOME = home;
    }
    // lib.optionalAttrs managed { INDUS_MANAGED = "1"; }
    // lib.optionalAttrs (cfg.extraPythonPackages != [ ]) {
      PYTHONPATH = lib.makeSearchPath (sitePackagesOf cfg) cfg.extraPythonPackages;
    }
    // cfg.environment;

  gatewayCommand = cfg: [
    (lib.getExe' cfg.package "indus")
    "gateway"
    "run"
  ] ++ cfg.extraArgs;

  # Assertions of a module instance from the renderer's problem list.
  assertionsFor =
    {
      cfg,
      optionPath,
      native ? true,
      container ? false,
      containerImage ? null,
    }:
    map
      (message: {
        assertion = false;
        inherit message;
      })
      (
        render.problems {
          inherit
            optionPath
            native
            container
            containerImage
            ;
          inherit (cfg)
            enable
            configFile
            settings
            mcpServers
            ;
        }
      );
}
