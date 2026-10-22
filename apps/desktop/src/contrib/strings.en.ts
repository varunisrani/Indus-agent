// English strings of the desktop plugin inventory (P24-32 wiring): Settings › Advanced › Plugins.
// Spread into `src/i18n/en.ts`.

export const PLUGIN_STRINGS = {
  "settings.page.advanced.plugins": "Plugins",
  "plugins.title": "Desktop plugins",
  "plugins.body":
    "Plugins add pages, panes and sidebar entries to this window. They run with the app's full authority: turn on only plugins you trust.",
  "plugins.rescan": "Check the folder again",
  "plugins.empty": "No desktop plugins are installed.",
  "plugins.unavailable": "Desktop plugins are available in the desktop app's main window.",
  "plugins.status.loaded": "On",
  "plugins.status.disabled": "Off",
  "plugins.status.error": "Failed",
  "plugins.status.refused": "Refused",
  "plugins.status.loading": "Starting",
  "plugins.kind.disk": "Plugin file",
  "plugins.kind.package": "Desktop half of the agent plugin {name}",
  "plugins.enable": "Use {name}",
  "plugins.on": "On",
  "plugins.reason": "Why: {reason}",
} as const;
