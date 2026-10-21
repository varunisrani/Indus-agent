# What the NixOS and home-manager modules write, and the problems their assertions report.
#
# Builtins only (no nixpkgs `lib`), so `nix-instantiate --eval` can test it with nothing fetched;
# the modules import it and wrap the problem lists in assertions.
let
  # MCP server fields the modules declare as typed options; every other key of an entry goes
  # through `extraConfig` unchanged. The names are those of `indus.mcp.config.McpServer`.
  mcpTypedFields = [
    "command"
    "args"
    "env"
    "cwd"
    "url"
    "transport"
    "headers"
    "auth"
    "enabled"
  ];

  isUnset = value: value == null || value == [ ] || value == { };

  # Deep merge of two attribute sets; values of `right` win, nested sets merge.
  merge =
    left: right:
    left
    // builtins.mapAttrs (
      name: value:
      if builtins.isAttrs value && builtins.isAttrs (left.${name} or null) then
        merge left.${name} value
      else
        value
    ) right;

  pick = names: set: builtins.listToAttrs (map (name: {
    inherit name;
    value = set.${name};
  }) (builtins.filter (name: set ? ${name} && !isUnset set.${name}) names));

  # One `mcp_servers.<name>` entry: unset typed fields dropped, `enabled` written only when off
  # (the loader's default is on), free-form keys kept; typed fields win over free-form ones.
  renderMcpServer =
    server:
    let
      typed = pick (builtins.filter (n: n != "enabled") mcpTypedFields) server;
      switch = if (server.enabled or true) then { } else { enabled = false; };
    in
    (server.extraConfig or { }) // typed // switch;

  mcpServerProblems =
    optionPath: name: server:
    let
      where = "${optionPath}.mcpServers.${name}";
      hasCommand = (server.command or null) != null;
      hasUrl = (server.url or null) != null;
    in
    (if hasCommand && hasUrl then
      [ "${where}: set either command (stdio) or url (http/sse), not both" ]
    else if !hasCommand && !hasUrl then
      [ "${where}: set command (stdio) or url (http/sse)" ]
    else
      [ ])
    ++ (if (server.transport or null) != null && !hasUrl then
      [ "${where}: transport applies to url servers only" ]
    else
      [ ]);

  # The whole `config.yaml`: declared settings, the config stamp unless the settings carry one,
  # the working directory as `terminal.cwd`, declared MCP servers over same-named settings entries.
  renderConfig =
    {
      settings ? { },
      workingDirectory ? null,
      mcpServers ? { },
      configVersion,
    }:
    let
      stamped = { _config_version = configVersion; } // settings;
      withCwd =
        if workingDirectory == null then
          stamped
        else
          merge stamped { terminal.cwd = workingDirectory; };
      servers = builtins.mapAttrs (_: renderMcpServer) mcpServers;
    in
    if servers == { } then withCwd else merge withCwd { mcp_servers = servers; };

  # Problems of one module instance; an empty list means the assertions pass.
  problems =
    {
      optionPath,
      enable ? true,
      native ? true,
      container ? false,
      containerImage ? null,
      configFile ? null,
      settings ? { },
      mcpServers ? { },
    }:
    if !enable then
      [ ]
    else
      (if native && container then
        [
          "${optionPath}: native and container mode are both enabled; pick one (set native.enable = false or container.enable = false)"
        ]
      else if !native && !container then
        [
          "${optionPath}: enabled, but neither native.enable nor container.enable is set, so nothing would run"
        ]
      else
        [ ])
      ++ (if container && containerImage == null then
        [ "${optionPath}: container.enable needs container.image (the image to run)" ]
      else
        [ ])
      ++ (if configFile != null && (settings != { } || mcpServers != { }) then
        [
          "${optionPath}: configFile replaces the whole config.yaml; do not also set settings or mcpServers"
        ]
      else
        [ ])
      ++ builtins.concatLists (
        builtins.attrValues (builtins.mapAttrs (mcpServerProblems optionPath) mcpServers)
      );
in
{
  inherit
    mcpTypedFields
    merge
    renderMcpServer
    renderConfig
    problems
    ;
}
