# Plugins and MCP

Three ways to extend Indus Agent, from lightest to heaviest:

| | What it adds | Runs |
|---|---|---|
| MCP servers | tools from any program that speaks the Model Context Protocol | as a separate process or a remote service |
| Shell hooks | your scripts at moments of a turn (before a tool runs, after a reply, ...) | as commands |
| Plugins | Python code that registers tools, hooks, providers, platforms or slash commands | inside Indus Agent |

## MCP servers

```bash
indus mcp add docs --url https://mcp.example.com/mcp --auth oauth
indus mcp add fs --preset filesystem
indus mcp add tickets --command npx --arg -y --arg @example/tickets-mcp --env TICKETS_TOKEN='${TICKETS_TOKEN}'
indus mcp test fs
```

<!-- test -->
```console
$ indus mcp list
No MCP servers are configured.
Add one with `indus mcp add NAME --command CMD` or `indus mcp add NAME --url URL`.
```

Servers are saved under `mcp_servers` in `config.yaml`; secrets are written there only as `${NAME}`
references to stored secrets. Each server's tools appear as the toolset `mcp-<name>`, so
`indus tools` turns them on or off per platform like any other toolset. `/mcp` shows the connected
servers in a chat and `/reload-mcp` reconnects after a change (the config file is also watched).

A server can be `--lazy` (started on first use) and gets a trust tier: `untrusted` servers'
tool results are marked as untrusted content for the model. Packages started through `npx`, `uvx`,
`pipx`, `bunx` or `pnpm dlx` are checked against the OSV malware advisories before they start.

Indus Agent is also an MCP server for other agents: see [Serving MCP](../mcp-serve.md).

## Shell hooks

```yaml
hooks:
  pre_tool_call:
    - command: ~/bin/check-command.sh
      matcher: terminal
      timeout: 10
```

The hook gets the event as JSON on stdin and can answer on stdout, for example to block a tool call
with a reason. Every hook command is shown to you and approved once before it first runs
(`indus hooks list`, `indus hooks test`, `indus hooks revoke`); `--accept-hooks` or
`hooks_auto_accept: true` skips the question.

## Plugins

```bash
indus plugins install example/indus-weather --ref <40-character commit>
indus plugins enable weather
indus plugins list
```

A plugin is a folder with a manifest (`plugin.yaml` or `plugin.json`) and a Python module whose
`register(ctx)` adds what the plugin offers. Installed plugins are listed but load only once enabled (`plugins.enabled`). Before an
install the plugin is scanned: a dangerous finding refuses it, a cautionary one needs `--force`.
`indus plugins capabilities` shows what a plugin asks to be allowed, and `plugins.entries.<name>.granted_capabilities` records what you allowed.

Plugins inside a project (`.indus/plugins/`) load only with `plugins.project: true` and never in
the messaging gateway. Updates are checked daily (`plugins.auto_update_check_hours`) and applied
only when you say so (`indus plugins update`, or `plugins.auto_apply`).

The [developer guide](../developer/adding-a-tool.md) shows how to write one.
