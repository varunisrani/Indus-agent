// The four tabs of the Capabilities page (P24-26), one module each since W52-k; this module keeps
// the names the page, the plugin host and the tests import.

export { ConnectorsTab } from "./ConnectorsTab.tsx";
export { PluginsTab } from "./PluginsTab.tsx";
export { HubSection, SkillsTab } from "./SkillsTab.tsx";
export {
  type AgentPluginRow,
  type DesktopPluginRow,
  type DesktopPluginSource,
  FULL_SHA,
  type HubResult,
  type McpServerRow,
  mergePlugins,
  type PluginBadge,
  type PluginRowView,
  type SkillRow,
  type ToolsetRow,
} from "./shared.ts";
export { ToolsetsTab } from "./ToolsetsTab.tsx";
