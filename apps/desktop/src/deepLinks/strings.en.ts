// English strings of deep links (P20-34), spread into `src/i18n/en.ts`.

export const DEEP_LINK_STRINGS = {
  "deepLink.cancel": "Cancel",
  "deepLink.skill.title": "Install a skill from a link?",
  "deepLink.skill.body":
    "A link asks Indus to install this skill from the hub. It is scanned before it is installed.",
  "deepLink.skill.install": "Install",
  "deepLink.skill.done": "The skill was installed.",
  "deepLink.mcp.title": "Add an MCP server from a link?",
  "deepLink.mcp.body":
    "A link asks Indus to connect to this MCP server. Its tools become available to the agent.",
  "deepLink.mcp.add": "Add the server",
  "deepLink.mcp.done": "The MCP server was added.",
  "deepLink.refused.path": "A link asked for a page this app does not have.",
  "deepLink.refused.skill": "A link named a skill that is not valid.",
  "deepLink.refused.mcpName": "A link named an MCP server that is not valid.",
  "deepLink.refused.mcpCommand":
    "A link may not add a server that runs a program on this computer.",
  "deepLink.refused.mcpUrl": "A link may only add MCP servers with an https address.",
} as const satisfies Record<string, string>;
